"""The scrolling page canvas.

Performance model: every visible page is rasterised once into a cairo surface
and handed to GTK as a GPU texture, so scrolling and zooming only move
textures around. While writing, only the growing tail of the stroke is
rasterised per frame; finished ink is painted into the page surface once.
Edits repaint just the affected rectangle.
"""
import bisect
import collections
import math
import os

import cairo
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango

from .i18n import tr
from . import geom
from .geom import intersects, union
from .model import Image, Shape, Stroke, Text, draw_paper, ink_on, page_box, render_page
from .render import rgba, texture_from_surface, worker

TOP, GAP, SIDE, BOTTOM = 64, 14, 24, 48
MAX_PIXELS = 12_000_000  # pages bigger than this on screen are cached as a window around the viewport
STRIP = 256              # page rasters reach the GPU in strips, so an edit re-uploads only what it touched
MIN_ZOOM = 0.2
BOARD_MIN_ZOOM = 0.04
BG_KEEP = 8  # PDF page backgrounds remembered after they scroll out of view
CHUNK = 48  # live stroke points rasterised per baked chunk
LASER_HOLD_MS = 1000  # a laser trail stays this long after the last stroke
LASER_FADE_MS = 350
_TOUCH = {Gdk.EventType.TOUCH_BEGIN: 'begin', Gdk.EventType.TOUCH_UPDATE: 'update',
          Gdk.EventType.TOUCH_END: 'end', Gdk.EventType.TOUCH_CANCEL: 'cancel'}

_WHITE = rgba(1, 1, 1)
_SHADOW = rgba(0, 0, 0, 0.16)
_ACCENT = rgba(0.21, 0.52, 0.89)
RULER_W = 46      # half the ruler's width, in pixels
RULER_SNAP = 18   # how near its edge a stroke has to start to follow it
_RULER_BODY = rgba(0.90, 0.92, 0.95, 0.66)
_RULER_INK = rgba(0.22, 0.25, 0.32, 0.9)
_MARK = rgba(1.0, 0.85, 0.1, 0.35)
_MARK_NOW = rgba(1.0, 0.5, 0.0, 0.5)
_ERASER_RING = rgba(0.3, 0.3, 0.3, 0.8)
_ERASER_FILL = rgba(1, 1, 1, 0.5)


_DOT = []


def _dot_cursor():
    """A pointer that is only the centre of the crosshair."""
    if not _DOT:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 16, 16)
        cr = cairo.Context(surface)
        cr.arc(8, 8, 2.6, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.fill()
        cr.arc(8, 8, 1.6, 0, 2 * math.pi)
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.95)
        cr.fill()
        _DOT.append(Gdk.Cursor.new_from_texture(texture_from_surface(surface), 8, 8, None))
    return _DOT[0]


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


