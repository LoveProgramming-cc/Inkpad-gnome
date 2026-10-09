"""Small dialogs and the paper style chooser."""
import cairo
from gi.repository import Adw, Gtk

from .i18n import tr
from .icons import TextureView
from gi.repository import Gdk, GLib

from . import icons
from .icons import Icon
from .model import BOARD_SIZE, COVER_COLORS, COVER_STYLES, PAPER_COLORS, PAPERS, SIZES, Page, draw_paper, render_cover
from .render import texture_from_surface


def ask_text(parent, heading, text, on_done):
    entry = Gtk.Entry(text=text, activates_default=True)
    dialog = Adw.AlertDialog(heading=heading, extra_child=entry)
    dialog.add_response('cancel', tr('取消'))
    dialog.add_response('ok', tr('确定'))
    dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response('ok')
    dialog.set_close_response('cancel')

    def response(_dialog, choice):
        value = entry.get_text().strip()
        if choice == 'ok' and value:
            on_done(value)

    dialog.connect('response', response)
    dialog.present(parent)
    entry.grab_focus()


def confirm(parent, heading, body, action, on_yes):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response('cancel', tr('取消'))
    dialog.add_response('yes', action)
    dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_close_response('cancel')
    dialog.connect('response', lambda d, choice: choice == 'yes' and on_yes())
    dialog.present(parent)


def ask_page(parent, current, total, on_done):
    spin = Gtk.SpinButton.new_with_range(1, total, 1)
    spin.set_value(current)
    spin.set_activates_default(True)
    dialog = Adw.AlertDialog(heading=tr('前往页面'), body=tr('共 %d 页') % total, extra_child=spin)
    dialog.add_response('cancel', tr('取消'))
    dialog.add_response('ok', tr('前往'))
    dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response('ok')
    dialog.set_close_response('cancel')
    dialog.connect('response', lambda d, choice: choice == 'ok' and on_done(spin.get_value_as_int() - 1))
    dialog.present(parent)




def cover_picker(current, on_change, title=None):
    """Choose a cover design and colour, or none (the first page then serves as the cover)."""
    title = tr('笔记本') if title is None else title
    state = {'style': current[0] if current else None, 'color': current[1] if current else COVER_COLORS[0]}
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    styles = Gtk.Box(spacing=2)
    colors = Gtk.Box(spacing=0)
    previews = []

    def render():
        for style, view in previews:
            png = render_cover((style, state['color']), title, 72, 96)
            view.set_texture(Gdk.Texture.new_from_bytes(GLib.Bytes.new(png)))
        colors.set_sensitive(state['style'] is not None)

    def changed(key, value):
        state[key] = value
        render()
        on_change((state['style'], state['color']) if state['style'] else None)

    first = None
    for style in [None] + COVER_STYLES:
        if style is None:
            child = Gtk.Box(width_request=36, height_request=48, halign=Gtk.Align.CENTER)
            child.add_css_class('thumb')
            child.append(Icon(icons.none, 18, hexpand=True))
        else:
            child = TextureView(36, 48)
            child.add_css_class('thumb')
            previews.append((style, child))
        button = Gtk.ToggleButton(child=child, tooltip_text=tr('用第一页作封面') if style is None else None)
        button.add_css_class('flat')
        button.add_css_class('cover-choice')
        if first is None:
            first = button
        else:
            button.set_group(first)
        button.set_active(style == state['style'])
        button.connect('toggled', lambda b, s=style: b.get_active() and changed('style', s))
        styles.append(button)
    first = None
    for color in COVER_COLORS:
        button = Gtk.ToggleButton(child=Icon(icons.swatch(color), 20, tint=False))
        button.add_css_class('flat')
        button.add_css_class('cover-color')
        if first is None:
            first = button
        else:
            button.set_group(first)
        button.set_active(color == state['color'])
        button.connect('toggled', lambda b, c=color: b.get_active() and changed('color', c))
        colors.append(button)
    box.append(styles)
    box.append(colors)
    render()
    return box


def ask_cover(parent, current, title, on_done):
    state = {'cover': current}
    dialog = Adw.AlertDialog(heading=tr('笔记本封面'), body=tr('封面显示在文稿库里。'),
                             extra_child=cover_picker(current, lambda c: state.update(cover=c), title))
    dialog.add_response('cancel', tr('取消'))
    dialog.add_response('ok', tr('应用'))
    dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response('ok')
    dialog.set_close_response('cancel')
    dialog.connect('response', lambda d, choice: choice == 'ok' and on_done(state['cover']))
    dialog.present(parent)


