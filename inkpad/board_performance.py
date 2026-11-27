"""Whiteboard overview and isolated snapshots for background saves."""
from copy import copy
from math import floor, log2

from .geom import densify, union
from .model import item_id, board_box


class BoardOverview:
    """Index occupied cells in world coordinates so extending any edge stays cheap."""

    def __init__(self):
        self.page = None
        self.rev = self.count = 0
        self.bounds = self.box = None
        self.cells = set()
        self.origin = (0, 0)
        self.cell = 1
        self.appends = self.added_count = 0

    def changed(self, kind, *args):
        if kind == 'added' and args[0] is self.page:
            self.appends += 1
            self.added_count += len(args[1])
        elif kind == 'pages' or (kind == 'dirty' and args[0] is self.page):
            self.page = None

    def refresh(self, page):
        incremental = (self.page is page and page.rev == self.rev + self.appends
                       and len(page.items) == self.count + self.added_count)
        items = page.items[self.count:] if incremental else page.items
        self.appends = self.added_count = 0
        if not incremental:
            self.bounds = None
            self.cells.clear()
        elif not items:
            return
        for item in items:
            self.bounds = union(self.bounds, item.bbox)
        self.box = x0, y0, x1, y1 = board_box(page, self.bounds)
        target = max((x1 - x0) / 64, (y1 - y0) / 44, 1e-6)
        if not incremental:
            self.origin = (x0, y0)
            self.cell = target
        elif target >= self.cell * 2:
            factor = 2 ** floor(log2(target / self.cell))
            self.cells = {(x // factor, y // factor) for x, y in self.cells}
            self.cell *= factor
        ox, oy = self.origin
        cell = self.cell
        for item in items:
            bx0, by0, bx1, by1 = item.bbox
            left, right = floor((bx0 - ox) / cell), floor((bx1 - ox) / cell)
            top, bottom = floor((by0 - oy) / cell), floor((by1 - oy) / cell)
            if left == right and top == bottom:
                self.cells.add((left, top))
            elif item.kind in ('i', 't'):
                self.cells.update((x, y) for x in range(left, right + 1)
                                  for y in range(top, bottom + 1))
            else:
                line = item.polyline()
                if line:
                    self.cells.update((floor((p[0] - ox) / cell), floor((p[1] - oy) / cell))
                                      for p in densify(line, cell * 0.45))
        self.page, self.rev, self.count = page, page.rev, len(page.items)


def large_board(page):
    return page.board and page.pdf is None and len(page.items) >= 512


def frozen_page(page):
    """Snapshot page membership and mutable fields before a worker reads them."""
    result = copy(page)
    result.gone = page.gone.copy()
    result.items = []
    for item in page.items:
        if item.kind == 's':
            result.items.append(item.save_view())
            continue
        item_id(item)
        # Copy fields directly. Calling constructors would remeasure text and recompute bounds.
        frozen = object.__new__(type(item))
        frozen.__dict__ = vars(item).copy()
        if hasattr(item, 'color'):
            frozen.color = tuple(item.color)
        if getattr(item, 'bg', None):
            frozen.bg = tuple(item.bg)
        if item.kind == 'i':
            frozen._surface = None  # each renderer owns its Cairo resources
        result.items.append(frozen)
    return result


def save_job(doc):
    """Capture one revision on the GTK thread. Return a callable for the save thread."""
    if any(p.pdf is not None for p in doc.pages) or not any(map(large_board, doc.pages)):
        snapshot = doc.snapshot()
        return lambda: snapshot
    doc.stamp()
    frozen = copy(doc)
    if doc.cover_spec is not None:
        style, color = doc.cover_spec
        frozen.cover_spec = (style, tuple(color))
    frozen.gone_pages = doc.gone_pages.copy()
    frozen.pages = [frozen_page(page) for page in doc.pages]
    frozen.listeners = []
    frozen.undo_stack = []
    frozen.redo_stack = []
    return frozen.snapshot
