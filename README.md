# Inkpad

Handwritten notes, whiteboards and PDF annotation for GNOME, written for a pen and a touch
screen. Python, GTK 4 and libadwaita.

- Pressure-sensitive pens, highlighter, shapes, text, sticky notes, tape, ruler, laser pointer
- Notebooks with pages (scrolling down or turning sideways) and endless whiteboards
- Lasso selection: move, resize, copy, recolour, rotate
- PDF import and annotation, export to vector PDF and PNG
- Import of Goodnotes documents (`.goodnotes`)
- A library with folders, favourites, trash, drag and drop
- Optional sync with your own Google Drive, using your own OAuth client
- Interface in Chinese, English and Japanese

## Running

Needs Python 3, PyGObject, GTK 4, libadwaita and pycairo, as shipped with a current GNOME
desktop.

```sh
bin/inkpad          # run from the source tree
./install.sh        # add it to the app grid for this user; ./uninstall.sh removes it
```

Notebooks are kept as ordinary files in `~/Documents/Inkpad`; settings are in
`~/.config/inkpad`.

## Google Drive sync

Sync uses an OAuth client that you create yourself in the Google Cloud console (type
"Desktop app", with the Drive API enabled). Inkpad asks only for the `drive.file` scope, so
it can see nothing in your Drive but the files it made. No credentials are part of this
repository; yours stay in `~/.config/inkpad`.

## Goodnotes

Inkpad is not affiliated with, endorsed by or connected to Goodnotes. "Goodnotes" is a
trademark of its owner and is used here only to say which files can be imported. The
importer was written by studying files, for interoperability, and contains no Goodnotes
code or assets.

## How it was made

Inkpad was written with the help of AI coding assistants, directed and tested by its author.

## Licence

Copyright © 2026 the Inkpad authors.

Inkpad is free software under the [GNU General Public License, version 3](LICENSE) or (at
your option) any later version. In short: you may use, change and pass it on, but anything
you distribute that is built on this code must keep the copyright and licence notices,
credit this project, and be released with its source under the same licence. It comes with
no warranty.

Third-party material is listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
