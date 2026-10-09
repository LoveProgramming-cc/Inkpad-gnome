"""The notebook library: a folder of .inkpad files shown as a Goodnotes-style home screen.

Notebooks live as ordinary files under ~/Documents/Inkpad, folders are real
directories and the trash is a hidden subfolder, so everything stays reachable
from the file manager and easy to back up.
"""
import json
import math
import os
import shutil
import threading
import time
import uuid
import zipfile

import cairo
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

from .i18n import tr
from . import APP_NAME, home_override, icons
from . import settings as app_settings
from .dialogs import PaperDialog, ask_text, confirm
from .icons import Icon, TextureView
from .model import Document, set_notebook_id
from . import ptext
from .render import rgba, texture_from_surface

EXT = '.inkpad'
_BAND_FILL = rgba(0.21, 0.52, 0.89, 0.14)
_BAND_EDGE = rgba(0.21, 0.52, 0.89, 0.8)
ARCHIVE_META = 'inkpad-library.json'
FOLDER_COLORS = [(0.49, 0.74, 0.99), (0.36, 0.80, 0.82), (0.47, 0.80, 0.52), (0.98, 0.82, 0.38),
                 (0.99, 0.66, 0.38), (0.96, 0.50, 0.50), (0.74, 0.60, 0.96), (0.66, 0.70, 0.76)]
FOLDER_ICONS = [''] + list(icons.GLYPHS)


