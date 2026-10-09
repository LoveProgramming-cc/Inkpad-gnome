#!/usr/bin/env python3
"""Draws the application icon (the same picture as the SVG) as a PNG: render_icon.py SIZE OUT."""
import math
import sys

import cairo


def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw(cr):
    rounded(cr, 12, 12, 104, 104, 24)
    cr.set_source_rgb(0x36 / 255, 0x60 / 255, 0x9f / 255)
    cr.fill()
    rounded(cr, 34, 24, 60, 80, 6)
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.move_to(44, 84)
    cr.rel_curve_to(8, -10, 12, 8, 20, -2)
    cr.rel_curve_to(8, -10, 10, -6, 18, -12)
    cr.set_source_rgb(0x4a / 255, 0x9e / 255, 0xf5 / 255)
    cr.set_line_width(5)
    cr.stroke()
    cr.move_to(96, 30)
    for dx, dy in ((10, 10), (-34, 34), (-13, 3), (3, -13)):
        cr.rel_line_to(dx, dy)
    cr.close_path()
    cr.set_source_rgb(1.0, 0xb0 / 255, 0x2e / 255)
    cr.fill_preserve()
    cr.set_source_rgb(0x2c / 255, 0x4a / 255, 0x7c / 255)
    cr.set_line_width(3)
    cr.stroke()


size = int(sys.argv[1])
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
context = cairo.Context(surface)
context.scale(size / 128, size / 128)
# The artwork is drawn 104 units wide; enlarge it to fill the icon like other apps' icons do.
context.translate(64, 64)
context.scale(1.16, 1.16)
context.translate(-64, -64)
draw(context)
surface.write_to_png(sys.argv[2])
