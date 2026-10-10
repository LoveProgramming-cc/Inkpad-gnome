"""Document model, item rendering, undo, file format and export.

Units are PDF points. Every item draws itself onto a cairo context, so the
screen cache, thumbnails, PNG export and vector PDF export share one code path.
"""
import base64
import hashlib
import io
import json
import math
import os
import struct
import threading
import time
import uuid
import zipfile
import zlib

import cairo

from . import geom, ptext
from .i18n import tr
from .geom import union
from .pdf import PdfDoc

A4 = (595.0, 842.0)
HL_ALPHA = 0.4
_TAU = 2 * math.pi

SIZES = [('A4', (595.0, 842.0)), ('A5', (420.0, 595.0)), ('A3', (842.0, 1191.0)),
         ('B5', (499.0, 709.0)), ('Letter', (612.0, 792.0))]
PAPER_COLORS = [('白色纸张', (1.0, 1.0, 1.0)), ('黄色纸张', (0.976, 0.965, 0.902)), ('深色纸张', (0.13, 0.13, 0.15))]
COVER_STYLES = ['simple', 'label', 'ribbon', 'band', 'grid']
COVER_COLORS = [(0.69, 0.61, 0.50), (0.13, 0.15, 0.27), (0.17, 0.27, 0.45), (0.24, 0.46, 0.38), (0.70, 0.26, 0.26),
                (0.33, 0.34, 0.37), (0.93, 0.75, 0.30), (0.89, 0.89, 0.88)]
PAPERS = [
    ('基本样式', [('blank', '空白'), ('dots', '点阵纸'), ('ruled', '窄行距'), ('wide', '宽行距'), ('grid', '方格纸')]),
    ('书写用纸', [('cornell', '康奈尔'), ('legal', '拍纸簿'), ('col2', '双栏'), ('col3', '三栏')]),
    ('日程与计划', [('todo', '待办清单'), ('weekly', '周计划')]),
    ('音乐与棋类', [('staff', '五线谱'), ('go', '围棋棋谱')]),
]


def _rot_pt(k, w, h, x, y):
    """Rotate a point with its w x h page by k quarter turns clockwise."""
    if k == 1:
        return h - y, x
    if k == 2:
        return w - x, h - y
    if k == 3:
        return y, w - x
    return x, y


def _rot_box(k, w, h, box):
    ax, ay = _rot_pt(k, w, h, box[0], box[1])
    bx, by = _rot_pt(k, w, h, box[2], box[3])
    return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)


def _smooth_path(cr, pts):
    """Quadratic midpoint smoothing through the sampled points."""
    px, py = pts[0][0], pts[0][1]
    cr.move_to(px, py)
    n = len(pts)
    for i in range(1, n - 1):
        cx, cy = pts[i][0], pts[i][1]
        mx, my = (cx + pts[i + 1][0]) / 2, (cy + pts[i + 1][1]) / 2
        cr.curve_to(px + (cx - px) * 2 / 3, py + (cy - py) * 2 / 3,
                    mx + (cx - mx) * 2 / 3, my + (cy - my) * 2 / 3, mx, my)
        px, py = mx, my
    cr.line_to(pts[-1][0], pts[-1][1])


def _dash_runs(pts, on, off):
    """Split a polyline into the 'pen down' runs of a dash pattern (on == 0 gives dots)."""
    runs, run = [], [pts[0]]
    drawing, left = True, on
    a = pts[0]
    for b in pts[1:]:
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        pos = 0.0
        while seg - pos > left:
            pos += left
            t = pos / seg
            q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
            if drawing:
                run.append(q)
                runs.append(run)
                run = []
            else:
                run = [q]
            drawing = not drawing
            left = on if drawing else off
        left -= seg - pos
        if drawing:
            run.append(b)
        a = b
    if drawing and run:
        runs.append(run)
    return runs


class Stroke:
    kind = 's'

    def __init__(self, tool, color, width, pts=None, sens=1.0, dash=0):
        self.tool = tool      # 'ball' | 'fountain' | 'brush' | 'hl' | 'tape'
        self.open = False     # a tape that has been lifted: it shows what it covers
        self.color = color
        self.width = width
        self.sens = sens      # pressure sensitivity, 1.0 is the default response
        self.dash = dash      # 0 solid, 1 dashed, 2 dotted
        self.pts = pts or []  # [(x, y, pressure)]
        self.bbox = (0, 0, 0, 0)
        if self.pts:
            self.update_bbox()

    def w_at(self, p):
        if self.tool == 'fountain':
            return self.width * max(0.15, 1 + self.sens * 1.2 * (p - 0.5))
        if self.tool == 'brush':
            return self.width * max(0.12, 1.4 + self.sens * 2.2 * (p - 0.5))
        return self.width

    def pad(self):
        return self.w_at(1.0) / 2 + 1

    def update_bbox(self):
        self.bbox = geom.bbox_of(self.pts, self.pad())

    on_dark = False   # set by ink_on() for the page about to be drawn

    def draw(self, cr):
        if not self.pts:
            return
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        hl = self.tool == 'hl'
        if self.tool == 'tape':
            # Tape covers what is under it; lifted, only a trace of it is left.
            cr.set_source_rgba(*self.color, 0.16 if self.open else 1.0)
            cr.set_line_width(self.width)
            _smooth_path(cr, self.pts if len(self.pts) > 1 else self.pts * 2)
            cr.stroke()
            return
        if hl:
            cr.save()
            # Multiplying darkens, which is what a marker does on light paper but
            # would make it invisible on a dark one.
            if not Stroke.on_dark:
                cr.set_operator(cairo.OPERATOR_MULTIPLY)
            cr.set_source_rgba(*self.color, HL_ALPHA)
        else:
            cr.set_source_rgb(*self.color)
        per_pixel = math.hypot(*cr.device_to_user_distance(1, 0))
        if per_pixel > 1.5 and not isinstance(cr.get_target(), cairo.PDFSurface):
            # Seen from far away: no pressure detail and no points closer together than a pixel,
            # and never thinner than a hairline, so the stroke stays visible and cheap to draw.
            step2 = per_pixel * per_pixel
            lx, ly = self.pts[0][0], self.pts[0][1]
            cr.move_to(lx, ly)
            for x, y, _ in self.pts[1:-1]:
                if (x - lx) ** 2 + (y - ly) ** 2 >= step2:
                    cr.line_to(x, y)
                    lx, ly = x, y
            cr.line_to(self.pts[-1][0] + 0.001, self.pts[-1][1])
            cr.set_line_width(max(self.width, per_pixel * 0.9))
            cr.stroke()
        elif self.dash and len(self.pts) > 1:
            w = self.width
            on, off = (max(3.0 * w, 2.5), max(2.6 * w, 3.0)) if self.dash == 1 else (0.0, max(2.4 * w, 2.5))
            for run in _dash_runs(self.pts, on, off):
                self._draw_run(cr, run)
        else:
            self._draw_run(cr, self.pts)
        if hl:
            cr.restore()

    def _draw_run(self, cr, pts):
        n = len(pts)
        if n == 1:
            cr.arc(pts[0][0], pts[0][1], self.w_at(pts[0][2]) / 2, 0, _TAU)
            cr.fill()
        elif self.tool in ('hl', 'ball'):
            cr.set_line_width(self.width)
            _smooth_path(cr, pts)
            cr.stroke()
        else:
            # Pressure-sensitive: one short segment per sample, each its own width.
            w_at = self.w_at
            px, py, pp = pts[0]
            for i in range(1, n):
                cx, cy, cp = pts[i]
                cr.move_to(px, py)
                if i < n - 1:
                    mx, my = (cx + pts[i + 1][0]) / 2, (cy + pts[i + 1][1]) / 2
                    cr.curve_to(px + (cx - px) * 2 / 3, py + (cy - py) * 2 / 3,
                                mx + (cx - mx) * 2 / 3, my + (cy - my) * 2 / 3, mx, my)
                else:
                    mx, my = cx, cy
                    cr.line_to(cx, cy)
                cr.set_line_width((w_at(pp) + w_at(cp)) / 2)
                cr.stroke()
                px, py, pp = mx, my, cp

    def polyline(self):
        return self.pts

    def sample(self):
        return geom.decimate(self.pts, 40)

    def hit(self, x, y, r):
        return geom.polyline_hit(self.pts, x, y, r + self.width / 2)

    def transformed(self, s, ox, oy, dx, dy):
        pts = [(ox + (x - ox) * s + dx, oy + (y - oy) * s + dy, p) for x, y, p in self.pts]
        new = Stroke(self.tool, self.color, self.width * s, pts, self.sens, self.dash)
        new.open = self.open
        return new

    def rotated(self, k, w, h):
        return Stroke(self.tool, self.color, self.width,
                      [_rot_pt(k, w, h, x, y) + (p,) for x, y, p in self.pts], self.sens, self.dash)

    def to_json(self, ctx):
        d = {'k': 's', 't': self.tool, 'c': list(self.color), 'w': self.width, 'ps': self.sens, 'd': self.dash,
             'q': pack_points(self.pts)}
        if self.open:
            d['o'] = True
        return d

    @classmethod
    def from_json(cls, d, ctx):
        if 'q' in d:
            pts = unpack_points(d['q'])
        else:   # files from before version 3 list the numbers
            f = d['p']
            pts = [(f[i], f[i + 1], f[i + 2]) for i in range(0, len(f), 3)]
        stroke = cls(d['t'], tuple(d['c']), d['w'], pts, d.get('ps', 1.0), d.get('d', 0))
        stroke.open = bool(d.get('o'))
        return stroke


