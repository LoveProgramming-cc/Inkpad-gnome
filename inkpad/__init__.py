APP_ID = 'io.github.inkpad.Inkpad'
APP_NAME = 'Inkpad'
VERSION = '0.1.1'


def home_override():
    """INKPAD_HOME, when set, holds everything the app reads or writes: settings and
    Google credentials in its config/ and the notebooks in its library/. It is how tests
    (and a portable copy) stay away from the real library and the real Drive account."""
    import os
    return os.environ.get('INKPAD_HOME') or None
