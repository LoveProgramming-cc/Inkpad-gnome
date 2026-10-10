"""The settings dialog: appearance, language, writing options and Google Drive."""
import os
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .i18n import tr
from . import gdrive, migrate, settings

# Labels are translated when the rows are built.
THEMES = [('system', '跟随系统'), ('light', '浅色'), ('dark', '深色')]
LANGUAGES = [('auto', '跟随系统'), ('zh', '简体中文'), ('en', 'English'), ('ja', '日本語')]


def apply_theme(name):
    scheme = {'light': Adw.ColorScheme.FORCE_LIGHT, 'dark': Adw.ColorScheme.FORCE_DARK}.get(name, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


def _home_path(path):
    """A path as it is usually written, with ~ for the home folder."""
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path.startswith(home + os.sep) else path


def _combo(title, subtitle, options, current, on_change):
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([tr(label) for _, label in options]))
    row.set_selected(next((i for i, (key, _) in enumerate(options) if key == current), 0))
    row.connect('notify::selected', lambda r, _: on_change(options[r.get_selected()][0]))
    return row


def _switch(title, subtitle, active, on_change):
    row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
    row.connect('notify::active', lambda r, _: on_change(r.get_active()))
    return row


class StorageDialog(Adw.Dialog):
    """What deleted notebooks still take up, here and in the cloud, and getting them back."""

    def __init__(self, win):
        super().__init__(title=tr('管理储存空间'), content_width=600, content_height=640)
        self.win = win
        self.cloud = None      # deleted notebooks listed from the cloud index, once asked for
        self.picked = set()
        self.busy = ''
        self.view = Adw.ToolbarView()
        self.view.add_top_bar(Adw.HeaderBar())
        self.toasts = Adw.ToastOverlay(child=self.view)
        self.set_child(self.toasts)
        self._fill()

    def _fill(self):
        win, store = self.win, self.win.store
        page = Adw.PreferencesPage()

        local = Adw.PreferencesGroup(
            title=tr('本机已删除的笔记'),
            description=tr('永久删除的笔记仍保留在本机，共 %s。清理只影响本机，不会删除云端的任何内容。')
            % GLib.format_size(store.cache_size()))
        entries = sorted(store.archived(), key=lambda e: -e.mtime)
        clear = Gtk.Button(label=tr('清理缓存'), valign=Gtk.Align.CENTER, sensitive=bool(entries))
        clear.add_css_class('destructive-action')
        clear.connect('clicked', self._clear)
        local.set_header_suffix(clear)
        for entry in entries:
            origin = store.archived_origin(entry.path)
            where = tr('文稿') if origin in ('.', '') else tr('文稿 / ') + origin.replace(os.sep, ' / ')
            name = entry.name.partition(' ')[2] or entry.name
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(tr('恢复到：%s') % where))
            back = Gtk.Button(label=tr('恢复'), valign=Gtk.Align.CENTER)
            back.connect('clicked', lambda b, path=entry.path: self._restore(path))
            row.add_suffix(back)
            local.add(row)
        if not entries:
            local.add(Adw.ActionRow(title=tr('没有已删除的项目')))
        page.add(local)

        cloud = Adw.PreferencesGroup(
            title=tr('云端保留的已删除笔记'),
            description=tr('在本机删除的笔记，云端的文件仍然保留，但不会自动下载回来。'
                           '先读取云端目录，再选择要还原的笔记。'))
        if not gdrive.connected():
            cloud.add(Adw.ActionRow(title=tr('尚未连接 Google Drive')))
        elif self.busy:
            row = Adw.ActionRow(title=self.busy)
            self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=160)
            row.add_suffix(self.bar)
            cloud.add(row)
        elif self.cloud is None:
            load = Adw.ButtonRow(title=tr('读取云端目录'))
            load.connect('activated', lambda *_: self._list_cloud())
            cloud.add(load)
        elif not self.cloud:
            cloud.add(Adw.ActionRow(title=tr('云端没有可还原的已删除笔记')))
        else:
            for item in self.cloud:
                when = time.strftime(tr('%Y年%-m月%-d日 %H:%M'), time.localtime(item['ts'])) if item['ts'] else ''
                folder, name = os.path.split(item['path'])
                where = tr('文稿') if not folder else tr('文稿 / ') + folder.replace('/', ' / ')
                row = Adw.ActionRow(title=GLib.markup_escape_text(os.path.splitext(name)[0]),
                                    subtitle=GLib.markup_escape_text('%s · %s' % (where, tr('删除于 %s') % when)))
                check = Gtk.CheckButton(active=item['id'] in self.picked, valign=Gtk.Align.CENTER)
                check.connect('toggled', lambda c, nid=item['id']: (
                    self.picked.add(nid) if c.get_active() else self.picked.discard(nid), self._count()))
                row.add_prefix(check)
                row.set_activatable_widget(check)
                cloud.add(row)
            self.fetch = Adw.ButtonRow(title='')
            self.fetch.connect('activated', lambda *_: self._fetch())
            cloud.add(self.fetch)
            self._count()
        page.add(cloud)
        self.view.set_content(page)

    def _count(self):
        self.fetch.set_title(tr('下载所选的 %d 本') % len(self.picked))
        self.fetch.set_sensitive(bool(self.picked))

    def _restore(self, path):
        try:
            new = self.win.store.unarchive(path)
        except Exception as exc:
            self.toasts.add_toast(Adw.Toast(title=str(exc)))
            return
        self.toasts.add_toast(Adw.Toast(title=tr('已恢复到 %s') % self.win.store.rel(new)))
        self.win.library.refresh()
        self._fill()

    def _clear(self, *_):
        dialog = Adw.AlertDialog(heading=tr('清理缓存？'),
                                 body=tr('本机上已永久删除的笔记将无法再恢复。云端的内容不受影响。'))
        dialog.add_response('cancel', tr('取消'))
        dialog.add_response('clear', tr('清理缓存'))
        dialog.set_response_appearance('clear', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda d, r: r == 'clear' and (self.win.store.clear_cache(), self._fill()))
        dialog.present(self)

    def _background(self, label, job, done):
        """Run job(drive, progress) off the main loop; nothing else may sync meanwhile."""
        win = self.win
        if win._sync_busy:
            self.toasts.add_toast(Adw.Toast(title=tr('正在同步，请等同步结束后再试')))
            return
        win._sync_busy = True
        self.busy = label
        self._fill()

        def progress(fraction, text):
            GLib.idle_add(lambda: self.bar.set_fraction(fraction) and False)

        def work():
            try:
                result, error = job(gdrive.Drive(), progress), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(finish, result, error)

        def finish(result, error):
            win._sync_busy = False
            self.busy = ''
            if error is not None:
                self.toasts.add_toast(Adw.Toast(title=str(error)))
            else:
                done(result)
            self._fill()
            return False

        threading.Thread(target=work, daemon=True).start()

    def _list_cloud(self):
        root = self.win.store.root
        self._background(tr('正在读取云端目录…'), lambda drive, progress: gdrive.cloud_deleted(root, drive),
                         lambda found: setattr(self, 'cloud', found))

    def _fetch(self):
        root, ids = self.win.store.root, [item['id'] for item in self.cloud if item['id'] in self.picked]

        def done(count):
            self.cloud = [item for item in self.cloud if item['id'] not in self.picked]
            self.picked.clear()
            self.win.library.refresh()
            self.toasts.add_toast(Adw.Toast(title=tr('已从云端还原 %d 本笔记') % count))

        self._background(tr('正在下载…'), lambda drive, progress: gdrive.cloud_restore(root, drive, ids, progress), done)


