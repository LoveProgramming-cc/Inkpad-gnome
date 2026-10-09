"""Reading Goodnotes documents (.goodnotes).

The format is not published; what is here was worked out from files. A document is a
zip archive:

  index.events.pb       a log of length-prefixed protobuf records describing the document:
                        its title, its page templates (a PDF attachment, a page of it, and
                        the size the template is laid out at) and its pages (which template,
                        and a sort key that gives the page order)
  index.notes.pb        page id -> the file with that page's content
  notes/<id>            length-prefixed protobuf records, a header and a body per element;
                        a later body for the same element replaces the earlier one, and one
                        marked as deleted removes it
  attachments/<id>      the PDFs that the pages are drawn on (paper templates included)

A stroke's geometry is an Apple-LZ4 compressed "tpl" block: a type string followed by the
values it describes. Three layouts occur: a constant-width path of quadratic curves, a
variable-width path (curves whose control and end points each carry a radius), and a third
one that lists points with radii. Only the centre line and the width are read; the cached
outline that follows is ignored.

What is imported: pages in order, their PDF backgrounds, pen strokes with colour and width,
text boxes and notes (plain text), and straight connectors. Anything else is skipped and counted.
"""
import math
import os
import struct
import zipfile

from .i18n import tr
from .model import BOARD_SIZE, Document, Image, Page, Shape, Stroke, Text
from .pdf import PdfDoc

# Goodnotes lays a template out larger than its PDF page; this is the factor when there is
# no PDF to measure it against.
FALLBACK_SCALE = 6 / 11
# The number stored with each point of a pressure-sensitive stroke is not the line's half
# width: measured against the outlines Goodnotes caches next to it, the line is about 1.42
# times that number wide.
PEN_WIDTH = 1.42


class GoodnotesError(Exception):
    pass


# -- protobuf, as much of it as is needed -----------------------------------

def _varint(b, i):
    value = shift = 0
    while True:
        c = b[i]
        i += 1
        value |= (c & 0x7f) << shift
        shift += 7
        if not c & 0x80:
            return value, i


