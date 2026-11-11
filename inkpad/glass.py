"""Frosted glass: bars that float over a view show it blurred through them.

GTK 4.22 and later can do this alone, with the CSS property backdrop-filter; older
GTK 4 has none, so here the overlay that holds the bars draws it: the view underneath is recorded once as a render node, and drawn a second time,
blurred and clipped to the bar's shape, behind each bar marked as glass. The
bar's own translucent background (style.css) is the wash over it. This is the
look of the other editions of Inkpad (Haze on Android, materials on iOS).

This file is also offered under the MIT licence, as published in
https://github.com/LoveProgramming-cc/gtk-frosted-glass (see README.md, Licence).
"""
from gi.repository import GLib, Graphene, Gsk, Gtk

# By style class: the corner radius, which has to follow style.css, and the radius of the
# blur. A bar across the top is blurred much more than a small pill, so that what scrolls
# under it is only soft patches of colour, as in the other editions.
_GLASS = (('pill', 18.0, 22.0), ('minimap', 16.0, 22.0), ('status', 999.0, 22.0),
          ('glass-bar', 0.0, 38.0))


def _glass(widget):
    for name, radius, blur in _GLASS:
        if widget.has_css_class(name):
            return radius, blur
    return None


class GlassOverlay(Gtk.Overlay):
    """An overlay whose pills, minimap and status label are frosted glass over its child."""

    __gtype_name__ = 'InkpadGlassOverlay'

    on_bar_height = None     # called with the height of a 'glass-bar' child when it changes
    _bar_height = 0
    backing = None           # the name of the theme colour of the view under the glass

    def do_snapshot(self, snap):
        main = self.get_child()
        node = None
        if main is not None:
            under = Gtk.Snapshot.new()
            self.snapshot_child(main, under)
            node = under.to_node()
            if node is not None:
                snap.append_node(node)
        child = self.get_first_child()
        while child is not None:
            if child is not main:
                if node is not None and child.get_mapped():
                    self._frost(snap, node, child)
                self.snapshot_child(child, snap)
            child = child.get_next_sibling()

    def _frost(self, snap, node, child):
        glass = _glass(child)
        if glass is None:
            return
        radius, blur = glass
        ok, box = child.compute_bounds(self)
        w, h = box.get_width(), box.get_height()
        if not ok or w <= 0 or h <= 0:
            return
        if child.has_css_class('glass-bar') and self.on_bar_height and h != self._bar_height:
            self._bar_height = h
            GLib.idle_add(self.on_bar_height, h)    # not while drawing
        radius = min(radius, w / 2, h / 2)
        shape = Gsk.RoundedRect()
        shape.init_from_rect(Graphene.Rect().init(box.get_x(), box.get_y(), w, h), radius)
        snap.push_rounded_clip(shape)
        reach = 2 * blur
        snap.push_blur(blur)
        snap.push_clip(Graphene.Rect().init(box.get_x() - reach, box.get_y() - reach,
                                            w + 2 * reach, h + 2 * reach))
        # Past the edge of the view there is nothing, and blurring that in would fade the glass
        # out towards the edge: the view's own colour is taken to go on beyond it.
        if self.backing:
            found, color = self.get_style_context().lookup_color(self.backing)
            if found:
                snap.append_color(color, Graphene.Rect().init(box.get_x() - reach, box.get_y() - reach,
                                                              w + 2 * reach, h + 2 * reach))
        snap.append_node(node)
        snap.pop()
        snap.pop()
        snap.pop()
