"""Main window: the notebook library and the Goodnotes-style editor."""
import copy
import math
import os
import threading
import time

import cairo
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk, Pango

from .i18n import tr
from . import APP_NAME, VERSION, gdrive, i18n, icons, settings
from .canvas import Canvas
from .dialogs import PaperDialog, ask_cover, ask_page, ask_text
from .icons import Icon, TextureView
from .minimap import Minimap
from .library import LibraryView, Store, fitted, menu_popover
from .model import Changed, Document, Image, Page, Stroke, export_pdf, export_png, find_text, notebook_id, page_surface
from .prefs import PrefsDialog, apply_theme
from .render import texture_from_surface
from .sidebar import Sidebar

LASER_COLORS = [(1.0, 0.16, 0.2), (0.1, 0.8, 0.3), (0.16, 0.5, 1.0)]
ERASER_SIZES = [4.0, 9.0, 18.0]
TEXT_SIZES = [12.0, 18.0, 28.0]

# Names are translated where they are shown, so the language can change while running.
TOOLS = [('lasso', icons.lasso, '套索'), ('pen', icons.pen, '笔'), ('eraser', icons.eraser, '橡皮擦'),
         ('hl', icons.highlighter, '荧光笔'), ('shape', icons.shapes, '形状'), ('text', icons.text, '文本'),
         ('note', icons.note, '便签'), ('tape', icons.tape, '胶带'), ('laser', icons.laser, '激光笔')]
TAPE_COLORS = [(0.96, 0.82, 0.36), (0.55, 0.8, 0.95), (0.95, 0.6, 0.65), (0.6, 0.85, 0.6), (0.72, 0.72, 0.76)]
TAPE_WIDTHS = [10.0, 16.0, 26.0]
NOTE_COLORS = [(1.0, 0.93, 0.55), (1.0, 0.8, 0.86), (0.75, 0.9, 1.0), (0.8, 0.95, 0.75)]
PENS = [('fountain', icons.fountain, '钢笔'), ('ball', icons.ball, '圆珠笔'), ('brush', icons.brush, '画笔')]
PEN_ICON = {name: draw for name, draw, _ in PENS}
ERASER_MODES = {'standard': '标准', 'stroke': '笔画'}
PALETTE = [(0, 0, 0), (0.37, 0.37, 0.37), (0.6, 0.6, 0.6), (0.82, 0.82, 0.82), (1, 1, 1),
           (1.0, 0.1, 0.1), (0.53, 0.18, 0.57), (0.95, 0.33, 0.33), (1.0, 0.6, 0.6), (0.97, 0.58, 0.11),
           (0.04, 0.47, 1.0), (0.05, 0.29, 0.56), (0.03, 0.62, 0.36), (0.49, 0.76, 0.22), (1.0, 1.0, 0.5)]


def _page_range(text, total):
    """'1-3, 5' -> [0, 1, 2, 4]; empty means every page (None). Raises ValueError when unreadable."""
    text = text.replace('，', ',').replace('、', ',').replace('–', '-').strip()
    if not text:
        return None
    out = []
    for part in text.split(','):
        a, _, b = part.strip().partition('-')
        first, last = int(a), int(b or a)
        if not 1 <= first <= last <= total:
            raise ValueError(part)
        out += [n - 1 for n in range(first, last + 1) if n - 1 not in out]
    return out


def _pct_to_width(pct):
    return 0.3 + pct * 0.047


def _width_to_pct(width):
    return max(0, min(100, round((width - 0.3) / 0.047)))


def _filters(*specs):
    store = Gio.ListStore.new(Gtk.FileFilter)
    for name, patterns in specs:
        f = Gtk.FileFilter()
        f.set_name(name)
        for pattern in patterns:
            f.add_pattern(pattern)
        store.append(f)
    return store


