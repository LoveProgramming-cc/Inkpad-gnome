import os
import sys

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID  # noqa: E402
from .window import Window  # noqa: E402


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # The window is matched to its launcher (and so to its icon) by this name.
        GLib.set_prgname(APP_ID)
        Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        provider.load_from_path(os.path.join(os.path.dirname(__file__), 'style.css'))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _window(self):
        window = self.get_active_window() or Window(self)
        window.present()
        return window

    def do_activate(self):
        self._window()

    def do_open(self, files, n_files, hint):
        self._window().open_external(files[0])


def main():
    return App().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