def folder_texture(color, icon, scale=1, w=150, h=118):
    """A soft two-tone folder in the given colour, with an optional emoji on the front."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * scale, h * scale)
    cr = cairo.Context(surface)
    cr.scale(scale, scale)
    r, tab_w, tab_h, top = 11.0, w * 0.40, h * 0.13, h * 0.20
    back = tuple(c * 0.84 for c in color)
    # Back panel with its tab.
    cr.move_to(0, h - r)
    cr.line_to(0, r)
    cr.arc(r, r, r, math.pi, 1.5 * math.pi)
    cr.line_to(tab_w - 6, 0)
    cr.curve_to(tab_w + 2, 0, tab_w + 4, tab_h, tab_w + 14, tab_h)
    cr.line_to(w - r, tab_h)
    cr.arc(w - r, tab_h + r, r, 1.5 * math.pi, 0)
    cr.line_to(w, h - r)
    cr.arc(w - r, h - r, r, 0, 0.5 * math.pi)
    cr.arc(r, h - r, r, 0.5 * math.pi, math.pi)
    cr.close_path()
    cr.set_source_rgb(*back)
    cr.fill()
    # Front panel.
    cr.new_sub_path()
    cr.arc(w - r, top + r, r, 1.5 * math.pi, 0)
    cr.arc(w - r, h - r, r, 0, 0.5 * math.pi)
    cr.arc(r, h - r, r, 0.5 * math.pi, math.pi)
    cr.arc(r, top + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()
    shade = cairo.LinearGradient(0, top, 0, h)
    shade.add_color_stop_rgb(0, *(min(1.0, c + 0.07) for c in color))
    shade.add_color_stop_rgb(1, *color)
    cr.set_source(shade)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.35)
    cr.set_line_width(1)
    cr.stroke()
    if icon in icons.GLYPHS:
        size = h * 0.36
        cr.set_source_rgba(*(c * 0.42 for c in color), 0.78)
        icons.draw_glyph(cr, icon, (w - size) / 2, top + (h - top - size) / 2, size, 1.9)
    elif icon:  # an emoji chosen in an earlier version
        size = h * 0.30
        tw, th = ptext.measure(cr, icon, size)
        cr.set_source_rgba(0, 0, 0, 0.85)
        ptext.show(cr, (w - tw) / 2, top + (h - top - th) / 2, icon, size)
    return texture_from_surface(surface)


class Entry:
    __slots__ = ('path', 'name', 'is_dir', 'mtime')

    def __init__(self, path):
        self.path = path
        self.is_dir = os.path.isdir(path)
        base = os.path.basename(path)
        self.name = base if self.is_dir else os.path.splitext(base)[0]
        try:
            self.mtime = os.path.getmtime(path)
        except OSError:
            self.mtime = 0


class Store:
    def __init__(self, root=None):
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or os.path.expanduser('~/Documents')
        chosen = app_settings.load().get('library') or None   # a place the user moved it to
        self.root = root or (os.path.join(home_override(), 'library') if home_override() and not chosen
                             else chosen or os.path.join(docs, APP_NAME))
        self.trash_dir = os.path.join(self.root, '.trash')
        # Things deleted "for good" are kept here until the user clears the cache.
        self.archive_dir = os.path.join(self.root, '.deleted')
        self._meta_path = os.path.join(self.root, '.library.json')
        os.makedirs(self.trash_dir, exist_ok=True)
        try:
            with open(self._meta_path, encoding='utf-8') as f:
                self.meta = json.load(f)
        except (OSError, ValueError):
            self.meta = {}
        self.meta.setdefault('favorites', [])
        self.meta.setdefault('trashed', {})  # trash name -> folder it came from
        self.meta.setdefault('folders', {})  # folder -> {'color': [r, g, b], 'icon': emoji}

    def reload_meta(self):
        """Pick up favourites and folder styles that a sync brought in."""
        try:
            with open(self._meta_path, encoding='utf-8') as f:
                self.meta.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save_meta(self):
        self.meta['ts'] = time.time()
        with open(self._meta_path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(self.meta, f, ensure_ascii=False)
        os.replace(self._meta_path + '.tmp', self._meta_path)

    def rel(self, path):
        return os.path.relpath(path, self.root)

    def contains(self, path):
        return os.path.abspath(path).startswith(self.root + os.sep)

    def entries(self, folder):
        out = []
        try:
            names = os.listdir(folder)
        except OSError:
            return out
        for name in names:
            path = os.path.join(folder, name)
            if name.startswith('.'):
                continue
            if os.path.isdir(path) or name.endswith(EXT):
                out.append(Entry(path))
        return out

    def favorites(self):
        return [Entry(p) for p in (os.path.join(self.root, r) for r in self.meta['favorites']) if os.path.exists(p)]

    def is_favorite(self, path):
        return self.rel(path) in self.meta['favorites']

    def toggle_favorite(self, path):
        rel = self.rel(path)
        favs = self.meta['favorites']
        favs.remove(rel) if rel in favs else favs.append(rel)
        self._save_meta()

    def folder_style(self, path):
        style = self.meta['folders'].get(self.rel(path), {})
        return tuple(style.get('color', FOLDER_COLORS[0])), style.get('icon', '')

    def set_folder_style(self, path, **changes):
        style = self.meta['folders'].setdefault(self.rel(path), {})
        style.update({k: list(v) if isinstance(v, tuple) else v for k, v in changes.items()})
        self._save_meta()

    def folders(self):
        """Every folder in the library as a tree read downwards: the root, then each folder
        followed at once by everything inside it."""
        out = []

        def walk(folder):
            out.append(folder)
            try:
                names = sorted((n for n in os.listdir(folder) if not n.startswith('.')), key=str.lower)
            except OSError:
                return
            for name in names:
                path = os.path.join(folder, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    walk(path)

        walk(self.root)
        return out

    @staticmethod
    def unique(folder, name, ext=''):
        path = os.path.join(folder, name + ext)
        n = 2
        while os.path.exists(path):
            path = os.path.join(folder, '%s %d%s' % (name, n, ext))
            n += 1
        return path

    def new_notebook(self, folder, template, name=None, cover=None):
        path = self.unique(folder, name or tr('未命名笔记'), EXT)
        doc = Document(template)
        doc.path = path  # the cover shows the name
        doc.cover_spec = cover
        doc.save(path)
        return path

    def import_pdf(self, folder, name, data):
        path = self.unique(folder, name, EXT)
        Document.from_pdf(data).save(path)
        return path

    def import_goodnotes(self, folder, source):
        """Turn a .goodnotes file into a notebook; returns (path, what was read)."""
        from . import goodnotes
        doc, title, stats = goodnotes.load(source)
        path = self.unique(folder, title.replace('/', '-'), EXT)
        doc.save(path)
        return path, stats

    def import_tree(self, source, folder, progress=None):
        """Import every Goodnotes document, PDF and Inkpad notebook under a folder, keeping its
        folder structure. Returns (how many were imported, [(file, why it failed)])."""
        from . import goodnotes
        found = []
        for base, dirs, names in os.walk(source):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
            for name in sorted(names):
                if name.lower().endswith(('.goodnotes', '.pdf', EXT)) and not name.startswith('.'):
                    found.append(os.path.join(base, name))
        top = self.unique(folder, os.path.basename(os.path.normpath(source)) or tr('导入'))
        done, failed = 0, []
        for n, path in enumerate(found):
            rel = os.path.relpath(path, source)
            if progress:
                progress(n / len(found), rel)
            target = os.path.join(top, os.path.dirname(rel))
            stem = os.path.splitext(os.path.basename(path))[0].replace('/', '-')
            try:
                os.makedirs(target, exist_ok=True)
                kind = path.lower().rsplit('.', 1)[-1]
                if kind == 'goodnotes':
                    doc = goodnotes.load(path)[0]
                    doc.save(self.unique(target, stem, EXT))
                elif kind == 'pdf':
                    with open(path, 'rb') as f:
                        Document.from_pdf(f.read()).save(self.unique(target, stem, EXT))
                else:
                    new = self.unique(target, stem, EXT)
                    shutil.copy2(path, new)
                    set_notebook_id(new, uuid.uuid4().hex)
                done += 1
            except Exception as exc:
                failed.append((rel, str(exc) or type(exc).__name__))
        if progress:
            progress(1.0, '')
        return done, failed

    def new_folder(self, folder, name):
        path = self.unique(folder, name)
        os.makedirs(path)
        return path

    def _moved(self, old, new):
        """Keep favourites pointing at an item (and what is inside it) after a move."""
        old_rel, new_rel = self.rel(old), self.rel(new)
        favs = self.meta['favorites']
        for i, rel in enumerate(favs):
            if rel == old_rel or rel.startswith(old_rel + os.sep):
                favs[i] = new_rel + rel[len(old_rel):]
        styles = self.meta['folders']
        for rel in [r for r in styles if r == old_rel or r.startswith(old_rel + os.sep)]:
            styles[new_rel + rel[len(old_rel):]] = styles.pop(rel)
        self._save_meta()

    def rename(self, path, name):
        name = name.strip().replace('/', '-')
        ext = '' if os.path.isdir(path) else EXT
        if not name or os.path.join(os.path.dirname(path), name + ext) == path:
            return path
        new = self.unique(os.path.dirname(path), name, ext)
        os.rename(path, new)
        self._moved(path, new)
        return new

    def move(self, path, folder, name=None):
        """Move an item into a folder, under another name if one is given (as when a move is
        undone and it gets back the name it had). Returns where it is now."""
        base = name or os.path.basename(path)
        if os.path.dirname(path) == folder and base == os.path.basename(path):
            return path
        if folder == path or (folder + os.sep).startswith(path + os.sep):
            raise ValueError(tr('文件夹不能移动到它自己里面'))
        name, ext = (base, '') if os.path.isdir(path) else os.path.splitext(base)
        new = self.unique(folder, name, ext)
        shutil.move(path, new)
        self._moved(path, new)
        return new

    def trash(self, path):
        base = os.path.basename(path)
        name, ext = (base, '') if os.path.isdir(path) else os.path.splitext(base)
        new = self.unique(self.trash_dir, name, ext)
        rel = self.rel(path)
        self.meta['favorites'] = [r for r in self.meta['favorites'] if r != rel and not r.startswith(rel + os.sep)]
        self.meta['trashed'][os.path.basename(new)] = self.rel(os.path.dirname(path))
        shutil.move(path, new)
        self._save_meta()

    def trashed(self):
        return self.entries(self.trash_dir)

    def restore(self, path):
        origin = self.meta['trashed'].pop(os.path.basename(path), '.')
        folder = os.path.normpath(os.path.join(self.root, origin))
        if not os.path.isdir(folder) or not (folder == self.root or self.contains(folder)):
            folder = self.root
        base = os.path.basename(path)
        name, ext = (base, '') if os.path.isdir(path) else os.path.splitext(base)
        shutil.move(path, self.unique(folder, name, ext))
        self._save_meta()

    def delete(self, path):
        """Remove something from the trash. It is kept out of sight until the cache is cleared."""
        if os.path.dirname(os.path.abspath(path)) != self.trash_dir:
            raise ValueError('only trashed items can be deleted')
        os.makedirs(self.archive_dir, exist_ok=True)
        base = os.path.basename(path)
        name, ext = (base, '') if os.path.isdir(path) else os.path.splitext(base)
        kept = self.unique(self.archive_dir, time.strftime('%Y%m%d-%H%M%S ') + name, ext)
        shutil.move(path, kept)
        # Where it lived before the trash, so that it can be put back there.
        self.meta.setdefault('archived', {})[os.path.basename(kept)] = self.meta['trashed'].pop(base, '.')
        self._save_meta()

    def duplicate(self, path):
        """A copy next to the original. Copies are notebooks of their own, with new ids."""
        base = os.path.basename(path)
        is_dir = os.path.isdir(path)
        name, ext = (base, '') if is_dir else os.path.splitext(base)
        new = self.unique(os.path.dirname(path), name + tr(' 副本'), ext)
        if is_dir:
            shutil.copytree(path, new, ignore=shutil.ignore_patterns('.*'))
            books = [os.path.join(b, n) for b, _, names in os.walk(new) for n in names if n.endswith(EXT)]
            style = self.meta['folders'].get(self.rel(path))
            if style:
                self.meta['folders'][self.rel(new)] = dict(style)
                self._save_meta()
        else:
            shutil.copy2(path, new)
            books = [new]
        for book in books:
            set_notebook_id(book, uuid.uuid4().hex)
        return new

    def export_paths(self, paths, dest):
        """Write the given notebooks and folders (with everything below them) into one zip file."""
        count = 0
        styles = {}
        used = set()
        with zipfile.ZipFile(dest + '.tmp', 'w', zipfile.ZIP_STORED) as z:
            for path in paths:
                top = os.path.basename(path)
                n = 2
                while top in used:   # two selected items may share a name (favourites, trash)
                    stem, ext = os.path.splitext(os.path.basename(path))
                    top = '%s %d%s' % (stem, n, ext)
                    n += 1
                used.add(top)
                if not os.path.isdir(path):
                    z.write(path, top)
                    count += 1
                    continue
                for base, dirs, names in os.walk(path):
                    dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
                    rel = os.path.relpath(base, path)
                    inside = top if rel == '.' else top + '/' + rel.replace(os.sep, '/')
                    z.writestr(inside + '/', '')
                    style = self.meta['folders'].get(self.rel(base)) if self.contains(base) else None
                    if style:
                        styles[inside] = style
                    for name in sorted(names):
                        if name.endswith(EXT) and not name.startswith('.'):
                            z.write(os.path.join(base, name), inside + '/' + name)
                            count += 1
            z.writestr(ARCHIVE_META, json.dumps({'app': APP_NAME, 'folders': styles}, ensure_ascii=False))
        os.replace(dest + '.tmp', dest)
        return count

    def archived(self):
        """What was deleted for good and is still kept on this device."""
        return self.entries(self.archive_dir) if os.path.isdir(self.archive_dir) else []

    def unarchive(self, path):
        """Bring a permanently deleted item back to the folder it was in (recreated if need be)."""
        base = os.path.basename(path)
        origin = self.meta.setdefault('archived', {}).pop(base, '.')
        folder = os.path.normpath(os.path.join(self.root, origin))
        if not (folder == self.root or self.contains(folder)):
            folder = self.root
        os.makedirs(folder, exist_ok=True)
        name, ext = (base, '') if os.path.isdir(path) else os.path.splitext(base)
        # Deleted items carry a date prefix ("20261007-164500 name") so they never collide.
        head, _, rest = name.partition(' ')
        if rest and len(head) == 15 and head[:8].isdigit():
            name = rest
        new = self.unique(folder, name, ext)
        shutil.move(path, new)
        self._save_meta()
        return new

    def archived_origin(self, path):
        """The folder (relative to the library) a deleted item would be restored to."""
        return self.meta.get('archived', {}).get(os.path.basename(path), '.')

    def find(self, query):
        """Notebooks and folders anywhere in the library whose name contains query."""
        out = []
        for base, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in dirs + [n for n in names if n.endswith(EXT) and not n.startswith('.')]:
                if query in os.path.splitext(name)[0].lower():
                    out.append(Entry(os.path.join(base, name)))
        return out

    def cache_size(self):
        total = 0
        for base, _, names in os.walk(self.archive_dir):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        return total

    def clear_cache(self):
        """Really delete what was deleted before. Nothing in the cloud is touched."""
        shutil.rmtree(self.archive_dir, ignore_errors=True)
        self.meta['archived'] = {}
        self._save_meta()

    def export_archive(self, folder, dest):
        """Write a folder (or the whole library) with everything below it into one zip file."""
        count = 0
        styles = {}
        with zipfile.ZipFile(dest + '.tmp', 'w', zipfile.ZIP_STORED) as z:
            for base, dirs, names in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
                rel = os.path.relpath(base, folder)
                if rel != '.':
                    z.writestr(rel.replace(os.sep, '/') + '/', '')
                    style = self.meta['folders'].get(self.rel(base))
                    if style:
                        styles[rel.replace(os.sep, '/')] = style
                for name in sorted(names):
                    if name.endswith(EXT) and not name.startswith('.'):
                        z.write(os.path.join(base, name), os.path.join(rel, name).replace(os.sep, '/')
                                if rel != '.' else name)
                        count += 1
            z.writestr(ARCHIVE_META, json.dumps({'app': APP_NAME, 'folders': styles}, ensure_ascii=False))
        os.replace(dest + '.tmp', dest)
        return count

    def import_archive(self, src, folder):
        """Unpack an exported zip into folder. Existing notebooks are never overwritten."""
        count = 0
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            try:
                styles = json.loads(z.read(ARCHIVE_META)).get('folders', {})
            except (KeyError, ValueError):
                styles = {}
            for name in names:
                parts = [p for p in name.split('/') if p]
                if not parts or any(p in ('.', '..') or p.startswith('.') for p in parts) or name.startswith('/'):
                    continue
                if name.endswith('/'):
                    os.makedirs(os.path.join(folder, *parts), exist_ok=True)
                elif name.endswith(EXT):
                    target_dir = os.path.join(folder, *parts[:-1])
                    os.makedirs(target_dir, exist_ok=True)
                    target = self.unique(target_dir, parts[-1][:-len(EXT)], EXT)
                    with z.open(name) as source, open(target, 'wb') as out:
                        shutil.copyfileobj(source, out)
                    count += 1
            known = self.meta['folders']
            for rel, style in styles.items():
                path = os.path.normpath(os.path.join(folder, *rel.split('/')))
                if os.path.isdir(path) and self.contains(path) and self.rel(path) not in known and isinstance(style, dict):
                    known[self.rel(path)] = style
        self._save_meta()
        return count

    @staticmethod
    def cover(path):
        try:
            with zipfile.ZipFile(path) as z:
                return z.read('cover.png')
        except (OSError, KeyError, zipfile.BadZipFile):
            return None


def _flat(child, tip=None, **kwargs):
    button = Gtk.Button(child=child, tooltip_text=tip, **kwargs)
    button.add_css_class('flat')
    return button


def fitted(child, max_height):
    """Wrap popover content so it scrolls instead of outgrowing the window.

    A popover taller than the space the compositor can give it is not shown at all.
    """
    return Gtk.ScrolledWindow(child=child, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                              propagate_natural_width=True, max_content_height=max(200, int(max_height)),
                              overlay_scrolling=False)


def menu_popover(rows, max_height=None):
    """A popover listing (label, callback[, css class[, icon]]) rows; None adds a separator."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    popover = Gtk.Popover(child=fitted(box, max_height) if max_height else box)
    popover.box = box
    popover.add_css_class('menu')
    for row in rows:
        if row is None:
            box.append(Gtk.Separator())
            continue
        label, fn = row[0], row[1]
        content = Gtk.Box(spacing=12)
        if len(row) > 3:
            content.append(Icon(row[3]))
        content.append(Gtk.Label(label=label, xalign=0))
        button = Gtk.Button(child=content)
        button.add_css_class('flat')
        button.add_css_class('menu-row')
        if len(row) > 2 and row[2]:
            button.add_css_class(row[2])

        def clicked(_button, fn=fn):
            popover.popdown()
            fn()

        button.connect('clicked', clicked)
        box.append(button)
    return popover


