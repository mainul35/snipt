#!/bin/sh
# Install Snipt for the current user.
#
# Runs from wherever the repository is checked out: the launcher is pointed at
# this directory, so there is no second copy of the source to drift out of sync.
set -e

SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
SHORTCUT="Meta+Shift+S"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c "import PyQt6.QtWidgets, PyQt6.QtDBus, PyQt6.QtNetwork" 2>/dev/null || {
    echo "PyQt6 is required:  sudo apt install python3-pyqt6" >&2; exit 1; }

mkdir -p "$BIN" "$APPS" "$AUTOSTART"

cat > "$BIN/snipt" <<LAUNCHER
#!/bin/sh
export QT_ENABLE_HIGHDPI_SCALING=1
exec python3 -c "import sys; sys.path.insert(0, '$SRC'); from snipt.main import main; sys.exit(main())" "\$@"
LAUNCHER
chmod +x "$BIN/snipt"

sed "s|^Exec=snipt|Exec=$BIN/snipt|" packaging/snipt.desktop > "$APPS/snipt.desktop"
sed "s|^Exec=snipt|Exec=$BIN/snipt|" packaging/snipt-daemon.desktop > "$AUTOSTART/snipt-daemon.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

# Register the global shortcut with KDE, without disturbing existing bindings.
KGS="$HOME/.config/kglobalshortcutsrc"
if [ -f "$KGS" ] && ! grep -q "^\[services\]\[snipt.desktop\]" "$KGS"; then
    cp "$KGS" "$KGS.bak-snipt"
    printf '\n[services][snipt.desktop]\n_launch=%s,none,Snipt\n' "$SHORTCUT" >> "$KGS"
    echo "Bound $SHORTCUT (previous config backed up to $KGS.bak-snipt)"
    echo "If the hotkey does not respond, log out and back in, or set it in"
    echo "System Settings > Keyboard > Shortcuts."
fi

echo "Installed. Starting the tray daemon…"
"$BIN/snipt" --daemon >/dev/null 2>&1 &
echo "Done. Press $SHORTCUT to snip."
