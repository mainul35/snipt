# Snipt

A lightweight snip-and-annotate tool for this KDE neon / Plasma 6 **Wayland** session.

    Meta+Shift+S  ->  drag a region  ->  it is on your clipboard, editor opens

## Install

Requires Plasma 6 on Wayland and PyQt6:

    sudo apt install python3-pyqt6
    git clone <this repo> snipt && cd snipt
    ./install.sh

`install.sh` points a launcher at wherever you cloned it, installs the desktop
entry and autostart entry, and binds `Meta+Shift+S`. It backs up
`kglobalshortcutsrc` before touching it.

## Usage

    snipt              # region snip (default)
    snipt full         # whole screen straight into the editor
    snipt -d 5         # wait 5 s, then snip (for menus/hover states)
    snipt --daemon     # sit in the tray without capturing

Left-clicking the tray icon starts a region snip; its menu has delays and
"Open snips folder".

### In the selection overlay
| Key / action | Effect |
|---|---|
| drag | select a region |
| click (no drag) | capture the whole screen |
| `Ctrl+A` | select everything |
| `Esc` / right-click | cancel |

A pixel loupe follows the cursor showing the magnified pixels, the exact
device-pixel coordinate and the colour under the crosshair.

### In the editor
Tools: `V` select/move, `P` pen, `H` highlighter, `A` arrow, `L` line,
`R` rectangle, `E` ellipse, `T` text, `N` numbered step, `B` blur,
`X` pixelate, `C` crop (drag, then `Enter`).

`Ctrl+Z`/`Ctrl+Shift+Z` undo/redo · `Ctrl+C` copy · `Ctrl+S` save ·
`Ctrl+Shift+S` save as · `Ctrl+±`/`Ctrl+0` zoom · `Esc` close.

Snips save to `~/Pictures/Snips/Snip_YYYYMMDD_HHMMSS.png`.

## Why it is built this way

**Capture goes through xdg-desktop-portal.** On Wayland a client cannot read
the framebuffer. KWin's fast `org.kde.KWin.ScreenShot2` DBus API exists but is
allowlisted to the process names `spectacle`/`org.kde.spectacle`, so anything
else gets `NoAuthorized`. Measured here: portal ~1.0 s warm, `spectacle -b`
~2.1 s (kept as the fallback). The portal writes its grab into `~/Pictures`;
Snipt loads that file and unlinks it so your picture library stays clean.

**It stays resident in the tray.** Two reasons, both Wayland rules rather than
preference: a Wayland client only owns the clipboard while it is alive, so a
fire-and-exit tool would take your snip with it when it quit; and the first
portal call of a session costs ~8 s to start the portal, which the daemon pays
once at login instead of on your first snip.

**Auto-copy waits for window activation.** Only a *focused* client may set the
Wayland selection. Copying in the editor's constructor silently produced an
empty clipboard, so the copy now fires from `changeEvent` on activation.

**Coordinates are derived from the captured image, not from Qt.** This panel is
rotated and runs at a 1.5x fractional scale: 800x1280 logical, 1200x1920
device. Qt reports `devicePixelRatio` 2.0 here (it renders at 2x and lets KWin
downscale), so trusting it would misplace every selection. The overlay computes
`scale = image.width / virtual_desktop.width` instead, which is exactly 1.5.
Crops are therefore taken from the original 1200x1920 capture and are
pixel-exact regardless of what Qt reports; only the on-screen overlay preview is
slightly resampled.

**Blur/pixelate composite incrementally.** They sample the partially composited
result, not the pristine base, so they obscure earlier annotations instead of
erasing them. Strength scales with region size — a fixed factor that redacts a
small box leaves large text readable in a big one.

## Layout

    snipt/capture.py    portal + spectacle capture backends
    snipt/overlay.py    frozen-screen region selector
    snipt/editor.py     annotation items, canvas, editor window
    snipt/main.py       tray daemon, single-instance IPC, CLI
    packaging/          desktop entries
    install.sh          per-user installer

Installed to `~/.local/bin/snipt`, `~/.local/share/applications/`,
`~/.config/autostart/`, plus a `[services][snipt.desktop]` group in
`~/.config/kglobalshortcutsrc`.

Set `SNIPT_DEBUG=1` to trace capture/IPC to stderr.

## Uninstall

    rm -f ~/.local/bin/snipt \
          ~/.local/share/applications/snipt.desktop \
          ~/.config/autostart/snipt-daemon.desktop
    # then remove the [services][snipt.desktop] group from kglobalshortcutsrc
    # (a pre-change backup is at ~/.config/kglobalshortcutsrc.bak-snipt)
