#!/bin/sh
# Removes the launcher and icons that install.sh added.
# Your notebooks (~/Documents/Inkpad) and settings (~/.config/inkpad) are left alone.
id=io.github.inkpad.Inkpad
data="${XDG_DATA_HOME:-$HOME/.local/share}"
apps="$data/applications"
theme="$data/icons/hicolor"

rm -f "$apps/$id.desktop"
rm -f "$theme/scalable/apps/$id.svg"
for size in 48 64 128 256 512; do
    rm -f "$theme/${size}x${size}/apps/$id.png"
done
update-desktop-database "$apps" 2>/dev/null || true
if [ -f "$theme/icon-theme.cache" ]; then
    gtk4-update-icon-cache -q -t -f "$theme" 2>/dev/null \
        || gtk-update-icon-cache -q -t -f "$theme" 2>/dev/null \
        || rm -f "$theme/icon-theme.cache"
fi
echo "Inkpad was removed from the app grid."
echo "Kept: your notebooks in ~/Documents/Inkpad and your settings in ~/.config/inkpad."
echo "The program itself is this folder; delete it if you no longer want it."
