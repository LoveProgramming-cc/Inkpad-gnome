"""Tool and app preferences, kept in ~/.config/inkpad/settings.json."""
import copy
import json
import os

from gi.repository import GLib

from . import home_override

CONFIG_DIR = os.path.join(home_override(), 'config') if home_override() \
    else os.path.join(GLib.get_user_config_dir(), 'inkpad')
PATH = os.path.join(CONFIG_DIR, 'settings.json')

DEFAULTS = {
    'pen_type': 'fountain',
    # Per pen: three width presets (points), the active preset, pressure
    # sensitivity and stabilisation in percent.
    'pens': {
        'fountain': {'widths': [0.9, 1.6, 2.8], 'slot': 1, 'sens': 50, 'stab': 0, 'dash': 0},
        'ball': {'widths': [0.7, 1.2, 2.0], 'slot': 1, 'sens': 50, 'stab': 0, 'dash': 0},
        'brush': {'widths': [1.4, 2.4, 3.8], 'slot': 1, 'sens': 50, 'stab': 0, 'dash': 0},
    },
    'pen_colors': [[0.29, 0.62, 0.96], [0.66, 0.11, 0.94], [0.94, 0.24, 0.24],
                   [0.49, 0.76, 0.19], [0.0, 0.0, 0.0], [1.0, 0.6, 0.12]],
    'pen_color': 4,
    'hl_colors': [[1.0, 0.88, 0.3], [0.56, 0.89, 0.41], [1.0, 0.54, 0.82], [0.49, 0.77, 1.0], [1.0, 0.7, 0.28]],
    'hl_color': 0,
    'hl_w': 1,
    'hl_widths': [8.0, 14.0, 22.0],
    'laser_mode': 'trail',
    'palm_ms': 80,
    'input_log': False,
    'library': '',
    'last_folder': '',
    'views': {},
    'note_color': 0,
    'tape_color': 0,
    'tape_w': 1,
    'mouse_hand': True,
    'paging': 'vertical',
    'scrollbars': True,
    'sync_every': 30,
    'max_zoom': 6.0,
    'hidden_tools': [],
    'crosshair': True,
    'laser_color': 0,
    'eraser_size': 1,
    'eraser_mode': 'standard',
    'erase_filter': ['pen', 'hl', 'shape'],
    'eraser_auto': False,
    'lasso_rect': False,
    'lasso_filter': ['ink', 'image', 'shape', 'text'],
    'shape_kind': 'line',
    'text_size': 1,
    'hold_shape': True,
    'scribble_erase': True,
    'scribble_loose': False,
    'finger_draw': False,
    'template': {'w': 595.0, 'h': 842.0, 'paper': 'blank', 'color': [1.0, 1.0, 1.0]},
    'sort': 'date',
    'theme': 'system',
    'language': 'auto',
    'add_pos': 'after',
}


def load():
    st = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH, encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return st
    for key, value in saved.items():
        if key == 'pens' and isinstance(value, dict):
            for name, pen in value.items():
                if name in st['pens'] and isinstance(pen, dict):
                    st['pens'][name].update(pen)
        elif key in st and type(value) is type(st[key]):
            st[key] = value
    return st


def save(st):
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(st, f)
        os.replace(PATH + '.tmp', PATH)
    except OSError as exc:
        print('inkpad: could not save settings:', exc)