def _fields(b):
    """(field number, wire type, value) for each field of a message."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(b, i)
        elif wire == 1:
            value, i = b[i:i + 8], i + 8
        elif wire == 2:
            size, i = _varint(b, i)
            value, i = b[i:i + size], i + size
        elif wire == 5:
            value, i = b[i:i + 4], i + 4
        else:
            raise ValueError('wire type %d' % wire)
        if i > n:
            raise ValueError('truncated message')
        yield number, wire, value


def _message(b):
    """A message as {field number: [values]}."""
    out = {}
    for number, _, value in _fields(b):
        out.setdefault(number, []).append(value)
    return out


def _records(b):
    i, n = 0, len(b)
    while i < n:
        size, i = _varint(b, i)
        yield b[i:i + size]
        i += size


def _first(message, number, default=None):
    values = message.get(number)
    return values[0] if values else default


def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else ''


def _vec(value, n):
    """A point, size or colour: float fields 1..n. A component that is zero is simply left
    out of the file, so the values have to be placed by field number, not by position."""
    out = [0.0] * n
    for number, wire, v in _fields(value or b''):
        if wire == 5 and 1 <= number <= n:
            out[number - 1] = struct.unpack('<f', v)[0]
    return out


# -- Apple's LZ4 framing ----------------------------------------------------

def _lz4_block(src):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                c = src[i]
                i += 1
                literal += c
                if c != 255:
                    break
        out += src[i:i + literal]
        i += literal
        if i >= n:
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        length = token & 15
        if length == 15:
            while True:
                c = src[i]
                i += 1
                length += c
                if c != 255:
                    break
        length += 4
        start = len(out) - offset
        if offset >= length:
            out += out[start:start + length]
        else:   # the match overlaps what it produces
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


def _decompress(b):
    """Blocks tagged bv41 (LZ4) or bv4- (stored), ended by bv4$."""
    out = bytearray()
    i = 0
    while i + 4 <= len(b):
        magic = b[i:i + 4]
        i += 4
        if magic == b'bv41':
            _, size = struct.unpack_from('<II', b, i)
            i += 8
            out += _lz4_block(b[i:i + size])
            i += size
        elif magic == b'bv4-':
            size, = struct.unpack_from('<I', b, i)
            i += 4
            out += b[i:i + size]
            i += size
        elif magic == b'bv4$':
            break
        else:
            raise ValueError('not an LZ4 stream')
    return bytes(out)


# -- "tpl" blocks -----------------------------------------------------------

def _signature(sig, i=0):
    """The type string as a tree: v u f are scalars, A(..) an array, S(..) a struct."""
    out = []
    while i < len(sig) and sig[i] != ')':
        c = sig[i]
        if c in 'AS':
            inner, i = _signature(sig, i + 2)
            out.append((c, inner))
            i += 1
        else:
            out.append(c)
            i += 1
    return out, i


def _read(types, d, i):
    values = []
    for t in types:
        if t == 'v':
            values.append(struct.unpack_from('<H', d, i)[0])
            i += 2
        elif t in ('u', 'f'):
            values.append(struct.unpack_from('<f', d, i)[0])
            i += 4
        elif t[0] == 'A':
            count, = struct.unpack_from('<I', d, i)
            i += 4
            inner = t[1]
            if inner == ['v']:
                values.append(struct.unpack_from('<%dH' % count, d, i))
                i += 2 * count
            elif inner in (['u'], ['f']):
                values.append(struct.unpack_from('<%df' % count, d, i))
                i += 4 * count
            else:
                items = []
                for _ in range(count):
                    item, i = _read(inner, d, i)
                    items.append(item)
                values.append(items)
        else:
            item, i = _read(t[1], d, i)
            values.append(item)
    return values, i


def _tpl(raw):
    if raw[:4] != b'tpl\0':
        raise ValueError('not a tpl block')
    end = raw.index(b'\0', 8)
    sig = raw[8:end].decode('ascii')
    return sig, _read(_signature(sig)[0], raw, end + 1)[0]


# -- strokes ----------------------------------------------------------------

def _quad(points, p0, c, p1):
    """Add the middle and the end of a quadratic curve to a polyline of (x, y, width)."""
    points.append(tuple(0.25 * a + 0.5 * b + 0.25 * e for a, b, e in zip(p0, c, p1)))
    points.append(p1)


def _factor(k):
    """A width factor, or 1 where the file holds something that is not one."""
    return k if 0.05 < k < 8 else 1.0


def _paths(raw):
    """A stroke's centre lines as [(tool hint, [(x, y, width), ...]), ...]; width is a diameter."""
    sig, v = _tpl(raw)
    paths = []
    if sig.startswith('vuA(v)A(S(uu))A(S(uuuu))'):
        # Constant width: op 0 starts at the next point, op 1 is a curve (control, end).
        width, ops, moves, curves = v[1], v[2], v[3], v[4]
        mi = ci = 0
        current = None
        for op in ops:
            if op == 0 and mi < len(moves):
                x, y = moves[mi][0]
                mi += 1
                current = [(x, y, width)]
                paths.append(('ball', current))
            elif op == 1 and ci < len(curves) and current is not None:
                cx, cy, x, y = curves[ci][0]
                ci += 1
                _quad(current, current[-1], (cx, cy, width), (x, y, width))
    elif sig.startswith('vA(v)A(u)A(u)'):
        # Variable width: op 4 starts at (x, y, radius, ...), op 5 is a curve whose control
        # and end point each carry a radius.
        ops, moves, curves = v[1], v[2], v[3]
        mi = ci = 0
        current = None
        for op in ops:
            if op == 4 and mi + 4 <= len(moves):
                x, y, r = moves[mi:mi + 3]
                mi += 4
                current = [(x, y, PEN_WIDTH * r)]
                paths.append(('fountain', current))
            elif op == 5 and ci + 9 <= len(curves) and current is not None:
                cx, cy, cr, x, y, r = curves[ci:ci + 6]
                ci += 9
                _quad(current, current[-1], (cx, cy, PEN_WIDTH * cr), (x, y, PEN_WIDTH * r))
    elif sig.startswith('vuA(v)A(u)A(u)'):
        # A brush stroke, in up to two stretches that share the two arrays. It starts with a
        # taper of overlapping dabs: op 2 is a dab's centre (x, y, size) and op 3 its extent,
        # which adds nothing to the centre line. It may go on at a constant width, op 0 (x, y)
        # and op 1 (control, end), or as a broad nib with a size at every point: op 6
        # (x, y, size, ..) and op 7 (control, end, their sizes, three more numbers).
        base, ops, points, curves = v[1], v[2], v[3], v[4]
        # Compared with Goodnotes' own rendering, a dab's size is the width of the line there,
        # and the constant width is the pen's nominal size, which is PEN_WIDTH times what is
        # drawn. A nib is a slanted edge about three quarters of its size long: the line is
        # as wide as the edge is across the direction of travel, and never thinner than six
        # tenths of it. Where the file gives the edge, each point gets its own width below.
        k, nib = 1.0, 0.74
        if base > 0:
            base /= PEN_WIDTH
        edge = None
        if len(v) > 10 and isinstance(v[10], (tuple, list)) and len(v[10]) >= 7 and v[10][3] > 0:
            edge = (v[10][4], v[10][5])
            nib = min(1.0, 2 * math.hypot(*edge) / v[10][3]) or 0.74
        broad = []   # the paths drawn with the nib
        pi = ci = 0
        current = None
        tail = None   # where the last dab reaches to, used when nothing follows the taper

        def to(x, y, w):
            nonlocal current
            if current is None:
                current = [(x, y, w)]
                paths.append(('fountain', current))
            elif abs(current[-1][0] - x) + abs(current[-1][1] - y) > 0.01:
                current.append((x, y, w))

        for op in ops:
            if op == 2 and pi + 3 <= len(points):
                x, y, r = points[pi:pi + 3]
                pi += 3
                to(x, y, k * r)
            elif op == 3 and ci + 6 <= len(curves):
                tail = (curves[ci + 2], curves[ci + 3], k * curves[ci + 5])
                ci += 6
            elif op == 0 and pi + 2 <= len(points):
                x, y = points[pi:pi + 2]
                pi += 2
                to(x, y, base if base > 0 else (current[-1][2] if current else 1.0))
                tail = None
            elif op == 1 and ci + 4 <= len(curves) and current is not None:
                cx, cy, x, y = curves[ci:ci + 4]
                ci += 4
                w = base if base > 0 else current[-1][2]
                _quad(current, current[-1], (cx, cy, w), (x, y, w))
            elif op == 6 and pi + 4 <= len(points):
                x, y, r = points[pi:pi + 3]
                pi += 4
                if nib < 1 and current is not None:   # the taper before it is in the same unit
                    current[:] = [(px, py, nib * w) for px, py, w in current]
                to(x, y, nib * r)
                if not any(current is b for b in broad):
                    broad.append(current)
                tail = None
            elif op == 7 and ci + 9 <= len(curves) and current is not None:
                cx, cy, x, y, cr, r = curves[ci:ci + 6]
                ci += 9
                _quad(current, current[-1], (cx, cy, nib * cr), (x, y, nib * r))
            else:
                raise ValueError('unknown op %d in a brush stroke' % op)
        if tail is not None and current is not None:
            to(*tail)
        if current is not None and len(current) == 1:
            current.append((current[0][0] + 0.01, current[0][1], current[0][2]))
        if edge is not None and math.hypot(*edge) > 0:
            ex, ey = edge[0] / math.hypot(*edge), edge[1] / math.hypot(*edge)
            for pts in broad:
                for i in range(len(pts)):
                    a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    d = math.hypot(dx, dy)
                    across = abs(ex * dy - ey * dx) / d if d > 0 else 1.0
                    pts[i] = (pts[i][0], pts[i][1], pts[i][2] * max(0.6, across))
    elif sig.startswith('vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))'):
        # A base width, and points that each carry a factor for it: op 0 starts at
        # (x, y, factor, ..), op 1 is a curve (.., control with its factor, end with its factor).
        width, ops, moves, curves = v[1], v[2], v[3], v[4]
        mi = ci = 0
        current = None
        for op in ops:
            if op == 0 and mi < len(moves):
                x, y, k = moves[mi][0][:3]
                mi += 1
                current = [(x, y, width * _factor(k))]
                paths.append(('fountain', current))
            elif op == 1 and ci < len(curves) and current is not None:
                c = curves[ci][0]
                ci += 1
                _quad(current, current[-1], (c[1], c[2], width * _factor(c[3])), (c[6], c[7], width * _factor(c[8])))
    else:
        raise ValueError('unknown stroke layout ' + sig)
    for _, pts in paths:
        if any(not (abs(x) < 1e7 and abs(y) < 1e7 and 0 <= w < 400) for x, y, w in pts):
            raise ValueError('stroke read as nonsense')   # better none than a blot over the page
    return [(tool, pts) for tool, pts in paths if pts]


