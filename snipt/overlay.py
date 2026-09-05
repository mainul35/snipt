"""Frozen-screen region selector.

The workspace grab arrives in *device* pixels (1200x1920 on this panel) while
Qt lays the overlay out in *logical* pixels (800x1280 at a 1.5 scale factor).
Every rectangle here is kept in logical virtual-desktop coordinates and only
converted to device pixels once, in `_crop`, so a selection lands on exactly
the pixels the user saw under the crosshair.

One borderless fullscreen window is created per QScreen.  They share a single
selection so a drag can cross monitors; on Wayland a client cannot position
itself freely, but it *can* go fullscreen on a named screen, which is enough.
"""

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QImage,
                         QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import QApplication, QWidget

DIM = QColor(0, 0, 0, 110)
ACCENT = QColor(61, 174, 233)          # Plasma "Breeze" blue
HANDLE = QColor(255, 255, 255)
LOUPE = 130                            # loupe widget size, logical px
LOUPE_ZOOM = 8                         # source px per loupe px
MIN_DRAG = 4                           # below this, treat as a click


class _Pane(QWidget):
    """One fullscreen window on one screen, drawing the shared selection."""

    def __init__(self, controller, screen, shot):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                               Qt.WindowType.WindowStaysOnTopHint)
        self.ctl = controller
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.origin = screen.geometry().topLeft()   # logical, virtual desktop
        self.shot = shot                            # QPixmap, dpr-tagged
        self.cursor_pos = None

    # -- coordinate helpers ------------------------------------------------
    def to_virtual(self, p):
        return p + self.origin

    def to_local(self, r):
        return r.translated(-self.origin)

    # -- events ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.ctl.cancel()
            return
        self.ctl.begin(self.to_virtual(e.position().toPoint()))

    def mouseMoveEvent(self, e):
        self.cursor_pos = e.position().toPoint()
        self.ctl.update_drag(self.to_virtual(self.cursor_pos))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.ctl.commit(self.to_virtual(e.position().toPoint()))

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Escape:
            self.ctl.cancel()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.ctl.commit(None)
        elif k == Qt.Key.Key_A and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.ctl.select_all()

    def enterEvent(self, e):
        self.cursor_pos = self.mapFromGlobal(QCursor.pos())
        self.update()

    def leaveEvent(self, e):
        self.cursor_pos = None
        self.update()

    # -- painting ----------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.shot)

        sel = self.ctl.selection
        local = self.to_local(sel) if sel else None

        # Dim everything except the current selection.
        if local and local.isValid():
            for band in (QRect(0, 0, self.width(), local.top()),
                         QRect(0, local.bottom() + 1, self.width(),
                               self.height() - local.bottom() - 1),
                         QRect(0, local.top(), local.left(), local.height()),
                         QRect(local.right() + 1, local.top(),
                               self.width() - local.right() - 1, local.height())):
                if band.isValid():
                    p.fillRect(band, DIM)
            p.setPen(QPen(ACCENT, 1))
            p.drawRect(local.adjusted(0, 0, -1, -1))
            self._draw_handles(p, local)
            self._draw_size_label(p, local, sel)
        else:
            p.fillRect(self.rect(), DIM)
            if self.cursor_pos is not None:
                p.setPen(QPen(QColor(255, 255, 255, 90), 1))
                p.drawLine(0, self.cursor_pos.y(), self.width(), self.cursor_pos.y())
                p.drawLine(self.cursor_pos.x(), 0, self.cursor_pos.x(), self.height())
            self._draw_hint(p)

        if self.cursor_pos is not None:
            self._draw_loupe(p, self.cursor_pos)

    def _draw_handles(self, p, r):
        p.setBrush(HANDLE)
        p.setPen(QPen(ACCENT, 1))
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight(),
                   QPoint(r.center().x(), r.top()), QPoint(r.center().x(), r.bottom()),
                   QPoint(r.left(), r.center().y()), QPoint(r.right(), r.center().y())):
            p.drawRect(QRect(pt.x() - 3, pt.y() - 3, 6, 6))
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_size_label(self, p, local, sel):
        dpr = self.ctl.scale
        text = "%d x %d" % (round(sel.width() * dpr), round(sel.height() * dpr))
        self._chip(p, text, QPoint(local.left(), local.top() - 26))

    def _draw_hint(self, p):
        self._chip(p, "Drag to snip  ·  Click = whole screen  ·  Esc cancels",
                   QPoint(self.width() // 2 - 150, 24))

    def _chip(self, p, text, at):
        f = QFont()
        f.setPointSizeF(9.5)
        p.setFont(f)
        rect = p.fontMetrics().boundingRect(text).adjusted(-8, -5, 8, 5)
        rect.moveTopLeft(at)
        rect = rect.intersected(self.rect().adjusted(4, 4, -4, -4)) if not \
            self.rect().contains(rect) else rect
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(QColor(255, 255, 255))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_loupe(self, p, at):
        """Pixel loupe so the user can land on an exact device pixel."""
        dpr = self.ctl.scale
        src_px = LOUPE / LOUPE_ZOOM                      # logical px sampled
        src = QRect(0, 0, round(src_px * dpr), round(src_px * dpr))
        src.moveCenter(QPoint(round((at.x() + self.origin.x()) * dpr),
                              round((at.y() + self.origin.y()) * dpr)))

        box = QRect(0, 0, LOUPE, LOUPE)
        off = 20
        box.moveTopLeft(at + QPoint(off, off))
        if box.right() > self.width() - 8:
            box.moveLeft(at.x() - off - LOUPE)
        if box.bottom() > self.height() - 8:
            box.moveTop(at.y() - off - LOUPE)

        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.fillRect(box, QColor(0, 0, 0))
        p.drawImage(box, self.ctl.image, src)
        p.setPen(QPen(ACCENT, 1))
        p.drawRect(box)
        c = box.center()
        p.setPen(QPen(QColor(255, 0, 0, 160), 1))
        p.drawLine(box.left(), c.y(), box.right(), c.y())
        p.drawLine(c.x(), box.top(), c.x(), box.bottom())

        gx = round((at.x() + self.origin.x()) * dpr)
        gy = round((at.y() + self.origin.y()) * dpr)
        col = self.ctl.image.pixelColor(
            min(max(gx, 0), self.ctl.image.width() - 1),
            min(max(gy, 0), self.ctl.image.height() - 1))
        self._chip(p, "%d,%d  %s" % (gx, gy, col.name()),
                   QPoint(box.left(), box.bottom() + 6))


class RegionOverlay:
    """Controller owning the shared selection across all panes."""

    def __init__(self, image, on_done, on_cancel):
        self.image = image                    # QImage, device pixels
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.selection = None
        self.anchor = None
        self.panes = []

        screens = QGuiApplication.screens()
        virt = QRect()
        for s in screens:
            virt = virt.united(s.geometry())
        self.virtual = virt
        # Uniform scale factor: device pixels per logical pixel.
        self.scale = image.width() / virt.width() if virt.width() else 1.0

        for s in screens:
            g = s.geometry()
            sub = QRect(round(g.x() * self.scale), round(g.y() * self.scale),
                        round(g.width() * self.scale), round(g.height() * self.scale))
            pm = QPixmap.fromImage(image.copy(sub))
            pm.setDevicePixelRatio(self.scale)
            self.panes.append(_Pane(self, s, pm))

    def show(self):
        for pane in self.panes:
            pane.showFullScreen()
        if self.panes:
            self.panes[0].activateWindow()
            self.panes[0].raise_()
            self.panes[0].setFocus()

    # -- selection ---------------------------------------------------------
    def begin(self, virtual_pt):
        self.anchor = virtual_pt
        self.selection = QRect(virtual_pt, virtual_pt)
        self._repaint()

    def update_drag(self, virtual_pt):
        if self.anchor is not None:
            self.selection = QRect(self.anchor, virtual_pt).normalized()
        self._repaint()

    def select_all(self):
        self.anchor = self.virtual.topLeft()
        self.selection = QRect(self.virtual)
        self._repaint()

    def commit(self, virtual_pt):
        if virtual_pt is not None and self.anchor is not None:
            self.selection = QRect(self.anchor, virtual_pt).normalized()
        sel = self.selection
        # A click with no meaningful drag means "the whole screen".
        if sel is None or sel.width() < MIN_DRAG or sel.height() < MIN_DRAG:
            sel = self.virtual
        crop = self._crop(sel)
        self.close()
        self.on_done(crop)

    def cancel(self):
        self.close()
        self.on_cancel()

    def close(self):
        for pane in self.panes:
            pane.close()
        self.panes = []

    def _crop(self, sel):
        sel = sel.intersected(self.virtual)
        dev = QRect(round(sel.x() * self.scale), round(sel.y() * self.scale),
                    round(sel.width() * self.scale), round(sel.height() * self.scale))
        dev = dev.intersected(self.image.rect())
        return self.image.copy(dev)

    def _repaint(self):
        for pane in self.panes:
            pane.update()