class PaperDialog(Adw.Dialog):
    """Pick page size, paper colour, orientation and a template.

    With title set (a string, possibly empty) the dialog also asks for a name,
    as when creating a notebook, and calls on_apply(template, title); otherwise
    on_apply(template). board=True is the whiteboard variant: fixed large
    canvas, background style and colour only.
    """

    PREVIEW_W = 96

    def __init__(self, template, on_apply, heading=None, apply_label=None, title=None, board=False):
        heading, apply_label = heading or tr('纸张样式'), apply_label or tr('应用')
        super().__init__(title=heading, content_width=820 if title is not None else 660, content_height=600)
        self._on_apply = on_apply
        self.board = board
        w, h = template['w'], template['h']
        self.landscape = w > h
        short, long_ = min(w, h), max(w, h)
        self.size = next((i for i, (_, s) in enumerate(SIZES) if abs(s[0] - short) < 2 and abs(s[1] - long_) < 2), 0)
        color = tuple(template['color'])
        self.color = next((i for i, (_, c) in enumerate(PAPER_COLORS)
                           if max(abs(a - b) for a, b in zip(c, color)) < 0.02), 0)
        self.paper = template['paper']
        self._cells = []
        self._flows = []
        self._entry = None
        self._big = None
        self.cover = None

        options = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=10)
        if not board:
            size = Gtk.DropDown.new_from_strings([name for name, _ in SIZES])
            size.set_selected(self.size)
            size.connect('notify::selected', lambda d, _: self._changed('size', d.get_selected()))
            options.append(size)
        colors = Gtk.DropDown.new_from_strings([tr(name) for name, _ in PAPER_COLORS])
        colors.set_selected(self.color)
        colors.connect('notify::selected', lambda d, _: self._changed('color', d.get_selected()))
        options.append(colors)
        if not board:
            orient = Gtk.Box()
            orient.add_css_class('linked')
            portrait = Gtk.ToggleButton(label=tr('竖向'), active=not self.landscape)
            landscape = Gtk.ToggleButton(label=tr('横向'), active=self.landscape, group=portrait)
            landscape.connect('toggled', lambda b: self._changed('landscape', b.get_active()))
            orient.append(portrait)
            orient.append(landscape)
            options.append(orient)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=22, margin_end=22, margin_bottom=16)
        groups = [(tr('背景样式'), [k for k in PAPERS[0][1] if k[0] != 'wide'])] if board else PAPERS
        for group, kinds in groups:
            label = Gtk.Label(label=tr(group), xalign=0, margin_top=14, margin_bottom=6)
            label.add_css_class('heading')
            body.append(label)
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                               max_children_per_line=5, min_children_per_line=5, homogeneous=True,
                               column_spacing=6, row_spacing=6)
            flow.add_css_class('papers')
            for kind, name in kinds:
                view = TextureView(self.PREVIEW_W, 128, halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
                view.add_css_class('thumb')
                frame = Gtk.Box(height_request=136, halign=Gtk.Align.CENTER)
                frame.append(view)
                cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                cell.append(frame)
                cell.append(Gtk.Label(label=tr(name)))
                child = Gtk.FlowBoxChild(child=cell)
                child.kind = kind
                flow.append(child)
                self._cells.append((kind, view))
                if kind == self.paper:
                    flow.select_child(child)
            flow.connect('child-activated', self._picked)
            self._flows.append(flow)
            body.append(flow)

        cancel = Gtk.Button(label=tr('取消'))
        cancel.connect('clicked', lambda *_: self.close())
        apply_btn = Gtk.Button(label=apply_label)
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', self._apply)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(apply_btn)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        right.append(options)
        right.append(Gtk.Separator())
        right.append(Gtk.ScrolledWindow(child=body, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        content = Gtk.Box()
        if title is not None:
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=250,
                           margin_start=18, margin_end=18, margin_top=16, margin_bottom=16)
            left.add_css_class('dialog-side')
            caption = Gtk.Label(label=tr('标题'), xalign=0)
            caption.add_css_class('heading')
            left.append(caption)
            self._entry = Gtk.Entry(text=title, placeholder_text=tr('未命名的白板') if board else tr('无标题的笔记本'),
                                    activates_default=True)
            self._entry.connect('activate', self._apply)
            left.append(self._entry)
            if not board:
                caption = Gtk.Label(label=tr('封面'), xalign=0, margin_top=14)
                caption.add_css_class('heading')
                left.append(caption)
                left.append(cover_picker(None, lambda c: setattr(self, 'cover', c)))
            caption = Gtk.Label(label=tr('背景') if board else tr('纸张样式'), xalign=0, margin_top=14)
            caption.add_css_class('heading')
            left.append(caption)
            self._big = TextureView(150, 200, halign=Gtk.Align.CENTER)
            self._big.add_css_class('thumb')
            holder = Gtk.Box(height_request=210, halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
            holder.append(self._big)
            left.append(holder)
            content.append(left)
            content.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        content.append(right)
        view = Adw.ToolbarView(content=content)
        view.add_top_bar(header)
        self.set_child(view)
        self._render()

    def template(self):
        color = PAPER_COLORS[self.color][1]
        if self.board:
            return {'w': BOARD_SIZE[0], 'h': BOARD_SIZE[1], 'paper': self.paper, 'color': color, 'board': True}
        w, h = SIZES[self.size][1]
        if self.landscape:
            w, h = h, w
        return {'w': w, 'h': h, 'paper': self.paper, 'color': color}

    def _changed(self, attr, value):
        setattr(self, attr, value)
        self._render()

    def _picked(self, flow, child):
        self.paper = child.kind
        for other in self._flows:
            if other is not flow:
                other.unselect_all()
        self._render_big()

    def _preview(self, kind, max_w, max_h):
        t = self.template()
        scale = self.get_scale_factor() or 1
        if self.board:
            # A whiteboard has no edges: show a patch of its background.
            page = Page(max_w * 2.2, max_h * 2.2, kind, color=t['color'], board=True)
        else:
            page = Page(t['w'], t['h'], kind, color=t['color'])
        k = min(max_w / page.w, max_h / page.h)
        w, h = round(page.w * k), round(page.h * k)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * scale, h * scale)
        cr = cairo.Context(surface)
        cr.scale(w * scale / page.w, h * scale / page.h)
        draw_paper(cr, page)
        return texture_from_surface(surface), (w, h)

    def _render_big(self):
        if self._big is not None:
            texture, self._big._size = self._preview(self.paper, 150, 200)
            self._big.set_texture(texture)
            self._big.queue_resize()

    def _render(self):
        for kind, view in self._cells:
            texture, view._size = self._preview(kind, self.PREVIEW_W, 128)
            view.set_texture(texture)
            view.queue_resize()
        self._render_big()

    def _apply(self, *_):
        if self._entry is not None:
            self._on_apply(self.template(), self._entry.get_text().strip(), self.cover)
        else:
            self._on_apply(self.template())
        self.close()
