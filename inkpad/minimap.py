"""The whiteboard navigator: a small map of where the board is written on, with the
part on screen marked, the zoom level, and a button that brings all of it into view."""
import cairo
from gi.repository import GLib, Graphene, Gsk, Gtk

from .i18n import tr
from . import icons
from .geom import union
from .board_performance import BoardOverview
from .icons import Icon
from .render import rgba, texture_from_surface

MAP_W, MAP_H = 216, 150
_VIEW_FILL = rgba(0.21, 0.52, 0.89, 0.22)
_VIEW_EDGE = rgba(0.21, 0.52, 0.89, 0.9)


class _Map(Gtk.Widget):
    def __init__(self, canvas):
        super().__init__(width_request=MAP_W, height_request=MAP_H, overflow=Gtk.Overflow.HIDDEN)
        self.canvas = canvas
        self.overview = BoardOverview()
        self._ink_key = self._ink_layer = None
        self._fit = None       # (region x0, region y0, scale, offset x, offset y) of the last frame
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', lambda g, x, y: self._go(x, y))
        drag.connect('drag-update', lambda g, dx, dy: self._go(g.get_start_point()[1] + dx, g.get_start_point()[2] + dy))
        self.add_controller(drag)

    def render(self):
        """Work out which parts of the board are written on; called a moment after it changes.

        The map does not draw the ink itself, which would be illegible at this size and
        invisible against a dark panel: it marks the occupied areas with small blocks.
        """
        canvas = self.canvas
        if canvas.doc is None:
            return
        page = canvas.doc.pages[canvas.current_page_index()]
        self.overview.refresh(page)
        self._ink_key = None
        self.queue_draw()

    def do_snapshot(self, snap):
        canvas = self.canvas
        view = canvas.view_box()
        if view is None:
            return
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        # The map covers the content and wherever the view currently is.
        rx0, ry0, rx1, ry1 = union(self.overview.box, view) if self.overview.box else view
        pad = 0.04 * max(rx1 - rx0, ry1 - ry0)
        rx0, ry0, rx1, ry1 = rx0 - pad, ry0 - pad, rx1 + pad, ry1 + pad
        s = min(w / (rx1 - rx0), h / (ry1 - ry0))
        ox, oy = (w - (rx1 - rx0) * s) / 2, (h - (ry1 - ry0) * s) / 2
        self._fit = (rx0, ry0, s, ox, oy)

        if self.overview.cells:
            # In the panel's own text colour, so the blocks show in light and dark themes alike.
            ink = self.get_color()
            # The ink layer changes independently of the viewport indicator.
            dev = self.get_scale_factor()
            key = (self.overview, w, h, dev, self._fit, ink.red, ink.green, ink.blue)
            if self._ink_key != key:
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * dev, h * dev)
                cr = cairo.Context(surface)
                cr.scale(dev, dev)
                cr.set_source_rgba(ink.red, ink.green, ink.blue, 0.55)
                bx, by = self.overview.origin
                cell = self.overview.cell
                side = max(cell * s + 0.6, 2.0)
                for cx, cy in self.overview.cells:
                    cr.rectangle(ox + (bx + cx * cell - rx0) * s,
                                 oy + (by + cy * cell - ry0) * s, side, side)
                    cr.fill()
                self._ink_layer = texture_from_surface(surface)
                self._ink_key = key
            snap.append_texture(self._ink_layer, Graphene.Rect().init(0, 0, w, h))
        rect = Graphene.Rect().init(ox + (view[0] - rx0) * s, oy + (view[1] - ry0) * s,
                                    (view[2] - view[0]) * s, (view[3] - view[1]) * s)
        snap.append_color(_VIEW_FILL, rect)
        b = Gsk.PathBuilder.new()
        b.add_rect(rect)
        snap.append_stroke(b.to_path(), Gsk.Stroke.new(1.2), _VIEW_EDGE)

    def _go(self, x, y):
        if self._fit is None:
            return
        rx0, ry0, s, ox, oy = self._fit
        self.canvas.center_at(self.canvas.current_page_index(), rx0 + (x - ox) / s, ry0 + (y - oy) / s)


class Minimap(Gtk.Box):
    def __init__(self, canvas):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, valign=Gtk.Align.END,
                         margin_start=14, margin_bottom=14, visible=False)
        self.add_css_class('minimap')
        self.canvas = canvas
        self.map = _Map(canvas)
        self.append(self.map)
        self.append(Gtk.Separator())
        row = Gtk.Box()
        self.zoom = Gtk.Button(hexpand=True, tooltip_text=tr('恢复 100%'))
        self.zoom.add_css_class('flat')
        self.zoom.connect('clicked', lambda *_: canvas.set_zoom(1.0))
        row.append(self.zoom)
        row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        fit = Gtk.Button(child=Icon(icons.fit_all), tooltip_text=tr('显示全部内容'))
        fit.add_css_class('flat')
        fit.connect('clicked', lambda *_: canvas.fit_content())
        row.append(fit)
        self.append(row)
        self._timer = 0
        canvas.connect('view-changed', lambda *_: self.refresh())

    def refresh(self):
        if self.get_visible():
            self.zoom.set_label('%d%%' % round(self.canvas.zoom * 100))
            self.map.queue_draw()

    def content_changed(self, kind, *args):
        """The board was drawn on: update the picture shortly, not on every stroke point."""
        self.map.overview.changed(kind, *args)
        if self.get_visible() and not self._timer:
            self._timer = GLib.timeout_add(350, self._render)

    def _render(self):
        self._timer = 0
        self.map.render()
        return False

    def show_for(self, board):
        self.map.overview = BoardOverview()
        self.set_visible(board)
        if board:
            self.map.render()
            self.refresh()