def run_migration(win, new):
    """Move the library to new behind a progress window; the app carries on there afterwards."""
    old = win.store.root
    try:
        migrate.check(old, new)
    except migrate.MigrateError as exc:
        win._error(tr('无法迁移文稿库'), exc)
        return
    if win._sync_busy:
        win._error(tr('无法迁移文稿库'), tr('正在同步，请等同步结束后再试'))
        return
    win.canvas._cancel()
    win.save_now()
    win._sync_busy = True   # nothing may sync while files are on the move

    bar = Gtk.ProgressBar(show_text=True, width_request=360, ellipsize=Pango.EllipsizeMode.MIDDLE)
    dialog = Adw.AlertDialog(heading=tr('正在迁移文稿库…'), body=tr('请不要关闭应用。'), extra_child=bar)
    dialog.set_can_close(False)
    dialog.present(win)

    def progress(fraction, text):
        GLib.idle_add(lambda: (bar.set_fraction(fraction), bar.set_text(text or '%d%%' % round(fraction * 100))) and False)

    def work():
        try:
            migrate.move_library(old, new, progress)
            error = None
        except Exception as exc:
            error = exc
        GLib.idle_add(done, error)

    def done(error):
        win._sync_busy = False
        dialog.set_can_close(True)
        dialog.force_close()
        if error is not None:
            win._error(tr('迁移失败，文稿库仍在原来的位置'), error)
        else:
            win.set_library(new)
            win.toast(tr('文稿库已迁移到 %s') % new)
        return False

    threading.Thread(target=work, daemon=True).start()


class PrefsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title=tr('设置'), content_width=620, content_height=680)
        self.win = win
        st = win.st
        page = Adw.PreferencesPage()

        look = Adw.PreferencesGroup(title=tr('外观与语言'))
        look.add(_combo(tr('外观'), tr('页面本身始终保持纸张颜色'), THEMES, st.get('theme', 'system'), self._theme))
        look.add(_combo(tr('语言'), '', LANGUAGES, st.get('language', 'auto'), self._language))
        page.add(look)

        writing = Adw.PreferencesGroup(title=tr('书写'))
        for key, title, subtitle in (('scribble_erase', tr('涂画以擦除'), tr('在字迹上来回涂抹即可删除')),
                                     ('hold_shape', tr('长按绘制规则形状'), tr('画完停住片刻，笔迹变成直线、矩形或椭圆')),
                                     ('finger_draw', tr('用手指书写'), tr('关闭时手指只用于滚动和缩放')),
                                     ('crosshair', tr('显示十字光标'), tr('关闭后书写时只显示十字中心的小圆点')),
                                     ('mouse_hand', tr('有手写笔时鼠标不书写'),
                                      tr('用过手写笔之后，鼠标拖动改为移动页面，点击胶带可掀开或盖回'))):
            writing.add(_switch(title, subtitle, st.get(key, True), lambda on, key=key: win._set(st, key, on)))
        writing.add(_combo(tr('翻页方向'), tr('左右翻页时，在第一页或最后一页继续拉动可新建一页'),
                           [('vertical', '上下'), ('horizontal', '左右')], st.get('paging', 'vertical'),
                           lambda value: win._set(st, 'paging', value)))
        writing.add(_switch(tr('显示滚动条'), tr('只能用鼠标拖动滑块；点击滑块以外的地方不会跳转，手指和笔不会碰到它'),
                            st.get('scrollbars', True), lambda on: win._set(st, 'scrollbars', on)))
        writing.add(_combo(tr('最大放大倍数'), tr('按屏幕上实际看到的大小计算，页面再大也能放大到这个程度'),
                           [(6.0, '600%'), (10.0, '1000%'), (20.0, '2000%'), (40.0, '4000%')],
                           float(st.get('max_zoom', 6.0)), lambda value: win._set(st, 'max_zoom', value)))
        page.add(writing)

        drive = Adw.PreferencesGroup(
            title=tr('Google Drive 同步'),
            description=tr('同步使用你自己的 Google Cloud OAuth 客户端。在 Google Cloud 控制台创建“桌面应用”类型的'
                        ' OAuth 客户端并启用 Google Drive API，然后把客户端 ID 和密钥填在这里。'
                        '应用只申请访问它自己创建的文件。'))
        client = gdrive.load_client() or {}
        self.client_id = Adw.EntryRow(title=tr('客户端 ID'), text=client.get('client_id', ''))
        self.secret = Adw.PasswordEntryRow(title=tr('客户端密钥'), text=client.get('client_secret', ''))
        drive.add(self.client_id)
        drive.add(self.secret)
        save = Adw.ButtonRow(title=tr('保存凭据'))
        save.connect('activated', self._save_client)
        drive.add(save)
        load = Adw.ButtonRow(title=tr('从 JSON 凭据文件导入…'))
        load.connect('activated', self._import_client)
        drive.add(load)
        export = Adw.ButtonRow(title=tr('导出凭据为 JSON…'))
        export.connect('activated', self._export_client)
        drive.add(export)
        self.folder = Adw.EntryRow(title=tr('文件夹 ID（留空：云端硬盘顶层的“Inkpad”文件夹）'), text=gdrive.folder_id(),
                                   show_apply_button=True)
        self.folder.connect('apply', self._set_folder)
        self.folder_now = Adw.ActionRow(title=tr('当前同步文件夹'), subtitle_selectable=True)
        visit = Gtk.Button(label=tr('在浏览器中打开'), valign=Gtk.Align.CENTER)
        visit.connect('clicked', lambda *_: Gio.AppInfo.launch_default_for_uri(
            'https://drive.google.com/drive/folders/' + gdrive.cloud_folder(win.store.root), None))
        self.folder_now.add_suffix(visit)
        self.folder_visit = visit
        drive.add(self.folder_now)
        # How to keep the library somewhere else in Drive, folded away until it is asked for.
        how = Adw.ExpanderRow(title=tr('如何把笔记放到云端硬盘的其它位置'),
                              subtitle=tr('Inkpad 只能使用它自己创建的文件夹'))
        steps = Gtk.Label(wrap=True, xalign=0, selectable=True, margin_start=14, margin_end=14, margin_top=12,
                          margin_bottom=12, label=tr(
            'Inkpad 只申请了“访问自己创建的文件”这一项权限，所以你在 Google Drive 里手动建的文件夹它看不到，'
            '直接填那个文件夹的 ID 会同步失败。要把笔记放进别的文件夹，这样做：\n\n'
            '1. 上面的“文件夹 ID”留空，同步一次。Inkpad 会在云端硬盘顶层建一个名为“Inkpad”的文件夹。\n\n'
            '2. 点“当前同步文件夹”右边的“在浏览器中打开”，在 Google Drive 里把这个“Inkpad”文件夹'
            '移动（拖动，或右键 → 移动）到你想放的文件夹里。移动不会改变它的 ID，这台设备不需要改任何设置。\n\n'
            '3. 在其它设备上，把“当前同步文件夹”里显示的 ID 填进“文件夹 ID”；或者在这里点“导出凭据为 JSON…”，'
            '到那台设备上用“从 JSON 凭据文件导入…”，文件夹 ID 会一起带过去。\n\n'
            '注意：其它设备必须使用同一组客户端 ID 和密钥，并登录同一个 Google 账号，否则同样看不到这个文件夹。'
            '以后如果更改“文件夹 ID”，这台设备会把全部笔记重新备份到新文件夹，其它设备也要改成同一个 ID。'))
        # The field itself is in here too: out in the open it is too easily changed by mistake.
        how.add_row(self.folder)
        how.add_row(Adw.PreferencesRow(child=steps, activatable=False))
        drive.add(how)
        self.client_where = Adw.ActionRow(title=tr('凭据保存位置'), subtitle_selectable=True)
        folder = Gtk.Button(label=tr('打开文件夹'), valign=Gtk.Align.CENTER)
        folder.connect('clicked', lambda *_: Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(os.path.dirname(gdrive.CLIENT_PATH)).get_uri(), None))
        self.client_where.add_suffix(folder)
        drive.add(self.client_where)
        drive.add(_combo(tr('打开的笔记本多久同步一次'),
                         tr('每隔这么久查看一次其它设备写了什么；自己写的内容在停笔几秒后就会同步，与这里无关'),
                         [(5, '5 秒'), (10, '10 秒'), (15, '15 秒'), (30, '30 秒'), (60, '1 分钟'), (120, '2 分钟'), (300, '5 分钟')],
                         int(st.get('sync_every', 30)),
                         lambda value: (win._set(st, 'sync_every', value), win.watch_open())))
        self.status = Adw.ActionRow(title=tr('状态'))
        self.connect_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.connect_btn.connect('clicked', self._toggle_connection)
        self.sync_btn = Gtk.Button(label=tr('立即同步'), valign=Gtk.Align.CENTER)
        self.sync_btn.connect('clicked', lambda *_: win.sync_now())
        self.status.add_suffix(self.sync_btn)
        self.status.add_suffix(self.connect_btn)
        drive.add(self.status)
        page.add(drive)

        storage = Adw.PreferencesGroup(title=tr('存储'))
        where = Adw.ActionRow(title=tr('文稿库位置'), subtitle=win.store.root, subtitle_selectable=True)
        reveal = Gtk.Button(label=tr('打开文件夹'), valign=Gtk.Align.CENTER)
        reveal.connect('clicked', lambda *_: Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(win.store.root).get_uri(), None))
        where.add_suffix(reveal)
        move = Gtk.Button(label=tr('更改…'), valign=Gtk.Align.CENTER)
        move.connect('clicked', self._move_library)
        where.add_suffix(move)
        storage.add(where)
        export = Adw.ButtonRow(title=tr('导出整个文稿库…'))
        export.connect('activated', lambda *_: (self.close(), win.library.export_folder(win.store.root)))
        storage.add(export)
        restore = Adw.ButtonRow(title=tr('导入存档（.zip）…'))
        restore.connect('activated', lambda *_: (self.close(), win.go_home(), win.library.import_archive()))
        storage.add(restore)
        manage = Adw.ButtonRow(title=tr('管理储存空间…'))
        manage.connect('activated', lambda *_: (self.close(), StorageDialog(win).present(win)))
        storage.add(manage)
        page.add(storage)

        from .window import TOOLS
        bar = Adw.PreferencesGroup(title=tr('工具栏'), description=tr('关掉不用的工具，让工具栏更清爽。'))
        hidden = st.setdefault('hidden_tools', [])

        def show_tool(name, on):
            if on and name in hidden:
                hidden.remove(name)
            elif not on and name not in hidden:
                hidden.append(name)
            win.apply_hidden_tools()

        for name, _draw, label in list(TOOLS) + [('image', None, '插入图片'), ('ruler', None, '尺子')]:
            if name != 'pen':
                bar.add(_switch(tr(label), '', name not in hidden, lambda on, name=name: show_tool(name, on)))
        page.add(bar)

        advanced = Adw.PreferencesGroup(title=tr('高级'))
        palm = Adw.SpinRow.new_with_range(0, 1000, 10)
        palm.set_title(tr('防误触时间（毫秒）'))
        palm.set_subtitle(tr('笔有动作后的这段时间内忽略手指触摸；页面在写字时乱跳就调大，手指反应迟钝就调小'))
        palm.set_value(st.get('palm_ms', 80))
        palm.connect('notify::value', lambda row, _: win._set(st, 'palm_ms', int(row.get_value())))
        advanced.add(palm)
        advanced.add(_switch(tr('记录输入日志'), tr('排查笔画丢失等问题时打开：笔和手指的事件会写入 %s') % os.path.join(settings.CONFIG_DIR, 'input.log'),
                             st.get('input_log', False), lambda on: win._set(st, 'input_log', on)))
        page.add(advanced)

        self.add(page)
        self._refresh()

    def _move_library(self, *_):
        """Pick a new folder and move the whole library there."""
        win = self.win
        dialog = Gtk.FileDialog(title=tr('选择文稿库的新位置（空文件夹）'))

        def chosen(dialog, result):
            try:
                new = dialog.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            self.close()
            run_migration(win, new)

        dialog.select_folder(win, None, chosen)

    def _language(self, value):
        if value != self.win.st.get('language', 'auto'):
            self.close()
            # After the dialog has gone: the window is rebuilt in the new language.
            GLib.idle_add(lambda: self.win.change_language(value) and False)

    def _theme(self, value):
        self.win.st['theme'] = value
        apply_theme(value)

    def _refresh(self):
        linked = gdrive.connected()
        self.status.set_subtitle(tr('已连接') if linked else
                                 (tr('未连接') if gdrive.load_client() else tr('请先填写并保存凭据')))
        self.connect_btn.set_label(tr('断开连接') if linked else tr('连接…'))
        self.connect_btn.set_sensitive(gdrive.load_client() is not None)
        self.sync_btn.set_visible(linked)
        saved = os.path.exists(gdrive.CLIENT_PATH)
        self.folder.set_sensitive(saved)
        now = gdrive.cloud_folder(self.win.store.root)
        self.folder_now.set_subtitle(now or tr('还没有同步过'))
        self.folder_visit.set_sensitive(bool(now))
        self.client_where.set_subtitle(_home_path(gdrive.CLIENT_PATH) if saved else tr('尚未保存'))

    def _save_client(self, *_):
        try:
            gdrive.save_client(self.client_id.get_text(), self.secret.get_text())
        except Exception as exc:
            self.add_toast(Adw.Toast(title=str(exc)))
            return
        self.add_toast(Adw.Toast(title=tr('凭据已保存到 %s') % _home_path(gdrive.CLIENT_PATH)))
        self._refresh()

    def _set_folder(self, *_):
        try:
            value = gdrive.parse_folder(self.folder.get_text())
        except Exception as exc:
            self.add_toast(Adw.Toast(title=str(exc)))
            return
        self.folder.set_text(value)

        def apply():
            try:
                gdrive.set_folder_id(value)
            except Exception as exc:
                self.add_toast(Adw.Toast(title=str(exc)))
                return
            self.add_toast(Adw.Toast(title=tr('同步文件夹已更改，下次同步时生效')))
            self._refresh()

        if value == gdrive.folder_id():
            return
        if not gdrive.cloud_folder(self.win.store.root):
            apply()   # nothing has been synced yet, so nothing has to be done over
            return
        dialog = Adw.AlertDialog(
            heading=tr('更改同步文件夹？'),
            body=tr('这台设备上的全部笔记需要重新备份到新的文件夹，可能要花很长时间。原文件夹里的文件不会被移动或删除。\n\n'
                    '其它设备也要填上同一个文件夹 ID（或导入从这里导出的凭据 JSON），否则它们仍然和原来的文件夹同步。\n\n'
                    'Inkpad 只能访问它自己创建的文件夹：填的应当是 Inkpad 生成的那个文件夹的 ID（在 Google Drive 里把它移到别处后 ID 不变）。'))
        dialog.add_response('cancel', tr('取消'))
        dialog.add_response('yes', tr('更改'))
        dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda d, choice: apply() if choice == 'yes' else self.folder.set_text(gdrive.folder_id()))
        dialog.present(self)

    def _export_client(self, *_):
        if gdrive.load_client() is None:
            self.add_toast(Adw.Toast(title=tr('请先填写并保存凭据')))
            return
        dialog = Gtk.FileDialog(title=tr('导出凭据为 JSON…'), initial_name='inkpad_google_client.json')

        def chosen(dialog, result):
            try:
                dest = dialog.save_finish(result).get_path()
            except GLib.Error:
                return  # dismissed
            try:
                gdrive.export_client(dest)
            except Exception as exc:
                self.add_toast(Adw.Toast(title=str(exc)))
                return
            self.add_toast(Adw.Toast(title=tr('已导出到 %s（其中有客户端密钥，请妥善保管）') % _home_path(dest)))

        dialog.save(self.win, None, chosen)

    def _import_client(self, *_):
        dialog = Gtk.FileDialog(title=tr('选择 Google OAuth 客户端凭据（JSON）'))

        def chosen(dialog, result):
            try:
                gdrive.install_client(dialog.open_finish(result).get_path())
            except GLib.Error:
                return  # dismissed
            except Exception as exc:
                self.add_toast(Adw.Toast(title=str(exc)))
                return
            client = gdrive.load_client() or {}
            self.client_id.set_text(client.get('client_id', ''))
            self.secret.set_text(client.get('client_secret', ''))
            self.folder.set_text(gdrive.folder_id())
            self.add_toast(Adw.Toast(title=tr('凭据已保存到 %s') % _home_path(gdrive.CLIENT_PATH)))
            self._refresh()

        dialog.open(self.win, None, chosen)

    def _toggle_connection(self, *_):
        if gdrive.connected():
            self.win._drive_disconnect()
            self._refresh()
        else:
            self.close()
            self.win._drive_connect()