def _shape_paths(shape):
    """The outline of a recognised shape, which is stored as its geometry instead of as a
    drawn path: a polyline, a curve, a rectangle or an ellipse."""
    m = _message(shape)
    width = _vec(shape, 15)[14] or 2.0
    pts = []
    corners = False
    if 1 in m:
        pts = [tuple(_vec(p, 2)) for p in _message(m[1][0]).get(1, [])]
        if len(pts) == 1:
            pts *= 2
        corners = True
    elif 2 in m:
        # The middle point is taken to be the curve's control point; the file does not say.
        g = _message(m[2][0])
        a, c, b = (_vec(_first(g, k), 2) for k in (1, 2, 3))
        for i in range(17):
            t = i / 16
            pts.append(tuple((1 - t) ** 2 * p + 2 * t * (1 - t) * q + t * t * r for p, q, r in zip(a, c, b)))
    elif 3 in m:
        g = _message(m[3][0])
        (cx, cy), (w, h) = _vec(_first(g, 1), 2), _vec(_first(g, 2), 2)
        pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2),
               (cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 2)]
        corners = True
    elif 4 in m:
        g = _message(m[4][0])
        (cx, cy), (rx, ry) = _vec(_first(g, 1), 2), _vec(_first(g, 2), 2)
        turn = _vec(m[4][0], 3)[2]
        cos, sin = math.cos(turn), math.sin(turn)
        for i in range(49):
            a = i * math.tau / 48
            x, y = rx * math.cos(a), ry * math.sin(a)
            pts.append((cx + x * cos - y * sin, cy + x * sin + y * cos))
    if corners:
        # Inkpad rounds a stroke through its points; a point given twice stays a sharp corner.
        pts = pts[:1] + [p for p in pts[1:-1] for _ in (0, 1)] + pts[-1:]
    dashed = 1 in _message(_first(m, 5, b''))
    return [('dash' if dashed else 'ball', [(x, y, width) for x, y in pts])] if len(pts) > 1 else []