class Window(Adw.ApplicationWindow):
    def __init__(self, app, store=None):
        super().__init__(application=app, default_width=1240, default_height=840, title=APP_NAME)
        self.st = settings.load()
        apply_theme(self.st.get('theme', 'system'))
        self.store = store or Store()
        self.doc = None
        self.tabs = []   # open notebooks: {'doc': Document, 'view': zoom and scroll, or None}
        self.tool = self._prev_tool = 'pen'
        self._save_timer = 0
        self._save_thread = None
        self._page_clip = None
        self._loading = False

        self.canvas = Canvas()
        self.canvas.connect('view-changed', lambda *_: self._update_status())
        self.canvas.on_erase_done = self._erase_done
        self.canvas.on_pull_add = self._pull_add

        self._sync_busy = False
        self._sync_error = None
        self._sync_next = None    # a sync asked for while one was running: (quiet, only the open notebook?)
        self._edit_timer = 0      # runs out a moment after the last stroke: the open notebook is synced
        self._open_synced = 0.0   # when the open notebook was last synced, and how long that took
        self._open_took = 0.0
        self.library = LibraryView(self.store, self.st, self.open_notebook, self._import_to_library, self._error,
                                   self._cloud_popover, self.show_settings)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=160)
        self.stack.add_named(self.library, 'library')
        self.stack.add_named(self._build_editor(), 'editor')
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.set_content(self.toasts)

        self.connect('close-request', self._close_request)
        # For the input log: every pointer event the window is given, before anything handles it.
        watch = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        watch.connect('event', self._watch_event)
        self.add_controller(watch)
        self._watched = [0, None]
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)
        self._rebuild_options()
        self._sync()
        self.library.refresh()
        # Sync shortly after launch, then every ten minutes: an open notebook too, so that what
        # is written on another device turns up in it.
        GLib.timeout_add_seconds(2, lambda: self.sync_now(True) and False)
        GLib.timeout_add_seconds(600, self._periodic_sync)
        GLib.timeout_add_seconds(30, self._watch_open)

    def _periodic_sync(self):
        if self.get_application() is None:
            return False   # this window was replaced or closed
        self.sync_now(True)
        return True

    # A notebook that is open is one that is being worked on: it is synced by itself when it is
    # opened, a moment after every pause in writing, and looked up on Drive twice a minute for
    # what other devices wrote. Only that notebook is touched, so this costs a few requests.

    def _watch_open(self):
        if self.get_application() is None:
            return False
        if self.in_editor() and self.is_active() and not self._edit_timer:
            self._sync_open()
        return True

    def _sync_open(self):
        if self.in_editor() and self.doc is not None and self.doc.path and gdrive.connected():
            self.sync_now(True, only=True)

    def _sync_after_edit(self):
        """Sync the open notebook once the pen has rested: after the autosave, and not sooner
        after the last sync than that one took, so that a large notebook is not sent non-stop."""
        if self._edit_timer:
            GLib.source_remove(self._edit_timer)
        wait = max(6.0, self._open_synced + 4 * self._open_took - time.monotonic())
        self._edit_timer = GLib.timeout_add(int(wait * 1000), self._edit_sync)

    def _edit_sync(self):
        self._edit_timer = 0
        if self.canvas._op is not None or self._sync_busy:
            self._sync_after_edit()
        else:
            self._sync_open()
        return False

    def change_language(self, value):
        """Switch the interface language now, by rebuilding the window in place."""
        self.canvas._cancel()
        self.save_now()
        self.st['language'] = value
        settings.save(self.st)
        i18n.set_language(value)
        path = self.doc.path if self.in_editor() and self.doc is not None else None
        new = Window(self.get_application(), self.store)
        new.set_default_size(self.get_width(), self.get_height())
        if self.is_maximized():
            new.maximize()
        new.present()
        if path:
            new.open_notebook(path)
        self.destroy()
        new.show_settings()
        return False

    # -- editor construction ------------------------------------------------

    def _build_editor(self):
        header = Adw.HeaderBar()
        header.add_css_class('gn-header')

        home = Gtk.Button(child=Icon(icons.home), tooltip_text=tr('文稿库'))
        home.connect('clicked', lambda *_: self.go_home())
        header.pack_start(home)
        self.title_label = Gtk.Label(ellipsize=3, max_width_chars=22)
        title_box = Gtk.Box(spacing=4)
        title_box.append(self.title_label)
        title_box.append(Icon(icons.chevron, 14))
        title_btn = Gtk.MenuButton(child=title_box, tooltip_text=tr('笔记本'))
        title_btn.add_css_class('title-btn')
        title_btn.set_create_popup_func(lambda b: b.set_popover(self._title_popover()))
        self.tab_count = Gtk.Label(visible=False, valign=Gtk.Align.CENTER)
        self.tab_count.add_css_class('tab-count')
        title_box.append(self.tab_count)
        header.pack_start(title_btn)
        header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        side_btn = Gtk.ToggleButton(child=Icon(icons.sidebar), tooltip_text=tr('页面、大纲与书签'))
        side_btn.add_css_class('side')
        header.pack_start(side_btn)

        tools = self._tools_box = Gtk.Box(spacing=3)
        tools.add_css_class('toolgroup')
        self._tool_buttons = {}
        first = None
        for name, draw, tip in TOOLS:
            button = Gtk.ToggleButton(child=Icon(draw), tooltip_text=tr(tip))
            button.add_css_class('tool')
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(name == self.tool)
            button.connect('toggled', self._tool_toggled, name)
            button.connect('clicked', self._tool_clicked, name)
            self._tool_buttons[name] = button
            tools.append(button)
        image_btn = self._tool_buttons['image'] = Gtk.Button(child=Icon(icons.image), tooltip_text=tr('插入图片'))
        image_btn.add_css_class('tool')
        image_btn.connect('clicked', lambda *_: self._insert_image())
        tools.append(image_btn)
        ruler = self._tool_buttons['ruler'] = Gtk.ToggleButton(
            child=Icon(icons.ruler),
            tooltip_text=tr('尺子：沿它的边缘画线会吸附成直线。手指拖动移动，双指旋转；鼠标拖动移动，滚轮旋转'))
        ruler.add_css_class('tool')
        ruler.connect('toggled', lambda b: self.canvas.set_ruler(b.get_active()))
        tools.append(ruler)
        header.set_title_widget(tools)
        self.apply_hidden_tools()

        find = Gtk.Button(child=Icon(icons.search), tooltip_text=tr('在笔记中查找 (Ctrl+F)'))
        find.connect('clicked', lambda *_: self.show_find())
        header.pack_start(find)

        more = self.more_btn = Gtk.MenuButton(child=Icon(icons.more), tooltip_text=tr('页面与设置'))
        more.set_create_popup_func(lambda b: b.set_popover(self.page_popover(self._page(), full=True)))
        header.pack_end(more)
        export = Gtk.MenuButton(child=Icon(icons.share), tooltip_text=tr('导出'))
        export.set_popover(menu_popover([(tr('导出为矢量 PDF…'), self._export_pdf),
                                         (tr('导出当前页为图片…'), self._export_png),
                                         (tr('导出全部页面为图片…'), self._export_png_all),
                                         None,
                                         (tr('导出为 Goodnotes（实验性）…'), self._export_goodnotes)]))
        header.pack_end(export)
        add = self.add_btn = Gtk.MenuButton(child=Icon(icons.page_add), tooltip_text=tr('添加页面'))
        add.set_create_popup_func(lambda b: b.set_popover(self._add_popover()))
        header.pack_end(add)

        # The canvas handles fingers itself (palm rejection, pinch); see Canvas._raw_event.
        scroller = self.scroller = Gtk.ScrolledWindow(child=self.canvas, hexpand=True, vexpand=True, kinetic_scrolling=False,
                                                      hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.EXTERNAL)   # the canvas draws its own bars
        overlay = Gtk.Overlay(child=scroller)

        history = Gtk.Box(halign=Gtk.Align.START, valign=Gtk.Align.START, margin_start=10, margin_top=10)
        history.add_css_class('pill')
        self.undo_btn = Gtk.Button(child=Icon(icons.undo), tooltip_text=tr('撤销 (Ctrl+Z)'))
        self.redo_btn = Gtk.Button(child=Icon(icons.redo), tooltip_text=tr('重做 (Ctrl+Shift+Z)'))
        for button, fn in ((self.undo_btn, self._undo), (self.redo_btn, self._redo)):
            button.add_css_class('flat')
            button.connect('clicked', lambda *_, fn=fn: fn())
            history.append(button)
        overlay.add_overlay(history)

        self._tool_switched = False
        self.opts = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=10, spacing=2)
        self.opts.add_css_class('pill')
        overlay.add_overlay(self.opts)

        self.selbar = self._build_selbar()
        overlay.add_overlay(self.selbar)
        # With a mouse, a right click on the selection opens the bar's "more" menu.
        context = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context.connect('pressed', self._canvas_context)
        self.canvas.add_controller(context)
        self.canvas.connect('selection-changed', lambda *_: self._place_selbar())
        self.canvas.connect('menu-wanted', self._canvas_menu)
        self.canvas.connect('view-changed', lambda *_: self._place_selbar())

        self.minimap = Minimap(self.canvas)
        overlay.add_overlay(self.minimap)
        self.findbar = self._build_findbar()
        overlay.add_overlay(self.findbar)

        self.status = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.END, can_target=False)
        self.status.add_css_class('status')
        overlay.add_overlay(self.status)

        self.sidebar = Sidebar(self.canvas.scroll_to_page, self.page_popover, self.add_end)
        split = Adw.OverlaySplitView(sidebar=self.sidebar, content=overlay, show_sidebar=False,
                                     min_sidebar_width=300, max_sidebar_width=300)
        side_btn.bind_property('active', split, 'show-sidebar', 1 | 2)  # bidirectional | sync-create

        view = Adw.ToolbarView(content=split)
        view.add_top_bar(header)
        return view

    # -- tool options -------------------------------------------------------

    def _tool_toggled(self, button, name):
        if button.get_active() and name != self.tool:
            if self.tool != 'eraser':
                self._prev_tool = self.tool
            self.tool = name
            self._tool_switched = True   # the click that selected it must not also hide the bar
            self.opts.set_visible(True)
            self._rebuild_options()
            self._sync()

    # -- selection bar ------------------------------------------------------

    def _build_selbar(self):
        """The actions that float above a lasso selection."""
        bar = Gtk.Box(halign=Gtk.Align.START, valign=Gtk.Align.START, spacing=2, visible=False)
        bar.add_css_class('pill')
        c = self.canvas

        def button(draw, tip, fn, css=None):
            b = Gtk.Button(child=Icon(draw), tooltip_text=tip)
            b.add_css_class('flat')
            if css:
                b.add_css_class(css)
            b.connect('clicked', lambda *_: fn())
            bar.append(b)

        color = Gtk.MenuButton(child=Icon(icons.palette), tooltip_text=tr('颜色'))
        color.add_css_class('flat')
        color.set_create_popup_func(lambda b: b.set_popover(self._sel_color_popover()))
        bar.append(color)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        button(icons.scissors, tr('剪切'), self._sel_cut)
        button(icons.copy, tr('拷贝'), self._sel_copy)
        button(icons.duplicate, tr('复制'), c.duplicate_selection)
        button(icons.trash, tr('删除'), c.delete_selection, 'danger')
        more = self._sel_more = Gtk.MenuButton(child=Icon(icons.dots), tooltip_text=tr('更多'))
        more.add_css_class('flat')
        def more_menu(button):
            # A menu that does not fit is not shown at all, so open it towards whichever side
            # of the bar has more room and let it scroll if even that is too little.
            above = bar.get_margin_top()
            below = self.canvas.get_height() - above - bar.get_height()
            popover = menu_popover([
                (tr('向左旋转'), lambda: c.rotate_selection(3), None, icons.reset),
                (tr('向右旋转'), lambda: c.rotate_selection(1), None, icons.rotate_cw),
                None,
                (tr('移到最前'), lambda: c.restack_selection(True), None, icons.to_front),
                (tr('移到最后'), lambda: c.restack_selection(False), None, icons.to_back),
                None,
                (tr('粘贴'), self._paste, None, icons.paste),
                (tr('截图到剪贴板'), self._sel_screenshot, None, icons.camera),
                (tr('存为图片…'), self._sel_save_image, None, icons.image)], max(above, below) - 40)
            popover.set_position(Gtk.PositionType.TOP if above > below else Gtk.PositionType.BOTTOM)
            button.set_popover(popover)

        more.set_create_popup_func(more_menu)
        bar.append(more)
        return bar

    def _canvas_context(self, gesture, n, x, y):
        rect = self.canvas.selection_rect()
        if rect is not None and rect[0] - 6 <= x <= rect[0] + rect[2] + 6 and rect[1] - 6 <= y <= rect[1] + rect[3] + 6:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._sel_more.popup()

    def _canvas_menu(self, canvas, x, y):
        """A long press with the select tool: what can be done with the selection if it was
        on it, and pasting at that place otherwise."""
        c, rect = self.canvas, self.canvas.selection_rect()
        rows = [(tr('粘贴'), lambda: self._paste((x, y)), None, icons.paste)]
        if rect is not None and rect[0] - 8 <= x <= rect[0] + rect[2] + 8 and rect[1] - 8 <= y <= rect[1] + rect[3] + 8:
            rows = [(tr('剪切'), self._sel_cut, None, icons.scissors),
                    (tr('拷贝'), self._sel_copy, None, icons.copy),
                    (tr('复制'), c.duplicate_selection, None, icons.duplicate),
                    (tr('删除'), c.delete_selection, 'danger', icons.trash),
                    None] + rows
        popover = menu_popover(rows)
        popover.set_parent(c)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        popover.connect('closed', lambda *_: GLib.idle_add(popover.unparent))
        popover.popup()

    def _place_selbar(self):
        bar, rect = self.selbar, self.canvas.selection_rect()
        if rect is None:
            bar.set_visible(False)
            return
        x, y, w, h = rect
        bar.set_visible(True)
        # A widget's measure includes its margins, and the margins are how the bar is placed:
        # without taking them off, each placement would be thrown out by the one before.
        bw = bar.measure(Gtk.Orientation.HORIZONTAL, -1)[1] - bar.get_margin_start()
        bh = bar.measure(Gtk.Orientation.VERTICAL, -1)[1] - bar.get_margin_top()
        cw, ch = self.canvas.get_width(), self.canvas.get_height()
        left = max(8, min(cw - bw - 8, x + w / 2 - bw / 2))
        top = y - bh - 14
        if top < 64:               # no room above (the tool options sit there): go below
            top = y + h + 18
        left, top = int(left), int(max(8, min(ch - bh - 8, top)))
        if (bar.get_margin_start(), bar.get_margin_top()) != (left, top):
            bar.set_margin_start(left)
            bar.set_margin_top(top)

    def _sel_color_popover(self):
        grid = Gtk.Grid(row_spacing=4, column_spacing=4, margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        popover = Gtk.Popover(child=grid)
        for k, color in enumerate(PALETTE):
            b = Gtk.Button(child=Icon(icons.swatch(color), 24, tint=False))
            b.add_css_class('flat')
            b.add_css_class('swatch')
            b.connect('clicked', lambda _b, color=color: (popover.popdown(), self.canvas.recolor_selection(color)))
            grid.attach(b, k % 5, k // 5, 1, 1)
        return popover

    def _sel_copy(self):
        if self.canvas.copy_selection():
            self.toast(tr('已拷贝'))

    def _sel_cut(self):
        if self.canvas.copy_selection():
            self.canvas.delete_selection()

    def _sel_screenshot(self):
        surface = self.canvas.selection_image()
        if surface is not None:
            self.get_clipboard().set_texture(texture_from_surface(surface))
            self.toast(tr('截图已拷贝到剪贴板'))

    def _sel_save_image(self):
        surface = self.canvas.selection_image(3.0)
        if surface is not None:
            self._save_dialog(tr('存为图片'), '.png', tr('PNG 图片'), lambda path: surface.write_to_png(path))

    # -- find in notebook ---------------------------------------------------

    def _build_findbar(self):
        bar = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=62, margin_end=12, spacing=2,
                      visible=False)
        bar.add_css_class('pill')
        self.find_entry = Gtk.SearchEntry(placeholder_text=tr('查找文本框和 PDF 中的文字'), width_request=240)
        self.find_entry.connect('search-changed', lambda *_: self._find())
        self.find_entry.connect('activate', lambda *_: self._find_step(1))
        self.find_entry.connect('stop-search', lambda *_: self.hide_find())
        bar.append(self.find_entry)
        self.find_count = Gtk.Label(width_chars=7, margin_start=4)
        self.find_count.add_css_class('dim-label')
        bar.append(self.find_count)
        for draw, tip, fn in ((icons.up, tr('上一个'), lambda: self._find_step(-1)),
                              (icons.down, tr('下一个'), lambda: self._find_step(1)),
                              (icons.close, tr('关闭'), self.hide_find)):
            button = Gtk.Button(child=Icon(draw, 16), tooltip_text=tip)
            button.add_css_class('flat')
            button.connect('clicked', lambda b, fn=fn: fn())
            bar.append(button)
        return bar

    def show_find(self):
        self.findbar.set_visible(True)
        self.find_entry.grab_focus()
        self._find()

    def hide_find(self):
        self.findbar.set_visible(False)
        self.canvas.set_marks([])
        self.canvas.grab_focus()

    def _find(self):
        query = self.find_entry.get_text()
        marks = []
        if self.doc is not None and query.strip():
            try:
                marks = [(page, box) for page in self.doc.pages for box in find_text(page, query)]
            except Exception as exc:
                self.toast(str(exc))
        self.canvas.set_marks(marks, 0 if marks else -1)
        self._find_label()

    def _find_step(self, step):
        marks = self.canvas.marks
        if marks:
            self.canvas.set_marks(marks, (self.canvas.mark_at + step) % len(marks))
            self._find_label()

    def _find_label(self):
        n = len(self.canvas.marks)
        self.find_count.set_label('%d / %d' % (self.canvas.mark_at + 1, n) if n else
                                  (tr('无结果') if self.find_entry.get_text().strip() else ''))

    def apply_hidden_tools(self):
        """Show only the tools the user kept in the toolbar (the pen always stays)."""
        hidden = set(self.st.get('hidden_tools', [])) - {'pen'}
        for name, button in self._tool_buttons.items():
            button.set_visible(name not in hidden)
        if self.tool in hidden:
            self.select_tool('pen')
        if 'ruler' in hidden:
            self._tool_buttons['ruler'].set_active(False)

    def _tool_clicked(self, button, name):
        """Tapping the tool that is already selected hides or shows its options bar."""
        if self._tool_switched:
            self._tool_switched = False
        elif name == self.tool:
            self.opts.set_visible(not self.opts.get_visible())

    def select_tool(self, name):
        self._tool_buttons[name].set_active(True)
        self._tool_switched = False

    def _erase_done(self):
        if self.st['eraser_auto'] and self.tool == 'eraser':
            self.select_tool(self._prev_tool)

    def _pen(self):
        return self.st['pens'][self.st['pen_type']]

    def _sync(self):
        st, c, pen = self.st, self.canvas, self._pen()
        c.pen_type = st['pen_type']
        c.pen_color = tuple(st['pen_colors'][st['pen_color']])
        c.pen_width = pen['widths'][pen['slot']]
        c.pen_sens, c.pen_stab, c.pen_dash = pen['sens'] / 50, pen['stab'] / 100, pen.get('dash', 0)
        c.hold_shape = st['hold_shape']
        c.hl_color, c.hl_width = tuple(st['hl_colors'][st['hl_color']]), st['hl_widths'][st['hl_w']]
        c.eraser_r, c.eraser_mode = ERASER_SIZES[st['eraser_size']], st['eraser_mode']
        c.erase_filter = set(st['erase_filter'])
        c.lasso_rect, c.lasso_filter = st['lasso_rect'], set(st['lasso_filter'])
        c.shape_kind, c.text_size = st['shape_kind'], TEXT_SIZES[st['text_size']]
        c.scribble_erase, c.finger_draw = st['scribble_erase'], st['finger_draw']
        c.palm_ms = st.get('palm_ms', 80)
        c.input_log = os.path.join(settings.CONFIG_DIR, 'input.log') if st.get('input_log') else None
        c.note_color = NOTE_COLORS[st.get('note_color', 0) % len(NOTE_COLORS)]
        c.tape_color = TAPE_COLORS[st.get('tape_color', 0) % len(TAPE_COLORS)]
        c.tape_width = TAPE_WIDTHS[st.get('tape_w', 1) % len(TAPE_WIDTHS)]
        c.mouse_hand = st.get('mouse_hand', True)
        c.set_paging(st.get('paging', 'vertical'))
        c.scrollbars, c.max_zoom = st.get('scrollbars', True), max(2.0, float(st.get('max_zoom', 6.0)))
        c.queue_draw()
        if c.show_cross != st.get('crosshair', True):
            c.show_cross = st.get('crosshair', True)
            c.update_cursor()
        c.laser_mode, c.laser_color = st['laser_mode'], LASER_COLORS[st['laser_color'] % len(LASER_COLORS)]
        if c.tool != self.tool:
            c.set_tool(self.tool)

    def _set(self, target, key, value):
        target[key] = value
        self._sync()

    def _later_rebuild(self, *_):
        GLib.idle_add(lambda: self._rebuild_options() and False)

    def _group(self, target, key, entries, css=None):
        """A row of radio buttons bound to target[key]."""
        buttons = []
        for value, child, tip in entries:
            button = Gtk.ToggleButton(child=child, tooltip_text=tip)
            button.add_css_class('flat')
            if css:
                button.add_css_class(css)
            if buttons:
                button.set_group(buttons[0])
            button.set_active(value == target[key])
            button.connect('toggled', lambda b, v=value: b.get_active() and self._set(target, key, v))
            self.opts.append(button)
            buttons.append(button)
        return buttons

    def _separator(self):
        self.opts.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

    def _dropdown(self, draw, label, make_popover, tip):
        box = Gtk.Box(spacing=4, margin_start=6, margin_end=2)
        box.append(Icon(draw))
        if label:
            box.append(Gtk.Label(label=label))
        box.append(Icon(icons.chevron, 14))
        button = Gtk.MenuButton(child=box, tooltip_text=tip)
        button.add_css_class('flat')
        button.add_css_class('drop')
        button.set_create_popup_func(lambda b: b.set_popover(make_popover()))
        self.opts.append(button)

    def _colors(self, colors_key, index_key):
        """Colour slots: tap to select, tap the selected one to edit or remove it, + adds one."""
        colors, current = self.st[colors_key], self.st[index_key]

        def pick(i):
            self.st[index_key] = i
            self._sync()
            self._later_rebuild()

        for i, color in enumerate(colors):
            if i == current:
                button = Gtk.MenuButton(child=Icon(icons.swatch(tuple(color), True), 22, tint=False),
                                        tooltip_text=tr('更改或删除此颜色'))
                button.add_css_class('current')
                button.set_create_popup_func(lambda b: b.set_popover(self._palette_popover(colors_key, index_key)))
            else:
                button = Gtk.Button(child=Icon(icons.swatch(tuple(color)), 22, tint=False))
                button.connect('clicked', lambda b, i=i: pick(i))
            button.add_css_class('flat')
            button.add_css_class('swatch')
            self.opts.append(button)
        if len(colors) < 9:
            add = Gtk.MenuButton(child=Icon(icons.plus, 16), tooltip_text=tr('添加颜色'))
            add.add_css_class('flat')
            add.add_css_class('swatch')
            add.set_create_popup_func(lambda b: b.set_popover(self._palette_popover(colors_key, index_key, True)))
            self.opts.append(add)

    def _weights(self, target, key, widths, low, high, look):
        """Thickness presets: tap to select, tap the selected one to set its width."""
        def pick(i):
            target[key] = i
            self._sync()
            self._later_rebuild()

        for i, w in enumerate(widths):
            icon = Icon(icons.weight(max(0.8, min(7.5, w * look))))
            if i == target[key]:
                button = Gtk.MenuButton(child=icon, tooltip_text=tr('调整粗细') + ' (%.1f pt)' % w)
                button.add_css_class('weight')
                button.set_create_popup_func(lambda b, i=i: b.set_popover(self._width_popover(widths, i, low, high)))
            else:
                button = Gtk.Button(child=icon, tooltip_text='%.1f pt' % w)
                button.connect('clicked', lambda b, i=i: pick(i))
            button.add_css_class('flat')
            self.opts.append(button)

    def _pen_weights(self):
        pen = self._pen()
        self._weights(pen, 'slot', pen['widths'], 0.3, 8.0, 1.3)

    def _width_popover(self, widths, slot, low, high):
        box, _ = self._panel(tr('粗细'), 280)
        preview = TextureView(280, 44, halign=Gtk.Align.CENTER)
        box.append(preview)
        row = Gtk.Box(spacing=6)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 0.1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        value = Gtk.Label(width_chars=7, xalign=1)
        value.add_css_class('dim-label')
        hl = self.tool == 'hl'

        def changed(*_):
            w = round(scale.get_value(), 1)
            widths[slot] = w
            value.set_label('%.1f pt' % w)
            self._sync()
            k = self.get_scale_factor()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 280 * k, 44 * k)
            cr = cairo.Context(surface)
            cr.scale(k, k)
            c = self.canvas
            pts = [(20 + 240 * t, 22 + 7 * math.sin(t * 2 * math.pi), 0.6) for t in (i / 40 for i in range(41))]
            item = Stroke('hl', c.hl_color, w, pts) if hl else Stroke('ball', c.pen_color, w, pts)
            Stroke.on_dark = True   # plain blending: the preview has no paper underneath
            item.draw(cr)
            preview.set_texture(texture_from_surface(surface))

        def nudge(delta):
            scale.set_value(scale.get_value() + delta)

        for draw, delta in ((icons.minus, -0.1), (icons.plus, 0.1)):
            button = Gtk.Button(child=Icon(draw, 16), valign=Gtk.Align.CENTER)
            button.add_css_class('flat')
            button.add_css_class('circular')
            button.connect('clicked', lambda b, d=delta: nudge(d))
            row.append(button)
            if delta < 0:
                row.append(scale)
        box.append(row)
        box.append(value)
        scale.set_value(widths[slot])
        scale.connect('value-changed', changed)
        changed()
        popover = Gtk.Popover(child=box)
        popover.connect('closed', self._later_rebuild)
        return popover

    def _rebuild_options(self):
        while (child := self.opts.get_first_child()) is not None:
            self.opts.remove(child)
        st, tool = self.st, self.tool
        if tool == 'pen':
            self._dropdown(PEN_ICON[st['pen_type']], None, self._pen_popover, tr('笔设置'))
            self._separator()
            self._pen_weights()
            self._separator()
            self._colors('pen_colors', 'pen_color')
        elif tool == 'tape':
            self._group(st, 'tape_w', [(i, Icon(icons.weight(w)), tip) for i, (w, tip) in
                                       enumerate(((3.5, tr('细')), (5.5, tr('中')), (7.5, tr('粗'))))])
            self._separator()
            self._group(st, 'tape_color', [(i, Icon(icons.swatch(color), 22, tint=False), None)
                                           for i, color in enumerate(TAPE_COLORS)], 'swatch')
            self._separator()
            self.opts.append(Gtk.Label(label=tr('画过要遮住的内容；用手指或鼠标点一下胶带可掀开或盖回'),
                                       margin_start=6, margin_end=10))
        elif tool == 'note':
            self._group(st, 'note_color', [(i, Icon(icons.swatch(color), 22, tint=False), None)
                                           for i, color in enumerate(NOTE_COLORS)], 'swatch')
            self._separator()
            self.opts.append(Gtk.Label(label=tr('点击页面放置便签，再点便签可修改'), margin_start=6, margin_end=10))
        elif tool == 'hl':
            self._weights(st, 'hl_w', st['hl_widths'], 3.0, 40.0, 0.34)
            self._separator()
            self._colors('hl_colors', 'hl_color')
        elif tool == 'eraser':
            self._dropdown(icons.eraser, tr(ERASER_MODES[st['eraser_mode']]), self._eraser_popover, tr('橡皮擦设置'))
            self._separator()
            self._group(st, 'eraser_size', [(i, Icon(icons.dot(r)), tip) for i, (r, tip) in
                                            enumerate(((2.5, tr('小')), (4.5, tr('中')), (7, tr('大'))))])
        elif tool == 'shape':
            self._group(st, 'shape_kind', [('line', Icon(icons.shape_line), tr('直线')), ('arrow', Icon(icons.shape_arrow), tr('箭头')),
                                           ('rect', Icon(icons.shape_rect), tr('矩形')), ('ellipse', Icon(icons.shape_ellipse), tr('椭圆'))])
            self._separator()
            self._pen_weights()
            self._separator()
            self._colors('pen_colors', 'pen_color')
        elif tool == 'laser':
            self._group(st, 'laser_mode', [('dot', Icon(icons.laser_dot), tr('点：跟随笔尖的光点')),
                                           ('trail', Icon(icons.laser_trail), tr('轨迹：画出的线约一秒后消失'))])
            self._separator()
            self._group(st, 'laser_color', [(i, Icon(icons.swatch(color), 22, tint=False), None)
                                            for i, color in enumerate(LASER_COLORS)], 'swatch')
        elif tool == 'text':
            self._group(st, 'text_size', [(i, Icon(icons.letter(s)), tip) for i, (s, tip) in
                                          enumerate(((0.6, tr('小')), (0.85, tr('中')), (1.15, tr('大'))))])
            self._separator()
            self._colors('pen_colors', 'pen_color')
        else:
            rect = st['lasso_rect']
            self._dropdown(icons.lasso_rect if rect else icons.lasso, tr('长方形') if rect else tr('自由'),
                           self._lasso_popover, tr('套索设置'))
            self._separator()
            self.opts.append(Gtk.Label(label=tr('拖动移动 · 右下角手柄缩放 · Delete 删除'), margin_start=6, margin_end=10))
        return False

    # -- popovers -----------------------------------------------------------

    def _fit(self, box):
        return fitted(box, self.get_height() - 150)

    @staticmethod
    def _panel(title, width=300):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=width,
                      margin_start=14, margin_end=14, margin_top=12, margin_bottom=12)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class('title-4')
        box.append(label)
        return box, label

    @staticmethod
    def _caption(box, text):
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=30)
        label.add_css_class('caption')
        label.add_css_class('dim-label')
        box.append(label)

    @staticmethod
    def _switch(box, text, active, on_change, draw=None):
        row = Gtk.Box(spacing=10)
        if draw:
            row.append(Icon(draw))
        row.append(Gtk.Label(label=text, xalign=0, hexpand=True))
        switch = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
        switch.connect('notify::active', lambda s, _: on_change(s.get_active()))
        row.append(switch)
        box.append(row)

    def _scale(self, box, text, on_change):
        head = Gtk.Box()
        name = Gtk.Label(label=text, xalign=0, hexpand=True)
        name.add_css_class('caption')
        value = Gtk.Label()
        value.add_css_class('caption')
        value.add_css_class('dim-label')
        head.append(name)
        head.append(value)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_draw_value(False)
        for mark in (25, 50, 75):
            scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)

        def changed(s):
            value.set_label('%d%%' % s.get_value())
            if not self._loading:
                on_change(int(s.get_value()))

        scale.connect('value-changed', changed)
        box.append(head)
        box.append(scale)
        return scale

    def _toggle_in(self, key, name, on):
        values = self.st[key]
        if on and name not in values:
            values.append(name)
        elif not on and name in values:
            values.remove(name)
        self._sync()

    def _choice_row(self, box, target, key, entries):
        """Big side-by-side radio buttons with an icon above a label."""
        row = Gtk.Box(homogeneous=True, spacing=6)
        first = None
        for value, draw, text in entries:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6)
            inner.append(Icon(draw))
            inner.append(Gtk.Label(label=text))
            button = Gtk.ToggleButton(child=inner)
            button.add_css_class('flat')
            button.add_css_class('choice')
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(target[key] == value)
            button.connect('toggled', lambda b, v=value: b.get_active() and self._set(target, key, v))
            row.append(button)
        box.append(row)
        return row

    def _pen_popover(self):
        st = self.st
        panel, title = self._panel('', 280)
        # Side by side where there is room, so that the panel never needs to scroll.
        wide = self.get_width() >= 760
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if wide else Gtk.Orientation.VERTICAL, spacing=8)
        panel.append(cols)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=280)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=280)
        cols.append(box)
        cols.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL if wide else Gtk.Orientation.HORIZONTAL))
        cols.append(right)
        preview = TextureView(280, 84, halign=Gtk.Align.CENTER)
        box.append(preview)

        def draw_preview():
            pen = self._pen()
            scale = self.get_scale_factor()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 280 * scale, 84 * scale)
            cr = cairo.Context(surface)
            cr.scale(scale, scale)
            pts = [(24 + 232 * t, 42 + 22 * math.sin(t * 2 * math.pi + math.pi), 0.12 + 0.85 * math.sin(t * math.pi))
                   for t in (i / 60 for i in range(61))]
            Stroke(st['pen_type'], tuple(st['pen_colors'][st['pen_color']]),
                   pen['widths'][pen['slot']] * 1.5, pts, pen['sens'] / 50).draw(cr)
            preview.set_texture(texture_from_surface(surface))

        def on_size(pct):
            pen = self._pen()
            pen['widths'][pen['slot']] = round(_pct_to_width(pct), 2)
            self._sync()
            draw_preview()

        def on_pen(key, pct):
            self._pen()[key] = pct
            self._sync()
            draw_preview()

        def on_dash(*_):
            self._sync()
            draw_preview()

        types = self._choice_row(box, st, 'pen_type', [(n, d, tr(t)) for n, d, t in PENS])
        box.append(Gtk.Separator())
        self._caption(box, tr('笔画样式'))
        dash_holder = Gtk.Box()
        box.append(dash_holder)
        size = self._scale(right, tr('笔尖'), on_size)
        mm = Gtk.Label(xalign=1)
        mm.add_css_class('caption')
        mm.add_css_class('dim-label')
        right.append(mm)
        size.connect('value-changed', lambda s: mm.set_label('%.2f mm' % (_pct_to_width(s.get_value()) * 0.3528)))
        sens = self._scale(right, tr('压力灵敏度'), lambda pct: on_pen('sens', pct))
        stab = self._scale(right, tr('画笔稳定性'), lambda pct: on_pen('stab', pct))
        right.append(Gtk.Separator())
        self._caption(right, tr('设置'))
        self._switch(right, tr('长按绘制规则形状'), st['hold_shape'], lambda on: self._set(st, 'hold_shape', on))
        self._switch(right, tr('涂画以擦除'), st['scribble_erase'], lambda on: self._set(st, 'scribble_erase', on))

        def load(*_):
            pen = self._pen()
            title.set_label(tr(dict((n, t) for n, _, t in PENS)[st['pen_type']]))
            # The line style row is bound to the active pen, so rebuild it on a switch.
            while (old := dash_holder.get_first_child()) is not None:
                dash_holder.remove(old)
            pen.setdefault('dash', 0)
            row = self._choice_row(dash_holder, pen, 'dash', [(0, icons.line_style(0), tr('实线')),
                                                              (1, icons.line_style(1), tr('虚线')),
                                                              (2, icons.line_style(2), tr('点线'))])
            row.set_hexpand(True)
            button = row.get_first_child()
            while button is not None:
                button.connect('toggled', on_dash)
                button = button.get_next_sibling()
            self._loading = True
            size.set_value(_width_to_pct(pen['widths'][pen['slot']]))
            sens.set_value(pen['sens'])
            stab.set_value(pen['stab'])
            for scale in (size, sens, stab):
                scale.emit('value-changed')  # refresh the percentage labels
            self._loading = False
            draw_preview()

        child = types.get_first_child()
        while child is not None:
            child.connect('toggled', lambda b: b.get_active() and load())
            child = child.get_next_sibling()
        def reset(*_):
            st['pens'][st['pen_type']] = copy.deepcopy(settings.DEFAULTS['pens'][st['pen_type']])
            self._sync()
            load()

        undo = Gtk.Button(child=Icon(icons.reset, 18), tooltip_text=tr('恢复此笔的默认设置'), halign=Gtk.Align.END)
        undo.add_css_class('flat')
        undo.connect('clicked', reset)
        right.append(undo)
        load()
        popover = Gtk.Popover(child=self._fit(panel))
        popover.connect('closed', self._later_rebuild)
        return popover

    def _palette_popover(self, colors_key, index_key, add=False):
        colors = self.st[colors_key]
        box, _ = self._panel(tr('添加颜色') if add else (tr('笔颜色') if colors_key == 'pen_colors' else tr('荧光笔颜色')), 0)
        popover = Gtk.Popover(child=self._fit(box))
        grid = Gtk.Grid(row_spacing=4, column_spacing=4)
        current = None if add else tuple(colors[self.st[index_key]])

        def choose(color):
            if add:
                colors.append(list(color))
                self.st[index_key] = len(colors) - 1
            else:
                colors[self.st[index_key]] = list(color)
            self._sync()
            popover.popdown()
            self._later_rebuild()

        for n, color in enumerate(PALETTE):
            button = Gtk.Button(child=Icon(icons.swatch(color), 26, tint=False))
            button.add_css_class('flat')
            button.add_css_class('swatch')
            if current and max(abs(a - b) for a, b in zip(color, current)) < 0.02:
                button.add_css_class('current')
            button.connect('clicked', lambda b, c=color: choose(c))
            grid.attach(button, n % 5, n // 5, 1, 1)
        wheel = Gtk.Button(child=Icon(icons.wheel, 26, tint=False), tooltip_text=tr('自定义颜色'))
        wheel.add_css_class('flat')
        wheel.add_css_class('swatch')

        def custom(*_):
            popover.popdown()
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = (*(current or (0, 0, 0)), 1.0)

            def done(dialog, result):
                try:
                    c = dialog.choose_rgba_finish(result)
                except GLib.Error:
                    return
                choose((c.red, c.green, c.blue))

            Gtk.ColorDialog(with_alpha=False, title=tr('自定义颜色')).choose_rgba(self, rgba, None, done)

        wheel.connect('clicked', custom)
        grid.attach(wheel, 0, 3, 1, 1)
        box.append(grid)
        if not add and len(colors) > 1:
            def remove(*_):
                del colors[self.st[index_key]]
                self.st[index_key] = min(self.st[index_key], len(colors) - 1)
                self._sync()
                popover.popdown()
                self._later_rebuild()

            delete = Gtk.Button(child=Gtk.Label(label=tr('删除此颜色'), xalign=0))
            delete.add_css_class('flat')
            delete.add_css_class('danger')
            delete.connect('clicked', remove)
            box.append(Gtk.Separator())
            box.append(delete)
        return popover

    def _eraser_popover(self):
        st = self.st
        box, _ = self._panel(tr('橡皮擦'))
        self._choice_row(box, st, 'eraser_mode', [('standard', icons.eraser, tr('标准')), ('stroke', icons.ball, tr('笔画'))])
        self._caption(box, tr('标准橡皮擦只擦掉碰到的部分；笔画橡皮擦一次擦除整条笔画。'))
        box.append(Gtk.Separator())
        self._caption(box, tr('橡皮擦筛选'))
        for name, draw, text in (('pen', icons.pen, tr('笔')), ('hl', icons.highlighter, tr('荧光笔')), ('shape', icons.shapes, tr('形状'))):
            self._switch(box, text, name in st['erase_filter'],
                         lambda on, name=name: self._toggle_in('erase_filter', name, on), draw)
        self._caption(box, tr('如工具已关闭，则橡皮擦不会擦除其留下的痕迹。'))
        box.append(Gtk.Separator())
        self._switch(box, tr('自动取消选择'), st['eraser_auto'], lambda on: self._set(st, 'eraser_auto', on))
        popover = Gtk.Popover(child=self._fit(box))
        clear = Gtk.Button(child=Gtk.Label(label=tr('清除页面'), xalign=0))
        clear.add_css_class('flat')
        clear.add_css_class('danger')
        clear.connect('clicked', lambda *_: (popover.popdown(), self.clear_page(self._page())))
        box.append(clear)
        popover.connect('closed', self._later_rebuild)
        return popover

    def _lasso_popover(self):
        st = self.st
        box, _ = self._panel(tr('套索选取'))
        self._caption(box, tr('套索类型'))
        self._choice_row(box, st, 'lasso_rect', [(False, icons.lasso, tr('自由')), (True, icons.lasso_rect, tr('长方形'))])
        self._caption(box, tr('包含在选取区域中的内容'))
        for name, text in (('ink', tr('手写内容')), ('image', tr('图像')), ('shape', tr('形状与箭头')), ('text', tr('文本框'))):
            self._switch(box, text, name in st['lasso_filter'],
                         lambda on, name=name: self._toggle_in('lasso_filter', name, on))
        popover = Gtk.Popover(child=self._fit(box))
        popover.connect('closed', self._later_rebuild)
        return popover

    # -- page operations ----------------------------------------------------

    def _page(self):
        return self.doc.pages[min(self.canvas.current_page_index(), len(self.doc.pages) - 1)]

    def _index(self, page):
        return self.doc.pages.index(page)

    def page_popover(self, page, full=False):
        n = self._index(page) + 1
        rows = []
        if full:
            rows.append((tr('前往页面…（第 %d / %d 页）') % (n, len(self.doc.pages)), self.goto_page))
        rows += [(tr('移除书签') if page.bookmark else tr('添加书签'), lambda: self.toggle_bookmark(page)),
                 (tr('从大纲移除') if page.outline else tr('将页面添加到大纲…'), lambda: self.edit_outline(page)),
                 None,
                 (tr('拷贝页面'), lambda: self.copy_page(page))]
        if self._page_clip is not None:
            rows.append((tr('粘贴页面'), lambda: self.paste_page(page)))
        rows += [(tr('复制页面'), lambda: self.duplicate_page(page)),
                 (tr('在后面添加页面'), lambda: self.add_after(page)),
                 (tr('顺时针旋转'), lambda: self.rotate_page(page, 1)),
                 (tr('逆时针旋转'), lambda: self.rotate_page(page, 3)),
                 (tr('旋转 180°'), lambda: self.rotate_page(page, 2)),
                 (tr('更改模板…'), lambda: self.change_template(page)),
                 None,
                 (tr('清除页面'), lambda: self.clear_page(page), 'danger'),
                 (tr('删除页面'), lambda: self.delete_page(page), 'danger')]
        if not full:
            return menu_popover(rows, self.get_height() - 110)
        return self._page_panel()

    def _page_panel(self):
        """The header menu: page actions as a grid of tiles, then view options and the rest.

        It stays open after an action, so the result can be watched and the action repeated;
        only entries that open a dialog close it.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=336,
                      margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        popover = Gtk.Popover(child=self._fit(box))

        def fill():
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            page = self._page()
            self._fill_page_panel(box, page, self._index(page) + 1, leave, act)
            return False

        def leave(fn):
            popover.popdown()
            fn()

        def act(fn):
            fn()
            GLib.idle_add(fill)   # page count, bookmark state and the like may have changed

        fill()
        return popover

    def _fill_page_panel(self, box, page, n, run, act):
        st = self.st

        head = Gtk.Box(spacing=8)
        where = Gtk.Label(label=tr('第 %d / %d 页') % (n, len(self.doc.pages)), xalign=0, hexpand=True)
        where.add_css_class('title-4')
        head.append(where)
        goto = Gtk.Button(label=tr('前往…'))
        goto.add_css_class('pill-button')
        goto.connect('clicked', lambda *_: run(self.goto_page))
        head.append(goto)
        box.append(head)

        tiles = [(icons.bookmark_filled if page.bookmark else icons.bookmark,
                  tr('移除书签') if page.bookmark else tr('书签'), lambda: self.toggle_bookmark(page), act),
                 (icons.outline, tr('大纲'), lambda: self.edit_outline(page), run),
                 (icons.grid, tr('模板'), lambda: self.change_template(page), run),
                 (icons.page_add, tr('加页'), lambda: self.add_after(page), act),
                 (icons.copy, tr('拷贝'), lambda: self.copy_page(page), act),
                 (icons.duplicate, tr('复制'), lambda: self.duplicate_page(page), act),
                 (icons.reset, tr('左转'), lambda: self.rotate_page(page, 3), act),
                 (icons.rotate_cw, tr('右转'), lambda: self.rotate_page(page, 1), act)]
        if self._page_clip is not None:
            tiles.append((icons.paste, tr('粘贴'), lambda: self.paste_page(page), act))
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=4, row_spacing=4)
        for k, (draw, text, fn, how) in enumerate(tiles):
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, margin_top=7, margin_bottom=6)
            inner.append(Icon(draw, halign=Gtk.Align.CENTER))
            label = Gtk.Label(label=text, ellipsize=3)
            label.add_css_class('caption')
            inner.append(label)
            button = Gtk.Button(child=inner)
            button.add_css_class('flat')
            button.add_css_class('tile')
            button.connect('clicked', lambda b, fn=fn, how=how: how(fn))
            grid.attach(button, k % 4, k // 4, 1, 1)
        box.append(grid)

        zoom = Gtk.Box(homogeneous=True, margin_top=4)
        zoom.add_css_class('linked')
        for child, fn in ((Icon(icons.minus, 16, halign=Gtk.Align.CENTER), lambda: self.canvas.zoom_by(1 / 1.2)),
                          (Gtk.Label(label=tr('适合宽度')), self.canvas.fit_width),
                          (Icon(icons.plus, 16, halign=Gtk.Align.CENTER), lambda: self.canvas.zoom_by(1.2))):
            button = Gtk.Button(child=child)
            button.connect('clicked', lambda b, fn=fn: fn())
            zoom.append(button)
        box.append(zoom)
        opts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=4, margin_end=4, margin_top=2)
        self._switch(opts, tr('涂画以擦除'), st['scribble_erase'], lambda on: self._set(st, 'scribble_erase', on))
        self._switch(opts, tr('用手指书写'), st['finger_draw'], lambda on: self._set(st, 'finger_draw', on))
        self._switch(opts, tr('全屏模式'), self.is_fullscreen(), self.set_fullscreen)
        box.append(opts)

        rows = Gtk.Grid(column_homogeneous=True, column_spacing=4, row_spacing=2, margin_top=4)
        for k, (draw, text, fn, css, how) in enumerate((
                (icons.settings, tr('设置…'), self.show_settings, None, run),
                (icons.info, tr('关于'), self._about, None, run),
                (icons.eraser, tr('清除页面'), lambda: self.clear_page(page), 'danger', act),
                (icons.trash, tr('删除页面'), lambda: self.delete_page(page), 'danger', act))):
            line = Gtk.Box(spacing=10)
            line.append(Icon(draw, 18))
            line.append(Gtk.Label(label=text, xalign=0, ellipsize=3))
            button = Gtk.Button(child=line)
            button.add_css_class('flat')
            button.add_css_class('menu-row')
            if css:
                button.add_css_class(css)
            button.connect('clicked', lambda b, fn=fn, how=how: how(fn))
            rows.attach(button, k % 2, k // 2, 1, 1)
        box.append(rows)

    def set_fullscreen(self, on):
        self.canvas.log('fullscreen on' if on else 'fullscreen off')
        if on:
            self.fullscreen()
        else:
            self.unfullscreen()

    def toggle_bookmark(self, page):
        page.bookmark = not page.bookmark
        self.doc.touch()

    def edit_outline(self, page):
        if page.outline:
            page.outline = None
            self.doc.touch()
            return

        def done(text):
            page.outline = text
            self.doc.touch()

        ask_text(self, tr('添加到大纲'), tr('第 %d 页') % (self._index(page) + 1), done)

    def copy_page(self, page):
        self._page_clip = page.clone()
        self.toast(tr('已拷贝页面'))

    def _insert_page(self, after, page):
        i = self._index(after) + 1
        self.doc.do(('pages', [], [(i, page)]))
        self.canvas.scroll_to_page(i)

    def paste_page(self, page):
        self._insert_page(page, self._page_clip.clone())

    def duplicate_page(self, page):
        self._insert_page(page, page.clone())

    def add_after(self, page):
        self._insert_page(page, page.blank_like())

    def add_end(self):
        self.add_after(self.doc.pages[-1])

    def rotate_page(self, page, k):
        i = self._index(page)
        self.canvas.clear_selection()
        self.doc.do(('pages', [(i, page)], [(i, page.rotated(k))]))

    def delete_page(self, page):
        i = self._index(page)
        added = [(0, page.blank_like())] if len(self.doc.pages) == 1 else []
        self.doc.do(('pages', [(i, page)], added))

    def clear_page(self, page):
        if page.items:
            self.canvas.clear_selection()
            self.doc.remove_items(page, list(page.items))

    def change_template(self, page):
        if page.pdf is not None:
            self.toast(tr('PDF 页面不能更换纸张模板'))
            return

        def apply(template):
            if not page.board:
                self.st['template'] = dict(template, color=list(template['color']))
            new = {key: template[key] for key in ('w', 'h', 'paper', 'color')}
            if new != page.template():
                self.doc.do(('template', page, page.template(), new))

        PaperDialog(page.template(), apply, board=page.board).present(self)

    # -- adding pages -------------------------------------------------------

    def _pull_add(self, side):
        """The pages were pulled past an end: add one like its neighbour there."""
        pages = self.doc.pages
        i = len(pages) if side > 0 else 0
        self.doc.do(('pages', [], [(i, pages[-1 if side > 0 else 0].blank_like())]))
        self.canvas.scroll_to_page(i)

    def _add_index(self):
        pos = self.st['add_pos']
        if pos == 'last':
            return len(self.doc.pages)
        return self._index(self._page()) + (pos == 'after')

    def _add_pages(self, pages):
        i = self._add_index()
        self.doc.do(('pages', [], [(i + n, page) for n, page in enumerate(pages)]))
        self.canvas.scroll_to_page(i)

    def _add_popover(self):
        st, page = self.st, self._page()
        box, _ = self._panel(tr('添加白板') if page.board else tr('添加页面'), 320)
        popover = Gtk.Popover(child=self._fit(box))
        where = Gtk.Box(homogeneous=True)
        where.add_css_class('linked')
        first = None
        for value, text in (('before', tr('在此之前')), ('after', tr('在此之后')), ('last', tr('最后一页'))):
            button = Gtk.ToggleButton(label=text, active=st['add_pos'] == value)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect('toggled', lambda b, v=value: b.get_active() and st.__setitem__('add_pos', v))
            where.append(button)
        box.append(where)

        self._caption(box, tr('模板'))
        blank = page.blank_like()
        scale = self.get_scale_factor()
        thumb = TextureView(78, 104, halign=Gtk.Align.CENTER)
        thumb.add_css_class('thumb')
        surface = page_surface(blank, 0, (78 * scale, 104 * scale)) if blank.board else \
            page_surface(blank, min(78 / blank.w, 104 / blank.h) * scale)
        thumb._size = (round(surface.get_width() / scale), round(surface.get_height() / scale))
        thumb.set_texture(texture_from_surface(surface))
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=4)
        cell.append(thumb)
        cell.append(Gtk.Label(label=tr('当前模板')))
        same = Gtk.Button(child=cell, halign=Gtk.Align.START, valign=Gtk.Align.START)
        same.add_css_class('flat')
        same.connect('clicked', lambda *_: (popover.popdown(), self._add_pages([blank])))
        box.append(same)
        box.append(Gtk.Separator())

        def more():
            PaperDialog(page.template(), lambda t: self._add_pages([Page(**t)]), heading=tr('添加页面'),
                        apply_label=tr('添加'), board=page.board).present(self)

        for text, draw, fn in ((tr('更多模板…'), icons.grid, more), (tr('图像…'), icons.image, self._add_image_page),
                               (tr('导入 PDF…'), icons.share, lambda: self._import_pdf(True))):
            line = Gtk.Box(spacing=12)
            line.append(Icon(draw))
            line.append(Gtk.Label(label=text, xalign=0))
            button = Gtk.Button(child=line)
            button.add_css_class('flat')
            button.add_css_class('menu-row')
            button.connect('clicked', lambda b, fn=fn: (popover.popdown(), fn()))
            box.append(button)
        return popover

    def _add_image_page(self):
        def place(png, w, h):
            page = self._page().blank_like()
            box = (page.w / 2 - 300, page.h / 2 - 220, page.w / 2 + 300, page.h / 2 + 220) if page.board \
                else (0, 0, page.w, page.h)
            k = min((box[2] - box[0]) * 0.9 / w, (box[3] - box[1]) * 0.9 / h, 1.5)
            page.items = [Image((box[0] + box[2] - w * k) / 2, (box[1] + box[3] - h * k) / 2, w * k, h * k, png)]
            self._add_pages([page])

        self._pick_image(place)

    def goto_page(self):
        ask_page(self, self.canvas.current_page_index() + 1, len(self.doc.pages), self.canvas.scroll_to_page)

    # -- document state and saving ------------------------------------------

    def set_document(self, doc):
        if self.doc is not None:
            self.doc.listeners.remove(self._doc_event)
        self.doc = doc
        doc.listeners.append(self._doc_event)
        self.canvas.set_document(doc)
        self.sidebar.set_document(doc)
        # A whiteboard has no edges, so it gets a map instead of scrollbars.
        board = doc.pages[0].board
        self.status.set_visible(not board)
        GLib.idle_add(lambda: self.minimap.show_for(self.doc is doc and board) and False)
        self._doc_event('state')

    def _doc_event(self, kind, *args):
        # Every kind of change counts: erasing, for one, only announces a new state.
        self.minimap.content_changed()
        if kind == 'state':
            self._update_title()
            self.undo_btn.set_sensitive(bool(self.doc.undo_stack))
            self.redo_btn.set_sensitive(bool(self.doc.redo_stack))
            if self.doc.modified:
                self._schedule_save()
                if gdrive.connected():
                    self._sync_after_edit()
        elif kind in ('pages', 'meta'):
            self._update_status()

    def _update_title(self):
        name = self.doc.display_name()
        self.title_label.set_label(name)
        self.set_title('%s — %s' % (name, APP_NAME))

    def _update_status(self):
        if self.doc is None:
            return
        i = self.canvas.current_page_index()
        self.status.set_label('%d / %d · %d%%' % (i + 1, len(self.doc.pages), round(self.canvas.shown_zoom() * 100)))
        self.sidebar.select(i)

    def _schedule_save(self):
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(2500, self._autosave)

    def _autosave(self):
        """Save in the background once the pen has rested for a moment."""
        self._save_timer = 0
        doc = self.doc
        if doc is None or doc.path is None or not doc.modified:
            return False
        if self._save_thread is not None or self.canvas._op is not None:
            self._schedule_save()
            return False
        self._relocate(doc)
        if not self._absorb(doc):
            return False
        snap, path, sign = doc.snapshot(), doc.path, doc.sign

        def work():
            try:
                result, error = Document.write_snapshot(path, snap, sign), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(done, result, error)

        def done(result, error):
            latest = self._save_thread is thread   # not so when save_now() has written since
            if latest:
                self._save_thread = None
            if isinstance(error, Changed):
                # A sync wrote the file in the meantime. Nothing was written over it; what it
                # brought is taken in first, and then this is saved again.
                if doc is self.doc:
                    self._schedule_save()
            elif error is not None:
                self.toast(tr('自动保存失败：%s') % error)
            else:
                if latest and doc.path == path:
                    doc.sign = result
                if doc.saved_rev < snap['rev']:
                    doc.saved_rev = snap['rev']
            return False

        thread = self._save_thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return False

    def _absorb(self, doc):
        """If a sync has written the open notebook's file, take in what it brought: merged into
        what is on screen when that has unsaved changes, loaded afresh otherwise. False if
        the file could not be read."""
        if doc is None or not doc.stale():
            return True
        try:
            if doc.modified or doc is not self.doc:
                doc.absorb()
                if doc is self.doc:
                    self._update_status()
                return True
            fresh = Document.load(doc.path)
        except Exception as exc:
            self.toast(tr('无法打开笔记本') + ': %s' % exc)
            return False
        tab = self._current_tab()
        view = self.canvas.view_state()
        if tab is not None:
            tab['doc'] = fresh
        self.set_document(fresh)
        self.canvas.restore_view(view)
        return True

    def _relocate(self, doc):
        """Find the open notebook again after a sync renamed or moved its file, as another
        device had; False if it is nowhere in the library any more."""
        if doc.sign is None or not doc.path or os.path.exists(doc.path):
            return True
        for path in gdrive._scan(self.store.root)[0].values():
            try:
                same = notebook_id(path) == doc.id
            except Exception:
                same = False
            if same:
                doc.path, doc.sign = path, (0, 0)   # its content may be another too
                if doc is self.doc:
                    self._update_title()
                return True
        doc.sign = None   # gone: if it is written again, it is as a new file
        return False

    def _follow_sync(self):
        """After a sync the open notebook may have been merged, replaced, moved or deleted."""
        doc = self.doc
        if doc is None or self._current_tab() is None:
            return False
        if self.canvas._op is not None:
            return True   # in the middle of a stroke; look again in a moment
        if doc.path and not os.path.exists(doc.path):
            if self._relocate(doc):
                self._absorb(doc)
            elif not doc.modified:
                # Deleted on another device: it is in the trash here now.
                self.tabs = []
                self._fill_tabs()
                self.library.refresh()
                self.stack.set_visible_child_name('library')
                self.set_title(APP_NAME)
                self.toast(tr('已删除'))
            return False
        if doc.stale():
            self._absorb(doc)
        return False

    def save_now(self):
        """Write pending changes synchronously; False if that failed."""
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        thread, self._save_thread = self._save_thread, None
        if thread is not None:
            thread.join()
        doc = self.doc
        if doc is not None and doc.path and doc.modified and not self.in_editor() and not os.path.exists(doc.path):
            return True   # it was trashed or moved from the library; do not write it back
        if doc is not None and doc.path and doc.modified:
            try:
                self._relocate(doc)
                doc.save()
            except Exception as exc:
                self._error(tr('保存失败'), exc)
                return False
        return True

    def _save_clicked(self):
        if self.save_now():
            self.toast(tr('已保存'))

    def _watch_event(self, controller, event):
        """Watch the pen come and go, and note in the input log what reaches the window and
        which widget it is aimed at."""
        event = event or controller.get_current_event()
        if event is None:
            return False
        kind = event.get_event_type()
        if event.get_device_tool() is not None and self.in_editor():
            # The pen's comings and goings are reported to the window, not to the canvas.
            if kind == Gdk.EventType.PROXIMITY_OUT:
                self.canvas._pen_gone()
            else:
                self.canvas._pen_back()
        if not self.canvas.input_log:
            return False
        if kind in (Gdk.EventType.KEY_PRESS, Gdk.EventType.KEY_RELEASE, Gdk.EventType.SCROLL):
            return False
        device = event.get_device()
        source = device.get_source().value_nick if device is not None else '?'
        tool = event.get_device_tool() is not None
        if kind in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.TOUCH_UPDATE):
            # Counted, not listed: they come by the hundred.
            if self._watched[1] != (kind, source, tool):
                self._watch_flush()
                self._watched = [0, (kind, source, tool)]
            self._watched[0] += 1
            return False
        self._watch_flush()
        ok, x, y = event.get_position()
        nx, ny = self.get_surface_transform()
        target = self.pick(x - nx, y - ny, Gtk.PickFlags.DEFAULT) if ok else None
        chain = []
        while target is not None and len(chain) < 4:
            chain.append(type(target).__name__)
            target = target.get_parent()
        extra = ' button %d' % event.get_button() if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE) else ''
        self.canvas.log('  window: %s from %s%s%s at %.0f,%.0f over %s%s' % (
            kind.value_nick, source, ' (pen tool)' if tool else '', extra, x, y, ' < '.join(chain) or 'nothing',
            ' [fullscreen]' if self.is_fullscreen() else ''))
        return False

    def _watch_flush(self):
        n, what = self._watched
        if n and what is not None:
            self.canvas.log('  window: %d x %s from %s%s' % (n, what[0].value_nick, what[1], ' (pen tool)' if what[2] else ''))
        self._watched = [0, None]

    def _close_request(self, *_):
        self.canvas._cancel()
        self._remember_view()
        self.save_now()
        settings.save(self.st)
        return False

    def toast(self, text, button=None, action=None):
        toast = Adw.Toast(title=text, timeout=5 if button else 3)
        if button:
            toast.set_button_label(button)
            toast.connect('button-clicked', lambda *_: action())
        self.toasts.add_toast(toast)

    def _error(self, heading, exc):
        dialog = Adw.AlertDialog(heading=heading, body=str(exc))
        dialog.add_response('ok', tr('好'))
        dialog.present(self)

    # -- library ------------------------------------------------------------

    def in_editor(self):
        return self.stack.get_visible_child_name() == 'editor'

    def _remember_view(self):
        """Note where the open notebook is being looked at, to open it there next time."""
        memo = self.canvas.view_memo() if self.doc is not None and self.in_editor() else None
        if memo is not None:
            views = self.st.setdefault('views', {})
            views.pop(self.doc.id, None)
            views[self.doc.id] = memo
            for old in list(views)[:-300]:   # the notebooks opened longest ago are forgotten
                del views[old]

    def go_home(self):
        self.canvas._cancel()
        self._remember_view()
        self.save_now()
        self.tabs = []   # closed: it may sync, and opening it again reads it afresh
        self._fill_tabs()
        self.library.refresh()
        self.stack.set_visible_child_name('library')
        self.set_title(APP_NAME)
        self.sync_now(True)

    # -- Google Drive -------------------------------------------------------

    def sync_now(self, quiet=False, only=False):
        """Sync the library with Google Drive in the background, or with only=True just the
        notebook that is open."""
        if not gdrive.connected():
            return
        if self._sync_busy:
            # Not forgotten: it runs when this one is over. The whole library outranks one notebook.
            was = self._sync_next
            self._sync_next = (quiet and (was is None or was[0]), only and (was is None or was[1]))
            return
        only = only and self.in_editor() and self.doc is not None and bool(self.doc.path)
        began = time.monotonic()
        self._sync_busy = True
        self.library.set_syncing(True)
        if getattr(self, '_cloud_fill', None):
            self._cloud_fill()   # the cloud menu is open: it shows this sync's progress
        # The open notebook syncs like any other. It is saved first; what the sync then does to
        # its file is taken into the editor when the sync is over (and before any later save).
        self._prune_tabs()
        if self.in_editor() and self.canvas._op is None:
            self.save_now()
        skip = []
        root = self.store.root
        which = [self.doc.path] if only else None

        def progress(fraction, text):
            GLib.idle_add(lambda: self.library.set_sync_progress(fraction, text) and False)

        def work():
            try:
                result, error = gdrive.sync(root, gdrive.Drive(), skip, progress, which), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(done, result, error)

        def done(result, error):
            self._sync_busy = False
            self.library.set_syncing(False)
            self._sync_error = None if error is None else str(error)
            if getattr(self, '_cloud_fill', None):
                self._cloud_fill()   # the cloud menu is open: it stops saying that a sync is running
            # Even a sync that failed may have brought something for the open notebook.
            if self._follow_sync():
                GLib.timeout_add(400, self._follow_sync)
            self._open_synced = time.monotonic()
            if only:
                self._open_took = self._open_synced - began
            if self._sync_next is not None:
                (again_quiet, again_only), self._sync_next = self._sync_next, None
                GLib.idle_add(lambda: self.sync_now(again_quiet, again_only) and False)
            if error is not None:
                if not quiet:
                    self.toast(tr('同步失败：%s') % error)
                return False
            if result['meta']:
                self.store.reload_meta()
            changed = (result['down'] or result['removed'] or result['conflicts'] or result['moved'] or result['meta']
                       or result.get('merged'))
            if changed and not self.in_editor():
                self.library.refresh()
            if result.get('merged') and not quiet:
                self.toast(tr('同步完成：%d 本笔记两边都改过，已自动合并') % result['merged'])
            if result['conflicts']:
                self.toast(tr('同步完成：%d 本笔记两边都改过，云端版本已另存为副本') % result['conflicts'])
            elif not quiet:
                self.toast(tr('同步完成：上传 %d，下载 %d') % (result['up'], result['down']))
            return False

        threading.Thread(target=work, daemon=True).start()

    def _drive_connect(self):
        if gdrive.load_client() is None:
            dialog = Gtk.FileDialog(title=tr('选择 Google OAuth 客户端凭据（JSON）'),
                                    filters=_filters(('JSON', ['*.json'])))

            def chosen(dialog, result):
                try:
                    gdrive.install_client(dialog.open_finish(result).get_path())
                except GLib.Error:
                    return
                except Exception as exc:
                    self._error(tr('无法使用这个凭据文件'), exc)
                    return
                self._drive_connect()

            dialog.open(self, None, chosen)
            return
        self.toast(tr('请在浏览器中登录 Google 并授权'))

        def open_url(url):
            GLib.idle_add(lambda: Gio.AppInfo.launch_default_for_uri(url, None) and False)

        def work():
            try:
                gdrive.authorize(open_url)
                error = None
            except Exception as exc:
                error = exc
            GLib.idle_add(done, error)

        def done(error):
            if error is not None:
                self._error(tr('连接 Google Drive 失败'), error)
            else:
                self.toast(tr('已连接 Google Drive'))
                self.sync_now()
            return False

        threading.Thread(target=work, daemon=True).start()

    def _drive_disconnect(self):
        threading.Thread(target=gdrive.disconnect, daemon=True).start()
        self._sync_error = None
        self.toast(tr('已断开 Google Drive；本机和云端的笔记都保留'))

    def show_settings(self):
        PrefsDialog(self).present(self)

    def _cloud_popover(self):
        popover = Gtk.Popover()
        popover.connect('closed', lambda *_: (setattr(self.library, 'sync_bar', None),
                                              setattr(self, '_cloud_fill', None)))

        def fill():
            """Put in the menu what is true now. Done again when a sync ends while it is open."""
            self.library.sync_bar = None
            box, _ = self._panel('Google Drive', 280)
            popover.set_child(box)

            def button(text, fn, css=None):
                b = Gtk.Button(label=text)
                if css:
                    b.add_css_class(css)
                b.connect('clicked', lambda *_: (popover.popdown(), fn()))
                box.append(b)

            if not gdrive.connected():
                self._caption(box, tr('把文稿库同步到你的 Google Drive 里的“Inkpad”文件夹。'
                                   '应用只能访问它自己创建的文件。'))
                button(tr('连接 Google Drive…'), self._drive_connect, 'suggested-action')
                return
            last = gdrive.last_sync(self.store.root)
            if self._sync_busy:
                status = tr('正在同步…')
            elif self._sync_error:
                status = tr('上次同步失败：') + self._sync_error
            elif last:
                status = tr('上次同步：') + time.strftime(tr('%-m月%-d日 %H:%M'), time.localtime(last))
            else:
                status = tr('已连接，尚未同步')
            self._caption(box, status)
            if self._sync_busy:
                # The running sync reports here, and only here. The name of the notebook being
                # moved has two lines to itself, whatever its length: a long one wraps and is cut
                # short, a short one sits in the middle, and the menu keeps its size either way.
                name = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                                 ellipsize=Pango.EllipsizeMode.END, width_chars=30, max_width_chars=30,
                                 justify=Gtk.Justification.CENTER, xalign=0.5, yalign=0.5, height_request=38)
                name.add_css_class('caption')
                box.append(name)
                bar = Gtk.ProgressBar()
                box.append(bar)
                percent = Gtk.Label(xalign=0.5)
                percent.add_css_class('caption')
                percent.add_css_class('dim-label')
                percent.add_css_class('numeric')
                box.append(percent)
                self.library.sync_bar = (bar, name, percent)
                self.library.set_sync_progress(self.library.sync_fraction, self.library.sync_text)
            self._caption(box, tr('回到文稿库时自动同步。删除的笔记会进入废纸篓，两边都改过的会保留副本。'))
            button(tr('立即同步'), self.sync_now, 'suggested-action')
            button(tr('断开连接'), self._drive_disconnect)

        fill()
        self._cloud_fill = fill
        return popover

    def open_notebook(self, path):
        """Show a notebook in its tab, opening one for it if it is not open yet."""
        self._prune_tabs()
        tab = next((t for t in self.tabs if t['doc'].path == path), None)
        if tab is None:
            try:
                doc = Document.load(path)
            except Exception as exc:
                self._error(tr('无法打开笔记本'), exc)
                return
            tab = {'doc': doc, 'view': None}
        # One notebook is open at a time: the one before is saved and let go of.
        self._remember_view()
        self.save_now()
        self.tabs = [tab]
        self._show_tab(tab)
        memo = self.st.get('views', {}).get(tab['doc'].id)
        if memo is not None and tab['view'] is None:
            self.canvas.restore_memo(memo)
        self._sync_open()   # so that it shows what other devices wrote in it

    def _current_tab(self):
        return next((t for t in self.tabs if t['doc'] is self.doc), None)

    def _prune_tabs(self):
        """Forget tabs whose notebook was moved, trashed or deleted in the library meanwhile."""
        self.tabs = [t for t in self.tabs if t['doc'].path and os.path.exists(t['doc'].path)]

    def _show_tab(self, tab):
        self.canvas._cancel()
        current = self._current_tab()
        if current is not None and current is not tab:
            current['view'] = self.canvas.view_state()
        self.save_now()
        if tab['doc'] is not self.doc:
            self.set_document(tab['doc'])
            if tab['view'] is not None:
                self.canvas.restore_view(tab['view'])
        self.stack.set_visible_child_name('editor')
        self._fill_tabs()
        self.canvas.grab_focus()

    def close_tab(self, tab):
        if tab['doc'] is self.doc:
            self.canvas._cancel()
            self.save_now()
        i = self.tabs.index(tab)
        self.tabs.remove(tab)
        if tab['doc'] is not self.doc:
            self._fill_tabs()
        elif self.tabs:
            self._show_tab(self.tabs[min(i, len(self.tabs) - 1)])
        else:
            self._fill_tabs()
            self.go_home()

    def _fill_tabs(self):
        """Open notebooks take no room on screen: the title shows how many there are, and its
        menu lists them (Ctrl+Tab steps through them)."""
        n = len(self.tabs)
        self.tab_count.set_label(str(n))
        self.tab_count.set_visible(n > 1)

    def _title_popover(self):
        popover = menu_popover([(tr('重命名…'), self._rename), (tr('更改封面…'), self._change_cover),
                                (tr('导入 PDF 页面…'), self._import_pdf), (tr('立即保存'), self._save_clicked)])
        if len(self.tabs) > 1:
            box = popover.box
            head = Gtk.Label(label=tr('已打开的笔记本'), xalign=0, margin_start=12, margin_top=8, margin_bottom=2)
            head.add_css_class('caption')
            head.add_css_class('dim-label')
            rows = [head]
            for tab in self.tabs:
                row = Gtk.Box()
                mark = Icon(icons.check_circle if tab['doc'] is self.doc else icons.cover, 16)
                inner = Gtk.Box(spacing=10)
                inner.append(mark)
                inner.append(Gtk.Label(label=tab['doc'].display_name(), xalign=0, ellipsize=3, max_width_chars=24))
                name = Gtk.Button(child=inner, hexpand=True)
                name.add_css_class('flat')
                name.add_css_class('menu-row')
                name.connect('clicked', lambda b, tab=tab: (popover.popdown(), self._show_tab(tab)))
                close = Gtk.Button(child=Icon(icons.close, 14), tooltip_text=tr('关闭'), valign=Gtk.Align.CENTER)
                close.add_css_class('flat')
                close.connect('clicked', lambda b, tab=tab: (popover.popdown(), self.close_tab(tab)))
                row.append(name)
                row.append(close)
                rows.append(row)
            rows.append(Gtk.Separator())
            for widget in reversed(rows):
                box.prepend(widget)
        return popover

    def next_tab(self, step=1):
        tab = self._current_tab()
        if tab is not None and len(self.tabs) > 1:
            self._show_tab(self.tabs[(self.tabs.index(tab) + step) % len(self.tabs)])

    def set_library(self, root):
        """Carry on with the library at another place (after it was moved there)."""
        self.canvas._cancel()
        self.save_now()
        self.tabs = []
        self._fill_tabs()
        self.st['library'] = root
        settings.save(self.st)
        self.store = Store(root)
        self.library.store = self.store
        self.library.folder = self.store.root
        self.library.set_selecting(False)
        self.stack.set_visible_child_name('library')
        self.library.refresh()

    def open_external(self, gfile):
        """Open a file given on the command line; PDFs become notebooks in the library."""
        name = gfile.get_basename() or ''
        try:
            if name.lower().endswith('.pdf'):
                path = self.store.import_pdf(self.store.root, os.path.splitext(name)[0], gfile.load_contents(None)[1])
            elif name.lower().endswith('.goodnotes'):
                path = self._import_goodnotes(self.store.root, gfile)
            else:
                path = gfile.get_path()
                if path is None:
                    raise ValueError(tr('只能打开本地文件'))
        except Exception as exc:
            self._error(tr('无法打开文件'), exc)
            return
        self.open_notebook(path)

    def _import_goodnotes(self, folder, gfile):
        if gfile.get_path() is None:
            raise ValueError(tr('只能打开本地文件'))
        path, stats = self.store.import_goodnotes(folder, gfile.get_path())
        text = tr('已导入 %d 笔、%d 个文本框') % (stats['strokes'] + stats.get('shapes', 0), stats['texts'])
        if stats['skipped']:
            text += tr('；有 %d 个元素无法识别，已跳过') % stats['skipped']
        self.toast(text)
        return path

    def _import_to_library(self, folder):
        dialog = Gtk.FileDialog(title=tr('导入 PDF 或 Goodnotes 文档'), filters=_filters(
            (tr('PDF 与 Goodnotes 文档'), ['*.pdf', '*.PDF', '*.goodnotes']),
            (tr('PDF 文档'), ['*.pdf', '*.PDF']), (tr('Goodnotes 文档'), ['*.goodnotes'])))

        def done(dialog, result):
            try:
                gfile = dialog.open_finish(result)
            except GLib.Error:
                return
            try:
                if gfile.get_basename().lower().endswith('.goodnotes'):
                    path = self._import_goodnotes(folder, gfile)
                else:
                    name = os.path.splitext(gfile.get_basename())[0]
                    path = self.store.import_pdf(folder, name, gfile.load_contents(None)[1])
            except Exception as exc:
                self._error(tr('无法导入'), exc)
                return
            self.open_notebook(path)

        dialog.open(self, None, done)

    def _rename(self):
        def done(name):
            if not self.save_now():
                return
            try:
                self.doc.path = self.store.rename(self.doc.path, name)
            except Exception as exc:
                self._error(tr('重命名失败'), exc)
            self._update_title()
            self._fill_tabs()
            if self.doc.cover_spec is not None:
                self.doc.set_cover(self.doc.cover_spec)  # the cover carries the name

        ask_text(self, tr('重命名笔记本'), self.doc.display_name(), done)

    def _change_cover(self):
        ask_cover(self, self.doc.cover_spec, self.doc.display_name(), self.doc.set_cover)

    def _import_pdf(self, positioned=False):
        dialog = Gtk.FileDialog(title=tr('导入 PDF 页面'), filters=_filters((tr('PDF 文档'), ['*.pdf', '*.PDF'])))

        def done(dialog, result):
            try:
                gfile = dialog.open_finish(result)
            except GLib.Error:
                return
            try:
                pages = Document.pdf_pages(gfile.load_contents(None)[1])
            except Exception as exc:
                self._error(tr('无法导入 PDF'), exc)
                return
            if positioned:
                self._add_pages(pages)
            else:
                at = self.canvas.current_page_index() + 1
                self.doc.do(('pages', [], [(at + i, p) for i, p in enumerate(pages)]))
                self.canvas.scroll_to_page(at)
            self.toast(tr('已导入 %d 页') % len(pages))

        dialog.open(self, None, done)

    # -- keyboard -----------------------------------------------------------

    def _undo(self):
        self.canvas.clear_selection()
        self.doc.undo()

    def _redo(self):
        self.canvas.clear_selection()
        self.doc.redo()

    def _key(self, controller, keyval, keycode, state):
        # Handled in the bubble phase so text entries keep their own shortcuts.
        if keyval == Gdk.KEY_f and state & Gdk.ModifierType.CONTROL_MASK and self.in_editor():
            self.show_find()
            return True
        if keyval == Gdk.KEY_F11:
            self.set_fullscreen(not self.is_fullscreen())
            return True
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        key = Gdk.keyval_to_lower(keyval)
        if not self.in_editor():
            # In the library, moves of items between folders are undone and redone.
            if ctrl and key in (Gdk.KEY_z, Gdk.KEY_y) and not isinstance(self.get_focus(), Gtk.Text):
                return self.library.redo_move() if shift or key == Gdk.KEY_y else self.library.undo_move()
            return False
        if ctrl and key == Gdk.KEY_z:
            self._redo() if shift else self._undo()
        elif ctrl and key == Gdk.KEY_y:
            self._redo()
        elif ctrl and key == Gdk.KEY_c:
            self.canvas.copy_selection()
        elif ctrl and key == Gdk.KEY_v:
            self._paste()
        elif ctrl and key == Gdk.KEY_s:
            self._save_clicked()
        elif ctrl and key == Gdk.KEY_e:
            self._export_pdf()
        elif ctrl and key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.canvas.zoom_by(1.2)
        elif ctrl and key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.canvas.zoom_by(1 / 1.2)
        elif not ctrl and key in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down) \
                and not isinstance(self.get_focus(), (Gtk.Text, Gtk.TextView)):
            # The arrows turn the pages, unless they are moving the caret in some text.
            i = self.canvas.current_page_index() + (1 if key in (Gdk.KEY_Right, Gdk.KEY_Page_Down) else -1)
            if 0 <= i < len(self.doc.pages):
                self.canvas.scroll_to_page(i)
        elif ctrl and key == Gdk.KEY_0:
            self.canvas.fit_width()
        elif ctrl and key == Gdk.KEY_g:
            self.goto_page()
        elif key in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self.canvas.delete_selection()
        else:
            return False
        return True

    # -- export -------------------------------------------------------------

    def _save_dialog(self, title, suffix, pattern_name, fn):
        dialog = Gtk.FileDialog(title=title, initial_name=self.doc.display_name() + suffix,
                                filters=_filters((pattern_name, ['*' + os.path.splitext(suffix)[1]])))

        def done(dialog, result):
            try:
                path = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            try:
                fn(path)
            except Exception as exc:
                self._error(tr('导出失败'), exc)
                return
            self.toast(tr('已导出到 ') + os.path.basename(path))

        dialog.save(self, None, done)

    def _ask_export(self, heading, with_range, on_ok):
        """Ask which pages and which layers to export; on_ok(page indexes, paper, ink)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pages = Gtk.Entry(placeholder_text=tr('全部页面；也可填 1-3,5'), activates_default=True)
        if with_range:
            box.append(Gtk.Label(label=tr('页面范围（共 %d 页）') % len(self.doc.pages), xalign=0))
            box.append(pages)
        box.append(Gtk.Label(label=tr('导出内容'), xalign=0, margin_top=4))
        what = Gtk.DropDown.new_from_strings([tr('笔迹和背景'), tr('仅笔迹（透明背景）'), tr('仅背景')])
        box.append(what)
        dialog = Adw.AlertDialog(heading=heading, extra_child=box)
        dialog.add_response('cancel', tr('取消'))
        dialog.add_response('ok', tr('导出'))
        dialog.set_default_response('ok')
        dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)

        def done(d, response):
            if response != 'ok':
                return
            try:
                picked = _page_range(pages.get_text(), len(self.doc.pages)) if with_range else None
            except ValueError:
                self.toast(tr('页面范围无法识别，请按 1-3,5 的格式填写'))
                return
            mode = what.get_selected()
            on_ok(picked, mode != 1, mode != 2)

        dialog.connect('response', done)
        dialog.present(self)

    def _export_pdf(self):
        self._ask_export(tr('导出为 PDF'), True, lambda pages, paper, ink: self._save_dialog(
            tr('导出为 PDF'), '.pdf', tr('PDF 文档'), lambda path: export_pdf(self.doc, path, pages, paper, ink)))

    def _export_goodnotes(self):
        from . import goodnotes
        self._save_dialog(tr('导出为 Goodnotes（实验性）…').rstrip('…'), '.goodnotes', tr('Goodnotes 文档'),
                          lambda path: goodnotes.save(self.doc, path, self.doc.display_name()))

    def _export_png(self):
        i = self.canvas.current_page_index()
        self._ask_export(tr('导出当前页为图片'), False, lambda pages, paper, ink: self._save_dialog(
            tr('导出当前页为图片'), '-%d.png' % (i + 1), tr('PNG 图片'),
            lambda path: export_png(self.doc.pages[i], path, paper=paper, ink=ink)))

    def _export_png_all(self):
        self._ask_export(tr('导出全部页面为图片…').rstrip('…'), True, self._export_png_pages)

    def _export_png_pages(self, pages, paper, ink):
        dialog = Gtk.FileDialog(title=tr('选择导出文件夹'))
        indexes = list(range(len(self.doc.pages))) if pages is None else pages

        def done(dialog, result):
            try:
                folder = dialog.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            name = self.doc.display_name()
            try:
                for n in indexes:
                    export_png(self.doc.pages[n], os.path.join(folder, '%s-%02d.png' % (name, n + 1)),
                               paper=paper, ink=ink)
            except Exception as exc:
                self._error(tr('导出失败'), exc)
                return
            self.toast(tr('已导出 %d 张图片') % len(indexes))

        dialog.select_folder(self, None, done)

    # -- images -------------------------------------------------------------

    @staticmethod
    def _png(pixbuf):
        """(png bytes, width, height) of a pixbuf, oriented and capped at 2000 px."""
        pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        w, h = pixbuf.get_width(), pixbuf.get_height()
        longest = max(w, h)
        if longest > 2000:
            w, h = round(w * 2000 / longest), round(h * 2000 / longest)
            pixbuf = pixbuf.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
        ok, png = pixbuf.save_to_bufferv('png', [], [])
        if not ok:
            raise ValueError(tr('无法读取图片'))
        return bytes(png), w, h

    def _place_image(self, png, w, h, at=None):
        self.select_tool('lasso')
        self.canvas.insert_image(png, w, h, at)

    def _pick_image(self, on_image):
        dialog = Gtk.FileDialog(title=tr('选择图片'), filters=_filters(
            (tr('图片'), ['*.png', '*.jpg', '*.jpeg', '*.webp', '*.gif', '*.bmp', '*.svg', '*.PNG', '*.JPG', '*.JPEG'])))

        def done(dialog, result):
            try:
                gfile = dialog.open_finish(result)
            except GLib.Error:
                return
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(gfile.load_contents(None)[1])
                loader.close()
                on_image(*self._png(loader.get_pixbuf()))
            except Exception as exc:
                self._error(tr('无法插入图片'), exc)

        dialog.open(self, None, done)

    def _insert_image(self):
        self._pick_image(self._place_image)

    def _paste(self, at=None):
        """Paste what was copied here, or else a picture from the clipboard; at a point of the
        canvas if one is given."""
        if self.canvas.paste(at):
            self.select_tool('lasso')
            return

        def done(clipboard, result):
            try:
                texture = clipboard.read_texture_finish(result)
            except GLib.Error:
                return
            if texture is not None:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(texture.save_to_png_bytes().get_data())
                loader.close()
                self._place_image(*self._png(loader.get_pixbuf()), at)

        self.get_clipboard().read_texture_async(None, done)

    def _about(self):
        Adw.AboutDialog(application_name=APP_NAME, version=VERSION, application_icon='io.github.inkpad.Inkpad',
                        comments=tr('GNOME 上的手写笔记与画板。\n支持手写笔压感、涂画以擦除、PDF 批注与矢量导出。\n'
                                 '笔记保存在：') + self.store.root).present(self)
