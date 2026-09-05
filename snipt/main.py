"""Snipt — a Windows-Snipping-Tool-style capture+annotate tool for Plasma Wayland.

Runs as one resident process with a tray icon.  That is not decoration: on
Wayland clipboard contents live only as long as the process that offered them,
so a fire-and-exit tool would leave you with an empty clipboard the moment it
quit.  Staying resident also lets the portal be pre-warmed, which turns the
first snip of a session from ~8s into ~1s.

Further invocations of `snipt` do not start a second process; they hand the
command to the running one over a Unix socket and exit.
"""

import argparse
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QStandardPaths, Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QIcon, QImage
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .capture import Capture, _log
from .editor import EditorWindow
from .overlay import RegionOverlay

SOCKET = "snipt-%d" % os.getuid()
ICON = "applets-screenshooter"


def pictures_dir():
    p = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    return Path(p or Path.home() / "Pictures") / "Snips"


class Snipt(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setApplicationName("Snipt")
        self.setDesktopFileName("snipt")
        self.setQuitOnLastWindowClosed(False)     # tray keeps us alive
        self.setWindowIcon(QIcon.fromTheme(ICON))

        self.save_dir = pictures_dir()
        self.capture = Capture(parent=self)
        self.capture.captured.connect(self._on_captured)
        self.capture.failed.connect(self._on_failed)
        self.overlay = None
        self.windows = []
        self.pending_mode = "region"
        self._conns = set()
        self.owns_socket = False

        self._build_tray()
        self._serve()
        QTimer.singleShot(300, self.capture.prewarm)

    # -- tray --------------------------------------------------------------
    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon.fromTheme(ICON), self)
        self.tray.setToolTip("Snipt — Meta+Shift+S to snip")
        menu = QMenu()
        menu.addAction(QAction("New snip\tMeta+Shift+S", self,
                               triggered=lambda: self.start("region")))
        menu.addAction(QAction("Whole screen", self, triggered=lambda: self.start("full")))
        menu.addSeparator()
        menu.addAction(QAction("Snip after 3 s", self,
                               triggered=lambda: self.start("region", delay=3)))
        menu.addAction(QAction("Snip after 5 s", self,
                               triggered=lambda: self.start("region", delay=5)))
        menu.addSeparator()
        menu.addAction(QAction("Open snips folder", self, triggered=self._open_folder))
        menu.addSeparator()
        menu.addAction(QAction("Quit Snipt", self, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def _on_tray(self, reason):
        _log("tray activated: %s" % reason)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.start("region")

    def _open_folder(self):
        self.save_dir.mkdir(parents=True, exist_ok=True)
        os.system("xdg-open %s &" % str(self.save_dir).replace(" ", r"\ "))

    # -- single instance ---------------------------------------------------
    def _serve(self):
        """Claim the command socket.

        Never remove the socket unconditionally: if a live instance owns it,
        doing so produces two processes that both believe they are primary,
        and commands then go to whichever one won the race.  A socket is only
        cleared once a connect attempt proves nothing is listening on it.
        """
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(SOCKET):
            probe = QLocalSocket()
            probe.connectToServer(SOCKET)
            if probe.waitForConnected(300):
                probe.abort()
                _log("another instance already owns the socket")
                self.owns_socket = False
                return
            QLocalServer.removeServer(SOCKET)      # stale, previous run died
            self.server.listen(SOCKET)
        self.owns_socket = True
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self):
        conn = self.server.nextPendingConnection()
        if conn is None:
            return
        self._conns.add(conn)          # keep a reference until it is drained

        def read():
            if conn.bytesAvailable() <= 0:
                return
            cmd = bytes(conn.readAll()).decode(errors="replace").strip()
            self._conns.discard(conn)
            conn.disconnectFromServer()
            parts = cmd.split(":")
            mode = parts[0] or "region"
            delay = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            _log("ipc: %r -> mode=%s delay=%s" % (cmd, mode, delay))
            self.start(mode, delay)

        conn.readyRead.connect(read)
        # The payload is small and may already have arrived before newConnection
        # was delivered, in which case readyRead will never fire for it.
        if conn.bytesAvailable() > 0:
            read()

    # -- capture flow ------------------------------------------------------
    def start(self, mode="region", delay=0):
        _log("start(mode=%s, delay=%s)" % (mode, delay))
        if self.overlay is not None:
            _log("ignored: a selection is already open")
            return                               # a selection is already open
        self.pending_mode = mode
        if delay:
            self.tray.showMessage("Snipt", "Capturing in %d s…" % delay,
                                  QIcon.fromTheme(ICON), delay * 1000)
            QTimer.singleShot(delay * 1000, self.capture.grab)
        else:
            self.capture.grab()

    def _on_captured(self, image: QImage):
        _log("captured %dx%d mode=%s" % (image.width(), image.height(), self.pending_mode))
        if self.pending_mode == "full":
            self._open_editor(image)
            return
        self.overlay = RegionOverlay(image, self._on_region, self._on_cancel)
        self.overlay.show()

    def _on_region(self, cropped: QImage):
        self.overlay = None
        if cropped.isNull() or cropped.width() < 2:
            return
        self._open_editor(cropped)

    def _on_cancel(self):
        self.overlay = None

    def _open_editor(self, image: QImage):
        w = EditorWindow(image, self.save_dir, auto_copy=True)
        w.closed.connect(lambda: self._forget(w))
        self.windows.append(w)
        w.show()
        w.raise_()
        w.activateWindow()
        # Give the compositor a moment to hand us focus before claiming the
        # clipboard; an unfocused Wayland client cannot set the selection.
        QTimer.singleShot(250, w.copy_when_ready)

    def _forget(self, w):
        if w in self.windows:
            self.windows.remove(w)

    def _on_failed(self, msg):
        _log("capture failed: %s" % msg)
        self.overlay = None
        if msg != "cancelled":
            self.tray.showMessage("Snipt", "Capture failed: %s" % msg,
                                  QSystemTrayIcon.MessageIcon.Warning, 5000)


def send_to_running(payload, attempts=2) -> bool:
    """Hand the command to an already-running Snipt.  True if it was taken."""
    for _ in range(attempts):
        sock = QLocalSocket()
        sock.connectToServer(SOCKET)
        if not sock.waitForConnected(400):
            continue
        sock.write(payload.encode())
        if sock.waitForBytesWritten(800):
            sock.flush()
            sock.waitForDisconnected(300)
            return True
    return False


def main():
    ap = argparse.ArgumentParser(prog="snipt", description="Snip and annotate.")
    ap.add_argument("mode", nargs="?", default="region", choices=["region", "full"],
                    help="region (default) or full")
    ap.add_argument("-d", "--delay", type=int, default=0, metavar="SEC",
                    help="wait SEC seconds before capturing")
    ap.add_argument("--daemon", action="store_true",
                    help="start resident in the tray without capturing")
    args = ap.parse_args()

    payload = "%s:%d" % (args.mode, args.delay)
    if not args.daemon and send_to_running(payload):
        return 0                                  # handed off, nothing to do

    app = Snipt(sys.argv)
    if not app.owns_socket:
        # Lost a startup race: another instance came up first.  Hand the
        # command over rather than lingering as a second tray icon.
        send_to_running(payload)
        return 0
    if not args.daemon:
        QTimer.singleShot(120, lambda: app.start(args.mode, args.delay))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