def _frame(body):
    """An element's bounding box (x0, y0, x1, y1), from its origin and size."""
    frame = _message(_first(body, 2, b''))
    x, y = _vec(_first(frame, 1), 2)
    w, h = _vec(_first(frame, 2), 2)
    return x - 4, y - 4, x + w + 4, y + h + 4


def _strokes(body, scale, data_field=2, color_field=4, within=None):
    """Inkpad strokes for one stroke element. within drops paths outside a box: a formula's
    parts include helper strokes in a coordinate system of their own."""
    data = _first(body, data_field)
    if not data:
        return []
    rgba = _vec(_first(body, color_field), 4)
    color, alpha = tuple(max(0.0, min(1.0, c)) for c in rgba[:3]), rgba[3]
    dx, dy = _vec(_first(body, 6), 2)   # where the stroke was moved to after it was drawn
    marker = bool(_first(body, 5))   # a highlighter: drawn in full colour, blended under the text
    out = []
    paths = _paths(_decompress(data))
    if not paths and _first(body, 9):
        paths = _shape_paths(_first(body, 9))
    for tool, pts in paths:
        if within and not all(within[0] <= x <= within[2] and within[1] <= y <= within[3] for x, y, _ in pts):
            continue
        pts = [((x + dx) * scale, (y + dy) * scale, w * scale) for x, y, w in pts]
        widths = [w for _, _, w in pts]
        mean = max(0.2, sum(widths) / len(widths))
        if marker or alpha < 0.9:
            out.append(Stroke('hl', color, mean, [(x, y, 0.5) for x, y, _ in pts]))
        elif tool == 'fountain' and max(widths) - min(widths) > 0.02:
            # Inkpad's fountain pen draws width * (0.4 + 1.2 * pressure): turn each width
            # back into the pressure that gives it.
            out.append(Stroke('fountain', color, mean,
                              [(x, y, max(0.0, min(1.0, (w / mean - 0.4) / 1.2))) for x, y, w in pts], 1.0))
        else:
            out.append(Stroke('ball', color, mean, [(x, y, 0.5) for x, y, _ in pts], dash=int(tool == 'dash')))
    return out


def _runs(packed):
    """The plain text of a compressed rich-text block."""
    text = ''.join(_text(_first(_message(run), 1, b'')) for run in _message(_decompress(packed)).get(1, []))
    return text.strip('\n')


def _text_box(body, scale):
    """A text box as plain text; its styling is not read."""
    content = _message(_first(body, 32, b''))
    packed = _first(_message(_first(content, 1, b'')), 2)
    text = _runs(packed) if packed else ''
    if not text.strip():
        return None
    x, y = _vec(_first(_message(_first(body, 20, b'')), 1), 2)
    layout = _message(_first(body, 21, b''))
    width = _vec(_first(layout, 2), 1)[0] if 2 in layout else _vec(_first(content, 2), 1)[0]
    style = _message(_first(_message(_first(content, 5, b'')), 1, b''))
    font = struct.unpack('<f', _first(style, 40, struct.pack('<f', 18.0)))[0]
    pad = 10 * scale   # the box's inner margin
    return Text(x * scale + pad, y * scale + pad, text, max(6.0, font * scale), (0.1, 0.1, 0.1),
                max(60.0, (width or 400.0) * scale - 2 * pad))


def _sticky(body, scale):
    """A comment or sticky note: its text on a slip of its colour."""
    packed = _first(_message(_first(_message(_first(body, 31, b'')), 1, b'')), 2)
    text = _runs(packed) if packed else ''
    if not text.strip():
        return None
    x, y = _vec(_first(_message(_first(body, 20, b'')), 1), 2)
    color = tuple(max(0.0, min(1.0, c)) for c in _vec(_first(body, 30), 3))
    layout = _message(_first(body, 21, b''))
    width = _vec(_first(layout, 2), 1)[0] * scale if 2 in layout else 0.0
    return Text(x * scale + 8, y * scale + 8, text, 11.0, (0.15, 0.15, 0.15), max(160.0, width - 16), color)