class Shape:
    kind = 'h'

    def __init__(self, shape, x0, y0, x1, y1, color, width):
        self.shape = shape  # 'line' | 'arrow' | 'rect' | 'ellipse'
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.color = color
        self.width = width
        self.update_bbox()

    def update_bbox(self):
        pad = self.width / 2 + (max(8.0, self.width * 4) if self.shape == 'arrow' else 1)
        self.bbox = (min(self.x0, self.x1) - pad, min(self.y0, self.y1) - pad,
                     max(self.x0, self.x1) + pad, max(self.y0, self.y1) + pad)

    def draw(self, cr):
        x0, y0, x1, y1 = self.x0, self.y0, self.x1, self.y1
        cr.set_source_rgb(*self.color)
        cr.set_line_width(self.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if self.shape == 'rect':
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        elif self.shape == 'ellipse':
            rx, ry = abs(x1 - x0) / 2, abs(y1 - y0) / 2
            if rx > 0.01 and ry > 0.01:
                cr.save()
                cr.translate((x0 + x1) / 2, (y0 + y1) / 2)
                cr.scale(rx, ry)
                cr.arc(0, 0, 1, 0, _TAU)
                cr.restore()
        else:
            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            if self.shape == 'arrow':
                a = math.atan2(y1 - y0, x1 - x0)
                head = max(8.0, self.width * 4)
                for da in (2.6, -2.6):
                    cr.move_to(x1, y1)
                    cr.line_to(x1 + head * math.cos(a + da), y1 + head * math.sin(a + da))
        cr.stroke()

    def polyline(self):
        x0, y0, x1, y1 = self.x0, self.y0, self.x1, self.y1
        if self.shape == 'ellipse':
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
            return [(cx + rx * math.cos(_TAU * i / 40), cy + ry * math.sin(_TAU * i / 40)) for i in range(41)]
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)] if self.shape == 'rect' else [(x0, y0), (x1, y1)]
        out = []
        for a, b in zip(corners, corners[1:]):
            out += [(a[0] + (b[0] - a[0]) * i / 10, a[1] + (b[1] - a[1]) * i / 10) for i in range(10)]
        out.append(corners[-1])
        return out

    sample = polyline

    def hit(self, x, y, r):
        return geom.polyline_hit(self.polyline(), x, y, r + self.width / 2)

    def transformed(self, s, ox, oy, dx, dy):
        return Shape(self.shape, ox + (self.x0 - ox) * s + dx, oy + (self.y0 - oy) * s + dy,
                     ox + (self.x1 - ox) * s + dx, oy + (self.y1 - oy) * s + dy, self.color, self.width * s)

    def rotated(self, k, w, h):
        return Shape(self.shape, *_rot_pt(k, w, h, self.x0, self.y0), *_rot_pt(k, w, h, self.x1, self.y1),
                     self.color, self.width)

    def to_json(self, ctx):
        return {'k': 'h', 't': self.shape, 'c': list(self.color), 'w': self.width,
                'r': [round(v, 2) for v in (self.x0, self.y0, self.x1, self.y1)]}

    @classmethod
    def from_json(cls, d, ctx):
        return cls(d['t'], *d['r'], tuple(d['c']), d['w'])


class _Box:
    """Shared behaviour of rectangular items (text, images)."""

    def polyline(self):
        x0, y0, x1, y1 = self.bbox
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]

    def sample(self):
        x0, y0, x1, y1 = self.bbox
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]

    def hit(self, x, y, r):
        return False


_measure_cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


class Text(_Box):
    kind = 't'

    def __init__(self, x, y, text, size, color, wrap=0.0, bg=None):
        self.x, self.y, self.text, self.size, self.color, self.wrap = x, y, text, size, color, wrap
        self.bg = bg   # a sticky note: the text sits on a coloured slip of this colour
        self.update_bbox()

    def update_bbox(self):
        w, h = ptext.measure(_measure_cr, self.text, self.size, self.wrap)
        pad = self.size * 0.7 if self.bg else 2
        if self.bg and self.wrap:
            w = max(w, self.wrap)   # a note keeps its width however little is written on it
        self.bbox = (self.x - pad, self.y - pad, self.x + w + pad, self.y + h + pad)

    def draw(self, cr):
        if self.bg:
            x0, y0, x1, y1 = self.bbox
            fold = min(self.size * 0.9, (x1 - x0) / 4, (y1 - y0) / 4)
            cr.move_to(x0, y0)
            cr.line_to(x1, y0)
            cr.line_to(x1, y1 - fold)
            cr.line_to(x1 - fold, y1)
            cr.line_to(x0, y1)
            cr.close_path()
            cr.set_source_rgb(*self.bg)
            cr.fill()
            cr.move_to(x1, y1 - fold)
            cr.line_to(x1 - fold, y1 - fold)
            cr.line_to(x1 - fold, y1)
            cr.close_path()
            cr.set_source_rgb(*(c * 0.86 for c in self.bg))
            cr.fill()
        cr.set_source_rgb(*self.color)
        ptext.show(cr, self.x, self.y, self.text, self.size, self.wrap)

    def transformed(self, s, ox, oy, dx, dy):
        return Text(ox + (self.x - ox) * s + dx, oy + (self.y - oy) * s + dy,
                    self.text, self.size * s, self.color, self.wrap * s, self.bg)

    def rotated(self, k, w, h):
        box = _rot_box(k, w, h, self.bbox)
        pad = self.x - self.bbox[0]
        return Text(box[0] + pad, box[1] + pad, self.text, self.size, self.color, self.wrap, self.bg)

    def to_json(self, ctx):
        d = {'k': 't', 'x': round(self.x, 2), 'y': round(self.y, 2), 's': self.text,
             'z': self.size, 'c': list(self.color), 'wr': self.wrap}
        if self.bg:
            d['bg'] = list(self.bg)
        return d

    @classmethod
    def from_json(cls, d, ctx):
        return cls(d['x'], d['y'], d['s'], d['z'], tuple(d['c']), d.get('wr', 0.0),
                   tuple(d['bg']) if d.get('bg') else None)


