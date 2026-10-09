"""Text layout on pycairo contexts through Pango, called with ctypes.

python3-gi-cairo (the pycairo/GObject bridge) is an optional package, so
PangoCairo cannot be reached through introspection here.
"""
import ctypes
from ctypes import POINTER, byref, c_char_p, c_double, c_int, c_void_p

from .pdf import _cairo_ptr

_pango = ctypes.CDLL('libpango-1.0.so.0')
_pc = ctypes.CDLL('libpangocairo-1.0.so.0')
_gobject = ctypes.CDLL('libgobject-2.0.so.0')

_pc.pango_cairo_create_layout.restype = c_void_p
_pc.pango_cairo_create_layout.argtypes = [c_void_p]
_pc.pango_cairo_show_layout.argtypes = [c_void_p, c_void_p]
_pango.pango_font_description_from_string.restype = c_void_p
_pango.pango_font_description_from_string.argtypes = [c_char_p]
_pango.pango_font_description_set_absolute_size.argtypes = [c_void_p, c_double]
_pango.pango_font_description_free.argtypes = [c_void_p]
_pango.pango_layout_set_font_description.argtypes = [c_void_p, c_void_p]
_pango.pango_layout_set_width.argtypes = [c_void_p, c_int]
_pango.pango_layout_set_wrap.argtypes = [c_void_p, c_int]
_pango.pango_layout_set_text.argtypes = [c_void_p, c_char_p, c_int]
_pango.pango_layout_get_size.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
_gobject.g_object_unref.argtypes = [c_void_p]

_SCALE = 1024
_WRAP_WORD_CHAR = 2


def _layout(cr, text, size, wrap):
    layout = _pc.pango_cairo_create_layout(_cairo_ptr(cr))
    font = _pango.pango_font_description_from_string(b'Sans')
    _pango.pango_font_description_set_absolute_size(font, size * _SCALE)
    _pango.pango_layout_set_font_description(layout, font)
    _pango.pango_font_description_free(font)
    if wrap > 0:
        _pango.pango_layout_set_width(layout, int(wrap * _SCALE))
        _pango.pango_layout_set_wrap(layout, _WRAP_WORD_CHAR)
    _pango.pango_layout_set_text(layout, text.encode('utf-8'), -1)
    return layout


def measure(cr, text, size, wrap=0.0):
    layout = _layout(cr, text, size, wrap)
    w, h = c_int(), c_int()
    _pango.pango_layout_get_size(layout, byref(w), byref(h))
    _gobject.g_object_unref(layout)
    return w.value / _SCALE, h.value / _SCALE


def show(cr, x, y, text, size, wrap=0.0):
    layout = _layout(cr, text, size, wrap)
    cr.move_to(x, y)
    _pc.pango_cairo_show_layout(_cairo_ptr(cr), layout)
    _gobject.g_object_unref(layout)