def _unrtf(rtf):
    """The text of an RTF fragment, without its formatting."""
    out, i, depth, skip = [], 0, 0, None
    while i < len(rtf):
        c = rtf[i]
        if c == '{':
            depth += 1
            if skip is None and rtf.startswith(('{\\fonttbl', '{\\colortbl', '{\\*', '{\\stylesheet', '{\\info'), i):
                skip = depth
            i += 1
        elif c == '}':
            if skip == depth:
                skip = None
            depth -= 1
            i += 1
        elif c == '\\':
            j = i + 1
            if rtf.startswith("'", j):   # \'hh: a byte in the document's code page
                if skip is None:
                    try:
                        out.append(bytes([int(rtf[j + 1:j + 3], 16)]).decode('cp1252', 'replace'))
                    except ValueError:
                        pass
                i = j + 3
            elif j < len(rtf) and not rtf[j].isalpha():   # an escaped character, or a line break
                if skip is None:
                    out.append('\n' if rtf[j] == '\n' else rtf[j])
                i = j + 1
            else:
                k = j
                while k < len(rtf) and rtf[k].isalpha():
                    k += 1
                word = rtf[j:k]
                m = k
                while m < len(rtf) and (rtf[m].isdigit() or rtf[m] == '-'):
                    m += 1
                if skip is None:
                    if word in ('par', 'line'):
                        out.append('\n')
                    elif word == 'tab':
                        out.append('\t')
                    elif word == 'uc':
                        pass
                    elif word == 'u' and m > k:
                        out.append(chr(int(rtf[k:m]) % 65536))
                        if m < len(rtf) and rtf[m] == ' ':
                            m += 1
                        if m < len(rtf) and rtf[m] not in '\\{}':   # the fallback character
                            m += 1
                        i = m
                        continue
                i = m + 1 if m < len(rtf) and rtf[m] == ' ' else m
        else:
            if skip is None and c not in '\r\n':
                out.append(c)
            i += 1
    # Characters outside the basic plane arrive as two halves; put them together.
    return ''.join(out).encode('utf-16', 'surrogatepass').decode('utf-16', 'replace').strip()


def _rtf_box(body, scale):
    """An older kind of text box, stored as RTF with its frame."""
    text = _unrtf(_text(_first(body, 6, b'')))
    if not text:
        return None
    frame = _message(_first(body, 3, b'') or _first(body, 2, b''))
    x, y = _vec(_first(frame, 1), 2)
    w = _vec(_first(frame, 2), 2)[0]
    return Text(x * scale, y * scale, text, 11.0, (0.1, 0.1, 0.1), max(60.0, w * scale))


def _line(body, scale):
    """A straight connector between two points."""
    geometry = _message(_first(body, 20, b''))
    x0, y0 = _vec(_first(geometry, 2), 2)
    x1, y1 = _vec(_first(_message(_first(geometry, 3, b'')), 1), 2)
    style = _message(_first(body, 32, b''))
    width = _vec(_first(body, 32), 1)[0] or 2.0
    color = _vec(_first(_message(_first(style, 3, b'')), 1), 3)
    return Shape('line', x0 * scale, y0 * scale, x1 * scale, y1 * scale,
                 tuple(max(0.0, min(1.0, c)) for c in color), max(0.3, width * scale))


# -- the document -----------------------------------------------------------

IMAGE_LIMIT = 2400   # pixels along the longer side of an imported picture


def _png(data):
    """A picture file as PNG bytes, which is what Inkpad keeps; None when it cannot be read."""
    try:
        import gi
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        longest = max(pixbuf.get_width(), pixbuf.get_height())
        if longest > IMAGE_LIMIT:
            k = IMAGE_LIMIT / longest
            pixbuf = pixbuf.scale_simple(max(1, round(pixbuf.get_width() * k)), max(1, round(pixbuf.get_height() * k)),
                                         GdkPixbuf.InterpType.BILINEAR)
        ok, out = pixbuf.save_to_bufferv('png', [], [])
        return bytes(out) if ok else None
    except Exception:
        return None


def _image(body, scale, archive, attachments, names):
    """A picture placed on the page: its frame and the attachment that holds the file."""
    name = attachments.get(_text(_first(body, 4, b'')))
    if name not in names:
        return None
    frame = _message(_first(body, 2, b''))
    x, y = _vec(_first(frame, 1), 2)
    w, h = _vec(_first(frame, 2), 2)
    png = _png(archive.read(name)) if w > 0 and h > 0 else None
    return Image(x * scale, y * scale, w * scale, h * scale, png) if png else None


def _made(element):
    try:
        stamp = _first(element[1], 5 if element[0] == 1 else 7, b'')   # a picture keeps it elsewhere
        number = _first(_message(_first(_message(stamp), 1, b'')), 1, 0) if isinstance(stamp, bytes) else 0
        return number if isinstance(number, int) else 0
    except (ValueError, IndexError, TypeError):
        return 0


