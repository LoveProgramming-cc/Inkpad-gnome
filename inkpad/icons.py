"""Toolbar icons.

The glyphs are from the Lucide icon set (https://lucide.dev, ISC licence),
embedded as SVG path data and drawn with cairo so the app needs no icon theme
and no gi-cairo bridge. Each icon is rasterised once per scale factor and used
as an alpha mask filled with the widget's CSS colour, so icons follow the
header, the checked state and dark mode.
"""
import colorsys
import math
import re

import cairo
from gi.repository import Graphene, Gsk, Gtk

from .render import texture_from_surface

_TAU = 2 * math.pi
_TOKEN = re.compile(r'([MmLlHhVvCcSsQqAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)')
_ARITY = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'A': 7, 'Z': 0}


def _svg_arc(cr, x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """An SVG elliptical arc from the current point (x1, y1) to (x2, y2)."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        cr.line_to(x2, y2)
        return
    phi = math.radians(phi)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    xp, yp = cosp * dx + sinp * dy, -sinp * dx + cosp * dy
    rx, ry = abs(rx), abs(ry)
    lam = xp * xp / (rx * rx) + yp * yp / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * yp * yp - ry * ry * xp * xp
    den = rx * rx * yp * yp + ry * ry * xp * xp
    k = math.sqrt(max(0.0, num / den)) * (-1 if large == sweep else 1)
    cxp, cyp = k * rx * yp / ry, -k * ry * xp / rx
    cx, cy = cosp * cxp - sinp * cyp + (x1 + x2) / 2, sinp * cxp + cosp * cyp + (y1 + y2) / 2
    a1 = math.atan2((yp - cyp) / ry, (xp - cxp) / rx)
    a2 = math.atan2((-yp - cyp) / ry, (-xp - cxp) / rx)
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(phi)
    cr.scale(rx, ry)
    (cr.arc if sweep else cr.arc_negative)(0, 0, 1, a1, a2)
    cr.restore()


def _svg_path(cr, d):
    tokens = _TOKEN.findall(d)
    i, n = 0, len(tokens)
    x = y = sx = sy = 0.0
    last_c = None  # previous cubic control point, for S
    cmd = None
    while i < n:
        if tokens[i][0]:
            cmd = tokens[i][0]
            i += 1
        up = cmd.upper()
        rel = cmd != up
        arity = _ARITY[up]
        a = [float(tokens[i + j][1]) for j in range(arity)]
        i += arity
        ox, oy = (x, y) if rel else (0.0, 0.0)
        ctrl = None
        if up == 'M':
            x, y = a[0] + ox, a[1] + oy
            sx, sy = x, y
            cr.move_to(x, y)
            cmd = 'l' if rel else 'L'  # further pairs are line-tos
        elif up == 'L':
            x, y = a[0] + ox, a[1] + oy
            cr.line_to(x, y)
        elif up == 'H':
            x = a[0] + ox
            cr.line_to(x, y)
        elif up == 'V':
            y = a[0] + oy
            cr.line_to(x, y)
        elif up == 'C':
            cr.curve_to(a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy, a[4] + ox, a[5] + oy)
            ctrl = (a[2] + ox, a[3] + oy)
            x, y = a[4] + ox, a[5] + oy
        elif up == 'S':
            c1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
            cr.curve_to(c1[0], c1[1], a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy)
            ctrl = (a[0] + ox, a[1] + oy)
            x, y = a[2] + ox, a[3] + oy
        elif up == 'Q':
            qx, qy, ex, ey = a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy
            cr.curve_to(x + (qx - x) * 2 / 3, y + (qy - y) * 2 / 3, ex + (qx - ex) * 2 / 3, ey + (qy - ey) * 2 / 3, ex, ey)
            x, y = ex, ey
        elif up == 'A':
            ex, ey = a[5] + ox, a[6] + oy
            _svg_arc(cr, x, y, a[0], a[1], a[2], a[3], a[4], ex, ey)
            x, y = ex, ey
        elif up == 'Z':
            cr.close_path()
            x, y = sx, sy
        last_c = ctrl
        if up == 'Z' and i < n and not tokens[i][0]:
            break


def _round_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -_TAU / 4, 0)
    cr.arc(x + w - r, y + h - r, r, 0, _TAU / 4)
    cr.arc(x + r, y + h - r, r, _TAU / 4, _TAU / 2)
    cr.arc(x + r, y + r, r, _TAU / 2, _TAU * 3 / 4)
    cr.close_path()


def _lucide(*elements, fill=False):
    """An icon from 24-unit Lucide elements: path strings, ('rect', x, y, w, h, r), ('circle', x, y, r)."""
    def draw(cr):
        cr.scale(20 / 24, 20 / 24)
        cr.set_line_width(2)
        for el in elements:
            if isinstance(el, str):
                _svg_path(cr, el)
            elif el[0] == 'rect':
                _round_rect(cr, *el[1:])
            else:
                cr.new_sub_path()
                cr.arc(el[1], el[2], el[3], 0, _TAU)
        if fill:
            cr.fill_preserve()
        cr.stroke()
    return draw


_PENCIL = ('M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622'
           'l4.353-1.32a2 2 0 0 0 .83-.497z')
_BOOKMARK = 'm19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z'
_STAR = 'M12 2 L15.09 8.26 L22 9.27 L17 14.14 L18.18 21.02 L12 17.77 L5.82 21.02 L7 14.14 L2 9.27 L8.91 8.26 Z'

pen = _lucide(_PENCIL, 'm15 5 4 4')
ball = _lucide('M12 20h9', 'M16.376 3.622a1 1 0 0 1 3.002 3.002L7.368 18.635a2 2 0 0 1-.855.506l-2.872.838'
               'a.5.5 0 0 1-.62-.62l.838-2.872a2 2 0 0 1 .506-.854z')
fountain = _lucide('M15.707 21.293a1 1 0 0 1-1.414 0l-1.586-1.586a1 1 0 0 1 0-1.414l5.586-5.586a1 1 0 0 1 1.414 0'
                   'l1.586 1.586a1 1 0 0 1 0 1.414z',
                   'm18 13-1.375-6.874a1 1 0 0 0-.746-.776L3.235 2.028a1 1 0 0 0-1.207 1.207L5.35 15.879'
                   'a1 1 0 0 0 .776.746L13 18', 'm2.3 2.3 7.286 7.286', ('circle', 11, 11, 2))
brush = _lucide('m9.06 11.9 8.07-8.06a2.85 2.85 0 1 1 4.03 4.03l-8.06 8.08',
                'M7.07 14.94c-1.66 0-3 1.35-3 3.02 0 1.33-2.5 1.52-2 2.02 1.08 1.1 2.49 2.02 4 2.02 2.2 0 4-1.8 4-4.04'
                'a3.01 3.01 0 0 0-3-3.02z')
highlighter = _lucide('m9 11-6 6v3h9l3-3', 'm22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4')
eraser = _lucide('m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21',
                 'M22 21H7', 'm5 11 9 9')
lasso = _lucide('M7 22a5 5 0 0 1-2-4', 'M3.3 14A6.8 6.8 0 0 1 2 10c0-4.4 4.5-8 10-8s10 3.6 10 8-4.5 8-10 8'
                'a12 12 0 0 1-5-1', ('circle', 5, 16, 2))
lasso_rect = _lucide('M5 3a2 2 0 0 0-2 2', 'M19 3a2 2 0 0 1 2 2', 'M21 19a2 2 0 0 1-2 2', 'M5 21a2 2 0 0 1-2-2',
                     'M9 3h1', 'M9 21h1', 'M14 3h1', 'M14 21h1', 'M3 9v1', 'M21 9v1', 'M3 14v1', 'M21 14v1')
shapes = _lucide('M8.3 10a.7.7 0 0 1-.626-1.079L11.4 3a.7.7 0 0 1 1.198-.043L16.3 8.9a.7.7 0 0 1-.572 1.1Z',
                 ('rect', 3, 14, 7, 7, 1), ('circle', 17.5, 17.5, 3.5))
text = _lucide('M4 7V4h16v3', 'M9 20h6', 'M12 4v16')
image = _lucide(('rect', 3, 3, 18, 18, 2), ('circle', 9, 9, 2), 'm21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21')
page_add = _lucide('M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z', 'M14 2v4a2 2 0 0 0 2 2h4',
                   'M9 15h6', 'M12 18v-6')
share = _lucide('M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8', 'm16 6-4-4-4 4', 'M12 2v13')
home = _lucide('M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8',
               'M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5'
               'a2 2 0 0 1-2-2z')
bookmark = _lucide(_BOOKMARK)
bookmark_filled = _lucide(_BOOKMARK, fill=True)
star = _lucide(_STAR)
star_filled = _lucide(_STAR, fill=True)
dots = _lucide(('circle', 5, 12, 1), ('circle', 12, 12, 1), ('circle', 19, 12, 1), fill=True)
more = _lucide(('circle', 12, 5, 1), ('circle', 12, 12, 1), ('circle', 12, 19, 1), fill=True)
grid = _lucide(('rect', 3, 3, 7, 7, 1), ('rect', 14, 3, 7, 7, 1), ('rect', 14, 14, 7, 7, 1), ('rect', 3, 14, 7, 7, 1))
outline = _lucide('M3 12h.01', 'M3 18h.01', 'M3 6h.01', 'M8 12h13', 'M8 18h13', 'M8 6h13')
trash = _lucide('M3 6h18', 'M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6', 'M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2',
                'M10 11v6', 'M14 11v6')
plus = _lucide('M5 12h14', 'M12 5v14')
minus = _lucide('M5 12h14')
chevron_right = _lucide('m9 18 6-6-6-6')
tape = _lucide(('rect', 2, 8, 20, 8, 1.5), 'M6 8v8', 'M18 8v8', 'M10 12h4')
ruler = _lucide('M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.41 2.41 0 0 1 0-3.4l2.6-2.6'
                'a2.41 2.41 0 0 1 3.4 0Z', 'm14.5 12.5 2-2', 'm11.5 9.5 2-2', 'm8.5 6.5 2-2', 'm17.5 15.5 2-2')
note = _lucide('M16 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8Z', 'M15 3v4a2 2 0 0 0 2 2h4')
search = _lucide(('circle', 11, 11, 8), 'm21 21-4.3-4.3')
close = _lucide('M18 6 6 18', 'm6 6 12 12')
up = _lucide('m18 15-6-6-6 6')
down = _lucide('m6 9 6 6 6-6')
check_circle = _lucide(('circle', 12, 12, 10), 'm9 12 2 2 4-4')
fit_all = _lucide('M8 3H5a2 2 0 0 0-2 2v3', 'M21 8V5a2 2 0 0 0-2-2h-3', 'M3 16v3a2 2 0 0 0 2 2h3',
                  'M16 21h3a2 2 0 0 0 2-2v-3', ('rect', 8, 8, 8, 8, 1))
scissors = _lucide(('circle', 6, 6, 3), 'M8.12 8.12 12 12', 'M20 4 8.12 15.88', ('circle', 6, 18, 3), 'M14.8 14.8 20 20')
camera = _lucide('M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z',
                 ('circle', 12, 13, 3))
to_front = _lucide(('rect', 8, 8, 8, 8, 2), 'M4 10a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2',
                   'M14 20a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-4a2 2 0 0 0-2-2')
to_back = _lucide(('rect', 14, 14, 8, 8, 2), ('rect', 2, 2, 8, 8, 2), 'M7 14v1a2 2 0 0 0 2 2h1',
                  'M14 7h1a2 2 0 0 1 2 2v1')
laser = _lucide('M2.6 18.4 9.5 11.5l3 3-6.9 6.9a2.1 2.1 0 0 1-3-3z', 'M7.5 13.5l3 3', ('circle', 15.5, 8.5, 1.4),
               'M15.5 2.5v2', 'M21.5 8.5h-2', 'M19.8 4.2l-1.4 1.4')
_COPY = 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2'
copy = _lucide(('rect', 8, 8, 14, 14, 2), _COPY)
duplicate = _lucide(('rect', 8, 8, 14, 14, 2), _COPY, 'M15 12v6', 'M12 15h6')
paste = _lucide(('rect', 8, 2, 8, 4, 1), 'M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2')
rotate_cw = _lucide('M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8', 'M21 3v5h-5')
info = _lucide(('circle', 12, 12, 10), 'M12 16v-4', 'M12 8h.01')
laser_dot = _lucide(('circle', 12, 12, 3.2), ('circle', 12, 12, 8))
laser_trail = _lucide('M3 15c3-8 6-8 8-3s5 5 7-2', ('circle', 19.5, 8, 1.6))
documents = _lucide('M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4'
                    'a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z')
reset = _lucide('M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5')
board = _lucide('M2 3h20', 'M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3', 'm7 21 5-5 5 5')
sidebar = _lucide(('rect', 3, 3, 18, 18, 2), 'M9 3v18')
undo = _lucide('M3 7v6h6', 'M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13')
redo = _lucide('M21 7v6h-6', 'M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3l3 2.7')
chevron = _lucide('m6 9 6 6 6-6')
back = _lucide('m15 18-6-6 6-6')
cover = _lucide('M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20')
cloud = _lucide('M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z')
syncing = _lucide('M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5',
                  'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5')
palette = _lucide('M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4'
                  'a1.75 1.75 0 0 1-1.4 2.8z', ('circle', 13.5, 6.5, .5), ('circle', 17.5, 10.5, .5),
                  ('circle', 6.5, 12.5, .5), ('circle', 8.5, 7.5, .5))
smile = _lucide(('circle', 12, 12, 10), 'M8 14s1.5 2 4 2 4-2 4-2', 'M9 9h.01', 'M15 9h.01')
rename = _lucide('M12 20h9', 'M16.376 3.622a1 1 0 0 1 3.002 3.002L7.368 18.635a2 2 0 0 1-.855.506l-2.872.838'
                 'a.5.5 0 0 1-.62-.62l.838-2.872a2 2 0 0 1 .506-.854z')
move = _lucide(('rect', 3, 3, 18, 18, 2), 'M8 12h8', 'm12 16 4-4-4-4')
restore = _lucide('M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5')
shape_line = _lucide('M5 19 19 5')
shape_arrow = _lucide('M7 7h10v10', 'M7 17 17 7')
shape_rect = _lucide(('rect', 3, 5, 18, 14, 2))
shape_ellipse = _lucide(('circle', 12, 12, 9))


# Glyphs offered for folders, drawn on the folder front (Lucide, 24-unit grid).
_FILE = 'M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z'
GLYPHS = {
    'apple': ['M12 20.94c1.5 0 2.75 1.06 4 1.06 3 0 6-8 6-12.22A4.91 4.91 0 0 0 17 5c-2.22 0-4 1.44-5 2-1-.56-2.78-2-5-2'
              'a4.9 4.9 0 0 0-5 4.78C2 14 5 22 8 22c1.25 0 2.5-1.06 4-1.06Z', 'M10 2c1 .5 2 2 2 5'],
    'atom': [('circle', 12, 12, 1),
             'M20.2 20.2c2.04-2.03.02-7.36-4.5-11.9-4.54-4.52-9.87-6.54-11.9-4.5-2.04 2.03-.02 7.36 4.5 11.9 '
             '4.54 4.52 9.87 6.54 11.9 4.5Z',
             'M15.7 15.7c4.52-4.54 6.54-9.87 4.5-11.9-2.03-2.04-7.36-.02-11.9 4.5-4.52 4.54-6.54 9.87-4.5 11.9 '
             '2.03 2.04 7.36.02 11.9-4.5Z'],
    'book': ['M12 7v14', 'M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13'
             'a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z'],
    'briefcase': ['M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16', ('rect', 2, 6, 20, 14, 2)],
    'calculator': [('rect', 4, 2, 16, 20, 2), 'M8 6h8', 'M16 14v4', 'M16 10h.01', 'M12 10h.01', 'M8 10h.01',
                   'M12 14h.01', 'M8 14h.01', 'M12 18h.01', 'M8 18h.01'],
    'chart': ['M3 3v16a2 2 0 0 0 2 2h16', 'M18 17V9', 'M13 17V5', 'M8 17v-3'],
    'code': ['m16 18 6-6-6-6', 'm8 6-6 6 6 6'],
    'gear': ['M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73'
             'l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73'
             'l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44'
             'a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39'
             'a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73'
             'l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
             ('circle', 12, 12, 3)],
    'bank': ['M3 22h18', 'M6 18v-7', 'M10 18v-7', 'M14 18v-7', 'M18 18v-7', 'm12 2 8 5H4z'],
    'money': [('rect', 2, 6, 20, 12, 2), ('circle', 12, 12, 2), 'M6 12h.01', 'M18 12h.01'],
    'flask': ['M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127'
              'A2 2 0 0 1 14 9.527V2', 'M8.5 2h7', 'M7 16h10'],
    'cap': ['M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908'
            'a2 2 0 0 0 1.66 0z', 'M22 10v6', 'M6 12.5V16a6 3 0 0 0 12 0v-3.5'],
    'scale': ['m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z', 'm2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z',
              'M7 21h10', 'M12 3v18', 'M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2'],
    'music': ['M9 18V5l12-2v13', ('circle', 6, 18, 3), ('circle', 18, 16, 3)],
    'moon': ['M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'],
    'file': [_FILE, 'M14 2v4a2 2 0 0 0 2 2h4', 'M10 9H8', 'M16 13H8', 'M16 17H8'],
    'palette': ['M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4'
                'a1.75 1.75 0 0 1-1.4 2.8z', ('circle', 13.5, 6.5, .5), ('circle', 17.5, 10.5, .5),
                ('circle', 6.5, 12.5, .5), ('circle', 8.5, 7.5, .5)],
    'smile': [('circle', 12, 12, 10), 'M8 14s1.5 2 4 2 4-2 4-2', 'M9 9h.01', 'M15 9h.01'],
    'compass': [('circle', 12, 5, 2), 'm12 7 7.5 13', 'm12 7-7.5 13', 'M4.5 17.5a11 11 0 0 0 15 0'],
    'globe': [('circle', 12, 12, 10), 'M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20', 'M2 12h20'],
    'laptop': ['M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62'
               'a1 1 0 0 1-.9-1.45L4 16'],
    'heart': ['M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2'
              'A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z'],
    'star': [_STAR],
    'pencil': [_PENCIL, 'm15 5 4 4'],
}


def draw_glyph(cr, name, x, y, size, width=2.0):
    """Stroke a folder glyph into a size x size box at (x, y), in the current colour."""
    cr.save()
    cr.translate(x, y)
    cr.scale(size / 24, size / 24)
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for el in GLYPHS[name]:
        if isinstance(el, str):
            _svg_path(cr, el)
        elif el[0] == 'rect':
            _round_rect(cr, *el[1:])
        else:
            cr.new_sub_path()
            cr.arc(el[1], el[2], el[3], 0, _TAU)
    cr.stroke()
    cr.restore()


def glyph(name):
    return lambda cr: draw_glyph(cr, name, 0, 0, 20)


none = _lucide('M5 19 19 5')
settings = _lucide(*GLYPHS['gear'])

def weight(width):
    def draw(cr):
        cr.set_line_width(width)
        cr.move_to(4, 10)
        cr.line_to(16, 10)
        cr.stroke()
    return draw


def dot(radius):
    def draw(cr):
        cr.arc(10, 10, radius, 0, _TAU)
        cr.stroke()
    return draw


def letter(scale):
    def draw(cr):
        cr.translate(10, 10.5)
        cr.scale(scale, scale)
        cr.move_to(-5, 6)
        cr.line_to(0, -6)
        cr.line_to(5, 6)
        cr.move_to(-3.2, 2)
        cr.line_to(3.2, 2)
        cr.stroke()
    return draw


def line_style(dash):
    def draw(cr):
        cr.set_line_width(1.8)
        if dash == 1:
            cr.set_dash([4, 3.2])
        elif dash == 2:
            cr.set_dash([0.01, 3.6])
        cr.move_to(2.5, 10)
        cr.line_to(17.5, 10)
        cr.stroke()
    return draw


def swatch(color, chevron=False):
    def draw(cr):
        cr.arc(10, 10, 8, 0, _TAU)
        cr.set_source_rgb(*color)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.set_line_width(1)
        cr.stroke()
        if chevron:
            # Marks the active colour: press it again to edit it.
            dark = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2] < 0.6
            cr.set_source_rgba(*((1, 1, 1, 0.95) if dark else (0, 0, 0, 0.6)))
            cr.set_line_width(1.5)
            cr.move_to(7, 9)
            cr.line_to(10, 12)
            cr.line_to(13, 9)
            cr.stroke()
    return draw


def folder(cr):
    cr.set_source_rgb(0.20, 0.68, 0.85)
    cr.move_to(1.5, 15.5)
    cr.line_to(1.5, 4.8)
    cr.curve_to(1.5, 4, 2, 3.5, 2.8, 3.5)
    cr.line_to(7.5, 3.5)
    cr.line_to(9.2, 5.3)
    cr.line_to(17.2, 5.3)
    cr.curve_to(18, 5.3, 18.5, 5.8, 18.5, 6.6)
    cr.line_to(18.5, 15.5)
    cr.close_path()
    cr.fill()
    cr.set_source_rgb(0.33, 0.84, 0.96)
    _round_rect(cr, 1.5, 7, 17, 10, 1.3)
    cr.fill()


def wheel(cr):
    for i in range(24):
        cr.move_to(10, 10)
        cr.arc(10, 10, 8, i * _TAU / 24, (i + 1.3) * _TAU / 24)
        cr.close_path()
        cr.set_source_rgb(*colorsys.hsv_to_rgb(i / 24, 0.9, 1.0))
        cr.fill()
    cr.arc(10, 10, 3, 0, _TAU)
    cr.set_source_rgb(1, 1, 1)
    cr.fill()


class TextureView(Gtk.Widget):
    """A fixed-size widget showing one texture, optionally tinted with the CSS colour."""

    def __init__(self, width, height, tint=False, **kwargs):
        super().__init__(**kwargs)
        self._size = (width, height)
        self._tint = tint
        self.texture = None

    def do_measure(self, orientation, for_size):
        size = self._size[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
        return (size, size, -1, -1)

    def get_texture(self):
        return self.texture

    def set_texture(self, texture):
        self.texture = texture
        self.queue_draw()

    def do_snapshot(self, snap):
        texture = self.get_texture()
        if texture is None:
            return
        rect = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
        if self._tint:
            snap.push_mask(Gsk.MaskMode.ALPHA)
            snap.append_texture(texture, rect)
            snap.pop()
            snap.append_color(self.get_color(), rect)
            snap.pop()
        else:
            snap.append_texture(texture, rect)


class Icon(TextureView):
    def __init__(self, draw, size=20, tint=True, **kwargs):
        kwargs.setdefault('halign', Gtk.Align.CENTER)
        kwargs.setdefault('valign', Gtk.Align.CENTER)
        super().__init__(size, size, tint, **kwargs)
        self._draw = draw
        self._scale = 0

    def get_texture(self):
        scale = self.get_scale_factor()
        if scale != self._scale:
            self._scale = scale
            px = self._size[0] * scale
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
            cr = cairo.Context(surface)
            cr.scale(px / 20, px / 20)
            cr.set_line_width(1.5)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            self._draw(cr)
            self.texture = texture_from_surface(surface)
        return self.texture
