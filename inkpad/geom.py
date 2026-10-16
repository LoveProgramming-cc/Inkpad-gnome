"""Geometry helpers: hit testing, lasso picking and scribble-to-erase detection.

All coordinates are page points. Point sequences may carry extra fields
(pressure); only indices 0 and 1 are read.
"""
import math


def union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def intersects(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def bbox_of(pts, pad=0.0):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def decimate(pts, limit):
    n = len(pts)
    if n <= limit:
        return pts
    step = n / limit
    out = [pts[int(i * step)] for i in range(limit)]
    out.append(pts[-1])
    return out


def dist2_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    d = dx * dx + dy * dy
    if d <= 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / d
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2


def polyline_hit(pts, x, y, r):
    r2 = r * r
    if len(pts) == 1:
        return (pts[0][0] - x) ** 2 + (pts[0][1] - y) ** 2 <= r2
    a = pts[0]
    for b in pts[1:]:
        if dist2_seg(x, y, a[0], a[1], b[0], b[1]) <= r2:
            return True
        a = b
    return False


def in_polygon(poly, x, y):
    inside = False
    ax, ay = poly[-1][0], poly[-1][1]
    for p in poly:
        bx, by = p[0], p[1]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
        ax, ay = bx, by
    return inside


def convex_hull(pts):
    pts = sorted({(p[0], p[1]) for p in pts})
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and (
                (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])
            ) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(pts) + half(reversed(pts))


def segs_cross(a, b, c, d):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return (side(a, b, c) > 0) != (side(a, b, d) > 0) and (side(c, d, a) > 0) != (side(c, d, b) > 0)


# -- scribble to erase ------------------------------------------------------

MIN_REVERSALS = 3       # a scribble is at least four back-and-forth passes
MIN_LENGTH_RATIO = 2.5  # path length relative to its extent along the main axis
SPREAD_FREE = 0.3       # a scribble this thin (width across / length along) needs no extra passes
SPREAD_STEP = 0.25      # one more pass for each step wider than that
DENSE_RATIO = 7.0       # path length relative to extent of a rub over one small spot


def is_scribble(pts):
    """True when a stroke looks like a quick back-and-forth scratch-out.

    Writing turns a lot too, so turning alone does not decide: a word written in one stroke goes
    round its letters, and a large one turns through 4π or 8π without being a scribble.
    What a scratch-out does is go back over the same ground: each pass runs at least half the
    stroke's length, it turns about half a turn at each end and seldom in between, and the more
    room it spreads over the more passes it takes before it reads as a scribble. A rub that keeps
    circling one small spot counts as well."""
    n = len(pts)
    if n < 10:
        return False
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = syy = sxy = 0.0
    for p in pts:
        dx, dy = p[0] - mx, p[1] - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(ang), math.sin(ang)
    t = [(p[0] - mx) * ux + (p[1] - my) * uy for p in pts]
    across = [(p[1] - my) * ux - (p[0] - mx) * uy for p in pts]
    extent = max(t) - min(t)
    if extent < 5.0:
        return False
    width = max(across) - min(across)
    length = 0.0
    a = pts[0]
    for b in pts[1:]:
        length += math.hypot(b[0] - a[0], b[1] - a[1])
        a = b
    if length < MIN_LENGTH_RATIO * extent:
        return False
    # Direction reversals along the main axis, each at least half the stroke's length away
    # from the one before, so that what letters do inside a word is not counted.
    h = extent * 0.5
    direction = 0
    ext = t[0]
    reversals = 0
    for v in t[1:]:
        if direction == 0:
            if abs(v - ext) >= h:
                direction = 1 if v > ext else -1
                ext = v
        elif direction == 1:
            if v > ext:
                ext = v
            elif ext - v >= h:
                reversals += 1
                direction = -1
                ext = v
        else:
            if v < ext:
                ext = v
            elif v - ext >= h:
                reversals += 1
                direction = 1
                ext = v
    if reversals < MIN_REVERSALS:
        return False
    turned = _turning(pts, max(math.hypot(extent, width) / 40, 0.5))
    if length >= DENSE_RATIO * extent and turned >= 4 * math.pi:
        return True
    if turned - math.pi * reversals > 2 * math.pi:
        return False   # it went round in loops besides: handwriting
    need = MIN_REVERSALS + max(0, math.ceil((width / extent - SPREAD_FREE) / SPREAD_STEP - 1e-9))
    return reversals >= need


def _turning(pts, step):
    """How far the direction of a stroke turns in all, left and right alike, measured on points
    about `step` apart so that the tremor of the hand does not add up."""
    kept = [pts[0]]
    run = 0.0
    for a, b in zip(pts, pts[1:]):
        run += math.hypot(b[0] - a[0], b[1] - a[1])
        if run >= step:
            kept.append(b)
            run = 0.0
    total = 0.0
    prev = None
    for a, b in zip(kept, kept[1:]):
        heading = math.atan2(b[1] - a[1], b[0] - a[0])
        if prev is not None:
            d = (heading - prev + math.pi) % (2 * math.pi) - math.pi
            total += abs(d)
        prev = heading
    return total


def scribble_targets(pts, items):
    """Items a scribble covers: mostly inside its hull, or crossed repeatedly."""
    sc = decimate(pts, 150)
    bb = bbox_of(sc)
    hull = convex_hull(sc)
    if len(hull) < 3:
        return []
    out = []
    for it in items:
        if it.kind not in ('s', 'h') or not intersects(it.bbox, bb):
            continue
        pl = decimate(it.polyline(), 150)
        if len(pl) == 1:
            if in_polygon(hull, pl[0][0], pl[0][1]):
                out.append(it)
            continue
        total = inside = 0.0
        a = pl[0]
        for b in pl[1:]:
            seg = math.hypot(b[0] - a[0], b[1] - a[1]) + 1e-6
            total += seg
            if in_polygon(hull, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2):
                inside += seg
            a = b
        cover = inside / total
        if cover >= 0.35:
            out.append(it)
        elif cover >= 0.08 and _crossings(sc, pl, 4) >= 4:
            out.append(it)
    return out


