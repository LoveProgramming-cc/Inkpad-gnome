"""Time the same synthetic input on a checkout supplied as argv[1]."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
area = TemporaryDirectory()
os.environ["INKPAD_HOME"] = area.name
from inkpad.__main__ import App
from inkpad.canvas import Canvas
from inkpad.model import Document, Stroke
from inkpad.window import Window
from gi.repository import Gtk, GLib

doc = Document({"board": True})
page, = doc.pages
ink = []
for row in range(100):
    for col in range(80):
        x, y = col * 24, row * 18
        points = [(x + i * 0.8, y + (i % 4), 0.3 + (i % 7) * 0.07) for i in range(18)]
        ink.append(Stroke("fountain", (0.2, 0.3, 0.4), 2, points))
doc.add_items(page, ink)
doc.path = str(Path(area.name) / "synthetic.inkpad")
canvas = Canvas()
canvas.set_document(doc)
canvas.allocate(900, 600, -1, None)
canvas.center_at(0, 450, 300)
canvas.do_snapshot(Gtk.Snapshot())
base_x, base_y = canvas._bo
pan = []
for lap in range(2):
    for step in [*range(30), *range(29, -1, -1)]:
        t = perf_counter()
        canvas._configure_adj(base_x + step * 44, base_y + step * 15)
        canvas.do_snapshot(Gtk.Snapshot())
        pan.append((perf_counter() - t) * 1000)
ordered = sorted(pan)
errors = []
win = SimpleNamespace(doc=doc, canvas=canvas, _save_thread=None, _save_timer=0,
                      _relocate=lambda d: True, _absorb=lambda d: True, toast=errors.append)
# Prime caches exactly as for a previously saved and reopened whiteboard.
doc.save()
doc = Document.load(doc.path)
canvas.set_document(doc)
win.doc = doc
doc.add_items(doc.pages[0], [Stroke("ball", (0, 0, 0), 2, [(1, 1, 0.5)])])
t = perf_counter()
Window._autosave(win)
capture = (perf_counter() - t) * 1000
win._save_thread.join(60)
assert not win._save_thread.is_alive()
while win._save_thread is not None:
    GLib.MainContext.default().iteration(True)
assert not errors and len(Document.load(doc.path).pages[0].items) == 8001
print(f"8000 strokes / 120 pan frames: p95 {ordered[113]:.2f} ms, max {max(pan):.2f} ms; "
      f"autosave main-thread call {capture:.2f} ms.", flush=True)