class Image(_Box):
    kind = 'i'

    def __init__(self, x, y, w, h, data, surface=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.data = data  # PNG bytes
        self._surface = surface
        self.update_bbox()

    def update_bbox(self):
        self.bbox = (self.x, self.y, self.x + self.w, self.y + self.h)

    def surface(self):
        if self._surface is None:
            self._surface = cairo.ImageSurface.create_from_png(io.BytesIO(self.data))
        return self._surface

    def draw(self, cr):
        surf = self.surface()
        cr.save()
        cr.translate(self.x, self.y)
        cr.scale(self.w / surf.get_width(), self.h / surf.get_height())
        cr.set_source_surface(surf, 0, 0)
        cr.paint()
        cr.restore()

    def transformed(self, s, ox, oy, dx, dy):
        return Image(ox + (self.x - ox) * s + dx, oy + (self.y - oy) * s + dy,
                     self.w * s, self.h * s, self.data, self._surface)

    def rotated(self, k, w, h):
        src = self.surface()
        sw, sh = src.get_width(), src.get_height()
        dst = cairo.ImageSurface(cairo.FORMAT_ARGB32, *((sh, sw) if k % 2 else (sw, sh)))
        cr = cairo.Context(dst)
        cr.translate(*{1: (sh, 0), 2: (sw, sh), 3: (0, sw)}[k])
        cr.rotate(k * math.pi / 2)
        cr.set_source_surface(src, 0, 0)
        cr.paint()
        png = io.BytesIO()
        dst.write_to_png(png)
        box = _rot_box(k, w, h, self.bbox)
        return Image(box[0], box[1], box[2] - box[0], box[3] - box[1], png.getvalue(), dst)

    def to_json(self, ctx):
        return {'k': 'i', 'r': [round(v, 2) for v in (self.x, self.y, self.w, self.h)], 'img': ctx.image(self.data)}

    @classmethod
    def from_json(cls, d, ctx):
        return cls(*d['r'], ctx.images[d['img']])


_KINDS = {'s': Stroke, 'h': Shape, 't': Text, 'i': Image}


class Page:
    def __init__(self, w=A4[0], h=A4[1], paper='blank', pdf=None, pdf_page=0, color=(1.0, 1.0, 1.0), board=False):
        self.w, self.h = w, h
        self.paper = paper
        self.color = tuple(color)
        self.id = new_id()
        self.rev = 0          # counts changes to the items, so an unchanged page is not serialised again
        self.gone = set()     # ids of items removed from this page, kept so that a sync can tell
        self._blob = None     # (rev, serialised items)
        self.board = board    # a whiteboard: one very large page, shown and exported cropped to its content
        self.pdf = pdf
        self.pdf_page = pdf_page
        self.rot = 0          # quarter turns applied to the PDF background
        self.bookmark = False
        self.outline = None   # outline title, if the page is in the outline
        self.items = []
        self.mt = 0.0         # when the settings above were last changed, for merging two versions
        self._sign = None     # ...and what they were when that was last looked at

    def template(self):
        return {'w': self.w, 'h': self.h, 'paper': self.paper, 'color': self.color}

    def blank_like(self):
        return Page(self.w, self.h, self.paper, color=self.color, board=self.board)

    def clone(self):
        page = Page(self.w, self.h, self.paper, self.pdf, self.pdf_page, self.color, self.board)
        page.rot = self.rot
        page.items = [it.transformed(1, 0, 0, 0, 0) for it in self.items]
        return page

    def rotated(self, k):
        """A copy turned k quarter turns clockwise, content included."""
        w, h = (self.h, self.w) if k % 2 else (self.w, self.h)
        page = Page(w, h, self.paper, self.pdf, self.pdf_page, self.color, self.board)
        page.rot = (self.rot + k) % 4
        page.bookmark, page.outline = self.bookmark, self.outline
        page.items = [it.rotated(k, self.w, self.h) for it in self.items]
        return page


# A whiteboard has no edges at all: its items may lie anywhere, at negative coordinates
# too, and the canvas shows whatever part is looked at. The size below is only nominal; it
# gives a new board's first strokes coordinates of a comfortable magnitude.
BOARD_SIZE = (1e8, 1e8)


def page_box(page):
    """The part of a page worth showing: all of it, or a whiteboard's content."""
    if not page.board:
        return 0.0, 0.0, page.w, page.h
    box = None
    for it in page.items:
        box = union(box, it.bbox)
    if box is None:
        return page.w / 2 - 400, page.h / 2 - 300, page.w / 2 + 400, page.h / 2 + 300
    # Pad, and keep a sensible minimum size and shape.
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    w, h = max(box[2] - box[0] + 80, 400), max(box[3] - box[1] + 80, 300)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _rule(c, strong=False):
    """Rule colour that suits a paper colour."""
    if sum(c) / 3 > 0.5:
        k = 0.72 if strong else 1.0
        return c[0] * 0.80 * k, c[1] * 0.84 * k, c[2] * 0.90 * k
    lift = 0.38 if strong else 0.2
    return tuple(min(1.0, v + lift) for v in c)


def _hlines(cr, x0, x1, y0, y1, step, top=None):
    y = y0
    if top is not None and top > y0:
        y = y0 + math.ceil((top - y0) / step) * step
    while y <= y1:
        cr.move_to(x0, y)
        cr.line_to(x1, y)
        y += step


def _board_paper(cr, page, box):
    """A whiteboard's background over box. The board has no edges, so the pattern simply
    continues wherever it is looked at, negative coordinates included."""
    x0, y0, x1, y1 = box
    cr.set_source_rgb(*page.color)
    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
    cr.fill()
    kind = page.paper
    if kind == 'blank':
        return
    pitch = {'dots': 20.0, 'grid': 20.0, 'wide': 32.0}.get(kind, 22.0)
    # Seen from far away the pattern would be millions of marks closer together than a
    # pixel: thin the dots out, and leave rulings off altogether.
    per_pixel = math.hypot(*cr.device_to_user_distance(1, 0)) or 1.0
    scale = 1.0
    if pitch / per_pixel < 6:
        if kind != 'dots':
            return
        scale = 2 ** math.ceil(math.log2(6 * per_pixel / pitch))
        pitch *= scale
    cr.save()
    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
    cr.clip()
    cr.set_line_cap(cairo.LINE_CAP_BUTT)
    cr.set_source_rgb(*_rule(page.color))
    cr.set_line_width(0.5)
    fx, fy = math.floor(x0 / pitch) * pitch, math.floor(y0 / pitch) * pitch
    if kind == 'dots':
        size = 1.6 * scale
        y = fy
        while y < y1 + 1:
            x = fx
            while x < x1 + 1:
                cr.rectangle(x - size / 2, y - size / 2, size, size)
                x += pitch
            y += pitch
        cr.fill()
    else:
        y = fy
        while y < y1 + 1:
            cr.move_to(x0, y)
            cr.line_to(x1, y)
            y += pitch
        if kind == 'grid':
            x = fx
            while x < x1 + 1:
                cr.move_to(x, y0)
                cr.line_to(x, y1)
                x += pitch
        cr.stroke()
    cr.restore()


def draw_paper(cr, page, box=None):
    """Paint the paper; box limits the work to one area of the page."""
    if page.board:
        _board_paper(cr, page, box or page_box(page))
        return
    w, h, kind = page.w, page.h, page.paper
    bx0, by0, bx1, by1 = box or (0.0, 0.0, w, h)
    bx0, by0, bx1, by1 = max(0.0, bx0), max(0.0, by0), min(w, bx1), min(h, by1)
    cr.set_source_rgb(*page.color)
    cr.rectangle(bx0, by0, bx1 - bx0, by1 - by0)
    cr.fill()
    if kind == 'blank':
        return
    pitch = 20.0
    cr.save()
    cr.rectangle(bx0, by0, bx1 - bx0, by1 - by0)
    cr.clip()
    cr.set_line_cap(cairo.LINE_CAP_BUTT)
    cr.set_source_rgb(*_rule(page.color))
    cr.set_line_width(0.5)
    strong = _rule(page.color, True)
    if kind == 'dots':
        size = 1.2
        y = max(pitch, math.floor(by0 / pitch) * pitch)
        while y < by1 + 1:
            x = max(pitch, math.floor(bx0 / pitch) * pitch)
            while x < bx1 + 1:
                cr.rectangle(x - size / 2, y - size / 2, size, size)
                x += pitch
            y += pitch
        cr.fill()
    elif kind == 'grid':
        _hlines(cr, bx0, bx1, 20, by1, 20, by0)
        x = max(20.0, math.floor(bx0 / 20) * 20)
        while x < bx1 + 1:
            cr.move_to(x, by0)
            cr.line_to(x, by1)
            x += 20
        cr.stroke()
    elif kind == 'cornell':
        top, cue, bottom = h * 0.09, w * 0.28, h * 0.80
        _hlines(cr, 0, w, top + 22, bottom - 4, 22)
        cr.stroke()
        cr.set_source_rgb(*strong)
        cr.set_line_width(0.9)
        _hlines(cr, 0, w, top, top, 1)
        _hlines(cr, 0, w, bottom, bottom, 1)
        cr.move_to(cue, top)
        cr.line_to(cue, bottom)
        cr.stroke()
    elif kind == 'todo':
        y = 92.0
        while y < h - 36:
            cr.rectangle(36, y - 11, 10, 10)
            cr.move_to(56, y)
            cr.line_to(w - 36, y)
            y += 28
        cr.stroke()
        cr.set_source_rgb(*strong)
        cr.set_line_width(0.9)
        _hlines(cr, 36, w - 36, 60, 60, 1)
        cr.stroke()
    elif kind == 'weekly':
        m, top = 30.0, 56.0
        cw, ch = (w - 2 * m) / 2, (h - top - m) / 4
        cr.set_source_rgb(*strong)
        cr.set_line_width(0.8)
        for n, name in enumerate(('周一', '周二', '周三', '周四', '周五', '周六', '周日', '备注')):
            x, y = m + (n % 2) * cw, top + (n // 2) * ch
            cr.rectangle(x, y, cw, ch)
            cr.stroke()
            ptext.show(cr, x + 6, y + 5, tr(name), 9)
    elif kind == 'staff':
        # Music manuscript: staves of five lines.
        cr.set_source_rgb(*strong)
        cr.set_line_width(0.6)
        gap, pitch = 7.0, 7.0 * 4 + 38
        y = 78.0
        while y + gap * 4 < h - 44:
            for i in range(5):
                cr.move_to(42, y + i * gap)
                cr.line_to(w - 42, y + i * gap)
            cr.move_to(42, y)
            cr.line_to(42, y + gap * 4)
            cr.move_to(w - 42, y)
            cr.line_to(w - 42, y + gap * 4)
            y += pitch
        cr.stroke()
    elif kind == 'go':
        # A 19 x 19 goban with star points, and lines underneath for commentary.
        side = min(w - 80, (h - 130) * 0.74)
        cell = side / 18
        x0, y0 = (w - side) / 2, 64.0
        cr.set_source_rgb(*strong)
        cr.set_line_width(0.55)
        for i in range(19):
            cr.move_to(x0 + i * cell, y0)
            cr.line_to(x0 + i * cell, y0 + side)
            cr.move_to(x0, y0 + i * cell)
            cr.line_to(x0 + side, y0 + i * cell)
        cr.stroke()
        cr.set_line_width(1.3)
        cr.rectangle(x0, y0, side, side)
        cr.stroke()
        for i in (3, 9, 15):
            for j in (3, 9, 15):
                cr.arc(x0 + i * cell, y0 + j * cell, max(1.3, cell * 0.11), 0, _TAU)
                cr.fill()
        cr.set_source_rgb(*_rule(page.color))
        cr.set_line_width(0.5)
        _hlines(cr, x0, x0 + side, y0 + side + 34, h - 30, 22)
        cr.stroke()
    else:
        wide = kind == 'wide'
        first = 70.0 if kind == 'legal' else 50.0
        _hlines(cr, bx0, bx1, first, min(h - 8, by1), 32 if wide else 22, by0)
        cr.stroke()
        if kind == 'legal':
            cr.set_source_rgb(0.86, 0.42, 0.42)
            cr.set_line_width(0.7)
            for x in (w * 0.13, w * 0.13 + 3):
                cr.move_to(x, 0)
                cr.line_to(x, h)
            cr.stroke()
        elif kind in ('col2', 'col3'):
            n = int(kind[3])
            cr.set_source_rgb(*strong)
            cr.set_line_width(0.8)
            for i in range(1, n):
                cr.move_to(w * i / n, 36)
                cr.line_to(w * i / n, h - 24)
            cr.stroke()
    cr.restore()


def render_pdf_bg(cr, page, printing=False):
    """Draw the page's PDF background, honouring its rotation."""
    ow, oh = page.pdf.page_size(page.pdf_page)
    k = page.rot % 4
    cr.save()
    if k:
        cr.translate(*{1: (oh, 0), 2: (ow, oh), 3: (0, ow)}[k])
        cr.rotate(k * math.pi / 2)
    page.pdf.render(page.pdf_page, cr, printing)
    cr.restore()


def find_text(page, query):
    """Boxes on a page where query occurs: in its text boxes and notes, and in the PDF under it."""
    query = query.strip().lower()
    out = []
    if not query:
        return out
    if page.pdf is not None:
        ow, oh = page.pdf.page_size(page.pdf_page)
        out += [_rot_box(page.rot % 4, ow, oh, box) for box in page.pdf.find(page.pdf_page, query)]
    out += [it.bbox for it in page.items if it.kind == 't' and query in it.text.lower()]
    return out


def ink_on(page):
    """Tell the items which kind of paper they are about to be drawn on."""
    r, g, b = page.color[:3]
    Stroke.on_dark = page.pdf is None and 0.299 * r + 0.587 * g + 0.114 * b < 0.45


def render_page(cr, page, printing=False, paper=True, ink=True):
    """Draw background and items; the context is in page points."""
    box = page_box(page)
    if not paper:
        pass
    elif page.pdf is None:
        draw_paper(cr, page, box)
    else:
        if not printing:
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(0, 0, page.w, page.h)
            cr.fill()
        render_pdf_bg(cr, page, printing)
    cr.rectangle(box[0], box[1], box[2] - box[0], box[3] - box[1])
    cr.clip()
    if not ink:
        return
    ink_on(page)
    if not paper:
        Stroke.on_dark = True   # nothing underneath to multiply with
    for it in page.items:
        it.draw(cr)


GONE_LIMIT = 20000


# Held while a notebook file is compared with what is expected to be there and then replaced:
# by an editor saving it, and by a sync putting another device's version in its place. Without
# it either of the two could write over what the other had only just written.
file_lock = threading.RLock()


def file_sign(path):
    """What tells one writing of a file from the next, or None when there is no file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class Changed(Exception):
    """The file is no longer the one that the document was read from or last written to."""


def _digest(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:16]


# A page's settings: what is kept in document.json about it, apart from what is drawn on it.
_SETTINGS = ('w', 'h', 'paper', 'color', 'board', 'pdf', 'pdf_page', 'rot', 'bookmark', 'outline')


def _page_sign(page):
    return (page.w, page.h, page.paper, tuple(page.color), bool(page.board),
            _pdf_key(page.pdf) if page.pdf is not None else None, page.pdf_page, page.rot,
            bool(page.bookmark), page.outline or None)


def merge(mine, theirs):
    """Fold another version of the same notebook into mine, item by item.

    A page or an item that either side removed stays removed; everything else that either
    side has is kept. A page's settings (paper, background, bookmark...) are those of the
    side that changed them last, and so is the order of the pages. Returns what changed in
    mine: (pages added, pages removed, items added, items removed, settings taken over).
    """
    mine.stamp()
    theirs.stamp()
    gone_pages = mine.gone_pages | theirs.gone_pages
    counts = [0, 0, 0, 0, 0]
    by_id = {page.id: page for page in mine.pages}
    other = {page.id: page for page in theirs.pages}
    # The pages are in the order of the side that arranged them last. A page only the other
    # side has goes after the page it follows there.
    lead, follow = (theirs, mine) if theirs.order_mt > mine.order_mt else (mine, theirs)
    order = [page.id for page in lead.pages]
    have = set(order)
    previous = None
    for page in follow.pages:
        if page.id not in have:
            order.insert(order.index(previous) + 1 if previous in have else len(order), page.id)
            have.add(page.id)
        previous = page.id
    pages = [by_id.get(i) or other[i] for i in order if i not in gone_pages]
    was = [page.id for page in mine.pages]
    counts[0] = sum(1 for page in pages if page.id not in by_id)
    mine.pages = pages or mine.pages[:1]
    now = {page.id for page in mine.pages}
    counts[1] = sum(1 for i in was if i not in now)
    if [i for i in was if i in now] != [page.id for page in mine.pages if page.id in by_id]:
        counts[4] += 1
    mine.order_mt = max(mine.order_mt, theirs.order_mt)
    mine._order = [page.id for page in mine.pages]
    changed = False
    for page in mine.pages:
        their = other.get(page.id)
        if their is None or their is page:
            continue
        if their.mt > page.mt:
            for key in _SETTINGS:
                setattr(page, key, getattr(their, key))
            page.mt, page._sign = their.mt, _page_sign(page)
            counts[4] += 1
        gone = page.gone | their.gone
        have = {item_id(it) for it in page.items}
        new = [it for it in their.items if item_id(it) not in have and item_id(it) not in gone]
        kept = [it for it in page.items if item_id(it) not in gone]
        counts[2] += len(new)
        counts[3] += len(page.items) - len(kept)
        if new or len(kept) != len(page.items) or gone != page.gone:
            page.items = kept + new
            page.gone = gone
            page.rev += 1
            changed = True
    if changed or gone_pages != mine.gone_pages or any(counts):
        mine.gone_pages = gone_pages
        mine.undo_stack.clear()
        mine.redo_stack.clear()
        mine.rev += 1
    return tuple(counts)


def new_id():
    return uuid.uuid4().hex[:16]


def item_id(it):
    """An item's permanent id, given on first use. Syncing merges two versions of a page by
    these ids, so an item keeps its id for as long as it is the same object."""
    try:
        return it.id
    except AttributeError:
        it.id = new_id()
        return it.id


def _zig(n):
    n = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return out


def pack_points(pts):
    """Stroke points as text a fifth the size of a list of numbers: the first point in full,
    then the steps between points in hundredths of a point, and each pressure as one byte."""
    if not pts:
        return ''
    out = bytearray(struct.pack('<dd', pts[0][0], pts[0][1]))
    px, py = round(pts[0][0] * 100), round(pts[0][1] * 100)
    for x, y, p in pts:
        cx, cy = round(x * 100), round(y * 100)
        out += _zig(cx - px)
        out += _zig(cy - py)
        out.append(max(0, min(255, round(p * 255))))
        px, py = cx, cy
    return base64.b64encode(bytes(out)).decode('ascii')


def unpack_points(text):
    data = base64.b64decode(text)
    if not data:
        return []
    x0, y0 = struct.unpack_from('<dd', data, 0)
    px, py = round(x0 * 100), round(y0 * 100)
    pts, i, n = [], 16, len(data)
    while i < n:
        step = []
        for _ in range(2):
            value = shift = 0
            while True:
                c = data[i]
                i += 1
                value |= (c & 0x7f) << shift
                shift += 7
                if not c & 0x80:
                    break
            step.append((value >> 1) ^ -(value & 1))
        px, py = px + step[0], py + step[1]
        pts.append((px / 100, py / 100, data[i] / 255))
        i += 1
    if pts:
        pts[0] = (x0, y0, pts[0][2])
    return pts


def _pdf_key(pdf):
    """What two copies of the same PDF have in common. A notebook holds a PDF once however many
    times it was imported into it, or came into it with pages merged from another device."""
    try:
        return pdf._key
    except AttributeError:
        pdf._key = (len(pdf.data), zlib.crc32(pdf.data))
        return pdf._key


class _SaveCtx:
    def __init__(self):
        self.images = []
        self._seen = {}

    def image(self, data):
        key = id(data)
        if key not in self._seen:
            self._seen[key] = len(self.images)
            self.images.append(data)
        return self._seen[key]


class Document:
    """Pages plus an undo history.

    Undo actions are plain tuples:
      ('items', page, removed, added)  with [(index, item)] lists
      ('pages', removed, added)        with [(index, page)] lists
      ('template', page, old, new)     with Page.template() dicts
    Listeners get ('added', page, items) for a pure append, ('dirty', page, rect)
    for anything else on one page, ('pages',) for structure changes, ('meta',)
    for bookmarks and outline, and ('state',) whenever the undo or saved state
    may have changed.
    """

    def __init__(self, template=None):
        self.pages = [Page(**template) if template else Page()]
        self.path = None
        self.rev = 0
        self.saved_rev = 0
        self.undo_stack = []
        self.redo_stack = []
        self.listeners = []
        self._cover = None
        self.id = uuid.uuid4().hex  # stable across renames and moves; what sync tracks
        self.gone_pages = set()     # ids of pages removed from the document
        self.cover_spec = None      # (style, colour) of a designed cover; None shows the first page
        self.sign = None            # file_sign() of the file as this document last read or wrote it
        self.order_mt = 0.0         # when the pages were last put in another order
        self._order = None          # ...and the order they were in when that was last looked at

    @property
    def modified(self):
        return self.rev != self.saved_rev

    def emit(self, *event):
        kind = event[0]
        if kind == 'pages' or (kind in ('added', 'dirty') and self.pages and event[1] is self.pages[0]):
            self._cover = None
        for fn in list(self.listeners):
            fn(*event)

    def touch(self):
        """Mark a change that is not on the undo stack (bookmarks, outline)."""
        self.rev += 1
        self.emit('meta')
        self.emit('state')

    # -- undo ---------------------------------------------------------------

    def do(self, action):
        self._apply(action, True)
        self.push(action)

    def _mark(self, action, forward=True):
        """Note what an action changed: the page is dirty, and removed items leave tombstones."""
        if action[0] == 'items':
            _, page, removed, added = action
            out, into = (removed, added) if forward else (added, removed)
            page.rev += 1
            kept = {id(it) for _, it in into}
            page.gone.update(item_id(it) for _, it in out if id(it) not in kept)
            page.gone.difference_update(item_id(it) for _, it in into)
        elif action[0] == 'pages':
            _, removed, added = action
            out, into = (removed, added) if forward else (added, removed)
            self.gone_pages.update(page.id for _, page in out)
            self.gone_pages.difference_update(page.id for _, page in into)

    def push(self, action):
        """Record an action whose effect is already applied."""
        self._mark(action)
        self.undo_stack.append(action)
        del self.undo_stack[:-300]
        self.redo_stack.clear()
        self.rev += 1
        self.emit('state')

    def undo(self):
        if self.undo_stack:
            action = self.undo_stack.pop()
            self._apply(action, False)
            self._mark(action, False)
            self.redo_stack.append(action)
            self.rev += 1
            self.emit('state')

    def redo(self):
        if self.redo_stack:
            action = self.redo_stack.pop()
            self._apply(action, True)
            self._mark(action, True)
            self.undo_stack.append(action)
            self.rev += 1
            self.emit('state')

    @staticmethod
    def _swap(seq, out, into):
        gone = {id(obj) for _, obj in out}
        seq[:] = [obj for obj in seq if id(obj) not in gone]
        for index, obj in sorted(into, key=lambda pair: pair[0]):
            seq.insert(min(index, len(seq)), obj)

    def _apply(self, action, forward):
        kind = action[0]
        if kind == 'items':
            _, page, removed, added = action
            out, into = (removed, added) if forward else (added, removed)
            before = len(page.items)
            self._swap(page.items, out, into)
            if not out and into and min(i for i, _ in into) >= before:
                self.emit('added', page, [it for _, it in into])
            else:
                rect = None
                for _, it in out + into:
                    rect = union(rect, it.bbox)
                self.emit('dirty', page, rect)
        elif kind == 'pages':
            _, removed, added = action
            out, into = (removed, added) if forward else (added, removed)
            self._swap(self.pages, out, into)
            self.emit('pages')
        elif kind == 'template':
            _, page, old, new = action
            for key, value in (new if forward else old).items():
                setattr(page, key, value)
            self.emit('pages')
            self.emit('dirty', page, None)

    def add_items(self, page, items):
        n = len(page.items)
        self.do(('items', page, [], [(n + i, it) for i, it in enumerate(items)]))

    def remove_items(self, page, items):
        index = {id(it): i for i, it in enumerate(page.items)}
        self.do(('items', page, [(index[id(it)], it) for it in items], []))

    def replace_items(self, page, olds, news):
        index = {id(it): i for i, it in enumerate(page.items)}
        pairs = sorted(zip(olds, news), key=lambda pair: index[id(pair[0])])
        self.do(('items', page, [(index[id(o)], o) for o, _ in pairs],
                 [(index[id(o)], n) for o, n in pairs]))

    def push_diff(self, page, before):
        """Record an edit made directly on page.items, given the list from before it."""
        now = {id(it) for it in page.items}
        old = {id(it) for it in before}
        removed = [(i, it) for i, it in enumerate(before) if id(it) not in now]
        added = [(i, it) for i, it in enumerate(page.items) if id(it) not in old]
        if removed or added:
            self.push(('items', page, removed, added))

    # -- pdf ----------------------------------------------------------------

    @staticmethod
    def pdf_pages(data):
        pdf = PdfDoc(data)
        return [Page(*pdf.page_size(i), pdf=pdf, pdf_page=i) for i in range(pdf.n_pages)]

    @classmethod
    def from_pdf(cls, data):
        doc = cls()
        doc.pages = cls.pdf_pages(data)
        doc.rev = 1  # not on disk yet
        return doc

    # -- file format: zip of document.json + embedded pdfs, images, cover ---

    def set_cover(self, spec):
        self.cover_spec = spec
        self._cover = None
        self.touch()

    def cover(self):
        if self._cover is None and self.cover_spec is not None:
            self._cover = render_cover(self.cover_spec, self.display_name())
        if self._cover is None:
            page = self.pages[0]
            png = io.BytesIO()
            box = page_box(page)
            if page.board:
                # A board's content can have any shape; show it centred on a sheet of its paper.
                page_surface(page, 0, (300, 400)).write_to_png(png)
            else:
                page_surface(page, 300 / max(box[2] - box[0], box[3] - box[1])).write_to_png(png)
            self._cover = png.getvalue()
        return self._cover

    # The file is a zip:
    #   document.json    the page list (id, size, paper, background, bookmark, ...) and what
    #                    was removed from it
    #   pages/<id>.json  one page's items, each with a permanent id, and the ids of the items
    #                    that were removed from the page
    #   pdf/N.pdf, img/N.png, cover.png, id
    # Pages are separate so that only a changed page is serialised again, and items have ids
    # and leave tombstones so that two versions of a notebook can be merged item by item.

    def stamp(self):
        """Note the time on the settings of every page that has changed them, and on the order
        of the pages if that has changed, since this was last done. When two versions of a
        notebook are merged, the later change is the one that stays."""
        now = time.time()
        for page in self.pages:
            sign = _page_sign(page)
            if sign != page._sign:
                page.mt, page._sign = now, sign
        ids = [page.id for page in self.pages]
        if self._order is not None and self._order != ids:
            here, before = set(ids), set(self._order)
            if [i for i in self._order if i in here] != [i for i in ids if i in before]:
                self.order_mt = now
        self._order = ids

    def snapshot(self):
        """Everything needed to write the file, as immutable data safe to hand to a thread."""
        self.stamp()
        ctx = _SaveCtx()
        pdfs, slots = [], {}
        pages = []
        blobs = {}
        for page in self.pages:
            d = {'id': page.id, 'w': page.w, 'h': page.h, 'paper': page.paper, 'color': list(page.color)}
            if page.board:
                d['board'] = True
            if page.pdf is not None:
                key = _pdf_key(page.pdf)
                if key not in slots:
                    slots[key] = len(pdfs)
                    pdfs.append(page.pdf)
                d['pdf'] = slots[key]
                d['pdf_page'] = page.pdf_page
                d['rot'] = page.rot
            if page.bookmark:
                d['bookmark'] = True
            if page.outline:
                d['outline'] = page.outline
            if page.mt:
                d['mt'] = page.mt
            pages.append(d)
            has_image = any(it.kind == 'i' for it in page.items)
            if page._blob is None or page._blob[0] != page.rev or has_image:
                items = []
                for it in page.items:
                    data = it.to_json(ctx)
                    data['id'] = item_id(it)
                    items.append(data)
                # Tombstones are only needed until every device has seen the removal; cap them.
                gone = sorted(page.gone)[-GONE_LIMIT:]
                page._blob = (page.rev, json.dumps({'items': items, 'gone': gone}, separators=(',', ':')))
            blobs[page.id] = page._blob[1]
        meta = {'format': 'inkpad', 'version': 3, 'pages': pages, 'id': self.id,
                'gone': sorted(self.gone_pages)[-GONE_LIMIT:]}
        if self.order_mt:
            meta['order_mt'] = self.order_mt
        if self.cover_spec is not None:
            meta['cover'] = [self.cover_spec[0], list(self.cover_spec[1])]
        return {'json': json.dumps(meta, separators=(',', ':')), 'pages': blobs, 'pdfs': [p.data for p in pdfs],
                'images': list(ctx.images), 'cover': self.cover(), 'rev': self.rev, 'id': self.id}

    @staticmethod
    def write_snapshot(path, snap, expect=None):
        """Write the file and return its file_sign(). With expect, the sign the file had when
        it was last read or written, Changed is raised if something else has written it since."""
        with file_lock:
            if expect is not None and file_sign(path) != expect:
                raise Changed(path)
            Document._write(path, snap)
            return file_sign(path)

    @staticmethod
    def _write(path, snap):
        tmp = path + '.tmp'
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('document.json', snap['json'])
            for page_id, blob in snap['pages'].items():
                z.writestr('pages/%s.json' % page_id, blob)
            z.writestr(zipfile.ZipInfo('id'), snap['id'])
            z.writestr(zipfile.ZipInfo('cover.png'), snap['cover'])
            for i, data in enumerate(snap['pdfs']):
                z.writestr(zipfile.ZipInfo('pdf/%d.pdf' % i), data)  # stored: PDFs are already compressed
            for i, data in enumerate(snap['images']):
                z.writestr(zipfile.ZipInfo('img/%d.png' % i), data)
        os.replace(tmp, path)

    def stale(self):
        """Whether the file was written by something else (a sync) since this document read it."""
        return self.path is not None and self.sign is not None and file_sign(self.path) not in (None, self.sign)

    def absorb(self):
        """Take in what something else wrote to the file, keeping what was done here since."""
        with file_lock:
            other = Document.load(self.path)
            self.sign = other.sign
        if any(merge(self, other)):
            self.emit('pages')
            for page in self.pages:
                self.emit('dirty', page, None)
        self.emit('state')

    def save(self, path=None):
        target = path or self.path
        with file_lock:
            if target == self.path and self.stale():
                try:
                    self.absorb()   # never write over what a sync brought: fold it in first
                except Exception:
                    pass            # unreadable; this document is the better of the two
            snap = self.snapshot()
            self.sign = self.write_snapshot(target, snap)
        self.path = target
        self.saved_rev = snap['rev']
        self.emit('state')

    @classmethod
    def load(cls, path):
        sign = file_sign(path)
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read('document.json'))
            if meta.get('format') != 'inkpad':
                raise ValueError(tr('不是 Inkpad 文档'))
            names = set(z.namelist())
            # The same PDF or picture stored twice is read once, and so saved once from then on.
            pdfs, same = [], {}
            while 'pdf/%d.pdf' % len(pdfs) in names:
                info = z.getinfo('pdf/%d.pdf' % len(pdfs))
                key = (info.file_size, info.CRC)
                if key not in same:
                    same[key] = PdfDoc(z.read(info))
                    same[key]._key = key
                pdfs.append(same[key])
            ctx = _SaveCtx()
            while 'img/%d.png' % len(ctx.images) in names:
                info = z.getinfo('img/%d.png' % len(ctx.images))
                key = ('img', info.file_size, info.CRC)
                if key not in same:
                    same[key] = z.read(info)
                ctx.images.append(same[key])
            contents = {d['id']: json.loads(z.read('pages/%s.json' % d['id']))
                        for d in meta['pages'] if 'id' in d and 'pages/%s.json' % d['id'] in names}
        doc = cls()
        doc.id = meta.get('id') or doc.id
        doc.pages = []
        for n, d in enumerate(meta['pages']):
            page = Page(d['w'], d['h'], d.get('paper', 'blank'), color=tuple(d.get('color', (1.0, 1.0, 1.0))),
                        board=d.get('board', False))
            if 'pdf' in d:
                page.pdf = pdfs[d['pdf']]
                page.pdf_page = d['pdf_page']
                page.rot = d.get('rot', 0)
            page.bookmark = d.get('bookmark', False)
            page.outline = d.get('outline')
            # Files from before version 3 have no ids. They are derived from the content, so
            # that two devices opening the same old file agree on them.
            page.id = d.get('id') or _digest('%s/%d' % (doc.id, n))
            content = contents.get(page.id) or {'items': d.get('items', [])}
            for k, data in enumerate(content['items']):
                item = _KINDS[data['k']].from_json(data, ctx)
                item.id = data.get('id') or _digest('%s/%d/%s' % (page.id, k, json.dumps(data, sort_keys=True)))
                page.items.append(item)
            page.gone = set(content.get('gone', []))
            page.mt, page._sign = d.get('mt', 0.0), _page_sign(page)
            doc.pages.append(page)
        if not doc.pages:
            doc.pages = [Page()]
        doc.gone_pages = set(meta.get('gone', []))
        doc.order_mt, doc._order = meta.get('order_mt', 0.0), [page.id for page in doc.pages]
        doc.path = path
        doc.sign = sign
        cover = meta.get('cover')
        if cover:
            # Older files stored a bare colour.
            doc.cover_spec = (cover[0], tuple(cover[1])) if isinstance(cover[0], str) else ('simple', tuple(cover))
        return doc

    def display_name(self):
        return os.path.splitext(os.path.basename(self.path))[0] if self.path else tr('未命名笔记')


def notebook_id(path):
    """The stable id of a notebook file, giving it one if it predates ids."""
    with zipfile.ZipFile(path) as z:
        if 'id' in z.namelist():
            return z.read('id').decode()
    new = uuid.uuid4().hex
    with zipfile.ZipFile(path, 'a') as z:
        z.writestr(zipfile.ZipInfo('id'), new)
    return new


def set_notebook_id(path, new):
    """Give a notebook file a different id (a file copied by hand shares its original's)."""
    tmp = path + '.tmp'
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename == 'id':
                continue
            data = src.read(info.filename)
            if info.filename == 'document.json':
                meta = json.loads(data)
                meta['id'] = new
                data = json.dumps(meta, separators=(',', ':'))
                dst.writestr('document.json', data)
            else:
                dst.writestr(info, data)
        dst.writestr(zipfile.ZipInfo('id'), new)
    os.replace(tmp, path)


def _cover_text(cr, x, y, title, size, width, ink, alpha=0.92):
    cr.set_source_rgba(*ink, alpha)
    ptext.show(cr, x, y, title, size, width)


def render_cover(spec, title, w=225, h=300):
    """PNG of a notebook cover in one of COVER_STYLES, carrying the title."""
    style, color = spec
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    light = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2] > 0.6
    ink = (0.12, 0.12, 0.14) if light else (1.0, 1.0, 1.0)
    dark = tuple(c * 0.78 for c in color)
    cr.set_source_rgb(*color)
    cr.paint()
    if style == 'grid':
        cr.set_source_rgba(*ink, 0.10)
        cr.set_line_width(0.6)
        step = w / 12
        for i in range(1, 12):
            cr.move_to(i * step, 0)
            cr.line_to(i * step, h)
        for i in range(1, int(h / step) + 1):
            cr.move_to(0, i * step)
            cr.line_to(w, i * step)
        cr.stroke()
        cr.set_source_rgb(*((1, 1, 1) if light else dark))
        cr.rectangle(w * 0.14, h * 0.12, w * 0.72, h * 0.20)
        cr.fill()
        _cover_text(cr, w * 0.19, h * 0.17, title, w * 0.075, w * 0.62, (0.12, 0.12, 0.14) if light else (1, 1, 1))
    elif style == 'band':
        cr.set_source_rgb(*dark)
        cr.rectangle(0, h * 0.66, w, h * 0.34)
        cr.fill()
        cr.set_source_rgba(*ink, 0.5)
        cr.rectangle(w * 0.14, h * 0.33, w * 0.16, 2.2)
        cr.fill()
        _cover_text(cr, w * 0.14, h * 0.37, title, w * 0.09, w * 0.76, ink)
    elif style == 'ribbon':
        # A loose ribbon looping across the cover, with a label for the title.
        accent = (0.22, 0.70, 0.84) if not light else tuple(c * 0.55 for c in color)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(w * 0.085)
        cr.set_source_rgb(*accent)
        cr.move_to(w * 0.20, -h * 0.05)
        cr.curve_to(-w * 0.10, h * 0.30, w * 0.45, h * 0.52, w * 0.52, h * 0.36)
        cr.curve_to(w * 0.58, h * 0.22, w * 0.30, h * 0.24, w * 0.42, h * 0.52)
        cr.curve_to(w * 0.60, h * 0.92, w * 0.95, h * 0.70, w * 1.08, h * 0.98)
        cr.stroke()
        cr.set_source_rgb(1, 1, 1)
        _round(cr, w * 0.16, h * 0.39, w * 0.68, h * 0.21, 4)
        cr.fill()
        _cover_text(cr, w * 0.21, h * 0.425, title, w * 0.07, w * 0.58, (0.12, 0.12, 0.14))
    elif style == 'label':
        cr.set_source_rgba(*ink, 0.28)
        cr.set_line_width(1)
        cr.rectangle(w * 0.09, h * 0.05, w * 0.84, h * 0.90)
        cr.stroke()
        cr.set_source_rgb(0.98, 0.97, 0.94)
        _round(cr, w * 0.20, h * 0.26, w * 0.62, h * 0.24, 3)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(0.8)
        _round(cr, w * 0.225, h * 0.275, w * 0.57, h * 0.21, 2)
        cr.stroke()
        _cover_text(cr, w * 0.26, h * 0.32, title, w * 0.072, w * 0.50, (0.12, 0.12, 0.14))
    else:
        left = w * 0.15
        _cover_text(cr, left, h * 0.13, title, w * 0.075, w * 0.75, ink)
        cr.set_source_rgba(*ink, 0.35)
        cr.set_line_width(1)
        cr.move_to(left, h * 0.30)
        cr.line_to(w * 0.9, h * 0.30)
        for i in range(3):
            cr.move_to(w * 0.72, h * (0.84 + i * 0.03))
            cr.line_to(w * 0.9, h * (0.84 + i * 0.03))
        cr.stroke()
    # Shared finish: a soft sheen and the pale spine.
    sheen = cairo.LinearGradient(0, 0, w, h)
    sheen.add_color_stop_rgba(0, 1, 1, 1, 0.10)
    sheen.add_color_stop_rgba(1, 0, 0, 0, 0.10)
    cr.set_source(sheen)
    cr.paint()
    cr.set_source_rgba(1, 1, 1, 0.92)
    cr.rectangle(0, 0, w * 0.045, h)
    cr.fill()
    cr.set_source_rgba(0, 0, 0, 0.16)
    cr.rectangle(w * 0.045, 0, 1.5, h)
    cr.fill()
    png = io.BytesIO()
    surface.write_to_png(png)
    return png.getvalue()


def _round(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -_TAU / 4, 0)
    cr.arc(x + w - r, y + h - r, r, 0, _TAU / 4)
    cr.arc(x + r, y + h - r, r, _TAU / 4, _TAU / 2)
    cr.arc(x + r, y + r, r, _TAU / 2, _TAU * 3 / 4)
    cr.close_path()


# -- export -----------------------------------------------------------------

def export_pdf(doc, path, pages=None, paper=True, ink=True):
    """Vector PDF: ink stays as paths, imported PDF pages keep their text and vectors.

    pages limits the export to those page indexes; paper and ink choose what is drawn.
    """
    surface = cairo.PDFSurface(path, *A4)
    for page in (doc.pages if pages is None else [doc.pages[i] for i in pages]):
        x0, y0, x1, y1 = page_box(page)
        # PDF pages cannot be larger than 200 inches; a sprawling whiteboard is scaled to fit.
        k = min(1.0, 14400 / max(x1 - x0, y1 - y0))
        surface.set_size((x1 - x0) * k, (y1 - y0) * k)
        cr = cairo.Context(surface)
        cr.scale(k, k)
        cr.translate(-x0, -y0)
        render_page(cr, page, printing=True, paper=paper, ink=ink)
        cr.show_page()
    surface.finish()


def page_surface(page, scale, size=None, paper=True, ink=True):
    """Raster of a page (a whiteboard's content); size=(w, h) letterboxes it into fixed pixels."""
    x0, y0, x1, y1 = page_box(page)
    if size is None:
        # An image has to stay a size that fits in memory, however far a whiteboard sprawls.
        scale = min(scale, 12000 / max(x1 - x0, y1 - y0))
        w, h = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
        sx, sy, dx, dy = w / (x1 - x0), h / (y1 - y0), 0.0, 0.0
    else:
        w, h = size
        sx = sy = min(w / (x1 - x0), h / (y1 - y0))
        dx, dy = (w - (x1 - x0) * sx) / 2, (h - (y1 - y0) * sy) / 2
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    if size is not None:
        cr.set_source_rgb(*page.color)
        cr.paint()
    cr.translate(dx, dy)
    cr.scale(sx, sy)
    cr.translate(-x0, -y0)
    render_page(cr, page, paper=paper, ink=ink)
    return surface


def export_png(page, path, scale=3.0, paper=True, ink=True):
    page_surface(page, scale, paper=paper, ink=ink).write_to_png(path)