def _crossings(a_pts, b_pts, enough):
    count = 0
    a0 = a_pts[0]
    for a1 in a_pts[1:]:
        b0 = b_pts[0]
        for b1 in b_pts[1:]:
            if segs_cross(a0, a1, b0, b1):
                count += 1
                if count >= enough:
                    return count
            b0 = b1
        a0 = a1
    return count


def _touching(it, box, inside):
    """Whether any part of an item lies in a region given by its box and a point test."""
    if not intersects(it.bbox, box):
        return False
    line = it.polyline()
    pts = densify(line, 3.0) if line and len(line) > 1 else it.sample()
    return any(inside(p[0], p[1]) for p in pts)


def lasso_pick(poly, items):
    """Items the lasso touches: having any part inside it selects the whole item."""
    poly = decimate(poly, 120)
    if len(poly) < 3:
        return []
    bb = bbox_of(poly)
    out = []
    for it in items:
        if _touching(it, bb, lambda x, y: in_polygon(poly, x, y)):
            out.append(it)
        elif it.kind in ('i', 't') and intersects(it.bbox, bb):
            # Pictures and text boxes have an inside: a loop drawn over one counts too.
            x0, y0, x1, y1 = it.bbox
            if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in poly):
                out.append(it)
    return out


def rect_pick(box, items):
    out = []
    for it in items:
        if it.kind in ('i', 't'):
            if intersects(it.bbox, box):
                out.append(it)
        elif _touching(it, box, lambda x, y: box[0] <= x <= box[2] and box[1] <= y <= box[3]):
            out.append(it)
    return out


# -- partial erasing --------------------------------------------------------

def densify(pts, step):
    """Insert interpolated samples so no segment is longer than step."""
    out = [pts[0]]
    for b in pts[1:]:
        a = out[-1]
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d > step:
            n = int(d / step) + 1
            for i in range(1, n):
                t = i / n
                # Outlines of shapes, pictures and text are plain (x, y); ink has a pressure too.
                out.append(tuple(u + (v - u) * t for u, v in zip(a, b)))
        out.append(b)
    return out


def erase_runs(pts, ax, ay, bx, by, r):
    """Cut the part of a stroke within r of segment a-b out of it.

    Returns (surviving runs of points, bounding box of the part that changed),
    or None when nothing was touched. Only the segments next to the eraser are
    resampled, so the rest of the stroke keeps its exact shape.
    """
    r2 = r * r
    n = len(pts)
    inside = [dist2_seg(p[0], p[1], ax, ay, bx, by) <= r2 for p in pts]
    step = max(r * 0.4, 0.3)
    ea, eb = (ax, ay), (bx, by)
    runs, run, cut = [], [], inside[0]
    lo, hi = (0, 0) if inside[0] else (n, -1)
    if not inside[0]:
        run.append(pts[0])
    for i in range(1, n):
        a, b = pts[i - 1], pts[i]
        if not (inside[i - 1] or inside[i]
                or dist2_seg(ax, ay, a[0], a[1], b[0], b[1]) <= r2
                or dist2_seg(bx, by, a[0], a[1], b[0], b[1]) <= r2
                or segs_cross(a, b, ea, eb)):
            run.append(b)
            continue
        lo, hi = min(lo, i - 1), max(hi, i)
        k = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step) + 1)
        for j in range(1, k + 1):
            t = j / k
            q = b if j == k else (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
            if dist2_seg(q[0], q[1], ax, ay, bx, by) <= r2:
                cut = True
                if run:
                    runs.append(run)
                    run = []
            else:
                run.append(q)
    if not cut:
        return None
    if run:
        runs.append(run)
    return runs, bbox_of(pts[max(0, lo - 2):hi + 3])


# -- hold to snap to a shape ------------------------------------------------

def recognize_shape(pts):
    """Classify a freehand stroke as ('line' | 'rect' | 'ellipse', x0, y0, x1, y1), or None."""
    n = len(pts)
    if n < 6:
        return None
    length = 0.0
    a = pts[0]
    for b in pts[1:]:
        length += math.hypot(b[0] - a[0], b[1] - a[1])
        a = b
    first, last = pts[0], pts[-1]
    chord = math.hypot(last[0] - first[0], last[1] - first[1])
    if length < 8:
        return None
    if chord > 0.88 * length:
        return ('line', first[0], first[1], last[0], last[1])
    if chord > 0.25 * length:
        return None  # neither straight nor closed
    x0, y0, x1, y1 = bbox_of(pts)
    w, h = x1 - x0, y1 - y0
    if min(w, h) < 4:
        return None
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, w / 2, h / 2
    e_err = r_err = 0.0
    for p in pts:
        e_err += abs(math.hypot((p[0] - cx) / rx, (p[1] - cy) / ry) - 1)
        r_err += min(p[0] - x0, x1 - p[0], p[1] - y0, y1 - p[1]) / min(w, h)
    e_err, r_err = e_err / n, r_err / n
    if r_err < 0.07 and r_err < e_err:
        return ('rect', x0, y0, x1, y1)
    if e_err < 0.16:
        return ('ellipse', x0, y0, x1, y1)
    return None
