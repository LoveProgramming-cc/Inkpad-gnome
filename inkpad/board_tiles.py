"""Viewport-sized working set of reusable raster tiles for an infinite whiteboard."""
from collections import OrderedDict
from math import ceil, floor

import cairo
from gi.repository import Graphene

from .geom import intersects
from .model import draw_paper, ink_on
from .render import texture_from_surface

TILE = 256
BUCKET = 256  # document points, independent of display zoom
CACHE_TILES = 96  # about 48 MiB including Cairo surfaces and GDK textures


def buckets(box):
    x0, y0, x1, y1 = (floor(v / BUCKET) for v in box)
    # Huge images and very distant views must not allocate a huge range of buckets.
    if (x1 - x0 + 1) * (y1 - y0 + 1) <= 256:
        return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return None


class ItemIndex:
    def __init__(self, items):
        self.by_cell = {}
        self.large = []
        self.items = items
        self.extend(0)

    def extend(self, start):
        for index in range(start, len(self.items)):
            keys = buckets(self.items[index].bbox)
            if keys is None:
                self.large.append(index)
            else:
                for key in keys:
                    self.by_cell.setdefault(key, []).append(index)

    def query(self, box):
        keys = buckets(box)
        if keys is None:
            return self.items
        indices = set(self.large)
        for key in keys:
            indices.update(self.by_cell.get(key, ()))
        return (self.items[i] for i in sorted(indices))


class Tile:
    def __init__(self, tx, ty, sx, sy):
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, TILE, TILE)
        self.texture = None
        self.box = (tx * TILE / sx, ty * TILE / sy,
                    (tx + 1) * TILE / sx, (ty + 1) * TILE / sy)
        self.cr = cairo.Context(self.surface)
        self.cr.translate(-tx * TILE, -ty * TILE)
        self.cr.scale(sx, sy)

    def paint(self, items, hidden):
        for item in items:
            if intersects(self.box, item.bbox) and not (hidden and id(item) in hidden):
                item.draw(self.cr)
                self.texture = None


class BoardTiles:
    def __init__(self, page):
        self.page = page
        self.index = None
        self.scale = None
        self.tiles = OrderedDict()

    def added(self, items, hidden):
        if self.index is not None:
            self.index.extend(len(self.page.items) - len(items))
        if len(items) > 16:
            # For bulk paste/import it is cheaper to rebuild only the visible tiles.
            self.tiles.clear()
            return
        ink_on(self.page)
        for tile in self.tiles.values():
            tile.paint(items, hidden)

    def dirty(self, rect):
        self.index = None
        if rect is None:
            self.tiles.clear()
        else:
            for key, tile in list(self.tiles.items()):
                if intersects(rect, tile.box):
                    del self.tiles[key]

    def snapshot(self, canvas, snap, i):
        page = self.page
        full = canvas._full_size(page)
        if full != self.scale:
            self.tiles.clear()
            self.scale = full
        if self.index is None:
            self.index = ItemIndex(page.items)
        sx, sy = full[0] / page.w, full[1] / page.h
        x, y, w, h = canvas._page_rect(i)
        dx, dy = TILE * (w / page.w) / sx, TILE * (h / page.h) / sy
        cols = range(floor(-x / dx), ceil((canvas.get_width() - x) / dx))
        rows = range(floor(-y / dy), ceil((canvas.get_height() - y) / dy))
        ink_on(page)
        for ty in rows:
            for tx in cols:
                key = tx, ty
                tile = self.tiles.get(key)
                if tile is None:
                    self.tiles[key] = tile = Tile(tx, ty, sx, sy)
                    draw_paper(tile.cr, page, tile.box)
                    tile.paint(self.index.query(tile.box), canvas._hidden)
                else:
                    self.tiles.move_to_end(key)
                if tile.texture is None:
                    tile.texture = texture_from_surface(tile.surface)
                snap.append_texture(tile.texture, Graphene.Rect().init(x + tx * dx, y + ty * dy, dx, dy))
        # A high-DPI monitor can need more tiles than the normal retention limit.
        limit = max(CACHE_TILES, (len(rows) + 1) * (len(cols) + 1))
        while len(self.tiles) > limit:
            self.tiles.popitem(last=False)
