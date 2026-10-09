"""The whiteboard navigator: a small map of where the board is written on, with the
part on screen marked, the zoom level, and a button that brings all of it into view."""
from gi.repository import GLib, Graphene, Gsk, Gtk

from .i18n import tr
from . import icons
from .geom import densify, union
from .icons import Icon
from .model import page_box
from .render import rgba

MAP_W, MAP_H = 216, 150
_VIEW_FILL = rgba(0.21, 0.52, 0.89, 0.22)
_VIEW_EDGE = rgba(0.21, 0.52, 0.89, 0.9)


class _Map(Gtk.Widget):
    def __init__(self, canvas):
        super().__init__(width_request=MAP_W, height_request=MAP_H, overflow=Gtk.Overflow.HIDDEN)
        self.canvas = canvas
        self.cells = set()     # occupied grid cells of the board's content
        self.cell = 1.0        # their size in page points
        self.box = None        # the page area the cells cover
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
        self.box = x0, y0, x1, y1 = page_box(page)
        cell = max((x1 - x0) / 64, (y1 - y0) / 44, 1e-6)
        cells = set()
        for it in page.items:
            if it.kind in ('i', 't'):
                bx0, by0, bx1, by1 = it.bbox
                for cx in range(int((bx0 - x0) / cell), int((bx1 - x0) / cell) + 1):
                    for cy in range(int((by0 - y0) / cell), int((by1 - y0) / cell) + 1):
                        cells.add((cx, cy))
            else:
                line = it.polyline()
                if line:
                    for p in (densify(line, cell * 0.45) if len(line) > 1 else line):
                        cells.add((int((p[0] - x0) / cell), int((p[1] - y0) / cell)))
        self.cell, self.cells = cell, cells
        self.queue_draw()

    def do_snapshot(self, snap):
        canvas = self.canvas
        view = canvas.view_box()
        if view is None:
            return
        w, h = self.get_width(), self.get_height()
        # The map covers the content and wherever the view currently is.
        rx0, ry0, rx1, ry1 = union(self.box, view) if self.box else view
        pad = 0.04 * max(rx1 - rx0, ry1 - ry0)
        rx0, ry0, rx1, ry1 = rx0 - pad, ry0 - pad, rx1 + pad, ry1 + pad
        s = min(w / (rx1 - rx0), h / (ry1 - ry0))
        ox, oy = (w - (rx1 - rx0) * s) / 2, (h - (ry1 - ry0) * s) / 2
        self._fit = (rx0, ry0, s, ox, oy)

        if self.cells:
            # In the panel's own text colour, so the blocks show in light and dark themes alike.
            ink = self.get_color()
            ink.alpha = 0.55
            bx, by = self.box[0], self.box[1]
            side = max(self.cell * s + 0.6, 2.0)   # a little overlap, so neighbours read as one block
            for cx, cy in self.cells:
                snap.append_color(ink, Graphene.Rect().init(ox + (bx + cx * self.cell - rx0) * s,
                                                            oy + (by + cy * self.cell - ry0) * s, side, side))
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

    def content_changed(self):
        """The board was drawn on: update the picture shortly, not on every stroke point."""
        if self.get_visible() and not self._timer:
            self._timer = GLib.timeout_add(350, self._render)

    def _render(self):
        self._timer = 0
        self.map.render()
        return False

    def show_for(self, board):
        self.set_visible(board)
        if board:
            self.map.render()
            self.refresh()