class _Entry:
    """Raster cache of one page: a window (ox, oy, pw, ph) of the page drawn at full x full pixels."""
    __slots__ = ('surf', 'bg', 'full', 'ox', 'oy', 'pw', 'ph', 'sx', 'sy', 'strips', 'dirty')

    def mark(self, y0, y1):
        self.dirty.update(range(max(0, int(y0) // STRIP), min(len(self.strips), int(y1) // STRIP + 1)))


class Canvas(Gtk.Widget, Gtk.Scrollable):
    __gtype_name__ = 'InkpadCanvas'
    __gsignals__ = {
        'view-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'selection-made': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'selection-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A long press with the select tool, at a place in the widget: a menu is wanted there.
        'menu-wanted': (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
    }

    # -- Gtk.Scrollable -----------------------------------------------------

    _hadj = _vadj = None
    _hadj_id = _vadj_id = 0

    def _get_hadj(self):
        return self._hadj

    def _set_hadj(self, adj):
        if self._hadj is not None:
            self._hadj.disconnect(self._hadj_id)
        self._hadj = adj
        if adj is not None:
            self._hadj_id = adj.connect('value-changed', self._scrolled)
            self._configure_adj()

    def _get_vadj(self):
        return self._vadj

    def _set_vadj(self, adj):
        if self._vadj is not None:
            self._vadj.disconnect(self._vadj_id)
        self._vadj = adj
        if adj is not None:
            self._vadj_id = adj.connect('value-changed', self._scrolled)
            self._configure_adj()

    hadjustment = GObject.Property(type=Gtk.Adjustment, getter=_get_hadj, setter=_set_hadj)
    vadjustment = GObject.Property(type=Gtk.Adjustment, getter=_get_vadj, setter=_set_vadj)
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)

    # -- setup --------------------------------------------------------------

    def __init__(self):
        self.doc = None
        self.zoom = 1.0
        self._render_zoom = 1.0
        self._zoom_init = False
        self._settle = 0
        self._dev = 1.0
        self._tops = []
        self._cw = self._ch = 0.0
        self._cache = {}
        self._pending = {}
        self._bgs = collections.OrderedDict()   # rendered PDF backgrounds, most recently used last
        self._shown = (0, 0)
        self._op = None
        self._sel = None
        self._hidden = None
        self._clip = []
        self._ptr = (0.0, 0.0)
        self._pen_down = False
        self._mouse_down = False
        self._mouse_p = 0.5
        self._popover = None
        # Tool configuration, written by the window.
        self.tool = 'pen'
        self.pen_type = 'fountain'
        self.pen_color = (0, 0, 0)
        self.pen_width = 1.6
        self.hl_color = (1.0, 0.88, 0.3)
        self.hl_width = 14.0
        self.eraser_r = 9.0
        self.shape_kind = 'line'
        self.text_size = 18.0
        self.scribble_erase = True
        self.finger_draw = False
        self.on_scribble = None
        self.pen_sens = 1.0            # pressure sensitivity factor
        self.pen_dash = 0              # 0 solid, 1 dashed, 2 dotted
        self._anim = None
        self.pen_stab = 0.0            # stroke stabilisation, 0..1
        self.hold_shape = True         # hold still at the end of a stroke to snap it to a shape
        self.eraser_mode = 'standard'  # 'standard' erases what is touched, 'stroke' whole strokes
        self.erase_filter = {'pen', 'hl', 'shape'}
        self.on_erase_done = None
        self.lasso_rect = False
        self.lasso_filter = {'ink', 'image', 'shape', 'text'}
        self._hold_timer = 0
        self._press_timer = 0          # running while something is held still with the select tool
        self._press_at = (0.0, 0.0)
        self.note_color = (1.0, 0.93, 0.55)
        self.tape_color, self.tape_width = (0.96, 0.82, 0.36), 16.0
        self.mouse_hand = True         # once a pen has been used, the mouse moves the page instead of drawing
        self._has_pen = False
        self._hand = None
        self.marks = []                # search hits: (page, box in page points)
        self.mark_at = -1
        self.show_cross = True         # crosshair pointer; off leaves only a small dot
        self.laser_mode = 'trail'      # 'dot' follows the pen, 'trail' leaves a line that fades
        self.laser_color = (1.0, 0.16, 0.2)
        self._laser_trails = []        # [(page, [(x, y), ...])]
        self._laser_dot = None         # (page, x, y)
        self._laser_timer = 0
        self._laser_fade = None
        self._laser_alpha = 1.0
        self.ruler = None              # the ruler on screen: {'x', 'y' (centre, pixels), 'a' (radians)}
        self._src = 'pen'              # what is drawing: 'pen', 'mouse' or 'touch'
        self._labels = {}
        self.paging = 'vertical'       # pages run downwards, or 'horizontal': side by side
        self.on_pull_add = None        # called with +1 or -1 when the end of the pages is pulled past
        self._lefts = []
        self._laid_for = 0
        self._memo = None              # a remembered view waiting for the window to have a size
        self.scrollbars = True         # show where in the page the view is, and let the mouse drag it
        self.max_zoom = 6.0            # how large a page may be shown, as a scale on the screen
        self._bar_drag = None
        self._pf = {}                  # side by side: each page's own scale beside the zoom
        self._slots = []               # side by side, every page has the window to itself
        self._pull = 0.0               # how far the last (+) or first (-) page is being pulled past
        self._turning = 0              # the page being turned to
        self._turn_at = 0              # when the last scroll event came in
        self._turn_sum = 0.0
        self._turned_at = 0
        self._turn_done = False
        self._want = (0.0, 0.0)
        self._board = False            # showing a whiteboard: one page, no edges, free scrolling
        self._bo = [0.0, 0.0]          # its scroll position in pixels; any value, either sign
        self._bat = 0                  # which of the document's boards is shown
        self._touches = {}             # event sequence -> (x, y)
        self._rejected = set()
        self.input_log = None          # a file to note pen and touch events in, for finding faults
        self._pen_at = (0.0, 0.0)
        self._nudger, self._nudge_timer, self._nudges = None, 0, 0
        self._raw_pen = None           # None, 'wait' for the stylus gesture, or 'down': drawn from raw events
        self._tg = None                # running touch pan/zoom
        self._fling = 0
        self.palm_ms = 80              # touches this soon after a pen event are the resting hand
        self._pen_seen = -10 ** 9

        super().__init__(focusable=True, hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.add_css_class('canvas')

        # Fingers are handled here rather than by the scrolled window, so that the
        # palm can be told apart from a gesture and pinching follows the fingers.
        raw = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        raw.connect('event', self._raw_event)
        self.add_controller(raw)

        stylus = Gtk.GestureStylus()
        stylus.connect('down', self._stylus_down)
        stylus.connect('motion', self._stylus_motion)
        stylus.connect('up', self._stylus_up)
        stylus.connect('cancel', lambda *a: self._drop('the stylus gesture was cancelled'))
        self.add_controller(stylus)

        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        drag.connect('cancel', lambda *a: self._drop('the drag gesture was cancelled'))
        self.add_controller(drag)

        pan = Gtk.GestureDrag(button=Gdk.BUTTON_MIDDLE)
        pan.connect('drag-begin', self._pan_begin)
        pan.connect('drag-update', self._pan_update)
        pan.connect('drag-end', lambda *a: self._rest((self.zoom,) + tuple(self._pan)))
        self.add_controller(pan)

        pinch = Gtk.GestureZoom()
        pinch.connect('begin', self._pinch_begin)
        pinch.connect('scale-changed', self._pinch_scale)
        self.add_controller(pinch)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll', self._ctrl_scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda c, x, y: setattr(self, '_ptr', (x, y)))
        self.add_controller(motion)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)

        self.connect('notify::scale-factor', lambda *a: self.queue_draw())
        self.set_tool('pen')

    def set_document(self, doc):
        if self.doc is not None:
            self.doc.listeners.remove(self._doc_event)
        self.doc = doc
        doc.listeners.append(self._doc_event)
        self._cache.clear()
        self._pending.clear()
        self._bgs.clear()
        self._op = self._sel = self._hidden = None
        self._bat = 0
        self._laser_trails, self._laser_dot = [], None
        self.marks, self.mark_at = [], -1
        self._relayout()
        self._memo = None
        if self._zoom_init:
            self._zoom_init = False
            self._init_zoom()
            self._home()
        self.queue_draw()
        self.emit('view-changed')

    def view_memo(self):
        """Where the view is, in a form that outlasts the window: the zoom and the point of
        the document in the middle of the window."""
        if self.doc is None or not self._tops or not self._zoom_init:
            return None
        i, fx, fy = self._anchor(self.get_width() / 2, self.get_height() / 2)
        return [self.zoom, i, fx, fy]

    def restore_memo(self, memo):
        """Go back to a place remembered with view_memo(), now or once there is a window."""
        try:
            zoom, i, fx, fy = float(memo[0]), int(memo[1]), float(memo[2]), float(memo[3])
        except (TypeError, ValueError, IndexError):
            return
        if not (0 <= i < len(self.doc.pages)) or not (zoom > 0):   # also turns away NaN
            return
        if not self._zoom_init:
            self._memo = memo
            return
        if self._board:
            self._bat = i
        self._place(max(self._min_zoom(), min(self._max_zoom(), zoom)), (i, fx, fy), self.get_width() / 2, self.get_height() / 2)
        self._render_zoom = self.zoom
        self._rest()
        self.queue_draw()
        self.emit('view-changed')

    def set_tool(self, tool):
        self.tool = tool
        if tool != 'lasso':
            self.clear_selection()
        if tool != 'laser':
            self.clear_laser()
        self.update_cursor()

    def update_cursor(self):
        name = {'text': 'text', 'note': 'text', 'eraser': 'cell'}.get(self.tool, 'crosshair')
        if name == 'crosshair' and not self.show_cross:
            self.set_cursor(_dot_cursor())
        else:
            self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    # -- layout and scrolling -----------------------------------------------

    def do_measure(self, orientation, for_size):
        return (120, 480, -1, -1)

    def do_size_allocate(self, width, height, baseline):
        if not self._zoom_init and width > 200:
            self._init_zoom()
            self._home()
            if self._memo is not None:
                memo, self._memo = self._memo, None
                self._configure_adj()
                self.restore_memo(memo)
        if self._across() and self.doc is not None and self._slots and width != self._laid_for:
            # The pages are a window apart, so a new width moves them all.
            i = self.current_page_index()
            self._relayout()
            self._configure_adj(self._slots[min(i, len(self._slots) - 1)])
        self._configure_adj()
        if self._popover is not None:
            self._popover.present()
        if self._nudger is not None:
            self._nudger.present()

    def _init_zoom(self):
        self._zoom_init = True
        if self.doc is None:
            return
        # Fitted to the usual page: one odd, huge page must not shrink all the others.
        pages = self.doc.pages
        rw, rh = self._usual()
        fit = (self.get_width() - 2 * SIDE) / rw
        if self.paging == 'horizontal':   # a whole page at a time
            fit = min(fit, (self.get_height() - TOP - BOTTOM) / rh)
        self.zoom = self._render_zoom = 1.0 if self.doc.pages[0].board else max(MIN_ZOOM, min(fit, 1.35))
        self._relayout()
        self.emit('view-changed')

    def _usual(self):
        """The size of the notebook's usual page: the middle one of its widths and heights."""
        pages = self.doc.pages
        return sorted(p.w for p in pages)[len(pages) // 2], sorted(p.h for p in pages)[len(pages) // 2]

    def _z(self, page):
        """The scale a page is shown at. Side by side every page is seen alone, so a page of
        another size is scaled to fill the same space as the usual one."""
        return self.zoom * self._pf.get(id(page), 1.0)

    def shown_zoom(self):
        if self.doc is None or not self._tops:
            return self.zoom
        # The layout may be a page behind the document while a change is being announced.
        return self._z(self.doc.pages[min(self.current_page_index(), len(self.doc.pages) - 1)])

    def _home(self):
        """Initial scroll position: the top, or the content of a whiteboard."""
        if self.doc.pages[0].board:
            self.center_on(self._bat)
        else:
            self._configure_adj(0, 0)

    def _left(self, page):
        """Where a page's left edge is in the scrolled content: centred, or a board's origin."""
        return 0.0 if self._board else (max(self.get_width(), self._cw) - page.w * self.zoom) / 2

    def center_on(self, i):
        page = self.doc.pages[i]
        x0, y0, x1, y1 = page_box(page)
        left, top = self._origin(i)
        self._configure_adj(left + (x0 + x1) / 2 * self.zoom - self.get_width() / 2,
                            top + (y0 + y1) / 2 * self.zoom - self.get_height() / 2)

    def _relayout(self):
        y = float(TOP)
        self._tops = tops = []
        width = 0.0
        self._board = self.doc is not None and self.doc.pages[0].board
        self._pf = {}
        if self._board:
            # Nothing to lay out: the one board on show starts at the origin and never ends.
            self._bat = min(self._bat, len(self.doc.pages) - 1)
            self._tops = [0.0] * len(self.doc.pages)
            self._cw = self._ch = 0.0
            return
        if self.doc is not None and self.paging == 'horizontal':
            # One page at a time: each has a slot as wide as the window, wider when zoomed in.
            x = 0.0
            self._lefts = lefts = []
            self._slots = slots = []
            self._laid_for = view = self.get_width()
            height = 0.0
            rw, rh = self._usual()
            for page in self.doc.pages:
                self._pf[id(page)] = min(rw / page.w, rh / page.h)
                z = self._z(page)
                slot = max(float(view), page.w * z + 2 * SIDE)
                slots.append(x)
                lefts.append(x + (slot - page.w * z) / 2)
                tops.append(float(TOP))
                x += slot
                height = max(height, page.h * z)
            self._cw = x
            self._ch = height + TOP + BOTTOM
            return
        if self.doc is not None:
            for page in self.doc.pages:
                tops.append(y)
                y += page.h * self.zoom + GAP
                width = max(width, page.w)
        self._ch = y - GAP + BOTTOM
        self._cw = width * self.zoom + 2 * SIDE

    def _across(self):
        return self.paging == 'horizontal' and not self._board

    def _origin(self, i):
        """Where page i's top left corner is in the scrolled content."""
        page = self.doc.pages[i]
        if self._across():
            return self._lefts[i], max(float(TOP), (max(self.get_height(), self._ch) - page.h * self._z(page)) / 2)
        return self._left(page), self._tops[i]

    def _page_at(self, x, y):
        """The page whose row or column holds a point of the scrolled content."""
        if self._board:
            return self._bat
        if self._across():
            return max(0, bisect.bisect_right(self._slots, x) - 1)
        return max(0, bisect.bisect_right(self._tops, y) - 1)

    def set_paging(self, paging):
        if paging != self.paging:
            i = self.current_page_index() if self.doc is not None and self._tops else 0
            self.paging = paging
            if self.doc is not None:
                self._init_zoom()
                self._configure_adj()
                if not self._board:
                    i = min(i, len(self.doc.pages) - 1)
                    self._configure_adj(self._slots[i] if self._across() else 0, 0 if self._across() else self._tops[i] - TOP)
                self.queue_draw()

    def _configure_adj(self, hval=None, vval=None):
        if self._board:
            before = tuple(self._bo)
            if hval is not None:
                self._bo[0] = float(hval)
            if vval is not None:
                self._bo[1] = float(vval)
            for adj, view in ((self._hadj, self.get_width()), (self._vadj, self.get_height())):
                if adj is not None and adj.get_page_size() != view:
                    adj.configure(0, 0, view, 40, view * 0.9, view)
            # Only a real move is announced: this also runs on every size allocation, and
            # listeners that lay widgets out in response would otherwise never come to rest.
            if tuple(self._bo) != before:
                self.queue_draw()
                self.emit('view-changed')
            return
        for adj, content, view, val in ((self._hadj, self._cw, self.get_width(), hval),
                                        (self._vadj, self._ch, self.get_height(), vval)):
            if adj is None:
                continue
            upper = max(content, view)
            value = adj.get_value() if val is None else val
            adj.configure(max(0.0, min(value, upper - view)), 0, upper, 40, view * 0.9, view)

    def _scrolled(self, adj):
        self.queue_draw()
        self.emit('view-changed')

    def _h(self):
        if self._board:
            return self._bo[0]
        return self._hadj.get_value() if self._hadj else 0.0

    def _v(self):
        if self._board:
            return self._bo[1]
        return self._vadj.get_value() if self._vadj else 0.0

    def _snap(self, v):
        return round(v * self._dev) / self._dev

    def _page_rect(self, i):
        """Page rectangle in widget coordinates, aligned to device pixels."""
        page = self.doc.pages[i]
        if self._board:
            # Only the origin and the scale matter; the board itself has no rectangle.
            return self._snap(-self._bo[0]), self._snap(-self._bo[1]), page.w * self.zoom, page.h * self.zoom
        w, h = self._snap(page.w * self._z(page)), self._snap(page.h * self._z(page))
        if self._across():
            ox, oy = self._origin(i)
            return self._snap(ox - self._h()), self._snap(oy - self._v()), w, h
        x = (max(self.get_width(), self._cw) - w) / 2 - self._h()
        return self._snap(x), self._snap(self._tops[i] - self._v()), w, h

    def _to_page(self, i, x, y):
        px, py, pw, ph = self._page_rect(i)
        page = self.doc.pages[i]
        return (x - px) * page.w / pw, (y - py) * page.h / ph

    def _locate(self, x, y):
        """(index, page x, page y) of the page under a widget point, or None."""
        if not self._tops:
            return None
        if self._board:
            return (self._bat,) + self._to_page(self._bat, x, y)
        i = self._page_at(x + self._h(), y + self._v())
        px, py, pw, ph = self._page_rect(i)
        if px <= x <= px + pw and py <= y <= py + ph:
            return (i,) + self._to_page(i, x, y)
        return None

    def _index(self, page):
        try:
            return self.doc.pages.index(page)
        except ValueError:
            return None

    def current_page_index(self):
        if not self._tops:
            return 0
        if self._board:
            return self._bat
        return self._page_at(self._h() + self.get_width() / 2, self._v() + self.get_height() / 2)

    def scroll_to_page(self, i):
        if not (self._vadj and 0 <= i < len(self._tops)):
            return
        if self._board:
            self._bat = i
            self.center_on(i)
            return
        adj = self._hadj if self._across() else self._vadj
        start = self._slots[i] if self._across() else self._tops[i] - TOP
        self._turning = i
        self._glide(adj, start)

    def _span(self, i):
        """Side by side: the scroll positions between which page i fills the window. They are
        one and the same when the whole page fits."""
        page = self.doc.pages[i]
        lo = self._slots[i]
        return lo, lo + max(0.0, page.w * self._z(page) + 2 * SIDE - self.get_width())

    def _glide(self, adj, target):
        target = max(0.0, min(target, adj.get_upper() - adj.get_page_size()))
        if self._anim is not None:
            self._anim.pause()
        self._anim = Adw.TimedAnimation.new(self, adj.get_value(), target, 280,
                                            Adw.CallbackAnimationTarget.new(adj.set_value))
        self._anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._anim.play()

    # -- zoom ---------------------------------------------------------------

    def set_zoom(self, zoom, ax=None, ay=None):
        zoom = max(self._min_zoom(), min(self._max_zoom(), zoom))
        if self.doc is None or abs(zoom - self.zoom) < 1e-4:
            return
        if ax is None:
            ax, ay = self.get_width() / 2, self.get_height() / 2
        # Keep the page point under the anchor fixed.
        self._place(zoom, self._anchor(ax, ay), ax, ay)

    def _max_zoom(self):
        """The limit is on what is seen, not on the number: a page that is shown smaller
        than others to fit beside them may be enlarged by as much more."""
        if self.doc is None or not self._tops or not self._pf:
            return self.max_zoom
        page = self.doc.pages[min(self.current_page_index(), len(self.doc.pages) - 1)]
        return self.max_zoom / self._pf.get(id(page), 1.0)

    def _min_zoom(self):
        # A whiteboard can be looked at from much further away than a page.
        return BOARD_MIN_ZOOM if self.doc is not None and self.doc.pages[0].board else MIN_ZOOM

    def view_state(self):
        return self.zoom, self._h(), self._v()

    def restore_view(self, state):
        """Go back to a zoom and scroll position remembered with view_state()."""
        self.zoom = self._render_zoom = state[0]
        self._relayout()
        self._configure_adj(state[1], state[2])
        self.queue_draw()
        self.emit('view-changed')

    def set_marks(self, marks, at=-1):
        """Highlight search hits and bring the current one into view."""
        self.marks, self.mark_at = marks, at
        if 0 <= at < len(marks):
            page, box = marks[at]
            i = self._index(page)
            if i is not None:
                self.center_at(i, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self.queue_draw()

    def view_box(self):
        """The part of the current page that is on screen, in page points (not clamped)."""
        if self.doc is None or not self._tops:
            return None
        i = self.current_page_index()
        x0, y0 = self._to_page(i, 0, 0)
        x1, y1 = self._to_page(i, self.get_width(), self.get_height())
        return x0, y0, x1, y1

    def center_at(self, i, x, y):
        """Scroll so that a page point is in the middle of the view."""
        page = self.doc.pages[i]
        self._place(self.zoom, (i, x / page.w, y / page.h), self.get_width() / 2, self.get_height() / 2)

    def fit_content(self):
        """Zoom and scroll so that everything on the current whiteboard is visible."""
        i = self.current_page_index()
        page = self.doc.pages[i]
        x0, y0, x1, y1 = page_box(page)
        zoom = min(self.get_width() / (x1 - x0), self.get_height() / (y1 - y0)) * 0.92
        zoom = max(self._min_zoom(), min(1.5, zoom))
        self._place(zoom, (i, (x0 + x1) / 2 / page.w, (y0 + y1) / 2 / page.h), self.get_width() / 2, self.get_height() / 2)

    def _anchor(self, x, y):
        """The page point under a widget point, as (index, fraction across, fraction down)."""
        i = self._page_at(x + self._h(), y + self._v())
        page = self.doc.pages[i]
        left, top = self._origin(i)
        z = self._z(page)
        return i, (x - left + self._h()) / (page.w * z), (y - top + self._v()) / (page.h * z)

    def _place(self, zoom, anchor, x, y):
        """Show the document at zoom with the anchored page point under widget point (x, y)."""
        i, fx, fy = anchor
        if i >= len(self.doc.pages):
            return
        page = self.doc.pages[i]
        changed = abs(zoom - self.zoom) > 1e-6
        if changed:
            self.zoom = zoom
            self._relayout()
        left, top = self._origin(i)
        z = self._z(page)
        self._want = (left + fx * page.w * z - x, top + fy * page.h * z - y)
        self._configure_adj(*self._want)
        if changed:
            self._zoom_changed()

    def _zoom_changed(self):
        if self._settle:
            GLib.source_remove(self._settle)
        # Textures are stretched while zooming and re-rendered sharp once it settles.
        self._settle = GLib.timeout_add(160, self._zoom_settled)
        self.queue_draw()
        self.emit('view-changed')

    def _zoom_settled(self):
        self._settle = 0
        self._render_zoom = self.zoom
        self.queue_draw()
        return False

    def zoom_by(self, factor):
        self.set_zoom(self.zoom * factor)

    def fit_width(self):
        if self.doc:
            page = self.doc.pages[self.current_page_index()]
            self.set_zoom((self.get_width() - 2 * SIDE) / (page.w * self._pf.get(id(page), 1.0)))

    # -- ruler --------------------------------------------------------------

    def set_ruler(self, on):
        """Put the ruler on the screen or take it away."""
        self.ruler = {'x': self.get_width() / 2, 'y': self.get_height() / 2, 'a': 0.0} if on else None
        self.queue_draw()

    def _ruler_len(self):
        return max(260.0, 0.42 * max(self.get_width(), self.get_height()))

    def _ruler_local(self, x, y):
        """A widget point as (distance along the ruler, distance across it) from its centre."""
        r = self.ruler
        c, s = math.cos(r['a']), math.sin(r['a'])
        dx, dy = x - r['x'], y - r['y']
        return dx * c + dy * s, -dx * s + dy * c

    def _ruler_zone(self, x, y):
        """'edge' near one of the long edges, 'body' on the ruler itself, None elsewhere."""
        if self.ruler is None:
            return None
        u, v = self._ruler_local(x, y)
        if abs(u) > self._ruler_len() + RULER_SNAP:
            return None
        if abs(abs(v) - RULER_W) <= RULER_SNAP:
            return 'edge'
        return 'body' if abs(v) < RULER_W else None

    def _ruler_edge(self, x, y):
        """The long edge nearest a point, as (a point on it, its direction)."""
        r = self.ruler
        c, s = math.cos(r['a']), math.sin(r['a'])
        side = 1.0 if self._ruler_local(x, y)[1] >= 0 else -1.0
        return r['x'] - s * RULER_W * side, r['y'] + c * RULER_W * side, c, s

    @staticmethod
    def _onto(line, x, y):
        x0, y0, dx, dy = line
        t = (x - x0) * dx + (y - y0) * dy
        return x0 + t * dx, y0 + t * dy

    def _turn_ruler(self, angle):
        # Settle on the nearest 45 degrees when turned to within a degree and a half of it.
        near = round(angle / (math.pi / 4)) * (math.pi / 4)
        self.ruler['a'] = near if abs(angle - near) < math.radians(1.5) else angle
        self.queue_draw()

    def _label(self, text):
        layout = self._labels.get(text)
        if layout is None:
            layout = self._labels[text] = self.create_pango_layout(text)
            layout.set_font_description(Pango.FontDescription.from_string('Sans 8'))
        return layout

    def _draw_ruler(self, snap):
        r, half = self.ruler, self._ruler_len()
        snap.save()
        snap.translate(Graphene.Point().init(r['x'], r['y']))
        snap.rotate(math.degrees(r['a']))
        snap.append_color(_RULER_BODY, _rect(-half, -RULER_W, 2 * half, 2 * RULER_W))
        for y in (-RULER_W, RULER_W - 1):
            snap.append_color(_RULER_INK, _rect(-half, y, 2 * half, 1))
        # The scale is the page's own: a centimetre on the ruler is a centimetre on the printed
        # page, so it grows and shrinks with the zoom.
        mm = 72 / 25.4 * self.shown_zoom()
        step = next((n for n in (1, 5, 10, 50, 100, 500, 1000) if n * mm >= 3), 5000)
        every = next((n for n in (10, 50, 100, 500, 1000, 5000) if n >= step and n * mm >= 26), 10000)
        start = -half + 12
        n = 0
        while start + n * mm <= half - 6:
            u = start + n * mm
            long = n % (step * 10 if step < 10 else every) == 0
            size = 15 if long else 10 if n % (step * 5) == 0 else 6
            snap.append_color(_RULER_INK, _rect(u - 0.5, -RULER_W, 1, size))
            snap.append_color(_RULER_INK, _rect(u - 0.5, RULER_W - size, 1, size))
            if n % every == 0:
                snap.save()
                snap.translate(Graphene.Point().init(u + 3, -RULER_W + 13))
                snap.append_layout(self._label('%g' % (n / 10)), _RULER_INK)
                snap.restore()
            n += step
        degrees = round(math.degrees(r['a'])) % 180
        snap.save()
        snap.translate(Graphene.Point().init(-12, RULER_W - 30))
        snap.append_layout(self._label('%d°  cm' % degrees), _RULER_INK)
        snap.restore()
        snap.restore()

    # -- touch: pan, pinch and palm rejection ------------------------------

    def _raw_event(self, controller, event):
        # PyGObject can hand the signal argument over as None; the controller always has it.
        event = event or controller.get_current_event()
        if event is None:
            return False
        kind = event.get_event_type()
        if event.get_device_tool() is not None:
            self._has_pen = True
            # A pen in range keeps sending motion, so recent pen events mean it is still there.
            # (Proximity-out is not reliably delivered, so it cannot be waited for.)
            if kind not in (Gdk.EventType.PROXIMITY_OUT, Gdk.EventType.LEAVE_NOTIFY):
                self._pen_seen = GLib.get_monotonic_time()
                self._palm()
            if kind not in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
                self.log('canvas: pen %s' % kind.value_nick)
            self._raw_pen_event(kind, event)
            return False
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.GRAB_BROKEN, Gdk.EventType.TOUCH_CANCEL):
            self.log('canvas: %s without a pen tool' % kind.value_nick)
        name = _TOUCH.get(kind)
        if name is None:
            return False
        ok, x, y = event.get_position()
        native = self.get_native()
        if native is not None:
            nx, ny = native.get_surface_transform()
            done, point = native.compute_point(self, Graphene.Point().init(x - nx, y - ny))
            if done:
                x, y = point.x, point.y
        self._touch(name, event.get_event_sequence(), x, y)
        return True

    def _raw_pen_event(self, kind, event):
        """The pen is driven from its own events, seen here before anything else in the window.
        GTK's stylus gesture was dropping strokes: a gesture can be cancelled or left waiting by
        what other gestures and touches do, and then a pen that comes down draws nothing. Here
        a press of the tip always starts a stroke and only the pen itself ends it."""
        if kind == Gdk.EventType.BUTTON_PRESS:
            if event.get_button() != 1:
                return   # a barrel button; it presses without the tip touching
            x, y = self._event_point(event)
            if self._pen_down:
                # The lift before this never arrived: finish that stroke where it stopped.
                self.log('pen press while still down: ending the earlier stroke')
                self._pen_down = False
                self._up(*self._pen_at)
            tool = event.get_device_tool()
            held = event.get_modifier_state() & (Gdk.ModifierType.BUTTON2_MASK | Gdk.ModifierType.BUTTON3_MASK)
            erase = tool.get_tool_type() == Gdk.DeviceToolType.ERASER or bool(held)
            self._raw_pen = 'down'
            self._pen_down = True
            self._src = 'pen'
            self._pen_at = (x, y)
            self._down(x, y, self._event_pressure(event), erase)
            self.log('pen down at %.0f,%.0f -> %s' % (x, y, self._op['kind'] if self._op else
                                                      'nothing (text, a selection grabbed, or outside every page)'))
        elif kind == Gdk.EventType.MOTION_NOTIFY and self._raw_pen == 'down':
            x, y = self._event_point(event)
            p = self._event_pressure(event)
            if self._op is not None and self._op['kind'] == 'stroke':
                # GTK merges motion into one event a frame; its history holds the samples in
                # between, which keeps fast handwriting smooth.
                try:
                    history = event.get_history() or []
                    if len(history) > 1:
                        ox, oy = event.get_position()[1:]
                        ax, ay, ap = int(Gdk.AxisUse.X), int(Gdk.AxisUse.Y), int(Gdk.AxisUse.PRESSURE)
                        last = history[-1].axes
                        if abs(last[ax] - ox) < 2 and abs(last[ay] - oy) < 2:
                            # A sample without a position reads as the corner of the window, and
                            # would draw a line to there. Only samples that have one, and that
                            # lie between the last point and this one, are used.
                            px, py = self._pen_at
                            pad = 24 + 0.5 * (abs(x - px) + abs(y - py))
                            both = Gdk.AxisFlags.X | Gdk.AxisFlags.Y
                            for tc in history[:-1]:
                                if tc.flags & both != both:
                                    continue
                                hx, hy = x + tc.axes[ax] - ox, y + tc.axes[ay] - oy
                                if not (min(x, px) - pad <= hx <= max(x, px) + pad and min(y, py) - pad <= hy <= max(y, py) + pad):
                                    continue
                                hp = tc.axes[ap] if tc.flags & Gdk.AxisFlags.PRESSURE else p
                                self._move(hx, hy, max(0.02, min(1.0, hp)))
                except Exception:
                    pass
            self._pen_at = (x, y)
            self._move(x, y, p)
        elif self._raw_pen == 'down' and (kind == Gdk.EventType.PROXIMITY_OUT or (
                kind == Gdk.EventType.BUTTON_RELEASE and event.get_button() == 1)):
            self._raw_pen = None
            if self._pen_down:
                self._pen_down = False
                had = self._op['kind'] if self._op else None
                self._up(*(self._event_point(event) if kind == Gdk.EventType.BUTTON_RELEASE else self._pen_at))
                self.log('pen up (%s), was drawing: %s' % ('lifted' if kind == Gdk.EventType.BUTTON_RELEASE else 'left range', had))
            else:
                self.log('pen up, but the stroke had been cancelled')

    # -- keeping GNOME's idea of where the pen is from going stale --------------
    #
    # THIS IS A WORKAROUND FOR ONE MACHINE'S FAULT, NOT PART OF HOW INKPAD WORKS. It was found
    # and is only known to be needed on a Chromebook running GNOME (Wayland, mutter 50.1) whose
    # screen is a Goodix panel (GDIX0000:00 27C6:0C0B) with a pen. Ordinary drawing tablets and
    # other compositors have not been seen to need it. It costs nothing where it is not needed,
    # and can be removed once mutter is fixed.
    #
    # What goes wrong. Writing in fullscreen, strokes were lost: the pen touched the screen
    # and nothing was drawn until a finger had moved the page. Inkpad's input log showed that
    # no pen event reached the window at all for those strokes, while a recording of the
    # kernel's input device showed the pen reporting them correctly. They were lost in between,
    # in the compositor.
    #
    # Why. For every pointing device mutter keeps a "sprite": the actor (window) under it and,
    # so as not to search again on every motion, a "clear area", the region around the point
    # in which that same actor is known to be on top. While the point stays inside the area
    # the search ("pick") is skipped: clutter_stage_pick_and_update_sprite(). When a pen leaves
    # the range of the screen, clutter_stage_emit_event() sets the sprite's actor to nothing
    # on PROXIMITY_OUT but leaves the clear area as it was. When the pen comes back inside that
    # area the pick is skipped, the actor stays nothing, the tablet tool is given no surface
    # to focus (repick_for_event() in meta-wayland-tablet-tool.c) and every event of that visit
    # is thrown away. It stays so until clutter_stage_invalidate_devices() runs, which only
    # happens when some actor on the stage is laid out anew: a window moving or changing size,
    # a menu opening. Redrawing the window's contents does not count.
    #
    # Why fullscreen, and why this device. A fullscreen window covers the whole stage, so the
    # clear area is the whole screen and the pen cannot come back outside it; and nothing else
    # on screen moves, so nothing invalidates it. In a normal window the top bar, the dock and
    # the clock keep doing so by themselves, which hides the fault most of the time. And this
    # panel reports the pen out of range every time it is lifted a centimetre or so, between
    # the strokes of one word; a tablet whose pen stays in range while writing never sends
    # the proximity-out that starts all this.
    #
    # What is done about it. Each time the pen leaves (the window sees the proximity-out and
    # calls _pen_gone()), a popup one pixel large with nothing drawn in it is shown at the
    # corner of the canvas for 60 ms. A popup is a surface of its own, so mapping it adds an
    # actor to the stage, which makes mutter throw its clear areas away and look again the
    # next time the pen appears. If the pen came back before that took effect, that visit is
    # lost and so is its proximity-out, so the popup is repeated every half second (at most
    # 40 times) until a pen event is seen again (_pen_back()).
    #
    # What was tried and did not work: redrawing the canvas every frame, a translucent window
    # background and a window opacity just below 1 (none of them is a new layout), and falling
    # back to other gestures (there is no event to fall back on).

    def _pen_gone(self):
        self._pen_back()
        self._nudges = 0
        self._nudge_timer = GLib.timeout_add(30, self._nudge)

    def _pen_back(self):
        if self._nudge_timer:
            GLib.source_remove(self._nudge_timer)
            self._nudge_timer = 0

    def _nudge(self):
        self._nudge_timer = 0
        if not self.get_mapped():
            return False
        if self._nudger is None:
            self._nudger = Gtk.Popover(autohide=False, has_arrow=False, can_focus=False, can_target=False,
                                       child=Gtk.Box(width_request=1, height_request=1))
            self._nudger.add_css_class('nudge')
            self._nudger.set_parent(self)
            box = Gdk.Rectangle()
            box.x, box.y, box.width, box.height = 0, 0, 1, 1
            self._nudger.set_pointing_to(box)
        self._nudger.popup()
        GLib.timeout_add(60, lambda: self._nudger.popdown() and False)
        self.log('nudged the compositor (%d)' % self._nudges)
        # If the pen came back before this took effect, that visit was lost and its leaving
        # was never reported here: keep on for a while, so that it cannot stay that way.
        self._nudges += 1
        if self._nudges < 40:
            self._nudge_timer = GLib.timeout_add(500, self._nudge)
        return False

    def log(self, text):
        """A line in the input log, when that is switched on in the settings."""
        if self.input_log:
            try:
                os.makedirs(os.path.dirname(self.input_log), exist_ok=True)
                with open(self.input_log, 'a', encoding='utf-8') as f:
                    f.write('%.3f %s\n' % (GLib.get_monotonic_time() / 1e6, text))
            except OSError:
                pass

    def _drop(self, why):
        """Something other than the pen wants the stroke in progress abandoned. While the pen
        is writing nothing else may do that; otherwise it is cancelled as asked."""
        if self._pen_down and self._raw_pen == 'down':
            self.log('kept the pen stroke although: ' + why)
            return
        if self._op is not None:
            self.log('cancelled %s: %s' % (self._op['kind'], why))
        self._cancel()

    def _event_point(self, event):
        ok, x, y = event.get_position()
        native = self.get_native()
        if native is not None:
            nx, ny = native.get_surface_transform()
            done, point = native.compute_point(self, Graphene.Point().init(x - nx, y - ny))
            if done:
                return point.x, point.y
        return x, y

    @staticmethod
    def _event_pressure(event):
        ok, p = event.get_axis(Gdk.AxisUse.PRESSURE)
        return max(0.02, min(1.0, p)) if ok else 0.5

    def _pen_active(self):
        return self._pen_down or GLib.get_monotonic_time() - self._pen_seen < self.palm_ms * 1000

    def _palm(self):
        """The pen is at the screen: whatever touches it now is the hand, so undo what it did."""
        self._stop_fling()
        if not self._touches:
            return
        self._rejected.update(self._touches)
        self._touches.clear()
        g, self._tg = self._tg, None
        if g is None:
            return
        if g.get('grab'):
            if g['grab'] != 'held':
                self._cancel()   # the hand had hold of the selection: it goes back
        elif g['draw']:
            self._drop('the hand that was drawing is a palm')
        elif g['moved']:
            # What the hand did as the pen came down is undone: the view goes back to where it
            # was a little before. A hand that has long been lying still changes nothing, and
            # neither does a pan that was finished well before the pen arrived.
            now = GLib.get_monotonic_time()
            if now - g['track'][-1][0] > 350_000:
                return   # it last moved a while ago: that was meant
            zoom, h, v = next((state for at, state in reversed(g['seen']) if now - at > 350_000), g['seen'][0][1])
            if (zoom, h, v) == (self.zoom, self._h(), self._v()):
                return
            self.zoom = zoom
            self._relayout()
            self._configure_adj(h, v)
            self._zoom_changed()

    def _touch(self, kind, seq, x, y):
        if kind == 'begin':
            if self._pen_active():
                self._rejected.add(seq)
                self.log('touch ignored: the pen is at work')
                return
            self.log('touch begins, %d on the screen' % (len(self._touches) + 1))
            self._touches[seq] = (x, y)
            self._touch_reset(x, y)
        elif kind == 'update':
            if seq in self._touches:
                self._touches[seq] = (x, y)
                self._touch_update()
        else:
            self._rejected.discard(seq)
            if self._touches.pop(seq, None) is not None:
                self._touch_reset(x, y, kind == 'end')

    def _touch_shape(self):
        pts = list(self._touches.values())
        n = len(pts)
        cx, cy = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        return cx, cy, sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / n

    def _touch_reset(self, x, y, lifted=False):
        """A finger came or went: restart the gesture from where the view is now."""
        old, n = self._tg, len(self._touches)
        self._stop_fling()
        self._stop_press()
        if old is not None and old.get('grab'):
            if self._grab_touch(old, n, x, y, lifted):
                return
            old = None
        if old is not None and old['draw']:
            # A second finger turns a finger stroke into a pinch; lifting ends it.
            if self._pen_down:
                pass   # the pen has taken over; the finger's stroke is long gone
            elif n == 0 and lifted:
                self._up(x, y)
            else:
                self._drop('a finger stroke turned into a gesture')
        if n == 0:
            self._tg = None
            pages = len(self.doc.pages)
            if self._pull:
                self._pull_release()
            if old is not None and self._across() and old.get('over') and old['page'] < len(self._slots):
                # Let go past the edge of a zoomed page: far enough turns it, less springs back.
                i, over = old['page'], old['over']
                j = i + (1 if over > 0 else -1)
                if abs(over) > max(110.0, 0.22 * self.get_width()) and 0 <= j < len(self._slots):
                    self._turning = j
                    self._glide(self._hadj, self._span(j)[0 if j > i else 1])
                else:
                    self._turning = i
                    self._glide(self._hadj, self._span(i)[1 if over > 0 else 0])
                return
            if old is not None and old['moved'] and not old['draw'] and len(self.doc.pages) == pages \
                    and self._rest(old['origin'], old['track']):
                pass   # side by side, the page in view comes to rest in the middle
            elif old is not None and old['moved'] and not old['draw'] and old['n'] == 1 and lifted:
                self._start_fling(old['track'])
            elif old is not None and lifted and not old['moved'] and not old['draw'] and not old.get('ruler') \
                    and old['n'] == 1:
                if self.tool == 'lasso':
                    self._pick_at(x, y)   # with the select tool a tap picks what is under it
                else:
                    self._toggle_tape(x, y)   # a tap
            return
        cx, cy, span = self._touch_shape()
        now = GLib.get_monotonic_time()
        draw = n == 1 and old is None and (self.finger_draw or self.tool == 'laser')
        self._tg = {'n': n, 'cx': cx, 'cy': cy, 'span': span, 'zoom': self.zoom, 'draw': draw,
                    'moved': old is not None and old['moved'] and not old['draw'],
                    't0': old['t0'] if old else now, 'track': [(now, cx, cy)],
                    'origin': old['origin'] if old else (self.zoom, self._h(), self._v()),
                    'anchor': self._anchor(cx, cy), 'over': 0.0,
                    'seen': old['seen'] if old else [(now, (self.zoom, self._h(), self._v()))],
                    'page': old['page'] if old else (self.current_page_index() if self._tops and not self._board else 0)}
        if self._ruler_zone(cx, cy):
            # Fingers on the ruler handle the ruler: one moves it, two also turn it.
            pts = list(self._touches.values())
            self._tg.update(draw=False, ruler=dict(self.ruler), turn=(
                math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]) if n > 1 else None))
        elif self.tool == 'lasso' and self._sel is not None and old is None and n == 1 and not self._pen_down \
                and self._grab_selection(cx, cy, 22):
            # A finger on the selection carries it; at its edge, resizes it.
            self._src = 'touch'
            self._tg.update(draw=True, grab='move')
            self._press(cx, cy)
        elif draw:
            self._src = 'touch'
            self._down(cx, cy, 0.5, False)
        elif n == 1 and old is None and self.tool == 'lasso':
            self._press(cx, cy)

    def _grab_touch(self, old, n, x, y, lifted):
        """Fingers that have hold of the selection: one moves or resizes it, a second one beside
        it takes a copy along instead. True when that is all the fingers' coming or going meant."""
        grab = old['grab']
        if n == 0:
            self._tg = None
            if grab in ('move', 'copy'):
                self._up(x, y) if lifted else self._cancel()
            return True
        if grab == 'move' and n == 2:
            self._cancel()
            cx, cy, _ = self._touch_shape()
            if self._copy_selection_from(cx, cy):
                old.update(grab='copy', n=2)
                return True
            return False   # the selection is gone: the fingers move the page
        if grab in ('move', 'copy'):
            # A finger more or less than the gesture needs ends it where it is.
            self._up(old['cx'], old['cy']) if grab == 'copy' else self._cancel()
        old['grab'] = 'held'   # nothing more until every finger is off the screen
        return True

    def _touch_update(self):
        g = self._tg
        if g is None:
            return
        cx, cy, span = self._touch_shape()
        if g.get('ruler') and self.ruler is not None:
            was, turned = g['ruler'], 0.0
            if g['turn'] is not None and len(self._touches) > 1:
                pts = list(self._touches.values())
                turned = math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]) - g['turn']
            # It turns about the point between the fingers and follows them.
            c, s = math.cos(turned), math.sin(turned)
            vx, vy = was['x'] - g['cx'], was['y'] - g['cy']
            self.ruler['x'], self.ruler['y'] = cx + vx * c - vy * s, cy + vx * s + vy * c
            self._turn_ruler(was['a'] + turned)
            g['moved'] = False   # lifting the fingers must not fling the page
            return
        if g.get('grab'):
            if g['grab'] in ('move', 'copy'):
                g['cx'], g['cy'] = cx, cy
                self._move(cx, cy, 0.5)
            return
        if g['draw']:
            self._move(cx, cy, 0.5)
            return
        if not g['moved']:
            # A small dead zone, so that a brush of the hand does not move the page.
            if math.hypot(cx - g['cx'], cy - g['cy']) < 8 and abs(span - g['span']) < 8:
                return
            g['moved'] = True
            self._stop_press()
        zoom = g['zoom']
        if g['n'] > 1 and g['span'] > 12:
            zoom = max(self._min_zoom(), min(self._max_zoom(), zoom * span / g['span']))
        # The page point that was between the fingers stays between them.
        self._place(zoom, g['anchor'], cx, cy)
        g['over'] = 0.0
        if self._across() and g['n'] == 1 and g['page'] < len(self._slots) and self._span(g['page'])[1] > self._span(g['page'])[0] + 1:
            # Zoomed in, a finger moves about inside its page. Past the edge the page gives
            # only a little: it takes a long, deliberate pull to turn to the next one.
            lo, hi = self._span(g['page'])
            want = self._want[0]
            g['over'] = want - hi if want > hi else want - lo if want < lo else 0.0
            self._hadj.set_value(max(lo, min(hi, want)) + 0.4 * g['over'])
        elif self._across() and g['n'] == 1 and abs(g['origin'][1] - self._h()) < 1 and abs(g['origin'][0] - self.zoom) < 1e-6:
            # Only a drag that began with the first or last page at rest pulls a new one out;
            # turning to that page with too much of a swing does not.
            over = self._want[0] - self._h()
            self._pulled(over if abs(over) > 1 else 0.0)
        track = g['track']
        track.append((GLib.get_monotonic_time(), cx, cy))
        del track[:-6]
        seen = g['seen']
        if track[-1][0] - seen[-1][0] > 40_000:   # the view at intervals, for the palm to be undone to
            seen.append((track[-1][0], (self.zoom, self._h(), self._v())))
            while len(seen) > 2 and track[-1][0] - seen[1][0] > 350_000:
                del seen[0]

    PULL = 280.0   # how far past the end the pages have to be pulled for a new page

    def _pulled(self, over):
        """Note that the view is being pushed past an end of a side-by-side notebook."""
        if not self._across() or self.on_pull_add is None:
            return
        if over != self._pull:
            self._pull = over
            self.queue_draw()

    def _rest(self, origin=None, track=None):
        """Side by side, bring one whole page to rest in the window. origin is the (zoom, x, y)
        a drag started from: a drag far or fast enough turns to the next page. False when
        there is nothing to settle, as inside a page that is zoomed in past the window."""
        if not self._across() or self._hadj is None or not self._slots:
            return False
        view, h = self.get_width(), self._h()
        i = self._page_at(h + view / 2, 0)
        lo, hi = self._span(i)
        if hi > lo + 1:
            # Zoomed in: the view belongs to one page and never rests across two.
            if lo - 0.5 <= h <= hi + 0.5:
                return False
            self._turning = i
            self._glide(self._hadj, max(lo, min(hi, h)))
            return True
        if origin is not None and abs(origin[0] - self.zoom) < 1e-6:
            start = self._page_at(origin[1] + view / 2, 0)
            moved = h - origin[1]
            speed = 0.0
            if track and len(track) > 1 and track[-1][0] > track[0][0] \
                    and GLib.get_monotonic_time() - track[-1][0] < 60_000:   # still moving when let go
                speed = (track[0][1] - track[-1][1]) / (track[-1][0] - track[0][0]) * 1e6
            if abs(moved) > 0.18 * view or (abs(speed) > 400 and speed * moved > 0):
                i = max(0, min(len(self._slots) - 1, start + (1 if moved > 0 else -1)))
            else:
                i = start
        if abs(self._slots[i] - h) > 0.5:
            self.scroll_to_page(i)
        return True

    def _pull_release(self):
        """The pull was let go: far enough makes a page, otherwise it just springs back."""
        pull, self._pull = self._pull, 0.0
        if pull:
            self.queue_draw()
            if abs(pull) >= self.PULL and self.on_pull_add is not None:
                self.on_pull_add(1 if pull > 0 else -1)
        return False

    def _draw_pull(self, snap, width, height):
        done = min(1.0, abs(self._pull) / self.PULL)
        x = width - 70 if self._pull > 0 else 70
        y = height / 2
        ring = Gsk.PathBuilder.new()
        ring.add_circle(Graphene.Point().init(x, y), 26)
        ring = ring.to_path()
        snap.append_fill(ring, Gsk.FillRule.WINDING, rgba(0.21, 0.52, 0.89, 0.12 + 0.5 * done))
        snap.append_stroke(ring, Gsk.Stroke.new(2), _ACCENT)
        snap.append_color(_WHITE if done >= 1 else _ACCENT, _rect(x - 9, y - 1, 18, 2))
        snap.append_color(_WHITE if done >= 1 else _ACCENT, _rect(x - 1, y - 9, 2, 18))
        layout = self._label(tr('松手添加页面') if done >= 1 else tr('继续拉动添加页面'))
        snap.save()
        snap.translate(Graphene.Point().init(x - layout.get_pixel_size()[0] / 2, y + 34))
        snap.append_layout(layout, _ACCENT)
        snap.restore()

    def _scroll_by(self, dx, dy):
        if self._board:
            self._configure_adj(self._bo[0] + dx, self._bo[1] + dy)
            return
        if self._hadj:
            x = self._hadj.get_value() + dx
            if self._across() and self._slots:
                # Inside a zoomed page, gliding and dragging stop at its edge.
                lo, hi = self._span(self.current_page_index())
                if hi > lo + 1 and lo - 0.5 <= self._hadj.get_value() <= hi + 0.5:
                    x = max(lo, min(hi, x))
            self._hadj.set_value(x)
        if self._vadj:
            self._vadj.set_value(self._vadj.get_value() + dy)

    def _start_fling(self, track):
        t1, x1, y1 = track[-1]
        t0, x0, y0 = next((s for s in track if t1 - s[0] < 90_000), track[0])
        if GLib.get_monotonic_time() - t1 > 60_000 or t1 - t0 < 8_000:
            return
        vx, vy = (x0 - x1) / (t1 - t0) * 1e6, (y0 - y1) / (t1 - t0) * 1e6
        if math.hypot(vx, vy) < 250:
            return
        state = {'v': (vx, vy), 't': None}

        def tick(widget, clock):
            now = clock.get_frame_time()
            dt = 0.0 if state['t'] is None else (now - state['t']) / 1e6
            state['t'] = now
            vx, vy = state['v']
            self._scroll_by(vx * dt, vy * dt)
            k = math.exp(-4.0 * dt)
            state['v'] = (vx * k, vy * k)
            if math.hypot(vx, vy) < 30:
                self._fling = 0
                return False
            return True

        self._fling = self.add_tick_callback(tick)

    def _stop_fling(self):
        if self._fling:
            self.remove_tick_callback(self._fling)
            self._fling = 0

    def _pinch_begin(self, gesture, seq):
        self._drop('a pinch began')
        self._pinch_zoom = self.zoom

    def _pinch_scale(self, gesture, scale):
        ok, x, y = gesture.get_bounding_box_center()
        self.set_zoom(self._pinch_zoom * scale, *((x, y) if ok else self._ptr))

    def _ctrl_scroll(self, controller, dx, dy):
        if self._ruler_zone(*self._ptr):
            # The wheel over the ruler turns it: a degree a notch, fifteen with Shift.
            shift = controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
            self._turn_ruler(self.ruler['a'] + math.radians((dy or dx) * (15 if shift else 1)))
            return True
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.set_zoom(self.zoom * (1.1 ** -dy), *self._ptr)
            return True
        if self._across() and self._vadj is not None and self._hadj is not None and (dx or dy):
            return self._turn_scroll(dx, dy, controller.get_unit() == Gdk.ScrollUnit.SURFACE)
        if self._board:
            # A board is not scrolled by the scrolled window: it has no extent to scroll within.
            step = 1.0 if controller.get_unit() == Gdk.ScrollUnit.SURFACE else 48.0
            if controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
                dx, dy = dy, dx
            self._scroll_by(dx * step, dy * step)
            return True
        return False

    def _turn_scroll(self, dx, dy, smooth):
        """Scrolling when the pages are side by side. It moves about inside a page that is
        larger than the window; where there is nothing left to move, it turns the page: once
        per notch of a wheel, once per swipe on a touchpad. It never pulls a new page out."""
        upright = abs(dy) >= abs(dx)
        d = dy if upright else dx
        step = 1.0 if smooth else 60.0
        v, h = self._vadj, self._hadj
        if upright:
            room = v.get_upper() - v.get_page_size() - v.get_value() if d > 0 else v.get_value()
            # A few pixels of play, as a scrollbar taking its space leaves, is not worth scrolling.
            if room > 0.5 and v.get_upper() - v.get_page_size() > 12:
                v.set_value(v.get_value() + d * step)
                self._turn_hold(smooth)   # reaching the edge mid-swipe must not turn the page too
                return True
        i = self.current_page_index()
        slot = self.doc.pages[i].w * self._z(self.doc.pages[i]) + 2 * SIDE
        if slot > self.get_width() + 1:
            lo, hi = self._slots[i], self._slots[i] + slot - self.get_width()
            room = hi - h.get_value() if d > 0 else h.get_value() - lo
            if room > 0.5:
                h.set_value(max(lo, min(hi, h.get_value() + d * step)))
                self._turn_hold(smooth)
                return True
        now = GLib.get_monotonic_time()
        quiet = now - self._turn_at > 180_000
        self._turn_at = now
        if smooth:
            # A swipe goes on sending movement long after the fingers have left; all of it,
            # up to the next pause, is one turn.
            if quiet:
                self._turn_sum, self._turn_done = 0.0, False
            self._turn_sum += d
            if self._turn_done or abs(self._turn_sum) < 24:
                return True
            self._turn_done = True
        else:
            if quiet or self._turn_sum * d < 0:
                self._turn_sum = 0.0
            self._turn_sum += d
            # A wheel may report fractions of a notch, or spin on freely: a whole notch turns
            # a page, and not more often than the eye can follow.
            if abs(self._turn_sum) < 0.99 or now - self._turned_at < 220_000:
                return True
            self._turn_sum = 0.0
            self._turned_at = now
        playing = self._anim is not None and self._anim.get_state() == Adw.AnimationState.PLAYING
        i = (self._turning if playing else i) + (1 if d > 0 else -1)
        if 0 <= i < len(self._slots):
            self.scroll_to_page(i)
        return True

    def _turn_hold(self, smooth):
        if smooth:
            self._turn_at, self._turn_done = GLib.get_monotonic_time(), True

    # -- scrollbars ----------------------------------------------------------
    #
    # Drawn here rather than by the scrolled window, so that they only do what is wanted:
    # they show the place, the mouse can drag them by the handle, a click beside the handle
    # does nothing, and fingers and the pen pass through them.

    def _bars(self):
        """The bars on show as (axis, lowest value, highest value, x, y, length, handle start,
        handle length). Side by side, the lower bar covers the page in view only."""
        if not self.scrollbars or self._board or self.doc is None or not self._tops:
            return []
        W, H = self.get_width(), self.get_height()
        out = []
        for axis, adj, view, across in (('v', self._vadj, H, W), ('h', self._hadj, W, H)):
            if adj is None:
                continue
            lo, extent = 0.0, adj.get_upper()
            if axis == 'h' and self._across() and self._slots:
                i = min(self.current_page_index(), len(self._slots) - 1)
                page = self.doc.pages[i]
                lo, extent = self._slots[i], max(float(view), page.w * self._z(page) + 2 * SIDE)
            if extent <= view + 1:
                continue
            hi = lo + extent - view
            track = view - 16
            handle = max(40.0, track * view / extent)
            at = 8 + (track - handle) * max(0.0, min(1.0, (adj.get_value() - lo) / (hi - lo)))
            out.append((axis, lo, hi, across - 9, 8, track, at, handle))
        return out

    def _draw_bars(self, snap):
        for axis, lo, hi, edge, start, track, at, handle in self._bars():
            thick = 8 if self._bar_drag and self._bar_drag[0] == axis else 5
            box = _rect(edge + 5 - thick, at, thick, handle) if axis == 'v' else _rect(at, edge + 5 - thick, handle, thick)
            shape = Gsk.RoundedRect()
            shape.init_from_rect(box, thick / 2)
            snap.push_rounded_clip(shape)
            snap.append_color(rgba(0.55, 0.55, 0.58, 0.75), box)
            snap.pop()

    def _bar_at(self, x, y):
        """The bar whose handle is under a point, ready to be dragged."""
        for axis, lo, hi, edge, start, track, at, handle in self._bars():
            along, across = (y, x) if axis == 'v' else (x, y)
            if across >= edge - 8 and at <= along <= at + handle:
                adj = self._vadj if axis == 'v' else self._hadj
                return axis, lo, hi, (hi - lo) / max(1.0, track - handle), adj.get_value()
        return None

    def _pan_begin(self, gesture, x, y):
        self._pan = (self._h(), self._v())

    def _pan_update(self, gesture, dx, dy):
        if self._board:
            self._configure_adj(self._pan[0] - dx, self._pan[1] - dy)
            return
        if self._hadj:
            self._hadj.set_value(self._pan[0] - dx)
        if self._vadj:
            self._vadj.set_value(self._pan[1] - dy)

    # -- page cache ---------------------------------------------------------

    def _full_size(self, page):
        if self.zoom < self._render_zoom / 1.5:
            # Zooming far out: stretching the old raster would mean caching a huge area of it,
            # so step the raster down along the way instead of waiting for the zoom to settle.
            self._render_zoom = self.zoom
        s = self._render_zoom * self._pf.get(id(page), 1.0) * self._dev
        return max(1, round(page.w * s)), max(1, round(page.h * s))

    def _visible(self, i):
        """The part of page i inside the viewport, in page points."""
        page = self.doc.pages[i]
        px, py, pw, ph = self._page_rect(i)
        k = pw / page.w
        if self._board:
            return -px / k, -py / k, (self.get_width() - px) / k, (self.get_height() - py) / k
        return (max(0.0, -px / k), max(0.0, -py / k),
                min(page.w, (self.get_width() - px) / k), min(page.h, (self.get_height() - py) / k))

    def _window(self, page, i, full):
        """Pixel window of the page raster to cache: all of it when it is small enough."""
        if full[0] * full[1] <= MAX_PIXELS and not self._board:
            return 0, 0, full[0], full[1]
        sx, sy = full[0] / page.w, full[1] / page.h
        x0, y0, x1, y1 = self._visible(i)
        mx, my = (x1 - x0) * 0.4, (y1 - y0) * 0.4
        if self._board:   # no edges to stop at; the window may lie anywhere
            wx0, wy0 = math.floor((x0 - mx) * sx / STRIP) * STRIP, math.floor((y0 - my) * sy / STRIP) * STRIP
            wx1 = (math.floor((x1 + mx) * sx / STRIP) + 1) * STRIP
            wy1 = (math.floor((y1 + my) * sy / STRIP) + 1) * STRIP
            return wx0, wy0, wx1 - wx0, wy1 - wy0
        wx0 = max(0, int((x0 - mx) * sx) // STRIP * STRIP)
        wy0 = max(0, int((y0 - my) * sy) // STRIP * STRIP)
        wx1 = min(full[0], (int((x1 + mx) * sx) // STRIP + 1) * STRIP)
        wy1 = min(full[1], (int((y1 + my) * sy) // STRIP + 1) * STRIP)
        return wx0, wy0, max(1, wx1 - wx0), max(1, wy1 - wy0)

    def _covers(self, win, page, i, full):
        if (win[2], win[3]) == full:
            return True
        sx, sy = full[0] / page.w, full[1] / page.h
        x0, y0, x1, y1 = self._visible(i)
        return (win[0] <= x0 * sx + 1 and win[1] <= y0 * sy + 1
                and win[0] + win[2] >= x1 * sx - 1 and win[1] + win[3] >= y1 * sy - 1)

    def _ensure(self, page, i):
        full = self._full_size(page)
        entry = self._cache.get(page)
        if entry is not None and entry.full == full and \
                self._covers((entry.ox, entry.oy, entry.pw, entry.ph), page, i, full):
            return entry
        if page.pdf is None:
            return self._build(page, full, self._window(page, i, full), None)
        self._request_bg(page, i, full, 0)
        fresh = self._cache.get(page)
        return fresh if fresh is not None else entry  # stale or missing until the worker delivers

    def _request_bg(self, page, i, full, priority):
        """Have a PDF page's background rendered, unless that is done or under way."""
        pending = self._pending.get(page)
        if pending is not None and pending[0] == full and (priority or self._covers(pending[1], page, i, full)):
            return
        win = self._window(page, i, full)
        key = (full, win)
        ready = self._bgs.get((page, key))
        if ready is not None:
            # Rendered before and still remembered: no waiting, no white page.
            self._bgs.move_to_end((page, key))
            self._pending.pop(page, None)
            self._build(page, full, win, ready)
            return
        self._pending[page] = key

        def valid():
            if self._pending.get(page) != key:
                return False
            if not self._near(page):
                # Scrolled far away while this waited in line: drop it, it can be asked for again.
                self._pending.pop(page, None)
                return False
            return True

        worker().submit(priority, valid, page, win[2], win[3], lambda surface: self._bg_ready(page, key, surface),
                        (win[0], win[1], full[0] / page.w, full[1] / page.h))

    def _near(self, page):
        i = self._index(page)
        return i is not None and self._shown[0] - 3 <= i <= self._shown[1] + 3

    def _prefetch(self, first, last):
        """Render the pages just outside the view ahead of time, so scrolling meets no blanks."""
        pages = self.doc.pages
        for step, i in enumerate((last + 1, first - 1, last + 2)):
            if 0 <= i < len(pages) and pages[i].pdf is not None:
                page = pages[i]
                full = self._full_size(page)
                entry = self._cache.get(page)
                if full[0] * full[1] <= MAX_PIXELS and (entry is None or entry.full != full):
                    self._request_bg(page, i, full, 1 + step)

    def _bg_ready(self, page, key, surface):
        self._bgs[(page, key)] = surface
        while len(self._bgs) > BG_KEEP:
            self._bgs.popitem(last=False)
        if self._pending.get(page) != key:
            return
        del self._pending[page]
        if self._index(page) is not None:
            self._build(page, key[0], key[1], surface)
            self.queue_draw()

    def _build(self, page, full, win, bg):
        entry = _Entry()
        entry.full = full
        entry.ox, entry.oy, entry.pw, entry.ph = win
        entry.sx, entry.sy = full[0] / page.w, full[1] / page.h
        entry.bg = bg
        entry.surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, win[2], win[3])
        entry.strips = [None] * ((win[3] + STRIP - 1) // STRIP)
        entry.dirty = set()
        self._cache[page] = entry
        self._repaint(entry, page, None)
        return entry

    def _repaint(self, entry, page, rect):
        """Redraw the cached raster inside rect (page points), or all of it."""
        sx, sy = entry.sx, entry.sy
        x0, y0, x1, y1 = 0, 0, entry.pw, entry.ph
        if rect is not None:
            # Whole device pixels, so no antialiased seam is left behind.
            x0, y0 = max(x0, math.floor(rect[0] * sx) - 2 - entry.ox), max(y0, math.floor(rect[1] * sy) - 2 - entry.oy)
            x1, y1 = min(x1, math.ceil(rect[2] * sx) + 2 - entry.ox), min(y1, math.ceil(rect[3] * sy) + 2 - entry.oy)
            if x1 <= x0 or y1 <= y0:
                return
        cr = cairo.Context(entry.surf)
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.clip()
        if entry.bg is not None:
            cr.set_source_surface(entry.bg, 0, 0)
            cr.paint()
        cr.translate(-entry.ox, -entry.oy)
        cr.scale(sx, sy)
        box = ((x0 + entry.ox) / sx, (y0 + entry.oy) / sy, (x1 + entry.ox) / sx, (y1 + entry.oy) / sy)
        if entry.bg is None:
            draw_paper(cr, page, box)
        hidden = self._hidden
        ink_on(page)
        for it in page.items:
            if hidden and id(it) in hidden:
                continue
            if intersects(it.bbox, box):
                it.draw(cr)
        entry.mark(y0, y1 - 1)

    def _strips(self, entry, first, last):
        """Textures for strips first..last, re-uploading only the ones that changed."""
        strips = entry.strips
        stale = [n for n in range(first, last + 1) if strips[n] is None or n in entry.dirty]
        if stale:
            entry.surf.flush()
            data = entry.surf.get_data()
            stride = entry.surf.get_stride()
            for n in stale:
                y = n * STRIP
                h = min(STRIP, entry.ph - y)
                strips[n] = Gdk.MemoryTexture.new(entry.pw, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                                  GLib.Bytes.new(bytes(data[y * stride:(y + h) * stride])), stride)
                entry.dirty.discard(n)
        return strips

    def _doc_event(self, kind, *args):
        if kind == 'added':
            page, items = args
            entry = self._cache.get(page)
            if entry is not None:
                cr = cairo.Context(entry.surf)
                cr.translate(-entry.ox, -entry.oy)
                cr.scale(entry.sx, entry.sy)
                ink_on(page)
                for it in items:
                    it.draw(cr)
                    entry.mark(it.bbox[1] * entry.sy - entry.oy - 2, it.bbox[3] * entry.sy - entry.oy + 2)
        elif kind == 'dirty':
            page, rect = args
            entry = self._cache.get(page)
            if entry is not None:
                self._repaint(entry, page, rect)
        elif kind == 'pages':
            alive = set(self.doc.pages)
            for page in [p for p in self._cache if p not in alive]:
                del self._cache[page]
            for page in [p for p in self._pending if p not in alive]:
                del self._pending[page]
            self._op = self._sel = self._hidden = None
            self._relayout()
            self._configure_adj()
            self.emit('view-changed')
        self.queue_draw()

    # -- drawing ------------------------------------------------------------

    def do_snapshot(self, snap):
        doc = self.doc
        width, height = self.get_width(), self.get_height()
        if doc is None or width <= 0 or not self._tops:
            return
        native = self.get_native()
        surface = native.get_surface() if native else None
        self._dev = surface.get_scale() if surface else float(self.get_scale_factor())

        board = self._board
        across = self._across()
        first = i = self._page_at(self._h(), self._v())
        outline = Gsk.RoundedRect()
        while i < len(doc.pages):
            x, y, w, h = self._page_rect(i)
            if (x > width if across else y > height) and not board:
                break
            page = doc.pages[i]
            if board:
                snap.append_color(rgba(*page.color), _rect(0, 0, width, height))   # it fills the view
            else:
                rect = _rect(x, y, w, h)
                snap.append_outset_shadow(outline.init_from_rect(rect, 0), _SHADOW, 0, 2, 0, 10)
                snap.append_color(_WHITE if page.pdf is not None else rgba(*page.color), rect)
            entry = self._ensure(page, i)
            if entry is not None:
                # Only the strips inside the viewport are uploaded and drawn.
                k = h / page.h / entry.sy
                top = y + entry.oy * k
                n0 = max(0, int((0 - top) / k) // STRIP)
                n1 = min(len(entry.strips) - 1, int((height - top) / k) // STRIP)
                if n1 >= n0:
                    strips = self._strips(entry, n0, n1)
                    ex, ew = x + entry.ox * w / page.w / entry.sx, entry.pw * w / page.w / entry.sx
                    for n in range(n0, n1 + 1):
                        sh = min(STRIP, entry.ph - n * STRIP)
                        snap.append_texture(strips[n], _rect(ex, top + n * STRIP * k, ew, sh * k))
            i += 1
            if board:
                break
        self._shown = (first, i - 1)
        if not board:
            self._prefetch(first, i - 1)
        if len(self._cache) > (i - first) + 5:
            keep = set(doc.pages[max(0, first - 2):i + 2])
            for page in [p for p in self._cache if p not in keep]:
                del self._cache[page]

        op = self._op
        if op is not None:
            draw = getattr(self, '_snap_' + op['kind'], None)
            idx = self._index(op['page'])
            if draw is not None and idx is not None:
                snap.push_clip(_rect(0, 0, width, height) if board else _rect(*self._page_rect(idx)))
                draw(snap, op, idx)
                snap.pop()
        if self._sel is not None:
            self._snap_selection(snap)
        if self._laser_trails or self._laser_dot:
            self._draw_laser(snap)
        if self.ruler is not None:
            self._draw_ruler(snap)
        if self._pull:
            self._draw_pull(snap, width, height)
        self._draw_bars(snap)
        for n, (page, box) in enumerate(self.marks):
            idx = self._index(page)
            if idx is None:
                continue
            px, py, pw, ph = self._page_rect(idx)
            k = pw / page.w
            rect = _rect(px + box[0] * k - 2, py + box[1] * k - 2, (box[2] - box[0]) * k + 4, (box[3] - box[1]) * k + 4)
            if rect.get_y() > height or rect.get_y() + rect.get_height() < 0:
                continue
            snap.append_color(_MARK_NOW if n == self.mark_at else _MARK, rect)

    def _raster(self, i, bbox, draw):
        """Rasterise draw(cr) for a page-space box; returns (texture, page rect)."""
        page = self.doc.pages[i]
        s = self._page_rect(i)[2] / page.w * self._dev
        vis = self._visible(i)
        x0, y0 = math.floor(max(bbox[0], vis[0]) * s), math.floor(max(bbox[1], vis[1]) * s)
        x1, y1 = math.ceil(min(bbox[2], vis[2]) * s), math.ceil(min(bbox[3], vis[3]) * s)
        if x1 <= x0 or y1 <= y0:
            return None
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, x1 - x0, y1 - y0)
        cr = cairo.Context(surface)
        cr.translate(-x0, -y0)
        cr.scale(s, s)
        ink_on(page)
        draw(cr)
        return texture_from_surface(surface), (x0 / s, y0 / s, (x1 - x0) / s, (y1 - y0) / s)

    def _put(self, snap, i, layer):
        if layer is None:
            return
        texture, (rx, ry, rw, rh) = layer
        px, py, pw, ph = self._page_rect(i)
        k = pw / self.doc.pages[i].w
        snap.append_texture(texture, _rect(px + rx * k, py + ry * k, rw * k, rh * k))

    def _page_path(self, i, pts, smooth=False, close=False):
        px, py, pw, ph = self._page_rect(i)
        k = pw / self.doc.pages[i].w
        b = Gsk.PathBuilder.new()
        b.move_to(px + pts[0][0] * k, py + pts[0][1] * k)
        n = len(pts)
        if smooth and n > 2:
            for j in range(1, n - 1):
                cx, cy = pts[j][0], pts[j][1]
                b.quad_to(px + cx * k, py + cy * k,
                          px + (cx + pts[j + 1][0]) / 2 * k, py + (cy + pts[j + 1][1]) / 2 * k)
            b.line_to(px + pts[-1][0] * k, py + pts[-1][1] * k)
        else:
            for p in pts[1:]:
                b.line_to(px + p[0] * k, py + p[1] * k)
        if close:
            b.close()
        return b.to_path(), k

    def _snap_stroke(self, snap, op, i):
        item = op['item']
        pts = item.pts
        if item.tool in ('hl', 'tape'):
            path, k = self._page_path(i, pts if len(pts) > 1 else pts * 2, smooth=True)
            stroke = Gsk.Stroke.new(max(item.width * k, 0.5))
            stroke.set_line_cap(Gsk.LineCap.ROUND)
            stroke.set_line_join(Gsk.LineJoin.ROUND)
            snap.append_stroke(path, stroke, rgba(*item.color, 0.4 if item.tool == 'hl' else 1.0))
            return
        for layer in op['chunks']:
            self._put(snap, i, layer)
        if op['tail_n'] != len(pts):
            tail = Stroke(item.tool, item.color, item.width, pts[max(0, op['baked'] - 1):], item.sens, item.dash)
            op['tail'] = self._raster(i, tail.bbox, tail.draw)
            op['tail_n'] = len(pts)
        self._put(snap, i, op['tail'])

    def _draw_laser(self, snap):
        color = self.laser_color
        snap.push_opacity(self._laser_alpha)
        for page, pts in self._laser_trails:
            i = self._index(page)
            if i is None:
                continue
            if len(pts) == 1:
                pts = pts + [(pts[0][0] + 0.01, pts[0][1])]
            path, k = self._page_path(i, pts, smooth=True)
            for width, paint in ((13.0, rgba(*color, 0.16)), (7.0, rgba(*color, 0.45)), (3.6, rgba(*color)),
                                 (1.3, rgba(1, 1, 1, 0.9))):
                stroke = Gsk.Stroke.new(width)
                stroke.set_line_cap(Gsk.LineCap.ROUND)
                stroke.set_line_join(Gsk.LineJoin.ROUND)
                snap.append_stroke(path, stroke, paint)
        if self._laser_dot is not None:
            page, x, y = self._laser_dot
            i = self._index(page)
            if i is not None:
                px, py, pw, ph = self._page_rect(i)
                k = pw / page.w
                cx, cy = px + x * k, py + y * k
                for radius, paint in ((15.0, rgba(*color, 0.14)), (10.0, rgba(*color, 0.3)), (6.0, rgba(*color)),
                                      (2.4, rgba(1, 1, 1, 0.9))):
                    b = Gsk.PathBuilder.new()
                    b.add_circle(Graphene.Point().init(cx, cy), radius)
                    snap.append_fill(b.to_path(), Gsk.FillRule.WINDING, paint)
        snap.pop()

    def _laser_reset(self):
        """Any laser activity keeps what is on screen and restarts the countdown."""
        if self._laser_timer:
            GLib.source_remove(self._laser_timer)
            self._laser_timer = 0
        if self._laser_fade is not None:
            self._laser_fade.pause()
            self._laser_fade = None
        self._laser_alpha = 1.0

    def _laser_expire(self):
        self._laser_timer = 0

        def step(value):
            self._laser_alpha = value
            self.queue_draw()

        def done(*_):
            self._laser_fade = None
            self._laser_trails = []
            self._laser_alpha = 1.0
            self.queue_draw()

        self._laser_fade = Adw.TimedAnimation.new(self, 1.0, 0.0, LASER_FADE_MS, Adw.CallbackAnimationTarget.new(step))
        self._laser_fade.connect('done', done)
        self._laser_fade.play()
        return False

    def clear_laser(self):
        self._laser_reset()
        self._laser_trails, self._laser_dot = [], None
        self.queue_draw()

    def _snap_shape(self, snap, op, i):
        item = op['item']
        self._put(snap, i, self._raster(i, item.bbox, item.draw))

    def _snap_erase(self, snap, op, i):
        x, y = op['pos']
        r = self.eraser_r * self._page_rect(i)[2] / op['page'].w
        b = Gsk.PathBuilder.new()
        b.add_circle(Graphene.Point().init(x, y), max(r, 2))
        path = b.to_path()
        snap.append_fill(path, Gsk.FillRule.WINDING, _ERASER_FILL)
        snap.append_stroke(path, Gsk.Stroke.new(1), _ERASER_RING)

    def _snap_lasso(self, snap, op, i):
        pts = op['pts']
        if len(pts) > 1:
            if op['rect']:
                (ax, ay), (bx, by) = pts[0], pts[1]
                pts = [(ax, ay), (bx, ay), (bx, by), (ax, by)]
            path, _ = self._page_path(i, pts, close=op['rect'])
            stroke = Gsk.Stroke.new(1.5)
            stroke.set_dash([5, 4])
            snap.append_stroke(path, stroke, _ACCENT)

    def _snap_move(self, snap, op, i):
        layer = op['layer']
        if layer is None:
            return
        texture, (rx, ry, rw, rh) = layer
        s, (ox, oy) = op['s'], op['origin']
        self._put(snap, i, (texture, (ox + (rx - ox) * s + op['dx'], oy + (ry - oy) * s + op['dy'], rw * s, rh * s)))

    def _sel_box(self):
        """Selection box in page points, following an in-progress move or scale."""
        x0, y0, x1, y1 = self._sel['bbox']
        op = self._op
        if op is not None and op['kind'] == 'move':
            s, (ox, oy), dx, dy = op['s'], op['origin'], op['dx'], op['dy']
            return ox + (x0 - ox) * s + dx, oy + (y0 - oy) * s + dy, ox + (x1 - ox) * s + dx, oy + (y1 - oy) * s + dy
        return x0, y0, x1, y1

    def _snap_selection(self, snap):
        i = self._index(self._sel['page'])
        if i is None:
            return
        x0, y0, x1, y1 = self._sel_box()
        px, py, pw, ph = self._page_rect(i)
        k = pw / self._sel['page'].w
        box = _rect(px + x0 * k - 4, py + y0 * k - 4, (x1 - x0) * k + 8, (y1 - y0) * k + 8)
        b = Gsk.PathBuilder.new()
        b.add_rect(box)
        stroke = Gsk.Stroke.new(1.5)
        stroke.set_dash([5, 4])
        snap.append_stroke(b.to_path(), stroke, _ACCENT)
        b = Gsk.PathBuilder.new()
        b.add_circle(Graphene.Point().init(px + x1 * k + 4, py + y1 * k + 4), 6)
        handle = b.to_path()
        snap.append_fill(handle, Gsk.FillRule.WINDING, _WHITE)
        snap.append_stroke(handle, Gsk.Stroke.new(2), _ACCENT)

    # -- input devices ------------------------------------------------------

    @staticmethod
    def _pressure(gesture):
        ok, p = gesture.get_axis(Gdk.AxisUse.PRESSURE)
        return max(0.02, min(1.0, p)) if ok else 0.5

    def _stylus_down(self, gesture, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._raw_pen == 'down':
            return
        tool = gesture.get_device_tool()
        erase = tool is not None and tool.get_tool_type() == Gdk.DeviceToolType.ERASER
        # Holding the barrel button while writing also erases.
        buttons = Gdk.ModifierType.BUTTON2_MASK | Gdk.ModifierType.BUTTON3_MASK
        if gesture.get_current_event_state() & buttons:
            erase = True
        self._pen_down = True
        self._src = 'pen'
        self._down(x, y, self._pressure(gesture), erase)

    def _stylus_motion(self, gesture, x, y):
        if not self._pen_down or self._raw_pen == 'down':
            return
        p = self._pressure(gesture)
        if self._op is not None and self._op['kind'] == 'stroke':
            # GTK compresses motion to one event per frame; the backlog holds
            # the samples in between, which keeps fast handwriting smooth.
            try:
                ok, backlog = gesture.get_backlog()
                if ok and len(backlog) > 1:
                    ax, ay, ap = int(Gdk.AxisUse.X), int(Gdk.AxisUse.Y), int(Gdk.AxisUse.PRESSURE)
                    last = backlog[-1].axes
                    if abs(last[ax] - x) < 2 and abs(last[ay] - y) < 2:
                        for tc in backlog[:-1]:
                            bp = tc.axes[ap] if tc.flags & Gdk.AxisFlags.PRESSURE else p
                            self._move(tc.axes[ax], tc.axes[ay], max(0.02, min(1.0, bp)))
            except Exception:
                pass
        self._move(x, y, p)

    def _stylus_up(self, gesture, x, y):
        if self._pen_down and self._raw_pen != 'down':
            self._pen_down = False
            self._up(x, y)

    def _drag_begin(self, gesture, x, y):
        event = gesture.get_last_event(gesture.get_current_sequence())
        device = gesture.get_device()
        touch = device is not None and device.get_source() == Gdk.InputSource.TOUCHSCREEN
        # Pens are handled by the stylus gesture; fingers scroll unless enabled.
        if (event is not None and event.get_device_tool() is not None) or (touch and not self.finger_draw):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._bar_drag = None if touch else self._bar_at(x, y)
        if self._bar_drag is not None:
            self.queue_draw()
            return
        if self.mouse_hand and self._has_pen and not touch and self._ruler_zone(x, y) is None:
            # With a pen in use the mouse is a hand: it moves the page, and a click lifts tape.
            self._hand = (self._h(), self._v(), x, y, False)
            return
        self._mouse_down = True
        self._mouse_p = 0.45
        self._mouse_last = (x, y)
        self._src = 'mouse'
        self._down(x, y, self._mouse_p, False)

    def _drag_update(self, gesture, dx, dy):
        if self._bar_drag is not None:
            axis, lo, hi, per_pixel, value = self._bar_drag
            adj = self._vadj if axis == 'v' else self._hadj
            adj.set_value(max(lo, min(hi, value + (dy if axis == 'v' else dx) * per_pixel)))
            return
        if self._hand is not None:
            h, v, x, y, moved = self._hand
            if moved or math.hypot(dx, dy) > 4:
                self._hand = (h, v, x, y, True)
                self._scroll_by(h - dx - self._h(), v - dy - self._v())
            return
        if not self._mouse_down:
            return
        ok, sx, sy = gesture.get_start_point()
        x, y = sx + dx, sy + dy
        # No pressure axis: thin the line with speed for a calligraphic feel.
        speed = math.hypot(x - self._mouse_last[0], y - self._mouse_last[1])
        self._mouse_p = self._mouse_p * 0.75 + max(0.15, min(0.85, 0.95 - speed / 45)) * 0.25
        self._mouse_last = (x, y)
        self._move(x, y, self._mouse_p)

    def _drag_end(self, gesture, dx, dy):
        if self._bar_drag is not None:
            self._bar_drag = None
            self.queue_draw()
            return
        if self._hand is not None:
            hand, self._hand = self._hand, None
            if not hand[4]:
                self._toggle_tape(hand[2], hand[3])
            else:
                self._rest((self.zoom, hand[0], hand[1]))
            return
        if self._mouse_down:
            self._mouse_down = False
            ok, sx, sy = gesture.get_start_point()
            self._up(sx + dx, sy + dy)

    def _key(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self._sel:
            self.delete_selection()
            return True
        if keyval == Gdk.KEY_Escape:
            self._cancel()
            self.clear_selection()
            return True
        return False

    # -- tools --------------------------------------------------------------

    def _down(self, x, y, pressure, erase):
        self.grab_focus()
        self._op = None
        tool = 'eraser' if erase else self.tool
        rule = None
        zone = self._ruler_zone(x, y) if tool != 'eraser' else None
        if self._src != 'pen' and tool not in ('eraser', 'lasso') and zone is None and self._toggle_tape(x, y):
            return   # a finger or a click on a tape lifts it or puts it back
        if zone == 'body' and self._src != 'pen':
            # A finger or the mouse on the ruler moves it; only the pen draws across it.
            self._op = {'kind': 'ruler', 'page': None, 'grab': (x - self.ruler['x'], y - self.ruler['y'])}
            return
        if zone and tool in ('pen', 'hl', 'tape'):
            # A stroke begun at the ruler runs along its edge.
            rule = self._ruler_edge(x, y)
            x, y = self._onto(rule, x, y)
        if tool == 'lasso':
            if self._src != 'pen':
                self._press(x, y)   # the pen holds still too often while writing for this to be meant
            if self._sel is not None and self._grab_selection(x, y):
                return
        self.clear_selection()
        loc = self._locate(x, y)
        if loc is None:
            return
        i, px, py = loc
        page = self.doc.pages[i]
        if tool in ('pen', 'hl', 'tape'):
            if tool == 'tape':
                item = Stroke('tape', self.tape_color, self.tape_width, [(px, py, 0.5)])
            elif tool == 'pen':
                item = Stroke(self.pen_type, self.pen_color, self.pen_width, [(px, py, pressure)], self.pen_sens,
                              self.pen_dash)
            else:
                item = Stroke('hl', self.hl_color, self.hl_width, [(px, py, 0.5)])
            self._op = {'kind': 'stroke', 'page': page, 'i': i, 'item': item, 'smooth': (px, py), 'rule': rule,
                        'anchor': (x, y), 'chunks': [], 'baked': 0, 'tail': None, 'tail_n': 0}
        elif tool == 'eraser':
            self._op = {'kind': 'erase', 'page': page, 'i': i, 'pos': (x, y), 'last': (px, py),
                        'before': list(page.items), 'changed': False}
            self._erase(self._op, px, py)
        elif tool == 'shape':
            item = Shape(self.shape_kind, px, py, px, py, self.pen_color, self.pen_width)
            self._op = {'kind': 'shape', 'page': page, 'i': i, 'item': item}
        elif tool == 'lasso':
            self._op = {'kind': 'lasso', 'page': page, 'i': i, 'pts': [(px, py)], 'rect': self.lasso_rect}
        elif tool in ('text', 'note'):
            self._edit_text(i, page, x, y, px, py, tool == 'note')
        elif tool == 'laser':
            self._laser_reset()
            self._op = {'kind': 'laser', 'page': page, 'i': i, 'trail': self.laser_mode == 'trail'}
            if self._op['trail']:
                self._laser_trails.append((page, [(px, py)]))
            else:
                self._laser_dot = (page, px, py)
        self.queue_draw()

    def _move(self, x, y, pressure):
        op = self._op
        if op is None:
            return
        kind = op['kind']
        if self._press_timer and math.hypot(x - self._press_at[0], y - self._press_at[1]) > 10:
            self._stop_press()
        if kind == 'move':
            i = self._index(op['page'])
            if i is None:
                return
            if not op['live']:
                # A finger does not hold still: the selection stays put until it clearly moves.
                if math.hypot(x - op['at'][0], y - op['at'][1]) < op['slack']:
                    return
                op['live'] = True
            px, py = self._to_page(i, x, y)
            if op['scaling']:
                ox, oy = op['origin']
                sx, sy = op['start']
                op['s'] = max(0.1, math.hypot(px - ox, py - oy) / max(1.0, math.hypot(sx - ox, sy - oy)))
            else:
                op['dx'], op['dy'] = px - op['start'][0], py - op['start'][1]
            self.queue_draw()
            return
        if kind == 'ruler':
            self.ruler['x'], self.ruler['y'] = x - op['grab'][0], y - op['grab'][1]
            self.queue_draw()
            return
        if op.get('fixed'):
            return
        rule = op.get('rule')
        if rule:
            x, y = self._onto(rule, x, y)
        px, py = self._to_page(op['i'], x, y)
        if kind == 'stroke':
            item = op['item']
            pts = item.pts
            if self.hold_shape and not rule:
                ax, ay = op['anchor']
                if not self._hold_timer or (x - ax) ** 2 + (y - ay) ** 2 > 25:
                    op['anchor'] = (x, y)
                    if self._hold_timer:
                        GLib.source_remove(self._hold_timer)
                    self._hold_timer = GLib.timeout_add(600, self._held)
            if self.pen_stab > 0 and item.tool not in ('hl', 'tape') and not rule:
                # Stabiliser: the ink follows the pen through a low-pass filter.
                k = 1.0 - 0.9 * self.pen_stab
                sx, sy = op['smooth']
                px, py = sx + (px - sx) * k, sy + (py - sy) * k
                op['smooth'] = (px, py)
            lx, ly = pts[-1][0], pts[-1][1]
            if (px - lx) ** 2 + (py - ly) ** 2 < (0.4 / self.zoom) ** 2:
                return
            pts.append((px, py, pressure))
            if item.tool not in ('hl', 'tape') and not item.dash and len(pts) - op['baked'] >= CHUNK:
                # Freeze what is drawn so far; only the tail is redrawn per frame.
                part = Stroke(item.tool, item.color, item.width, pts[max(0, op['baked'] - 1):], item.sens)
                layer = self._raster(op['i'], part.bbox, part.draw)
                if layer is not None:
                    op['chunks'].append(layer)
                op['baked'] = len(pts) - 1
        elif kind == 'erase':
            op['pos'] = (x, y)
            self._erase(op, px, py)
        elif kind == 'laser':
            if op['trail']:
                self._laser_trails[-1][1].append((px, py))
            else:
                self._laser_dot = (op['page'], px, py)
        elif kind == 'shape':
            item = op['item']
            item.x1, item.y1 = px, py
            item.update_bbox()
        elif kind == 'lasso':
            if op['rect']:
                op['pts'][1:] = [(px, py)]
            else:
                op['pts'].append((px, py))
        self.queue_draw()

    def _toggle_tape(self, x, y):
        """Lift the tape under a point, or put it back; True if there was one."""
        loc = self._locate(x, y)
        if loc is None:
            return False
        i, px, py = loc
        page = self.doc.pages[i]
        for it in reversed(page.items):
            if it.kind == 's' and it.tool == 'tape' and intersects(it.bbox, (px, py, px, py)) \
                    and it.hit(px, py, 2 / self.zoom):
                it.open = not it.open
                page.rev += 1
                self.doc.touch()
                self._repaint_page(page, it.bbox)
                self.queue_draw()
                return True
        return False

    def _held(self):
        """The pen rested at the end of a stroke: snap the stroke to a clean shape."""
        self._hold_timer = 0
        op = self._op
        if op is None or op['kind'] != 'stroke' or len(op['item'].pts) < 6:
            return False
        item = op['item']
        found = geom.recognize_shape(item.pts)
        if found is None:
            return False
        if item.tool in ('hl', 'tape'):
            if found[0] != 'line':
                return False
            item.pts[:] = [(found[1], found[2], 0.5), (found[3], found[4], 0.5)]
            item.update_bbox()
        else:
            op['kind'] = 'shape'
            op['item'] = Shape(found[0], *found[1:], item.color, item.width)
        op['fixed'] = True
        self.queue_draw()
        return False

    def _stop_hold(self):
        if self._hold_timer:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = 0

    def _press(self, x, y):
        """The select tool came down: if it stays where it is, a menu is wanted there."""
        self._stop_press()
        self._press_at = (x, y)
        self._press_timer = GLib.timeout_add(550, self._pressed)

    def _stop_press(self):
        if self._press_timer:
            GLib.source_remove(self._press_timer)
            self._press_timer = 0

    def _pressed(self):
        self._press_timer = 0
        # Whatever the press had begun (a lasso, a move of the selection) is given up, and what
        # is left of the press does nothing more.
        self._cancel()
        if self._tg is not None:
            self._tg.update(grab='held', draw=False, moved=False)
        self.emit('menu-wanted', *self._press_at)
        return False

    def _pick_at(self, x, y):
        """Select the topmost picture, shape or text box at a point of the widget, or nothing
        if none is there."""
        loc = self._locate(x, y)
        picked = []
        if loc is not None:
            i, px, py = loc
            page = self.doc.pages[i]
            r = 10 / max(1e-6, self._z(page))
            for it in reversed(page.items):
                # Not handwriting: a tap among the lines of a page would always catch some of it.
                if it.kind == 's' or self._lasso_kind(it) not in self.lasso_filter or not intersects(it.bbox, (px - r, py - r, px + r, py + r)):
                    continue
                # Pictures and text are picked anywhere inside; ink and shapes on their lines.
                if it.kind in ('i', 't') or it.hit(px, py, r):
                    picked = [it]
                    break
        if picked:
            self.select(page, picked)
        else:
            self.clear_selection()
        return bool(picked)

    def _up(self, x, y):
        op, self._op = self._op, None
        self._stop_hold()
        self._stop_press()
        if op is None:
            return
        kind, page = op['kind'], op['page']
        fixed = op.get('fixed')
        if kind == 'stroke':
            item = op['item']
            if self.pen_stab > 0 and item.tool not in ('hl', 'tape') and not fixed and not op.get('rule'):
                # Catch the lagging stabilised line up with where the pen lifted.
                px, py = self._to_page(op['i'], x, y)
                item.pts.append((px, py, item.pts[-1][2]))
            item.update_bbox()
            if item.tool not in ('hl', 'tape') and not fixed and not op.get('rule') and self.scribble_erase \
                    and geom.is_scribble(item.pts):
                targets = geom.scribble_targets(item.pts, page.items)
                if targets:
                    # Scribble to erase, recorded as two steps: the scribble as ordinary ink, then
                    # the erasure. One undo brings everything back with the scribble still there
                    # (in case it was not meant as one), a second undo removes the scribble too.
                    self.doc.add_items(page, [item])
                    self.doc.remove_items(page, targets + [item])
                    if self.on_scribble:
                        self.on_scribble(len(targets))
                    return
            self.doc.add_items(page, [item])
        elif kind == 'erase':
            if op['changed']:
                self.doc.push_diff(page, op['before'])
                if self.on_erase_done:
                    self.on_erase_done()
        elif kind == 'laser':
            self._laser_dot = None
            if self._laser_trails:
                self._laser_timer = GLib.timeout_add(LASER_HOLD_MS, self._laser_expire)
        elif kind == 'shape':
            item = op['item']
            if math.hypot(item.x1 - item.x0, item.y1 - item.y0) > 2:
                self.doc.add_items(page, [item])
        elif kind == 'lasso':
            pts = op['pts']
            box = geom.bbox_of(pts)
            allowed = self.lasso_filter
            items = [it for it in page.items if self._lasso_kind(it) in allowed]
            if max(box[2] - box[0], box[3] - box[1]) * self._z(page) < 8:
                self._pick_at(x, y)   # a tap selects the topmost item under it
                self.queue_draw()
                return
            elif op['rect']:
                picked = geom.rect_pick(box, items)
            else:
                picked = geom.lasso_pick(pts, items)
            self.select(page, picked)
        elif kind == 'move':
            self._hidden = None
            olds = self._sel['items']
            s, (ox, oy), dx, dy = op['s'], op['origin'], op['dx'], op['dy']
            if abs(s - 1) > 1e-3 or math.hypot(dx, dy) * self.zoom > 1:
                news = [it.transformed(s, ox, oy, dx, dy) for it in olds]
                if op['copy']:
                    self.doc.add_items(page, news)   # the originals stay where they were
                else:
                    self.doc.replace_items(page, olds, news)
                self.select(page, news)
            else:
                self._repaint_page(page, self._sel['bbox'])
            self._op = None
            self.emit('selection-changed')
        self.queue_draw()

    def _cancel(self):
        op, self._op = self._op, None
        self._pen_down = self._mouse_down = False
        self._stop_hold()
        self._stop_press()
        if op is None:
            return
        if op['kind'] == 'laser':
            self._laser_dot = None
            if self._laser_trails:
                self._laser_trails.pop()
        elif op['kind'] == 'erase' and op['changed']:
            self.doc.push_diff(op['page'], op['before'])
        elif op['kind'] == 'move':
            self._hidden = None
            self._repaint_page(op['page'], self._sel['bbox'])
            self.emit('selection-changed')
        self.queue_draw()

    def _repaint_page(self, page, rect):
        entry = self._cache.get(page)
        if entry is not None:
            self._repaint(entry, page, rect)

    @staticmethod
    def _lasso_kind(it):
        return {'s': 'ink', 'h': 'shape', 'i': 'image', 't': 'text'}[it.kind]

    @staticmethod
    def _erase_kind(it):
        if it.kind == 's':
            return 'hl' if it.tool == 'hl' else 'pen'
        return 'shape' if it.kind == 'h' else None

    def _erase(self, op, px, py):
        page = op['page']
        r = self.eraser_r
        lx, ly = op['last']
        op['last'] = (px, py)
        reach = (min(lx, px) - r, min(ly, py) - r, max(lx, px) + r, max(ly, py) + r)
        whole = self.eraser_mode == 'stroke'
        allowed = self.erase_filter
        out = []
        rect = None
        for it in page.items:
            if not intersects(it.bbox, reach) or self._erase_kind(it) not in allowed:
                out.append(it)
                continue
            if whole or (it.kind == 'h' and it.shape == 'arrow'):
                steps = max(1, int(math.hypot(px - lx, py - ly) / (r * 0.7)))
                if any(it.hit(lx + (px - lx) * k / steps, ly + (py - ly) * k / steps, r)
                       for k in range(steps + 1)):
                    rect = union(rect, it.bbox)
                else:
                    out.append(it)
                continue
            # Standard eraser: cut the touched part out and keep the rest as shorter strokes.
            pts = it.pts if it.kind == 's' else [(x, y, 0.5) for x, y in it.polyline()]
            cut = geom.erase_runs(pts, lx, ly, px, py, r + it.width / 2)
            if cut is None:
                out.append(it)
                continue
            runs, box = cut
            if it.kind == 's' and not it.dash:
                pad = it.pad() + 2
                rect = union(rect, (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
                tool, sens, dash = it.tool, it.sens, 0
            else:
                rect = union(rect, it.bbox)  # a shape or dashed line is redrawn as a whole
                tool, sens, dash = (it.tool, it.sens, it.dash) if it.kind == 's' else ('ball', 1.0, 0)
            out += [Stroke(tool, it.color, it.width, run, sens, dash) for run in runs if len(run) > 1]
        if rect is None:
            return
        page.items = out
        op['changed'] = True
        self._repaint_page(page, rect)

    # -- selection ----------------------------------------------------------

    def select(self, page, items):
        if not items:
            self._sel = None
        else:
            box = None
            for it in items:
                box = union(box, it.bbox)
            self._sel = {'page': page, 'items': list(items), 'bbox': box}
            self.emit('selection-made')
        self.emit('selection-changed')
        self.queue_draw()

    def clear_selection(self):
        if self._sel is not None:
            self._sel = None
            self.emit('selection-changed')
            self.queue_draw()

    def selection_rect(self):
        """The selection box in widget coordinates, or None (also while it is being dragged)."""
        sel = self._sel
        if sel is None or (self._op is not None and self._op['kind'] == 'move'):
            return None
        i = self._index(sel['page'])
        if i is None:
            return None
        px, py, pw, ph = self._page_rect(i)
        k = pw / sel['page'].w
        x0, y0, x1, y1 = sel['bbox']
        return px + x0 * k, py + y0 * k, (x1 - x0) * k, (y1 - y0) * k

    def _grab_selection(self, x, y, reach=7):
        """Take hold of the selection at a point of the widget: inside it to move it, on its
        edge (within reach pixels) or its handle to resize it. False if the point is elsewhere."""
        sel = self._sel
        i = self._index(sel['page'])
        if i is None:
            return False
        px, py = self._to_page(i, x, y)
        x0, y0, x1, y1 = sel['bbox']
        k = self._page_rect(i)[2] / sel['page'].w
        # The box as drawn is 4 pixels larger all round.
        bx0, by0, bx1, by1 = x0 - 4 / k, y0 - 4 / k, x1 + 4 / k, y1 + 4 / k
        r = reach / k
        origin = (x0, y0)
        scaling = math.hypot((px - x1) * k - 4, (py - y1) * k - 4) <= 13
        if not scaling:
            if not (bx0 - r <= px <= bx1 + r and by0 - r <= py <= by1 + r):
                return False
            # The edge is a band about the outline; a small selection keeps a middle to move it by.
            inner = min(r, (bx1 - bx0) / 4, (by1 - by0) / 4)
            if not (bx0 + inner < px < bx1 - inner and by0 + inner < py < by1 - inner):
                # It grows and shrinks about the point opposite the one taken hold of.
                scaling = True
                origin = (x0 + x1 - max(x0, min(x1, px)), y0 + y1 - max(y0, min(y1, py)))
        items = sel['items']
        # Lift the items out of the page raster and carry them as one texture.
        layer = self._raster(i, sel['bbox'], lambda cr: [it.draw(cr) for it in items])
        self._hidden = {id(it) for it in items}
        self._repaint_page(sel['page'], sel['bbox'])
        self._op = {'kind': 'move', 'page': sel['page'], 'start': (px, py), 'origin': origin, 'copy': False,
                    'scaling': scaling, 's': 1.0, 'dx': 0.0, 'dy': 0.0, 'layer': layer,
                    'at': (x, y), 'slack': 8 if reach > 7 else 0, 'live': False}
        self.emit('selection-changed')
        self.queue_draw()
        return True

    def _copy_selection_from(self, x, y):
        """Start dragging a copy of the selection from a point of the widget."""
        sel = self._sel
        i = self._index(sel['page']) if sel else None
        if i is None:
            return False
        items = sel['items']
        layer = self._raster(i, sel['bbox'], lambda cr: [it.draw(cr) for it in items])
        self._op = {'kind': 'move', 'page': sel['page'], 'start': self._to_page(i, x, y), 'origin': sel['bbox'][:2],
                    'copy': True, 'scaling': False, 's': 1.0, 'dx': 0.0, 'dy': 0.0, 'layer': layer,
                    'at': (x, y), 'slack': 8, 'live': False}
        self.emit('selection-changed')
        self.queue_draw()
        return True

    def delete_selection(self):
        sel, self._sel = self._sel, None
        if sel:
            self.doc.remove_items(sel['page'], sel['items'])
            self.emit('selection-changed')

    def duplicate_selection(self):
        sel = self._sel
        if sel:
            items = [it.transformed(1, 0, 0, 14, 14) for it in sel['items']]
            self.doc.add_items(sel['page'], items)
            self.select(sel['page'], items)

    def recolor_selection(self, color):
        """Give the selected ink, shapes and text a colour; pictures are left as they are."""
        sel = self._sel
        if not sel:
            return
        news = []
        for it in sel['items']:
            new = it.transformed(1, 0, 0, 0, 0)
            if hasattr(new, 'color'):
                new.color = tuple(color)
            news.append(new)
        self.doc.replace_items(sel['page'], sel['items'], news)
        self.select(sel['page'], news)

    def rotate_selection(self, k):
        """Turn the selection k quarter turns clockwise about its own centre (text stays upright)."""
        sel = self._sel
        if not sel:
            return
        page = sel['page']
        x0, y0, x1, y1 = sel['bbox']
        turned = [it.rotated(k % 4, page.w, page.h) for it in sel['items']]
        box = None
        for it in turned:
            box = union(box, it.bbox)
        dx, dy = (x0 + x1 - box[0] - box[2]) / 2, (y0 + y1 - box[1] - box[3]) / 2
        news = [it.transformed(1, 0, 0, dx, dy) for it in turned]
        self.doc.replace_items(page, sel['items'], news)
        self.select(page, news)

    def restack_selection(self, front):
        """Move the selection above (or below) everything else on its page."""
        sel = self._sel
        if not sel:
            return
        page = sel['page']
        chosen = {id(it) for it in sel['items']}
        ordered = [(i, it) for i, it in enumerate(page.items) if id(it) in chosen]
        base = len(page.items) - len(ordered) if front else 0
        self.doc.do(('items', page, ordered, [(base + j, it) for j, (_, it) in enumerate(ordered)]))
        self.queue_draw()

    def selection_image(self, scale=2.0):
        """The selected area of the page, paper included, as a cairo surface."""
        sel = self._sel
        if not sel:
            return None
        page = sel['page']
        x0, y0, x1, y1 = sel['bbox']
        x0, y0, x1, y1 = max(0, x0 - 6), max(0, y0 - 6), min(page.w, x1 + 6), min(page.h, y1 + 6)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, round((x1 - x0) * scale)),
                                     max(1, round((y1 - y0) * scale)))
        cr = cairo.Context(surface)
        cr.scale(scale, scale)
        cr.translate(-x0, -y0)
        render_page(cr, page)
        return surface

    def copy_selection(self):
        if self._sel:
            self._clip = [it.transformed(1, 0, 0, 0, 0) for it in self._sel['items']]
        return bool(self._sel)

    def paste(self, at=None):
        """Put the copied items on the page: a little beside where they were, or centred on a
        point of the widget."""
        if not self._clip:
            return False
        loc = self._locate(*at) if at else None
        if loc is not None:
            page = self.doc.pages[loc[0]]
            box = None
            for it in self._clip:
                box = union(box, it.bbox)
            items = [it.transformed(1, 0, 0, loc[1] - (box[0] + box[2]) / 2, loc[2] - (box[1] + box[3]) / 2)
                     for it in self._clip]
            self.doc.add_items(page, items)
            self.select(page, items)
            return True
        page = self.doc.pages[self.current_page_index()]
        items = [it.transformed(1, 0, 0, 14, 14) for it in self._clip]
        self._clip = items
        self.doc.add_items(page, items)
        self.select(page, items)
        return True

    def insert_image(self, png, width, height, at=None):
        """Place an image in the middle of the visible part of the current page, or centred on
        a point of the widget."""
        loc = self._locate(*at) if at else None
        i = loc[0] if loc else self.current_page_index()
        page = self.doc.pages[i]
        k = min(1.0, page.w * 0.6 / width, page.h * 0.6 / height)
        w, h = width * k, height * k
        cx, cy = loc[1:] if loc else self._to_page(i, self.get_width() / 2, self.get_height() / 2)
        x, y = cx - w / 2, cy - h / 2
        if not page.board:
            x, y = max(0, min(page.w - w, x)), max(0, min(page.h - h, y))
        item = Image(x, y, w, h, png)
        self.doc.add_items(page, [item])
        self.select(page, [item])

    # -- text ---------------------------------------------------------------

    def _edit_text(self, i, page, x, y, px, py, note=False):
        existing = next((it for it in reversed(page.items)
                         if it.kind == 't' and intersects(it.bbox, (px, py, px, py))), None)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                            left_margin=8, right_margin=8)
        if existing is not None:
            view.get_buffer().set_text(existing.text)
        scroller = Gtk.ScrolledWindow(child=view, min_content_width=280, min_content_height=96)
        scroller.add_css_class('card')
        done = Gtk.Button(label=tr('完成'), halign=Gtk.Align.END)
        done.add_css_class('suggested-action')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(scroller)
        box.append(done)
        popover = Gtk.Popover(child=box)
        popover.set_parent(self)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        color, size = self.pen_color, self.text_size

        def closed(*_):
            buf = view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            if existing is not None:
                if not text:
                    self.doc.remove_items(page, [existing])
                elif text != existing.text:
                    new = Text(existing.x, existing.y, text, existing.size, existing.color, existing.wrap, existing.bg)
                    self.doc.replace_items(page, [existing], [new])
            elif text and note:
                self.doc.add_items(page, [Text(px, py, text, 13.0, (0.16, 0.15, 0.12), 170.0, self.note_color)])
            elif text:
                self.doc.add_items(page, [Text(px, py, text, size, color, 400.0 if page.board else max(60.0, page.w - px - 12))])
            self._popover = None
            GLib.idle_add(popover.unparent)

        popover.connect('closed', closed)
        done.connect('clicked', lambda *_: popover.popdown())
        self._popover = popover
        popover.popup()
        view.grab_focus()
