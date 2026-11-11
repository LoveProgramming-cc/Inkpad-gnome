# Frosted glass in a GTK 4 app

How Inkpad draws bars that show what is behind them blurred, and how to do the same in
another GNOME app. The code is in [`inkpad/glass.py`](inkpad/glass.py); it is about 80 lines
and has no dependency beyond GTK 4.

## What it is, and what it is not

There are two things people call frosted glass:

| | Blurs | Who draws it | Works on |
|---|---|---|---|
| **In-app glass** (this document) | the app's own content under a bar | the app, with GTK | GNOME, KDE, any desktop |
| **Window glass** | the desktop and other windows behind the app | the compositor | only compositors with a blur protocol (KWin); not GNOME Shell |

GTK's CSS has no `backdrop-filter`, so a translucent background alone only gives a see-through
bar, not a frosted one. The blur has to be drawn by hand. GTK's scene graph (GSK) has a blur
node, and that is all that is needed.

## The idea

A bar that floats over a view is normally a child of a `Gtk.Overlay`. The overlay draws its
main child, then each bar on top. To frost a bar, the overlay draws the main child a second
time, blurred and clipped to the bar's shape, just before it draws the bar:

1. Record the main child once as a render node.
2. Draw that node.
3. For each bar: clip to the bar's rounded rectangle, push a blur, draw the same node again.
4. Draw the bar itself. Its CSS background is translucent, so it tints the blur.

The node is recorded once and reused, so the view is not laid out or rasterised twice; the
GPU only blurs the small area under each bar.

## Minimal code

```python
from gi.repository import Graphene, Gsk, Gtk


class GlassOverlay(Gtk.Overlay):
    def do_snapshot(self, snap):
        main = self.get_child()
        under = Gtk.Snapshot.new()
        self.snapshot_child(main, under)
        node = under.to_node()              # None when the child draws nothing
        if node is not None:
            snap.append_node(node)

        child = self.get_first_child()
        while child is not None:
            if child is not main:
                if node is not None and child.get_mapped() and child.has_css_class('glass'):
                    self._frost(snap, node, child)
                self.snapshot_child(child, snap)
            child = child.get_next_sibling()

    def _frost(self, snap, node, child, radius=18.0, blur=22.0):
        ok, box = child.compute_bounds(self)        # the border box, without CSS margins
        if not ok or box.get_width() <= 0 or box.get_height() <= 0:
            return
        shape = Gsk.RoundedRect()
        shape.init_from_rect(box, min(radius, box.get_width() / 2, box.get_height() / 2))
        snap.push_rounded_clip(shape)
        snap.push_blur(blur)
        snap.append_node(node)
        snap.pop()
        snap.pop()
```

```css
.glass {
  background: alpha(@popover_bg_color, 0.72);   /* the wash over the blur */
  border-radius: 18px;                          /* the same radius as in the code */
}
```

Use it exactly like `Gtk.Overlay`: `GlassOverlay(child=view)`, then `add_overlay(bar)` with
`bar.add_css_class('glass')`.

## Four things that go wrong

These all happened while writing Inkpad's version.

### 1. The blurred layer has holes, so it looks like a flat grey box

If the view under the bar has a transparent background (its colour is painted by a parent),
the recorded node contains only the items, with nothing between them. Blurred, that is a
half-transparent smear, and the sharp picture underneath shows through it. The stronger the
blur, the worse it gets.

**Fix:** make the blurred view opaque. Give the overlay's main child its own background:

```css
.glass-under { background: @view_bg_color; }
```

### 2. The glass fades out near the edge of the view

Beyond the edge of the view there is nothing, and the blur mixes that nothing in. A bar at the
very top of a window turns dark (or light) towards its top edge.

**Fix:** inside the blur, paint the view's colour over an area larger than the bar before
drawing the node, so the view appears to continue past its edge:

```python
found, color = self.get_style_context().lookup_color('view_bg_color')
snap.append_color(color, bigger_rect)   # bar bounds grown by the blur's reach
snap.append_node(node)
```

### 3. Blurring more than the bar can show

Clip the node inside the blur to the bar's bounds grown by the blur's reach (twice the radius
is enough). Without it GTK is asked to blur the whole view for every bar.

```python
snap.push_blur(blur)
snap.push_clip(bar_bounds_grown_by(2 * blur))
snap.append_node(node)
snap.pop()
snap.pop()
```

A note on strength: the radius can simply be raised. Measured on GTK 4.22 (Vulkan renderer),
the 10–90 % width of a blurred black/white edge grows in step with it:

| Radius | Edge width |
|---|---|
| 10 | 12 px |
| 22 | 26 px |
| 44 | 52 px |
| 88 | 104 px |

If a stronger blur seems to look *sharper*, with streaks or a ghost of the picture in it, the
cause is problem 1 (a transparent layer), not the radius.

### 4. Content has to be able to go under the bar

Glass over nothing looks like a plain bar. A header that sits *above* a scrolled list in a
vertical box never has anything behind it. Move the header into the overlay, so the list
scrolls underneath, and give the list a top margin equal to the bar's height so that its
first row starts below the bar.

The bar's height changes (a row appears, the font changes), so it has to be measured.
Overriding `do_size_allocate` on a `Gtk.Box` subclass in Python was not called here. What
works is to notice the height where the bar's bounds are already known, in the overlay's
snapshot, and apply the margin from an idle callback (never change layout while drawing):

```python
if h != self._bar_height:
    self._bar_height = h
    GLib.idle_add(lambda: flow.set_margin_top(h + 10) and False)
```

## Choosing the numbers

| | Small floating pill | Wide bar over a scrolling list |
|---|---|---|
| Blur radius | 22 | 38 |
| Tint | popover colour at 72 % | view colour at 74 % |
| Result | content stays recognisable, softened | content becomes soft patches of colour |

These follow the Android edition, which uses Haze with a radius of 22–24 and the same tints.

## Limits

- **Popovers and menus stay opaque.** They are separate surfaces, so the overlay cannot
  draw what is behind them.
- **The corner radius is written twice,** in the CSS and in the code. GTK does not give a
  widget's computed border radius to Python.
- **Each glass bar is an extra blur every frame** that the view under it redraws. It is cheap
  on the GPU renderers (GL, Vulkan). With the Cairo software renderer
  (`GSK_RENDERER=cairo`) blur is slow.
- **`lookup_color` is deprecated** in GTK 4. It still works; the alternative is to pass the
  colour in from your own palette.
- **Window glass** (the desktop blurred behind the window) is a different thing and is not
  available to GTK apps on GNOME.

## Checking it without a screenshot tool

A window can be rendered to a PNG from inside the app, which also works on Wayland where
screenshots need permission:

```python
snap = Gtk.Snapshot.new()
Gtk.WidgetPaintable.new(win).snapshot(snap, win.get_width(), win.get_height())
texture = win.get_renderer().render_texture(snap.to_node(), None)
texture.save_to_png('window.png')
```

Scroll some content under the bar first: glass with nothing behind it cannot be judged.
