"""Editor sidebar: page thumbnails in two columns, outline and bookmarks."""
import cairo
from gi.repository import GLib, Gtk

from .i18n import tr
from . import icons
from .icons import Icon, TextureView
from .model import ink_on, page_surface
from .render import texture_from_surface, worker


class Sidebar(Gtk.Box):
    WIDTH = 124

    def __init__(self, on_pick, page_menu, on_add):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class('sidebar')
        self.doc = None
        self._on_pick, self._page_menu, self._on_add = on_pick, page_menu, on_add
        self._views = {}
        self._marks = {}
        self._dirty = set()
        self._timer = 0

        tabs = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=10, margin_bottom=8)
        tabs.add_css_class('linked')
        self.stack = Gtk.Stack()
        first = None
        for name, draw, tip in (('thumbs', icons.grid, tr('缩略图')), ('outline', icons.outline, tr('大纲')),
                                ('marks', icons.bookmark, tr('书签'))):
            button = Gtk.ToggleButton(child=Icon(draw), tooltip_text=tip, width_request=46)
            if first is None:
                first = button
                button.set_active(True)
            else:
                button.set_group(first)
            button.connect('toggled', lambda b, n=name: b.get_active() and self.stack.set_visible_child_name(n))
            tabs.append(button)
        self.append(tabs)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                                min_children_per_line=2, max_children_per_line=2, homogeneous=True,
                                valign=Gtk.Align.START, margin_start=10, margin_end=10, margin_bottom=12,
                                column_spacing=6, row_spacing=6)
        self.flow.add_css_class('thumbs')
        self.flow.connect('child-activated', lambda box, child: child.get_index() < len(self.doc.pages)
                          and self._on_pick(child.get_index()))
        self.stack.add_named(Gtk.ScrolledWindow(child=self.flow, vexpand=True,
                                                hscrollbar_policy=Gtk.PolicyType.NEVER), 'thumbs')
        self.outline = self._list()
        self.marks = self._list()
        self.stack.add_named(Gtk.ScrolledWindow(child=self.outline, vexpand=True), 'outline')
        self.stack.add_named(Gtk.ScrolledWindow(child=self.marks, vexpand=True), 'marks')
        self.append(self.stack)
        self.connect('map', lambda *_: self._schedule())

    def _list(self):
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class('navigation-sidebar')
        box.set_placeholder(Gtk.Label(label=tr('暂无内容'), margin_top=30, css_classes=['dim-label']))
        box.connect('row-activated', lambda b, row: self._on_pick(row.page_index))
        return box

    def set_document(self, doc):
        if self.doc is not None:
            self.doc.listeners.remove(self._event)
        self.doc = doc
        doc.listeners.append(self._event)
        self._rebuild()

    def _event(self, kind, *args):
        if kind == 'pages':
            self._rebuild()
        elif kind == 'meta':
            self._refresh_meta()
        elif kind in ('added', 'dirty'):
            self._dirty.add(args[0])
            self._schedule()

    def _rebuild(self):
        self.flow.remove_all()
        self._views, self._marks = {}, {}
        for n, page in enumerate(self.doc.pages):
            height = round(self.WIDTH * (0.72 if page.board else page.h / page.w))
            view = TextureView(self.WIDTH, min(height, 200), halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
            if height > 200:
                view._size = (round(200 * page.w / page.h), 200)
            view.add_css_class('thumb')
            mark = Icon(icons.bookmark_filled, 18)
            mark.set_halign(Gtk.Align.END)
            mark.set_valign(Gtk.Align.START)
            mark.add_css_class('mark')
            frame = Gtk.Overlay(child=view, halign=Gtk.Align.CENTER)
            frame.add_overlay(mark)
            more = Gtk.MenuButton(child=Icon(icons.dots, 16), tooltip_text=tr('页面操作'))
            more.add_css_class('flat')
            more.set_create_popup_func(lambda b, page=page: b.set_popover(self._page_menu(page)))
            row = Gtk.Box()
            row.append(Gtk.Label(label=str(n + 1), xalign=0, hexpand=True, margin_start=6))
            row.append(more)
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.END)
            cell.append(frame)
            cell.append(row)
            self.flow.append(cell)
            self._views[page] = view
            self._marks[page] = mark
        add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        add_box.append(Icon(icons.plus))
        add_box.append(Gtk.Label(label=tr('添加白板') if self.doc.pages[0].board else tr('添加页面')))
        add = Gtk.Button(child=add_box, width_request=self.WIDTH, height_request=150, valign=Gtk.Align.START,
                         halign=Gtk.Align.CENTER)
        add.add_css_class('add-page')
        add.connect('clicked', lambda *_: self._on_add())
        self.flow.append(add)
        self._dirty = set(self.doc.pages)
        self._refresh_meta()
        self._schedule()

    def _refresh_meta(self):
        self.outline.remove_all()
        self.marks.remove_all()
        for n, page in enumerate(self.doc.pages):
            mark = self._marks.get(page)
            if mark is not None:
                mark.set_visible(page.bookmark)
            for box, text in ((self.outline, page.outline), (self.marks, tr('第 %d 页') % (n + 1) if page.bookmark else None)):
                if text:
                    line = Gtk.Box(spacing=8)
                    line.append(Gtk.Label(label=text, xalign=0, hexpand=True, ellipsize=3))
                    line.append(Gtk.Label(label=str(n + 1), css_classes=['dim-label']))
                    row = Gtk.ListBoxRow(child=line)
                    row.page_index = n
                    box.append(row)

    def select(self, index):
        child = self.flow.get_child_at_index(index)
        if child is not None and not child.is_selected():
            self.flow.select_child(child)

    # -- thumbnails, rendered lazily while the sidebar is visible -----------

    def _schedule(self):
        if not self._timer and self._dirty:
            self._timer = GLib.timeout_add(350, self._flush)

    def _flush(self):
        self._timer = 0
        if not self.get_mapped():
            return False
        scale = self.get_scale_factor()
        for _ in range(min(12, len(self._dirty))):
            page = self._dirty.pop()
            view = self._views.get(page)
            if view is None:
                continue
            pw, ph = view._size[0] * scale, view._size[1] * scale
            if page.pdf is not None:
                worker().submit(3, lambda page=page: page in self._views, page, pw, ph,
                                lambda surface, page=page: self._compose(page, surface, False))
            else:
                view.set_texture(texture_from_surface(page_surface(page, 0, (pw, ph))))
        self._schedule()
        return False

    def _compose(self, page, surface, paper):
        view = self._views.get(page)
        if view is None:
            return
        cr = cairo.Context(surface)
        cr.scale(surface.get_width() / page.w, surface.get_height() / page.h)
        ink_on(page)
        for it in page.items:
            it.draw(cr)
        view.set_texture(texture_from_surface(surface))
