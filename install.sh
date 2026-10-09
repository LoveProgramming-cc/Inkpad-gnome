#!/bin/sh
# Adds Inkpad to the GNOME app grid for the current user (no root needed).
# Undo with ./uninstall.sh
set -e
here="$(cd "$(dirname "$0")" && pwd)"
id=io.github.inkpad.Inkpad
data="${XDG_DATA_HOME:-$HOME/.local/share}"
apps="$data/applications"
theme="$data/icons/hicolor"

mkdir -p "$apps" "$theme/scalable/apps"
cp "$here/data/$id.svg" "$theme/scalable/apps/"
# Bitmap sizes as well: not every launcher renders SVG icons from a user theme.
for size in 48 64 128 256 512; do
    mkdir -p "$theme/${size}x${size}/apps"
    python3 "$here/data/render_icon.py" "$size" "$theme/${size}x${size}/apps/$id.png"
done

# The path may contain spaces; inside a desktop file the command is quoted and
# backslash, quote, dollar and backtick are escaped.
exec_path=$(printf '%s' "$here/bin/inkpad" | sed 's/[\\"$`]/\\&/g')
cat > "$apps/$id.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Inkpad
Name[zh_CN]=Inkpad 笔记
Name[ja]=Inkpad ノート
Comment=Handwritten notes and PDF annotation
Comment[zh_CN]=手写笔记与 PDF 批注
Comment[ja]=手書きノートと PDF への書き込み
Exec="$exec_path" %F
Icon=$id
Terminal=false
Categories=GNOME;GTK;Office;
MimeType=application/pdf;
StartupNotify=true
StartupWMClass=$id
DESKTOP
chmod 644 "$apps/$id.desktop"

update-desktop-database "$apps" 2>/dev/null || true
# A stale icon cache hides new icons, so refresh it, or drop it if that is not possible.
if [ -f "$theme/icon-theme.cache" ]; then
    gtk4-update-icon-cache -q -t -f "$theme" 2>/dev/null \
        || gtk-update-icon-cache -q -t -f "$theme" 2>/dev/null \
        || rm -f "$theme/icon-theme.cache"
fi
touch "$theme"
echo "Installed. Launch “Inkpad” from the app grid, or run: $here/bin/inkpad"
echo "If the icon does not show at once, log out and back in."
