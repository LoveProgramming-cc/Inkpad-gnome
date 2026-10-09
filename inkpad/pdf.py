"""PDF backgrounds through poppler-glib, called with ctypes.

The GObject-introspection typelib for Poppler is an optional package, while
libpoppler-glib itself ships with every GNOME desktop, so the library is used
directly. Rendering goes onto a pycairo context: onto an image surface for the
screen, or onto a PDF surface for export, which keeps the page vector.
"""
import ctypes
import threading

from .i18n import tr
from ctypes import POINTER, byref, c_char_p, c_double, c_int, c_size_t, c_uint32, c_void_p


class PdfError(Exception):
    pass


class _GList(ctypes.Structure):
    _fields_ = [('data', c_void_p), ('next', c_void_p), ('prev', c_void_p)]


class _Rectangle(ctypes.Structure):
    _fields_ = [('x1', c_double), ('y1', c_double), ('x2', c_double), ('y2', c_double)]


class _GError(ctypes.Structure):
    _fields_ = [('domain', c_uint32), ('code', c_int), ('message', c_char_p)]


def _load():
    glib = ctypes.CDLL('libglib-2.0.so.0')
    gobject = ctypes.CDLL('libgobject-2.0.so.0')
    pop = ctypes.CDLL('libpoppler-glib.so.8')
    glib.g_bytes_new.restype = c_void_p
    glib.g_bytes_new.argtypes = [c_char_p, c_size_t]
    glib.g_bytes_unref.argtypes = [c_void_p]
    glib.g_error_free.argtypes = [c_void_p]
    gobject.g_object_unref.argtypes = [c_void_p]
    pop.poppler_document_new_from_bytes.restype = c_void_p
    pop.poppler_document_new_from_bytes.argtypes = [c_void_p, c_char_p, POINTER(POINTER(_GError))]
    pop.poppler_document_get_n_pages.restype = c_int
    pop.poppler_document_get_n_pages.argtypes = [c_void_p]
    pop.poppler_document_get_page.restype = c_void_p
    pop.poppler_document_get_page.argtypes = [c_void_p, c_int]
    pop.poppler_page_get_size.argtypes = [c_void_p, POINTER(c_double), POINTER(c_double)]
    pop.poppler_page_find_text.restype = c_void_p
    pop.poppler_page_find_text.argtypes = [c_void_p, c_char_p]
    pop.poppler_rectangle_free.argtypes = [c_void_p]
    glib.g_list_free.argtypes = [c_void_p]
    pop.poppler_page_render.argtypes = [c_void_p, c_void_p]
    pop.poppler_page_render_for_printing.argtypes = [c_void_p, c_void_p]
    return glib, gobject, pop


try:
    _glib, _gobject, _pop = _load()
except (OSError, AttributeError) as exc:  # poppler missing or too old
    _glib = _gobject = _pop = None
    _load_error = str(exc)

# Poppler documents are not thread safe; screen rendering runs on a worker.
LOCK = threading.RLock()

# struct PycairoContext { PyObject_HEAD; cairo_t *ctx; PyObject *base; }
_CTX_OFFSET = object.__basicsize__


def _cairo_ptr(cr):
    return c_void_p.from_address(id(cr) + _CTX_OFFSET).value


class PdfDoc:
    def __init__(self, data):
        if _pop is None:
            raise PdfError(tr('找不到 libpoppler-glib：') + _load_error)
        self.data = bytes(data)
        self._sizes = {}
        err = POINTER(_GError)()
        with LOCK:
            gbytes = _glib.g_bytes_new(self.data, len(self.data))
            self._doc = _pop.poppler_document_new_from_bytes(gbytes, None, byref(err))
            _glib.g_bytes_unref(gbytes)
        if not self._doc:
            msg = tr('PDF 无法打开')
            if err:
                msg = (err.contents.message or b'').decode('utf-8', 'replace') or msg
                _glib.g_error_free(err)
            raise PdfError(msg)
        self.n_pages = _pop.poppler_document_get_n_pages(self._doc)

    def __del__(self):
        doc = getattr(self, '_doc', None)
        if doc and _gobject is not None:
            _gobject.g_object_unref(doc)
            self._doc = None

    def page_size(self, index):
        size = self._sizes.get(index)
        if size is None:
            w, h = c_double(), c_double()
            with LOCK:
                page = _pop.poppler_document_get_page(self._doc, index)
                if not page:
                    raise PdfError(tr('PDF 页面 %d 不存在') % (index + 1))
                _pop.poppler_page_get_size(page, byref(w), byref(h))
                _gobject.g_object_unref(page)
            size = self._sizes[index] = (w.value, h.value)
        return size

    def find(self, index, text):
        """Where text occurs on a page: boxes in points, measured from the top left."""
        out = []
        height = self.page_size(index)[1]
        with LOCK:
            page = _pop.poppler_document_get_page(self._doc, index)
            if not page:
                return out
            try:
                head = node = _pop.poppler_page_find_text(page, text.encode('utf-8'))
                while node:
                    link = _GList.from_address(node)
                    r = _Rectangle.from_address(link.data)
                    # Poppler measures from the bottom of the page.
                    out.append((min(r.x1, r.x2), height - max(r.y1, r.y2), max(r.x1, r.x2), height - min(r.y1, r.y2)))
                    _pop.poppler_rectangle_free(link.data)
                    node = link.next
                _glib.g_list_free(head)
            finally:
                _gobject.g_object_unref(page)
        return out

    def render(self, index, cr, printing=False):
        """Draw a page at the context's current transform, one unit per point."""
        with LOCK:
            page = _pop.poppler_document_get_page(self._doc, index)
            if not page:
                return
            try:
                if printing:
                    _pop.poppler_page_render_for_printing(page, _cairo_ptr(cr))
                else:
                    _pop.poppler_page_render(page, _cairo_ptr(cr))
            finally:
                _gobject.g_object_unref(page)