def _notes_id(page_id):
    """A page's content is stored under its own id with the last byte one higher."""
    try:
        return '%s%02X' % (page_id[:-2], (int(page_id[-2:], 16) + 1) & 0xff)
    except ValueError:
        return page_id


def load(path):
    """Read a .goodnotes file; returns (Document, title, {'strokes', 'texts', 'skipped'})."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise GoodnotesError(tr('不是 Goodnotes 文档'))
    names = set(archive.namelist())
    if 'index.events.pb' not in names or 'index.notes.pb' not in names:
        raise GoodnotesError(tr('不是 Goodnotes 文档'))

    title = None
    templates = {}   # id -> (attachment id, PDF page number from 1, (width, height))
    pages = {}       # id -> [template id, sort key]
    for record in _records(archive.read('index.events.pb')):
        for number, wire, value in _fields(record):
            if wire != 2 or number == 1:
                continue
            try:
                m = _message(value)
            except (ValueError, IndexError):
                continue
            if number == 30:
                title = _text(_first(_message(_first(m, 2, b'')), 1, b'')) or title
            elif number == 2:
                size = _vec(_first(m, 8), 2)
                templates[_text(_first(m, 2))] = (_text(_first(m, 4, b'')), _first(m, 5, 1),
                                                 tuple(size) if size[0] > 0 and size[1] > 0 else None)
            elif number == 54:
                page = pages.setdefault(_text(_first(m, 2)), [None, ''])
                template = _text(_first(_message(_first(m, 3, b'')), 1, b''))
                key = _text(_first(_message(_first(m, 4, b'')), 1, b''))
                page[0], page[1] = template or page[0], key or page[1]
            elif number == 55:
                # A page that was moved gets a new sort key.
                key = _text(_first(_message(_first(m, 3, b'')), 1, b''))
                page = pages.get(_text(_first(m, 2)))
                if page is not None and key:
                    page[1] = key

    files = {}
    for index, table in (('index.notes.pb', files),):
        for record in _records(archive.read(index)):
            m = _message(record)
            table[_text(_first(m, 1))] = _text(_first(m, 2))
    attachments = {}
    if 'index.attachments.pb' in names:
        for record in _records(archive.read('index.attachments.pb')):
            m = _message(record)
            attachments[_text(_first(m, 1))] = _text(_first(m, 2))

    pdfs = {}

    def pdf_for(attachment):
        if attachment not in pdfs:
            pdfs[attachment] = None
            name = attachments.get(attachment)
            if name in names:
                data = archive.read(name)
                if data[:5] == b'%PDF-':
                    try:
                        pdfs[attachment] = PdfDoc(data)
                    except Exception:
                        pass
        return pdfs[attachment]

    doc = Document()
    doc.pages = []
    stats = {'strokes': 0, 'texts': 0, 'shapes': 0, 'formulas': 0, 'images': 0, 'skipped': 0}
    for page_id, (template, _) in sorted(pages.items(), key=lambda item: item[1][1]):
        attachment, number, size = templates.get(template, ('', 1, None))
        pdf = pdf_for(attachment) if attachment else None
        if pdf is not None and 1 <= number <= pdf.n_pages:
            w, h = pdf.page_size(number - 1)
            page = Page(w, h, pdf=pdf, pdf_page=number - 1)
            scale = w / size[0] if size and size[0] > 0 else FALLBACK_SCALE
        elif size:
            scale = FALLBACK_SCALE
            page = Page(size[0] * scale, size[1] * scale)
        else:
            # No template and no size: a whiteboard, which has no edges in Inkpad either.
            scale = FALLBACK_SCALE
            page = Page(BOARD_SIZE[0], BOARD_SIZE[1], 'dots', board=True)

        name = files.get(_notes_id(page_id)) or files.get(page_id)
        elements = {}   # element id -> (kind, body); a later record replaces an earlier one
        if name in names:
            for record in _records(archive.read(name)):
                try:
                    fields = list(_fields(record))
                    number, wire, value = fields[0]
                    if wire != 2 or (number == 1 and len(fields) > 1):
                        continue   # the header that precedes each element
                    body = _message(value)
                    if number == 1 and not isinstance(_first(body, 1), bytes):
                        continue
                except (ValueError, IndexError, StopIteration):
                    continue
                elements[_text(_first(body, 1, b''))] = (number, body)
        # Elements are numbered as they were made; later ones lie on top of earlier ones.
        for kind, body in sorted(elements.values(), key=_made):
            if _first(body, 14):   # erased or removed
                continue
            try:
                if kind == 7:
                    strokes = _strokes(body, scale)
                    page.items += strokes
                    stats['strokes'] += len(strokes)
                elif kind in (21, 20, 8):
                    text = {21: _text_box, 20: _sticky, 8: _rtf_box}[kind](body, scale)
                    if text is not None:
                        page.items.append(text)
                        stats['texts'] += 1
                elif kind == 11:
                    # Handwriting that Goodnotes shows typeset as a formula. Inkpad cannot
                    # typeset, so the handwriting it was made from is kept instead.
                    if _first(body, 6):
                        continue   # a conversion that was undone: its handwriting is on the page as strokes
                    for part in body.get(11, []):
                        strokes = _strokes(_message(part), scale, 1, 2, _frame(body))
                        page.items += strokes
                        stats['strokes'] += len(strokes)
                    stats['formulas'] += 1
                elif kind == 1:
                    if max(_vec(_first(_message(_first(body, 2, b'')), 2), 2)) < 1:
                        continue   # a speck too small to see, which Goodnotes leaves as a marker
                    image = _image(body, scale, archive, attachments, names)
                    if image is not None:
                        page.items.append(image)
                        stats['images'] += 1
                    else:
                        stats['skipped'] += 1
                elif kind == 9:
                    pass   # a region marked for some tool; it shows nothing on the page
                elif kind == 22:
                    page.items.append(_line(body, scale))
                    stats['shapes'] += 1
                else:
                    stats['skipped'] += 1
            except Exception:
                stats['skipped'] += 1
        doc.pages.append(page)

    # Inkpad treats a document as either pages or whiteboards, going by its first page.
    if any(p.board for p in doc.pages) and not all(p.board for p in doc.pages):
        doc.pages = [p for p in doc.pages if p.board == doc.pages[0].board]
    if not doc.pages:
        raise GoodnotesError(tr('这个 Goodnotes 文档里没有可以读取的页面'))
    doc.rev = 1   # not on disk yet
    return doc, title or os.path.splitext(os.path.basename(path))[0], stats


# -- writing ----------------------------------------------------------------
#
# Experimental. The records below copy the shape of what Goodnotes itself writes, but the
# meaning of several of their fields (the counters and hashes that look like sync clocks) is
# a guess, and no Goodnotes app was available to open the result. What is certain is only
# that load() reads it back.

def _enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _f(number, value):
    """One field: an int is a varint, a float a float32, bytes or str length-delimited."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return _enc_varint(number << 3) + _enc_varint(value)
    if isinstance(value, float):
        return _enc_varint(number << 3 | 5) + struct.pack('<f', value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    return _enc_varint(number << 3 | 2) + _enc_varint(len(value)) + value


def _f64(number, value):
    return _enc_varint(number << 3 | 1) + struct.pack('<d', value)


def _record(message):
    return _enc_varint(len(message)) + message


def _stamp(counter=1):
    import random
    return _f(1, counter) + _f(2, random.getrandbits(32))


def _new_id():
    import uuid
    return str(uuid.uuid4()).upper()


def _pack_stroke(points, width):
    """A constant-width path in the simplest of the three layouts, stored uncompressed."""
    ops = [0] + [1] * (len(points) - 1)
    data = struct.pack('<H', 2) + struct.pack('<f', width)
    data += struct.pack('<I', len(ops)) + struct.pack('<%dH' % len(ops), *ops)
    data += struct.pack('<I', 1) + struct.pack('<2f', *points[0])
    data += struct.pack('<I', len(points) - 1)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        # A straight piece as a curve whose control point is its middle.
        data += struct.pack('<4f', (x0 + x1) / 2, (y0 + y1) / 2, x1, y1)
    data += struct.pack('<H', 1) + struct.pack('<I', 0)
    sig = b'vuA(v)A(S(uu))A(S(uuuu))vA(f)\0'
    raw = b'tpl\0' + struct.pack('<I', 8 + len(sig) + len(data)) + sig + data
    return b'bv4-' + struct.pack('<I', len(raw)) + raw + b'bv4$'


def save(doc, path, title):
    """Write an Inkpad document as a .goodnotes file (experimental, see above).

    Each page's paper, PDF, pictures, shapes and text go into one background PDF; pen and
    highlighter strokes are written as strokes, at their average width.
    """
    import io
    import random
    import time

    import cairo

    from .model import page_box, render_page

    now = time.time()
    device = random.getrandbits(62)
    clock = [int(now * 1000)]

    def tick():
        clock[0] += 1
        return clock[0]

    doc_id, attachment = _new_id(), _new_id()
    up = 1 / FALLBACK_SCALE   # Goodnotes lays pages out this much larger than their PDF

    # The background of every page, without its strokes, as one PDF.
    buf = io.BytesIO()
    surface = cairo.PDFSurface(buf, 595, 842)
    boxes = []
    for page in doc.pages:
        x0, y0, x1, y1 = page_box(page)
        k = min(1.0, 14400 / max(x1 - x0, y1 - y0))
        boxes.append((x0, y0, k, (x1 - x0) * k, (y1 - y0) * k))
        surface.set_size((x1 - x0) * k, (y1 - y0) * k)
        cr = cairo.Context(surface)
        cr.scale(k, k)
        cr.translate(-x0, -y0)
        strokes, page.items = page.items, [it for it in page.items if it.kind != 's']
        try:
            render_page(cr, page, printing=True)
        finally:
            page.items = strokes
        cr.show_page()
    surface.finish()
    pdf = buf.getvalue()

    events = [_record(_f(1, doc_id) + _f(30, (
        _f(1, doc_id) + _f(2, _f(1, title) + _f(2, _stamp())) + _f(6, _f(1, 'P') + _f(2, _stamp()))
        + _f(9, 'en_US') + _f64(10, now * 1000) + _f(11, _new_id()) + _f(13, device) + _f(14, tick())
        + _f(17, b'') + _f(18, b'') + _f(19, _f(2, _stamp())) + _f(20, 24))))]
    events.append(_record(_f(1, attachment) + _f(6, (
        _f(1, attachment) + _f(2, attachment) + _f(5, len(pdf)) + _f(6, doc_id) + _f64(10, now * 1000)
        + _f(11, _new_id()) + _f(12, _f(1, 1) + _f(2, 1)) + _f(14, device) + _f(15, tick()) + _f(16, 24)))))

    notes_index, files = [], {}
    for n, (page, (bx, by, k, pw, ph)) in enumerate(zip(doc.pages, boxes)):
        template, page_id = _new_id(), _new_id()
        page_id = page_id[:-2] + '%02X' % (int(page_id[-2:], 16) & 0xfe)   # so that +1 does not wrap
        events.append(_record(_f(1, template) + _f(2, (
            _f(1, doc_id) + _f(2, template) + _f(4, attachment) + _f(5, n + 1) + _f(6, 1)
            + _f(8, _f(1, float(pw * up)) + _f(2, float(ph * up))) + _f64(10, now * 1000) + _f(11, _new_id())
            + _f(12, _f(2, _stamp())) + _f(13, _f(2, _stamp())) + _f(15, device) + _f(16, tick())
            + _f(17, _f(1, 1) + _f(2, _stamp())) + _f(19, _f(2, _stamp())) + _f(21, 24)))))
        events.append(_record(_f(1, page_id) + _f(54, (
            _f(1, doc_id) + _f(2, page_id) + _f(3, _f(1, template) + _f(2, _stamp()))
            + _f(4, _f(1, 'P%06d' % n) + _f(2, _stamp())) + _f64(10, now * 1000) + _f(11, _new_id())
            + _f(13, device) + _f(14, tick()) + _f(15, 24)))))

        body = bytearray()
        counter = 1000000
        for it in page.items:
            if it.kind != 's' or len(it.pts) < 1:
                continue
            pts = [((x - bx) * k * up, (y - by) * k * up) for x, y, _ in it.pts]
            if len(pts) == 1:
                pts.append((pts[0][0] + 0.01, pts[0][1]))
            width = sum(it.w_at(p) for _, _, p in it.pts) / len(it.pts) * k * up
            sid = _new_id()
            stamp = _stamp(2)
            counter += 1
            body += _record(_f(1, sid) + _f(2, stamp) + _f(6, sid) + _f(8, device) + _f(9, counter)
                            + _f(14, 5381) + _f(16, 24))
            r, g, b = it.color[:3]
            color = _f(1, float(r)) + _f(2, float(g)) + _f(3, float(b)) + _f(4, 0.5 if it.tool == 'hl' else 1.0)
            body += _record(_f(7, _f(1, sid) + _f(2, _pack_stroke(pts, float(width))) + _f(4, color) + _f(6, b'')
                               + _f(7, _f(1, _stamp(len(pts)))) + _f(9, b'') + _f(15, stamp) + _f(20, b'') + _f(21, 24)))
        notes_id = _notes_id(page_id)
        files['notes/' + notes_id] = bytes(body)
        notes_index.append(_record(_f(1, notes_id) + _f(2, 'notes/' + notes_id)))

    with zipfile.ZipFile(path + '.tmp', 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('document.info.pb', b'')
        z.writestr('schema.pb', _f(1, 25))
        z.writestr('index.events.pb', b''.join(events))
        z.writestr('index.notes.pb', b''.join(notes_index))
        z.writestr('index.search.pb', b'')
        z.writestr('index.attachments.pb', _record(_f(1, attachment) + _f(2, 'attachments/' + attachment)))
        z.writestr('attachments/' + attachment, pdf)
        for name, data in files.items():
            z.writestr(name, data)
        try:
            z.writestr('thumbnail.jpg', _thumbnail(doc))
        except Exception:
            pass
    os.replace(path + '.tmp', path)


def _thumbnail(doc):
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf, GLib

    loader = GdkPixbuf.PixbufLoader.new_with_type('png')
    loader.write(doc.cover())
    loader.close()
    ok, data = loader.get_pixbuf().save_to_bufferv('jpeg', ['quality'], ['85'])
    return bytes(data)
