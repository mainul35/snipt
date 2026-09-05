"""Screen capture backends.

On a Plasma 6 Wayland session a normal client cannot read the framebuffer.
KWin's fast org.kde.KWin.ScreenShot2 DBus API is allowlisted to the process
names "spectacle"/"org.kde.spectacle", so third-party tools are refused with
NoAuthorized.  That leaves two usable paths, measured on this machine:

    xdg-desktop-portal Screenshot   ~1.0s warm   (primary)
    spectacle -b -n -f -o FILE      ~2.1s warm   (fallback)

The portal hands back a *file URI* rather than pixels, and portal-kde writes
that file into ~/Pictures.  We load it and unlink it so the tool does not
litter the user's picture library with intermediate grabs.
"""

import os
import sys
import time
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
from PyQt6.QtGui import QImage

def _log(msg):
    """Trace to stderr when SNIPT_DEBUG is set; silent otherwise."""
    if os.environ.get("SNIPT_DEBUG"):
        print("snipt: %s" % msg, file=sys.stderr, flush=True)


PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"


def _token_path(bus, token):
    """The Request object path the portal will use for our handle_token."""
    sender = bus.baseService()[1:].replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


class Capture(QObject):
    """Grabs the whole workspace, asynchronously, without blocking the UI."""

    captured = pyqtSignal(QImage)
    failed = pyqtSignal(str)

    def __init__(self, cleanup_portal_file=True, parent=None):
        super().__init__(parent)
        self.cleanup_portal_file = cleanup_portal_file
        self._bus = QDBusConnection.sessionBus()
        self._busy = False
        self._proc = None

    def prewarm(self):
        """Start xdg-desktop-portal early.

        The first portal call of a session costs ~8s because the portal and its
        KDE backend have to launch.  Reading a cheap property pays that once, at
        daemon start, instead of on the user's first snip.
        """
        iface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH,
                               "org.freedesktop.DBus.Properties", self._bus)
        iface.setTimeout(15000)
        self._prewarm_call = iface.asyncCall("Get", PORTAL_IFACE, "version")

    def grab(self):
        if self._busy:
            _log("grab ignored: already capturing")
            return
        self._busy = True
        if not self._grab_portal():
            _log("portal path unavailable, falling back to spectacle")
            self._grab_spectacle()

    # ---------------------------------------------------------------- portal

    def _grab_portal(self):
        token = "snipt_%d" % int(time.time() * 1000)
        path = _token_path(self._bus, token)
        ok = self._bus.connect(PORTAL_SERVICE, path, REQUEST_IFACE,
                               "Response", self._on_portal_response)
        if not ok:
            return False
        self._request_path = path

        iface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, PORTAL_IFACE, self._bus)
        if not iface.isValid():
            return False
        iface.setTimeout(30000)
        # interactive=False: grab immediately, we do our own region selection.
        reply = iface.call("Screenshot", "", {"interactive": False,
                                              "handle_token": token})
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            self._bus.disconnect(PORTAL_SERVICE, path, REQUEST_IFACE,
                                 "Response", self._on_portal_response)
            return False
        return True

    @pyqtSlot("QDBusMessage")
    def _on_portal_response(self, message):
        self._bus.disconnect(PORTAL_SERVICE, self._request_path, REQUEST_IFACE,
                             "Response", self._on_portal_response)
        args = message.arguments()
        code = int(args[0]) if args else 2
        results = args[1] if len(args) > 1 else {}
        if code != 0:                 # 1 = user cancelled, 2 = other error
            self._busy = False
            self.failed.emit("cancelled" if code == 1 else "portal error")
            return
        uri = results.get("uri") if hasattr(results, "get") else None
        if not uri:
            self._grab_spectacle()
            return
        _log("portal returned %s" % uri)
        path = unquote(urlparse(str(uri)).path)
        self._finish(path, unlink=self.cleanup_portal_file)

    # ------------------------------------------------------------- spectacle

    def _grab_spectacle(self):
        path = os.path.join("/tmp", "snipt-%d.png" % os.getpid())
        self._proc = QProcess(self)
        self._proc.finished.connect(
            lambda *_: self._finish(path, unlink=True))
        self._proc.errorOccurred.connect(
            lambda *_: self._fail("no capture backend available"))
        self._proc.start("spectacle", ["-b", "-n", "-f", "-o", path])

    # ---------------------------------------------------------------- common

    def _finish(self, path, unlink):
        img = QImage(path)
        if unlink:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._busy = False
        if img.isNull():
            self.failed.emit("capture produced no image")
        else:
            self.captured.emit(img)

    def _fail(self, msg):
        self._busy = False
        self.failed.emit(msg)