class _Band(Gtk.Widget):
    """The rubber band drawn while dragging over the grid."""

    def __init__(self):
        super().__init__(can_target=False)
        self.rect = None

    def set_rect(self, rect):
        self.rect = rect
        self.queue_draw()

    def do_snapshot(self, snap):
        if self.rect is None:
            return
        rect = Graphene.Rect().init(*self.rect)
        snap.append_color(_BAND_FILL, rect)
        builder = Gsk.PathBuilder.new()
        builder.add_rect(rect)
        snap.append_stroke(builder.to_path(), Gsk.Stroke.new(1), _BAND_EDGE)


class LibraryView(Gtk.Box):
    COVER_W, COVER_H = 150, 200

    def __init__(self, store, settings, on_open, on_import, on_error, cloud_popover, on_settings):
        super().__init__()
        self.store = store
        self.settings = settings
        self.on_open, self.on_import, self.on_error = on_open, on_import, on_error
        self.mode = 'docs'       # 'docs' | 'favs' | 'trash'
        # The folder that was open last time is where the library opens again.
        last = os.path.normpath(os.path.join(store.root, settings.get('last_folder') or ''))
        self.folder = last if os.path.isdir(last) and (last + os.sep).startswith(store.root + os.sep) else store.root
        self.add_css_class('library')
        self.cursor = None       # the folder clicked once: a second click soon after opens it
        self._clicked_at = 0
        self._dragging = []      # what is being dragged to another folder
        self._undo, self._redo = [], []   # moves, each a list of (path before, path after)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=250)
        side.add_css_class('library-side')
        brand = Gtk.Label(label=APP_NAME, xalign=0)
        brand.add_css_class('brand')
        side.append(Gtk.WindowHandle(child=brand))
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        for draw, label in ((icons.documents, tr('文稿')), (icons.star, tr('收藏')), (icons.trash, tr('废纸篓'))):
            row = Gtk.Box(spacing=14)
            row.append(Icon(draw, 22))
            row.append(Gtk.Label(label=label))
            self.nav.append(row)
        self.nav.connect('row-selected', self._nav_selected)
        # Clicking the section that is already showing goes back to its top.
        self.nav.connect('row-activated', lambda box, row: self.mode == 'docs' and row.get_index() == 0
                         and self.folder != self.store.root and self.open_folder(self.store.root))
        side.append(self.nav)
        self.append(side)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        top = Adw.HeaderBar(show_title=False)
        top.add_css_class('flat')
        self.cloud = Gtk.MenuButton(child=Icon(icons.cloud), tooltip_text=tr('Google Drive 同步'))
        self.cloud.add_css_class('flat')
        self.cloud.set_create_popup_func(lambda b: b.set_popover(cloud_popover()))
        gear = Gtk.Button(child=Icon(icons.settings), tooltip_text=tr('设置'))
        gear.add_css_class('flat')
        gear.connect('clicked', lambda *_: on_settings())
        top.pack_end(gear)
        top.pack_end(self.cloud)
        self.search = Gtk.SearchEntry(placeholder_text=tr('搜索'), width_request=220)
        self.search.add_css_class('round')
        self.search.connect('search-changed', lambda *_: self.refresh())
        top.pack_end(self.search)
        main.append(top)
        # Sync progress is kept here and shown in the cloud menu while that is open.
        self.sync_fraction, self.sync_text, self.sync_bar = 0.0, '', None

        # The way down to the open folder; each part is a button that goes there.
        self.crumbs = Gtk.Box(spacing=2, margin_start=30, margin_end=30, visible=False)
        self.crumbs.add_css_class('crumbs')
        main.append(self.crumbs)
        bar = Gtk.Box(spacing=10, margin_start=34, margin_end=30, margin_top=2, margin_bottom=14)
        self.back = Gtk.Button(child=Icon(icons.back), tooltip_text=tr('返回上一级'), valign=Gtk.Align.CENTER)
        self.back.add_css_class('circular')
        self.back.connect('clicked', lambda *_: self.open_folder(os.path.dirname(self.folder)))
        self._accept_drops(self.back, lambda: os.path.dirname(self.folder))
        bar.append(self.back)
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.title.add_css_class('library-title')
        bar.append(self.title)
        new_box = Gtk.Box(spacing=6)
        new_box.append(Icon(icons.plus, 16))
        new_box.append(Gtk.Label(label=tr('新建')))
        new_box.append(Icon(icons.chevron, 14))
        self.new_btn = Gtk.MenuButton(child=new_box, valign=Gtk.Align.CENTER)
        self.new_btn.add_css_class('suggested-action')
        self.new_btn.add_css_class('pill-btn')
        self.new_btn.set_popover(menu_popover([(tr('笔记本'), self._new_notebook, None, icons.cover),
                                               (tr('白板'), self._new_board, None, icons.board),
                                               (tr('导入 PDF 或 Goodnotes…'), lambda: self.on_import(self.folder), None, icons.share),
                                               (tr('导入整个文件夹…'), self.import_folder, None, icons.documents),
                                               (tr('导入存档（.zip）…'), self.import_archive, None, icons.restore),
                                               None,
                                               (tr('文件夹'), self._new_folder, None, icons.documents)]))
        bar.append(self.new_btn)
        self.empty_btn = Gtk.Button(label=tr('清空废纸篓'), valign=Gtk.Align.CENTER)
        self.empty_btn.add_css_class('destructive-action')
        self.empty_btn.add_css_class('pill-btn')
        self.empty_btn.connect('clicked', lambda *_: self._empty_trash())
        bar.append(self.empty_btn)
        self.sort = Gtk.DropDown.new_from_strings([tr('日期'), tr('名称')])
        self.sort.set_valign(Gtk.Align.CENTER)
        self.sort.add_css_class('pill-drop')
        self.sort.set_selected(1 if settings.get('sort') == 'name' else 0)
        self.sort.connect('notify::selected', self._sort_changed)
        bar.append(self.sort)
        self.select_btn = Gtk.Button(child=Icon(icons.check_circle), tooltip_text=tr('选择项目'),
                                     valign=Gtk.Align.CENTER)
        self.select_btn.add_css_class('circular')
        self.select_btn.connect('clicked', lambda *_: self.set_selecting(True))
        bar.append(self.select_btn)
        main.append(bar)

        # Shown instead of the bar above while items are being selected.
        self.selecting = False
        self.selected = set()
        sel = self.selbar = Gtk.Box(spacing=8, margin_start=34, margin_end=30, margin_top=2, margin_bottom=14,
                                    visible=False)
        everything = Gtk.Button(label=tr('全选'), valign=Gtk.Align.CENTER)
        everything.add_css_class('pill-btn')
        everything.connect('clicked', lambda *_: self._select_all())
        sel.append(everything)
        self.sel_count = Gtk.Label(xalign=0, hexpand=True, margin_start=6)
        self.sel_count.add_css_class('dim-label')
        sel.append(self.sel_count)
        self.sel_actions = Gtk.Box(spacing=4)
        sel.append(self.sel_actions)
        done = Gtk.Button(label=tr('完成'), valign=Gtk.Align.CENTER, margin_start=10)
        done.add_css_class('suggested-action')
        done.add_css_class('pill-btn')
        done.connect('clicked', lambda *_: self.set_selecting(False))
        sel.append(done)
        main.append(sel)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True,
                                valign=Gtk.Align.START, halign=Gtk.Align.START, homogeneous=True,
                                max_children_per_line=12, column_spacing=22, row_spacing=26,
                                margin_start=26, margin_end=26, margin_top=10, margin_bottom=30)
        self.flow.connect('child-activated', lambda box, child: self._activate(child.entry))
        self.empty = Adw.StatusPage(vexpand=True)
        self.stack = Gtk.Stack()
        self.scroller = Gtk.ScrolledWindow(child=self.flow, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.band = _Band()
        grid = Gtk.Overlay(child=self.scroller)
        grid.add_overlay(self.band)
        self.stack.add_named(grid, 'grid')
        self.stack.add_named(self.empty, 'empty')
        main.append(self.stack)
        # Dragging with the mouse draws a rubber band that selects what it touches.
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        drag.connect('drag-update', self._band_update)
        drag.connect('drag-end', self._band_end)
        grid.add_controller(drag)
        self._band_base = None
        self._band_off = False
        drag.connect('drag-begin', self._band_begin)
        # A right click beside the items: undoing and redoing moves.
        beside = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        beside.connect('pressed', self._beside_menu)
        grid.add_controller(beside)
        self.append(main)
        self.nav.select_row(self.nav.get_row_at_index(0))

    def set_sync_progress(self, fraction, text):
        """Remember how far the running sync is; the cloud menu shows it when it is open."""
        self.sync_fraction, self.sync_text = max(0.0, min(1.0, fraction)), text
        if self.sync_bar is not None:
            bar, name, percent = self.sync_bar
            bar.set_fraction(self.sync_fraction)
            name.set_label(text)
            name.set_tooltip_text(text or None)
            percent.set_label('%d%%' % round(self.sync_fraction * 100))

    def set_syncing(self, busy):
        if busy:
            self.set_sync_progress(0.0, '')
        self.cloud.set_child(Icon(icons.syncing if busy else icons.cloud))
        if busy:
            self.cloud.add_css_class('busy')
        else:
            self.cloud.remove_css_class('busy')

    # -- navigation ---------------------------------------------------------

    def _nav_selected(self, box, row):
        if row is None:
            return
        mode = ('docs', 'favs', 'trash', 'deleted')[row.get_index()]
        if mode == self.mode:
            return   # the list only got the focus back, as on returning from a notebook: stay in the folder
        self.mode = mode
        self.folder = self.store.root
        self.refresh()

    def open_folder(self, folder):
        self.folder = folder
        rel = os.path.relpath(folder, self.store.root)
        self.settings['last_folder'] = '' if rel == '.' or rel.startswith('..') else rel
        self.refresh()

    def _sort_changed(self, *_):
        self.settings['sort'] = 'name' if self.sort.get_selected() == 1 else 'date'
        self.refresh()

    def refresh(self):
        store = self.store
        if not os.path.isdir(self.folder):
            self.folder = store.root
        query = self.search.get_text().strip().lower()
        if self.mode == 'docs':
            # A search looks through every folder, not only the one that is open.
            entries = store.find(query) if query else store.entries(self.folder)
            title = tr('搜索结果') if query else tr('文稿') if self.folder == store.root else os.path.basename(self.folder)
        elif self.mode == 'favs':
            entries, title = store.favorites(), tr('收藏')
        elif self.mode == 'trash':
            entries, title = store.trashed(), tr('废纸篓')
        else:
            entries, title = store.archived(), tr('已删除')
        if query and self.mode != 'docs':
            entries = [e for e in entries if query in e.name.lower()]
        if self.settings.get('sort') == 'name':
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        else:
            entries.sort(key=lambda e: -e.mtime)
        self.title.set_label(title)
        while (old := self.crumbs.get_first_child()) is not None:
            self.crumbs.remove(old)
        inside = self.mode == 'docs' and self.folder != store.root and not query
        self.crumbs.set_visible(inside)
        if inside:
            parts = store.rel(os.path.dirname(self.folder)).split(os.sep)
            path = store.root
            for n, name in enumerate([tr('文稿')] + [p for p in parts if p not in ('.', '')]):
                if n:
                    path = os.path.join(path, name)
                    self.crumbs.append(Icon(icons.chevron_right, 12))
                step = Gtk.Button(label=name)
                step.add_css_class('flat')
                step.connect('clicked', lambda b, path=path: self.open_folder(path))
                self._accept_drops(step, lambda path=path: path)
                self.crumbs.append(step)
        self.back.set_visible(self.mode == 'docs' and self.folder != store.root and not query)
        self.new_btn.set_visible(self.mode == 'docs')
        self.empty_btn.set_visible(self.mode == 'trash' and bool(entries))
        self.flow.remove_all()
        self.selected &= {e.path for e in entries}
        for entry in entries:
            self.flow.append(self._cell(entry))
        self._sync_selection()
        if entries:
            self.stack.set_visible_child_name('grid')
        else:
            self.empty.set_title(tr('没有匹配的项目') if query else
                                 {'docs': tr('还没有笔记'), 'favs': tr('还没有收藏'), 'trash': tr('废纸篓是空的'),
                                  'deleted': tr('没有已删除的项目')}[self.mode])
            self.empty.set_description('' if query or self.mode != 'docs' else tr('点击“新建”创建笔记本，或导入一份 PDF。'))
            self.stack.set_visible_child_name('empty')

    # -- cells --------------------------------------------------------------

    def _cell(self, entry):
        if entry.is_dir:
            cover = TextureView(150, 118, halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
            cover.add_css_class('folder-art')
            cover.set_texture(folder_texture(*self.store.folder_style(entry.path), self.get_scale_factor()))
        else:
            cover = TextureView(self.COVER_W, self.COVER_H, halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
            cover.add_css_class('cover')
            png = self.store.cover(entry.path)
            if png:
                try:
                    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
                    k = min(self.COVER_W / texture.get_width(), self.COVER_H / texture.get_height())
                    cover._size = (round(texture.get_width() * k), round(texture.get_height() * k))
                    cover.set_texture(texture)
                except GLib.Error:
                    pass
        frame = Gtk.Overlay(child=cover, halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        if self.mode in ('docs', 'favs'):
            fav = self.store.is_favorite(entry.path)
            star = Gtk.Button(child=Icon(icons.star_filled if fav else icons.star, 18), halign=Gtk.Align.END,
                              valign=Gtk.Align.START, margin_top=28 if entry.is_dir else 4, margin_end=4,
                              tooltip_text=tr('取消收藏') if fav else tr('收藏'))
            star.add_css_class('flat')
            star.add_css_class('star')
            if fav:
                star.add_css_class('on')
            star.connect('clicked', lambda *_: (self.store.toggle_favorite(entry.path), self.refresh()))
            frame.add_overlay(star)

        name_box = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER)
        name_box.append(Gtk.Label(label=entry.name, ellipsize=3, max_width_chars=14))
        name_box.append(Icon(icons.chevron, 14))
        name = Gtk.MenuButton(child=name_box, halign=Gtk.Align.CENTER)
        name.add_css_class('flat')
        name.set_create_popup_func(lambda button: button.set_popover(self._menu(entry)))
        date = Gtk.Label(label=time.strftime(tr('%Y年%-m月%-d日 %H:%M'), time.localtime(entry.mtime)))
        date.add_css_class('caption')
        date.add_css_class('dim-label')

        holder = Gtk.Box(height_request=self.COVER_H, halign=Gtk.Align.CENTER)
        holder.append(frame)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=186)
        box.append(holder)
        box.append(name)
        box.append(date)
        child = Gtk.FlowBoxChild(child=box)
        child.entry = entry
        # With a mouse, a right click anywhere on the item opens the same menu as the arrow.
        context = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context.connect('pressed', lambda g, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED), name.popup()))
        child.add_controller(context)
        if self.mode == 'docs':
            # As in a file manager: items are dragged onto a folder, or onto a step of the
            # path above, to be moved there.
            source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            source.connect('prepare', self._drag_prepare, entry)
            source.connect('drag-begin', lambda s, drag: s.set_icon(Gtk.WidgetPaintable.new(cover), 20, 20))
            source.connect('drag-end', lambda *_: self._dragging.clear())
            source.connect('drag-cancel', lambda *_: self._dragging.clear() or False)
            child.add_controller(source)
            if entry.is_dir:
                self._accept_drops(child, lambda: entry.path)
        return child

    # -- dragging items into folders ------------------------------------------

    _DRAG = 'inkpad-library-items'   # what a drag carries; the paths themselves stay in the view

    def _drag_prepare(self, source, x, y, entry):
        # Dragging one of several selected items takes them all along.
        if self.selecting and entry.path in self.selected:
            self._dragging[:] = [e.path for e in self._picked()]
        else:
            self._dragging[:] = [entry.path]
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, self._DRAG))

    def _accept_drops(self, widget, folder):
        """Let dragged items be dropped on a widget, to go into the folder it stands for."""
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def dropped(target, value, x, y):
            paths = list(self._dragging)
            if value != self._DRAG or not paths:
                return False
            # After the drag has ended: the item dropped on may itself be rebuilt.
            GLib.idle_add(lambda: self.move_items(paths, folder()) and False)
            return True

        target.connect('drop', dropped)
        widget.add_controller(target)

    def move_items(self, paths, folder):
        """Move items into a folder, as one step that can be undone. Items already there, and
        a folder asked to go inside itself, are left alone."""
        done = []
        for path in paths:
            if os.path.dirname(path) == folder or folder == path or (folder + os.sep).startswith(path + os.sep):
                continue
            try:
                done.append((path, self.store.move(path, folder)))
            except Exception as exc:
                self.on_error(tr('操作失败'), exc)
                break
        if done:
            self._undo.append(done)
            del self._undo[:-50]
            self._redo.clear()
            where = tr('文稿') if folder == self.store.root else os.path.basename(folder)
            self._toast(tr('已将 %d 项移到“%s”') % (len(done), where), tr('撤销'), self.undo_move)
        self.selected.clear()
        self.selecting = False
        self.refresh()
        return len(done)

    def _replay(self, source, dest):
        """Take the last move back (or do it again): everything returns to where, and to the
        name, it had."""
        if self.mode != 'docs' or not source:
            return False
        back = []
        for before, after in reversed(source.pop()):
            if not os.path.exists(after) or not os.path.isdir(os.path.dirname(before)):
                continue   # changed since by something else; leave it be
            try:
                back.append((after, self.store.move(after, os.path.dirname(before), os.path.basename(before))))
            except Exception as exc:
                self.on_error(tr('操作失败'), exc)
                break
        if back:
            dest.append(back)
        self.refresh()
        return True

    def undo_move(self):
        if self._replay(self._undo, self._redo):
            self._toast(tr('已撤销移动'))
            return True
        return False

    def redo_move(self):
        if self._replay(self._redo, self._undo):
            self._toast(tr('已重做移动'))
            return True
        return False

    def _beside_menu(self, gesture, n, x, y):
        if self.mode != 'docs' or not (self._undo or self._redo):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        rows = []
        if self._undo:
            rows.append((tr('撤销移动'), self.undo_move, None, icons.undo))
        if self._redo:
            rows.append((tr('重做移动'), self.redo_move, None, icons.redo))
        popover = menu_popover(rows)
        popover.set_parent(gesture.get_widget())
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        popover.connect('closed', lambda *_: GLib.idle_add(popover.unparent))
        popover.popup()

    def _menu(self, entry):
        if self.mode == 'deleted':
            return menu_popover([(tr('恢复到文稿'), lambda: self._do(self.store.unarchive, entry.path), None, icons.restore)])
        if self.mode == 'trash':
            return menu_popover([(tr('恢复'), lambda: self._do(self.store.restore, entry.path), None, icons.restore),
                                 (tr('永久删除'), lambda: self._delete(entry), 'danger', icons.trash)])
        rows = [(tr('重命名'), lambda: self._rename(entry), None, icons.rename)]
        if entry.is_dir:
            rows += [(tr('自定义颜色'), lambda: self._folder_color(entry), None, icons.palette),
                     (tr('设置图标'), lambda: self._folder_icon(entry), None, icons.smile)]
        fav = self.store.is_favorite(entry.path)
        if entry.is_dir:
            rows.append((tr('导出文件夹…'), lambda: self.export_folder(entry.path), None, icons.share))
        rows += [None,
                 (tr('移动到…'), lambda: self._move(entry), None, icons.move),
                 (tr('取消收藏') if fav else tr('收藏'), lambda: self._do(self.store.toggle_favorite, entry.path), None, icons.star),
                 None,
                 (tr('移到废纸篓'), lambda: self._do(self.store.trash, entry.path), 'danger', icons.trash)]
        return menu_popover(rows)

    def _choice_dialog(self, heading, cells, columns, on_pick):
        """A small dialog with a grid of buttons; cells are (widget, value)."""
        grid = Gtk.Grid(row_spacing=4, column_spacing=4, halign=Gtk.Align.CENTER)
        dialog = Adw.AlertDialog(heading=heading, extra_child=grid)
        dialog.add_response('cancel', tr('取消'))
        for n, (child, value) in enumerate(cells):
            button = Gtk.Button(child=child)
            button.add_css_class('flat')
            button.add_css_class('choice-cell')
            button.connect('clicked', lambda b, v=value: (dialog.close(), on_pick(v), self.refresh()))
            grid.attach(button, n % columns, n // columns, 1, 1)
        dialog.present(self)

    def _folder_color(self, entry):
        cells = [(Icon(icons.swatch(c), 34, tint=False), c) for c in FOLDER_COLORS]
        self._choice_dialog(tr('文件夹颜色'), cells, 4, lambda c: self.store.set_folder_style(entry.path, color=c))

    def _folder_icon(self, entry):
        cells = [(Icon(icons.glyph(e) if e else icons.none, 30), e) for e in FOLDER_ICONS]
        self._choice_dialog(tr('文件夹图标'), cells, 6, lambda e: self.store.set_folder_style(entry.path, icon=e))

    # -- selecting several items ---------------------------------------------

    def set_selecting(self, on):
        self.selecting = on
        if not on:
            self.selected.clear()
        self._sync_selection()

    def _cells(self):
        child = self.flow.get_first_child()
        while child is not None:
            yield child
            child = child.get_next_sibling()

    def _select_all(self):
        paths = {c.entry.path for c in self._cells()}
        self.selected = set() if self.selected == paths else paths
        self._sync_selection()

    def _sync_selection(self):
        """Bring the bars, the action buttons and the cell highlights in line with the selection."""
        on = self.selecting
        self.selbar.set_visible(on)
        self.select_btn.get_parent().set_visible(not on)
        for child in self._cells():
            if (on and child.entry.path in self.selected) or (not on and child.entry.path == self.cursor):
                child.add_css_class('picked')
            else:
                child.remove_css_class('picked')
        if not on:
            return
        n = len(self.selected)
        self.sel_count.set_label(tr('已选择 %d 项') % n)
        while (old := self.sel_actions.get_first_child()) is not None:
            self.sel_actions.remove(old)
        if self.mode == 'deleted':
            actions = [(icons.restore, tr('恢复到文稿'), lambda: self._each(self.store.unarchive), None)]
        elif self.mode == 'trash':
            actions = [(icons.restore, tr('恢复'), self._sel_restore, None),
                       (icons.trash, tr('永久删除'), self._sel_delete, 'danger')]
        else:
            actions = [(icons.share, tr('导出'), self._sel_export, None),
                       (icons.duplicate, tr('复制'), self._sel_duplicate, None),
                       (icons.move, tr('移动'), self._sel_move, None),
                       (icons.trash, tr('废纸篓'), self._sel_trash, 'danger')]
        for draw, text, fn, css in actions:
            inner = Gtk.Box(spacing=6)
            inner.append(Icon(draw, 18))
            inner.append(Gtk.Label(label=text))
            button = Gtk.Button(child=inner, valign=Gtk.Align.CENTER, sensitive=n > 0)
            button.add_css_class('flat')
            if css:
                button.add_css_class(css)
            button.connect('clicked', lambda b, fn=fn: fn())
            self.sel_actions.append(button)

    def _picked(self):
        return [c.entry for c in self._cells() if c.entry.path in self.selected]

    def _each(self, fn):
        """Apply a store operation to every selected item, then leave selection mode."""
        for entry in self._picked():
            try:
                fn(entry.path)
            except Exception as exc:
                self.on_error(tr('操作失败'), exc)
                break
        self.selected.clear()
        self.selecting = False
        self.refresh()

    def _sel_trash(self):
        self._each(self.store.trash)

    def _sel_restore(self):
        self._each(self.store.restore)

    def _sel_duplicate(self):
        self._each(self.store.duplicate)

    def _sel_delete(self):
        confirm(self, tr('永久删除 %d 项？') % len(self.selected),
                tr('它会从废纸篓消失，但仍保留在本机缓存和云端，直到你在设置中清理缓存。'), tr('永久删除'),
                lambda: self._each(self.store.delete))

    def _pick_folder(self, heading, moving, on_pick):
        """Ask which folder to move things to: the library's folders as a tree to tap in.
        moving are the folders being moved, which cannot go inside themselves."""
        root = self.store.root
        folders = [f for f in self.store.folders()
                   if not any(f == p or f.startswith(p + os.sep) for p in moving)]
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        rows.add_css_class('boxed-list')
        parents = {os.path.dirname(f) for f in folders if f != root}
        opened = {root}   # only the top level shows at first; the arrows open the rest
        arrows = {}

        def show():
            for n, f in enumerate(folders):
                row = rows.get_row_at_index(n)
                above = f
                while above != root and (above := os.path.dirname(above)) in opened:
                    pass
                seen = f == root or (above == root and root in opened)
                if not seen and row.is_selected():
                    rows.unselect_row(row)
                row.set_visible(seen)
            for f, arrow in arrows.items():
                arrow.set_child(Icon(icons.chevron if f in opened else icons.chevron_right, 14))

        def toggle(f):
            opened.symmetric_difference_update({f})
            show()

        for f in folders:
            depth = 0 if f == root else self.store.rel(f).count(os.sep) + 1
            line = Gtk.Box(spacing=6, margin_start=4 + 22 * depth, margin_end=10, margin_top=3, margin_bottom=3)
            if f in parents and f != root:
                arrow = arrows[f] = Gtk.Button(valign=Gtk.Align.CENTER)
                arrow.add_css_class('flat')
                arrow.add_css_class('circular')
                arrow.connect('clicked', lambda b, f=f: toggle(f))
                line.append(arrow)
            else:
                line.append(Gtk.Box(width_request=34, height_request=34))   # keeps the names in line
            line.append(Icon(icons.documents, 18))
            line.append(Gtk.Label(label=tr('文稿') if f == root else os.path.basename(f), xalign=0, ellipsize=3))
            rows.append(line)
        show()
        scroller = Gtk.ScrolledWindow(child=rows, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=360, min_content_width=320)
        dialog = Adw.AlertDialog(heading=heading, extra_child=scroller)
        dialog.add_response('cancel', tr('取消'))
        dialog.add_response('move', tr('移动'))
        dialog.set_response_appearance('move', Adw.ResponseAppearance.SUGGESTED)
        # Nothing is chosen to begin with, so that nothing is moved by mistake.
        dialog.set_response_enabled('move', False)
        rows.connect('selected-rows-changed', lambda *_: dialog.set_response_enabled('move', rows.get_selected_row() is not None))

        def response(d, r):
            row = rows.get_selected_row()
            if r == 'move' and row is not None:
                on_pick(folders[row.get_index()])
        dialog.connect('response', response)
        dialog.present(self)

    def _sel_move(self):
        entries = self._picked()
        self._pick_folder(tr('移动 %d 项到') % len(entries), [e.path for e in entries if e.is_dir],
                          lambda folder: self.move_items([e.path for e in entries], folder))

    def _sel_export(self):
        entries = self._picked()
        name = (entries[0].name if len(entries) == 1 else APP_NAME + time.strftime(' %Y-%m-%d')) + '.zip'
        dialog = Gtk.FileDialog(title=tr('导出'), initial_name=name, filters=self._zip_filter())

        def done(dialog, result):
            try:
                dest = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            try:
                count = self.store.export_paths([e.path for e in entries], dest)
            except Exception as exc:
                self.on_error(tr('导出失败'), exc)
                return
            self._toast(tr('已导出 %d 本笔记') % count)
            self.set_selecting(False)

        dialog.save(self.get_root(), None, done)

    def _band_begin(self, gesture, x, y):
        # A drag that starts on an item carries the item; the rubber band is for the space between.
        ok, point = gesture.get_widget().compute_point(self.flow, Graphene.Point().init(x, y))
        self._band_off = ok and self.flow.get_child_at_pos(int(point.x), int(point.y)) is not None

    def _band_update(self, gesture, dx, dy):
        if self._band_off:
            return
        if self._band_base is None:
            device = gesture.get_device()
            touch = device is not None and device.get_source() == Gdk.InputSource.TOUCHSCREEN
            if touch or math.hypot(dx, dy) < 8 or self.stack.get_visible_child_name() != 'grid':
                return   # fingers scroll; a click is not a drag
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            # Holding Ctrl or Shift adds to what is already selected.
            keep = gesture.get_current_event_state() & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
            self._band_base = set(self.selected) if keep and self.selecting else set()
            self.selecting = True
        ok, x0, y0 = gesture.get_start_point()
        x, y, w, h = min(x0, x0 + dx), min(y0, y0 + dy), abs(dx), abs(dy)
        self.band.set_rect((x, y, w, h))
        area = Graphene.Rect().init(x, y, w, h)
        hit = set()
        for child in self._cells():
            ok, bounds = child.compute_bounds(self.band)
            if ok and bounds.intersection(area)[0]:
                hit.add(child.entry.path)
        self.selected = self._band_base | hit
        self._sync_selection()

    def _band_end(self, gesture, dx, dy):
        if self._band_base is not None:
            self._band_base = None
            self.band.set_rect(None)
            if not self.selected:
                self.set_selecting(False)

    def _do(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:
            self.on_error(tr('操作失败'), exc)
        self.refresh()

    def _activate(self, entry):
        if self.selecting:
            self.selected ^= {entry.path}
            self._sync_selection()
            return
        if self.mode in ('trash', 'deleted'):
            return
        if entry.is_dir:
            # A folder opens on a double click; the first click only marks it.
            now, last = GLib.get_monotonic_time(), (self.cursor, self._clicked_at)
            self.cursor, self._clicked_at = entry.path, now
            wait = (self.get_settings().get_property('gtk-double-click-time') or 400) * 1000
            if last[0] != entry.path or now - last[1] > max(wait, 500_000):
                self._sync_selection()
                return
            self.cursor = None
            self.search.set_text('')   # a folder found by searching opens as itself
            self.mode = 'docs'
            self.nav.select_row(self.nav.get_row_at_index(0))
            self.open_folder(entry.path)
        else:
            self.on_open(entry.path)

    # -- actions ------------------------------------------------------------

    def _create(self, template, title, fallback, cover=None):
        try:
            path = self.store.new_notebook(self.folder, template, title.replace('/', '-') or fallback, cover)
        except Exception as exc:
            self.on_error(tr('无法创建'), exc)
            return
        self.on_open(path)

    def _new_notebook(self):
        def create(template, title, cover):
            self.settings['template'] = dict(template, color=list(template['color']))
            self._create(template, title, tr('未命名笔记'), cover)

        PaperDialog(self.settings['template'], create, heading=tr('新建笔记本'), apply_label=tr('创建笔记本'),
                    title='').present(self)

    def _new_board(self):
        start = {'w': 1, 'h': 1, 'paper': 'dots', 'color': (1.0, 1.0, 1.0)}
        PaperDialog(start, lambda template, title, cover: self._create(template, title, tr('未命名白板')),
                    heading=tr('新建白板'), apply_label=tr('创建白板'), title='', board=True).present(self)

    def _new_folder(self):
        ask_text(self, tr('新建文件夹'), tr('新文件夹'), lambda name: self._do(self.store.new_folder, self.folder, name))

    def _rename(self, entry):
        ask_text(self, tr('重命名'), entry.name, lambda name: self._do(self.store.rename, entry.path, name))

    def _move(self, entry):
        self._pick_folder(tr('移动“%s”到') % entry.name, [entry.path] if entry.is_dir else [],
                          lambda folder: self.move_items([entry.path], folder))

    def _toast(self, text, button=None, action=None):
        root = self.get_root()
        if hasattr(root, 'toast'):
            root.toast(text, button, action)

    @staticmethod
    def _zip_filter():
        zips = Gtk.FileFilter()
        zips.set_name('Zip')
        zips.add_pattern('*.zip')
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(zips)
        return store

    def export_folder(self, folder):
        """Save a folder, or the whole library when folder is its root, as one zip file."""
        whole = folder == self.store.root
        name = (APP_NAME + time.strftime(' %Y-%m-%d') if whole else os.path.basename(folder)) + '.zip'
        dialog = Gtk.FileDialog(title=tr('导出整个文稿库') if whole else tr('导出文件夹'), initial_name=name,
                                filters=self._zip_filter())

        def done(dialog, result):
            try:
                dest = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            try:
                count = self.store.export_archive(folder, dest)
            except Exception as exc:
                self.on_error(tr('导出失败'), exc)
                return
            self._toast(tr('已导出 %d 本笔记') % count)

        dialog.save(self.get_root(), None, done)

    def import_folder(self):
        """Pick a folder and import everything in it, subfolders and all, behind a progress window."""
        dest = self.folder if self.mode == 'docs' else self.store.root
        chooser = Gtk.FileDialog(title=tr('选择要导入的文件夹（Goodnotes、PDF 或 Inkpad 文件）'))

        def chosen(chooser, result):
            try:
                source = chooser.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            self.run_import(source, dest)

        chooser.select_folder(self.get_root(), None, chosen)

    def run_import(self, source, dest):
        bar = Gtk.ProgressBar(show_text=True, width_request=380, ellipsize=Pango.EllipsizeMode.MIDDLE)
        dialog = Adw.AlertDialog(heading=tr('正在导入…'), extra_child=bar)
        dialog.set_can_close(False)
        dialog.present(self.get_root())

        def progress(fraction, text):
            GLib.idle_add(lambda: (bar.set_fraction(fraction), bar.set_text(text or '%d%%' % round(fraction * 100))) and False)

        def work():
            try:
                result, error = self.store.import_tree(source, dest, progress), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(finish, result, error)

        def finish(result, error):
            dialog.set_can_close(True)
            dialog.force_close()
            self.refresh()
            if error is not None:
                self.on_error(tr('导入失败'), error)
                return False
            done, failed = result
            if failed:
                lines = '\n'.join('%s：%s' % item for item in failed[:12])
                self.on_error(tr('已导入 %d 本，%d 个文件未能导入') % (done, len(failed)), lines)
            else:
                self._toast(tr('已导入 %d 本笔记') % done)
            return False

        threading.Thread(target=work, daemon=True).start()

    def import_archive(self):
        folder = self.folder if self.mode == 'docs' else self.store.root
        dialog = Gtk.FileDialog(title=tr('导入存档'), filters=self._zip_filter())

        def done(dialog, result):
            try:
                src = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            try:
                count = self.store.import_archive(src, folder)
            except Exception as exc:
                self.on_error(tr('导入失败'), exc)
                return
            self.refresh()
            self._toast(tr('已导入 %d 本笔记') % count)

        dialog.open(self.get_root(), None, done)

    def _delete(self, entry):
        confirm(self, tr('永久删除“%s”？') % entry.name,
                tr('它会从废纸篓消失，但仍保留在本机缓存和云端，直到你在设置中清理缓存。'), tr('永久删除'),
                lambda: self._do(self.store.delete, entry.path))

    def _empty_trash(self):
        def run():
            for entry in self.store.trashed():
                self._do(self.store.delete, entry.path)

        confirm(self, tr('清空废纸篓？'), tr('其中的内容会从废纸篓消失，但仍保留在本机缓存和云端，直到你在设置中清理缓存。'), tr('清空'), run)
