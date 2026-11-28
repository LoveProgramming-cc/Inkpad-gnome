"""Run with python3 tests/whiteboard.py under a display (or xvfb-run)."""
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temp = TemporaryDirectory()
home = Path(temp.name)
os.environ["INKPAD_HOME"] = str(home)

from inkpad.__main__ import App  # configure GI versions before constructing a canvas
import cairo
from gi.repository import GLib, Gtk
from inkpad import model
from inkpad.board_performance import BoardOverview, save_job
from inkpad.canvas import Canvas
from inkpad.window import Window


def pen(x, y):
    return model.Stroke("ball", (0.2, 0.3, 0.4), 2, [(x, y, 0.4), (x + 16, y + 8, 0.8)])


def draw(canvas, n):
    x, y = 120 + n % 20, 80 + n % 30
    canvas._down(x, y, 0.4, False)
    assert canvas._op is not None
    for j in range(1, 9):
        canvas._move(x + 2 * j, y + j, 0.4 + j * 0.04)
    canvas.do_snapshot(Gtk.Snapshot())
    canvas._up(x + 16, y + 8)


doc = model.Document({"board": True})
page, = doc.pages
doc.add_items(page, [pen(i % 25 * 32, i // 25 * 20) for i in range(650)])
tape = model.Stroke("tape", (1, 0.9, 0.1), 10, [(0, 0, 0.5)])
png = io.BytesIO()
cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4).write_to_png(png)
doc.add_items(page, [tape, model.Image(40, 60, 30, 20, png.getvalue()),
                     model.Text(80, 30, "test", 14, (0, 0, 0))])
overview = BoardOverview()
doc.listeners.append(overview.changed)
overview.refresh(page)
initial = set(overview.cells)
extra = pen(-10000, -5000)
with patch.object(page.items[0], "polyline", side_effect=AssertionError("Rescanned old ink")):
    doc.add_items(page, [extra])
    overview.refresh(page)
    doc.touch()
    overview.refresh(page)
assert overview.box == model.page_box(page)
doc.undo()
overview.refresh(page)
assert overview.cells == initial
doc.redo()
overview.refresh(page)
assert overview.box == model.page_box(page)
before_erase = list(page.items)
page.items[-1] = pen(-500, 310)
doc.push_diff(page, before_erase)  # eraser can announce only a revision, keeping the same count
overview.refresh(page)
fresh = BoardOverview()
fresh.refresh(page)
assert (fresh.box, fresh.cells) == (overview.box, overview.cells)

doc.path = str(home / "generated-board.inkpad")
reference = doc.snapshot()
deferred = save_job(doc)
tape.open = True
doc.add_items(page, [pen(49, 60)])
assert deferred() == reference
assert save_job(doc)() == doc.snapshot()
canvas = Canvas()
canvas.set_document(doc)
canvas.allocate(800, 600, -1, None)
canvas.do_snapshot(Gtk.Snapshot())
win = SimpleNamespace(doc=doc, canvas=canvas, _save_thread=None, _save_timer=0,
                      _relocate=lambda d: True, _absorb=lambda d: True,
                      toast=lambda msg: errors.append(msg))
errors = []
def finish():
    thread = win._save_thread
    thread.join(30)
    assert not thread.is_alive()
    loop = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while win._save_thread and time.monotonic() < deadline:
        loop.iteration(False)
    assert not errors and win._save_thread is None

count, revision = len(page.items), doc.rev
Window._autosave(win)
for n in range(20):
    draw(canvas, n)
finish()
assert len(model.Document.load(doc.path).pages[0].items) == count
assert doc.saved_rev == revision and doc.modified
Window._autosave(win)
finish()
assert not doc.modified
reopened = model.Document.load(doc.path)
assert len(reopened.pages[0].items) == count + 20
assert reopened.snapshot()['pages'] == doc.snapshot()['pages']
assert reopened.snapshot()['images'] == [png.getvalue()]
print("Verified whiteboard drawing during autosave, overview updates, undo/redo and reopening")

# Visit negative coordinates, pan away and back, edit across a tile boundary,
# then compare the actual cached pixels against a complete fresh rendering.
from inkpad.board_tiles import TILE, ItemIndex
from inkpad.geom import intersects
from inkpad import gdrive

board = model.Document({"board": True, "paper": "grid"})
sheet, = board.pages
board.add_items(sheet, [pen(-8, 120), pen(250, 120), pen(260, 270)])
canvas.set_document(board)
canvas.center_at(0, 250, 180)

def frame():
    canvas.do_snapshot(Gtk.Snapshot())

def check_tile(key):
    tiles = canvas._board_tiles
    tile = tiles.tiles[key]
    sx, sy = tiles.scale[0] / sheet.w, tiles.scale[1] / sheet.h
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, TILE, TILE)
    cr = cairo.Context(surface)
    cr.translate(-key[0] * TILE, -key[1] * TILE)
    cr.scale(sx, sy)
    model.draw_paper(cr, sheet, tile.box)
    model.ink_on(sheet)
    for item in sheet.items:
        if not (canvas._hidden and id(item) in canvas._hidden):
            item.draw(cr)
    surface.flush()
    tile.surface.flush()
    assert bytes(surface.get_data()) == bytes(tile.surface.get_data())

frame()
key = (0, 0)
check_tile(key)
retained = canvas._board_tiles.tiles[key].texture
canvas.center_at(0, 900, 180)
frame()
canvas.center_at(0, 250, 180)
frame()
assert canvas._board_tiles.tiles[key].texture is retained
boundary = pen(246, 110)
board.add_items(sheet, [boundary])
frame()
for k in [(0, 0), (1, 0), (-1, 0)]:
    check_tile(k)
board.undo()
frame()
check_tile(key)
board.redo()
frame()
check_tile(key)
canvas._hidden = {id(boundary)}
canvas._repaint_page(sheet, boundary.bbox)
frame()
check_tile(key)
canvas._hidden = None
canvas._repaint_page(sheet, boundary.bbox)
frame()
check_tile(key)
before = list(sheet.items)
sheet.items = [s for s in before if s is not boundary]
canvas._repaint_page(sheet, boundary.bbox)
board.push_diff(sheet, before)
frame()
check_tile(key)
canvas._render_zoom = canvas.zoom = 1.25
canvas.center_at(0, 250, 180)
frame()
check_tile(key)

# Bound spatial indexing for oversized and negative-coordinate objects,
# and retain the document's paint order regardless of bucket order.
large = model.Shape('line', -40000, -40000, 40000, 40000, (1,0,0), 3)
objects = [pen(-300, -250), large, pen(-20, 0)]
index = ItemIndex(objects)
for box in [(-300,-251,10,10), (-1,-1,1,1), large.bbox]:
    assert [i for i in index.query(box) if intersects(i.bbox, box)] == [
        i for i in objects if intersects(i.bbox, box)]

# Quiet sync requests coalesce and yield to both input and an unfinished save.
# No credentials or network are needed to exercise the scheduling path.
from unittest.mock import Mock
request = Mock()
sync_state = SimpleNamespace(_sync_busy=False, _quiet_pending=None, in_editor=lambda: True,
                             doc=doc, canvas=canvas, _save_thread=object(), _save_timer=0,
                             get_application=lambda: True, sync_now=request)
with patch.object(gdrive, 'connected', return_value=True), patch.object(GLib, 'timeout_add') as later:
    Window.sync_now(sync_state, True, True)
    Window.sync_now(sync_state, True, False)
    assert later.call_count == 1
    retry = later.call_args.args[1]
    assert retry() is False and sync_state._quiet_pending is None
    request.assert_called_once_with(True, False)
print("Verified pan reuse, layered tile pixels, edits, zoom, and nonblocking sync scheduling")
