"""Bridges from pycairo surfaces to GDK textures, and the background PDF renderer."""
import itertools
import queue
import threading

import cairo
from gi.repository import Gdk, GLib

from .model import render_pdf_bg


def texture_from_surface(surface):
    surface.flush()
    data = GLib.Bytes.new(bytes(surface.get_data()))
    return Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(),
                                 Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, surface.get_stride())


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


class Worker(threading.Thread):
    """Rasterises PDF page backgrounds off the main loop.

    Poppler runs through ctypes, which releases the GIL, so scrolling and
    writing stay responsive while a heavy page renders.
    """

    def __init__(self):
        super().__init__(daemon=True, name='inkpad-pdf')
        self._jobs = queue.PriorityQueue()
        self._seq = itertools.count()
        self.start()

    def submit(self, priority, valid, page, pw, ph, done, view=None):
        """Render page.pdf into pw x ph pixels, then call done(surface) on the main loop.

        view=(ox, oy, sx, sy) renders the window at pixel offset (ox, oy) of the
        page scaled by (sx, sy); without it the whole page is fitted. valid() is
        checked right before rendering so superseded requests are skipped.
        """
        # Within a priority the newest request goes first: it is what is on screen now.
        self._jobs.put((priority, -next(self._seq), valid, page, pw, ph, done, view))

    def run(self):
        while True:
            _, _, valid, page, pw, ph, done, view = self._jobs.get()
            try:
                if not valid():
                    continue
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
                cr = cairo.Context(surface)
                cr.set_source_rgb(1, 1, 1)
                cr.paint()
                if view is None:
                    cr.scale(pw / page.w, ph / page.h)
                else:
                    cr.translate(-view[0], -view[1])
                    cr.scale(view[2], view[3])
                render_pdf_bg(cr, page)
                surface.flush()
                GLib.idle_add(self._deliver, done, surface)
            except Exception as exc:  # a broken page must not kill the worker
                print('inkpad: pdf render failed:', exc)

    @staticmethod
    def _deliver(done, surface):
        done(surface)
        return False


_worker = None


def worker():
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker
