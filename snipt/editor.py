"""Annotation editor.

Annotations are kept as vector items over the untouched base image, so every
mark stays undoable and the pixels underneath are never destroyed until the
image is exported.  Coordinates are image (device) pixels throughout; the view
applies a zoom factor only at paint time.
"""

import math
import os
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont, QGuiApplication,
                         QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QComboBox, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QScrollArea, QSizePolicy, QSpinBox, QToolBar, QWidget)

def _debug():
    return bool(os.environ.get("SNIPT_DEBUG"))


PALETTE = ["#e93a3a", "#f5a623", "#f8e71c", "#41c464", "#3daee9",
           "#9b51e0", "#ffffff", "#1a1a1a"]

PEN, HIGHLIGHT, ARROW, LINE, RECT, ELLIPSE, TEXT, STEP, BLUR, PIXELATE, CROP, MOVE = range(12)

TOOL_NAMES = {
    PEN: "Pen", HIGHLIGHT: "Highlighter", ARROW: "Arrow", LINE: "Line",
    RECT: "Rectangle", ELLIPSE: "Ellipse", TEXT: "Text", STEP: "Step number",
    BLUR: "Blur", PIXELATE: "Pixelate", CROP: "Crop", MOVE: "Select",
}


class Item:
    """One annotation.  `pts` holds image-space points."""

    def __init__(self, kind, color, width, pts=None, text="", step=0):
        self.kind = kind
        self.color = QColor(color)
        self.width = width
        self.pts = pts or []
        self.text = text
        self.step = step

    # -- geometry ----------------------------------------------------------
    def rect(self):
        if len(self.pts) < 2:
            return QRect()
        return QRect(self.pts[0], self.pts[-1]).normalized()

    def translate(self, dx, dy):
        self.pts = [QPoint(p.x() + dx, p.y() + dy) for p in self.pts]

    def bounds(self):
        if self.kind in (TEXT, STEP):
            p = self.pts[0]
            s = max(self.width * 6, 24)
            return QRect(p.x() - s, p.y() - s, s * 2, s * 2)
        r = QRect()
        for p in self.pts:
            r = r.united(QRect(p.x(), p.y(), 1, 1))
        pad = self.width + 4
        return r.adjusted(-pad, -pad, pad, pad)

    def hit(self, pt):
        return self.bounds().contains(pt)

    # -- painting ----------------------------------------------------------
    def draw(self, p, base):
        p.save()
        if self.kind == HIGHLIGHT:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            c = QColor(self.color)
            c.setAlpha(110)
            p.setPen(QPen(c, self.width * 4, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPolyline(QPolygonF([QPointF(x) for x in self.pts]))
        elif self.kind == PEN:
            p.setPen(QPen(self.color, self.width, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPolyline(QPolygonF([QPointF(x) for x in self.pts]))
        elif self.kind == LINE and len(self.pts) >= 2:
            p.setPen(QPen(self.color, self.width, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(self.pts[0], self.pts[-1])
        elif self.kind == ARROW and len(self.pts) >= 2:
            self._arrow(p)
        elif self.kind == RECT:
            p.setPen(QPen(self.color, self.width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.rect())
        elif self.kind == ELLIPSE:
            p.setPen(QPen(self.color, self.width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(self.rect())
        elif self.kind in (BLUR, PIXELATE):
            self._obscure(p, base)
        elif self.kind == TEXT and self.text:
            f = QFont()
            f.setPixelSize(max(self.width * 6, 14))
            f.setBold(True)
            p.setFont(f)
            p.setPen(self.color)
            p.drawText(self.pts[0], self.text)
        elif self.kind == STEP:
            self._step(p)
        p.restore()

    def _arrow(self, p):
        a, b = QPointF(self.pts[0]), QPointF(self.pts[-1])
        p.setPen(QPen(self.color, self.width, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(QBrush(self.color))
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = max(self.width * 4.0, 12.0)
        if math.hypot(b.x() - a.x(), b.y() - a.y()) < head:
            head = max(math.hypot(b.x() - a.x(), b.y() - a.y()) * 0.8, 2.0)
        # Stop the shaft short so it does not poke through the head.
        tip = QPointF(b.x() - math.cos(ang) * head * 0.55,
                      b.y() - math.sin(ang) * head * 0.55)
        p.drawLine(a, tip)
        wing = math.radians(26)
        p.drawPolygon(QPolygonF([
            b,
            QPointF(b.x() - head * math.cos(ang - wing), b.y() - head * math.sin(ang - wing)),
            QPointF(b.x() - head * math.cos(ang + wing), b.y() - head * math.sin(ang + wing)),
        ]))

    def _obscure(self, p, base):
        r = self.rect().intersected(base.rect())
        if r.width() < 2 or r.height() < 2:
            return
        patch = base.copy(r)
        # Strength scales with the region: a fixed factor that redacts a small
        # box leaves large-font text legible in a big one.  These are sized so
        # that text is destroyed, not merely softened -- this is used to hide
        # passwords and tokens, so erring coarse is the safe direction.
        if self.kind == PIXELATE:
            block = max(12, int(min(r.width(), r.height()) / 6))
            small = patch.scaled(max(1, r.width() // block), max(1, r.height() // block),
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            patch = small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.FastTransformation)
        else:
            f = max(12, int(min(r.width(), r.height()) / 8))
            small = patch.scaled(max(1, r.width() // f), max(1, r.height() // f),
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            patch = small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        p.drawImage(r.topLeft(), patch)

    def _step(self, p):
        rad = max(self.width * 5, 16)
        c = self.pts[0]
        p.setBrush(QBrush(self.color))
        p.setPen(QPen(QColor("#ffffff"), max(2, self.width // 2)))
        p.drawEllipse(c, rad, rad)
        f = QFont()
        f.setPixelSize(int(rad * 1.2))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff") if self.color.lightness() < 160 else QColor("#1a1a1a"))
        p.drawText(QRect(c.x() - rad, c.y() - rad, rad * 2, rad * 2),
                   Qt.AlignmentFlag.AlignCenter, str(self.step))


class Canvas(QWidget):
    changed = pyqtSignal()

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.base = image
        self.items = []
        self.redo_stack = []
        self.tool = PEN
        self.color = QColor(PALETTE[0])
        self.width = 4
        self.zoom = 1.0
        self.draft = None
        self.crop_rect = None
        self.drag_item = None
        self.drag_from = None
        self.step_no = 1
        self.editor = None
        self._composite = None
        self.changed.connect(self._invalidate)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fit_pending = True

    # -- sizing ------------------------------------------------------------
    def sizeHint(self):
        return QSize(int(self.base.width() * self.zoom),
                     int(self.base.height() * self.zoom))

    def set_zoom(self, z):
        self.zoom = max(0.1, min(8.0, z))
        self.updateGeometry()
        self.resize(self.sizeHint())
        self.update()

    def fit_to(self, avail: QSize):
        if self.base.width() and self.base.height():
            z = min(avail.width() / self.base.width(),
                    avail.height() / self.base.height(), 1.0)
            self.set_zoom(z)

    def to_image(self, pt):
        return QPoint(int(pt.x() / self.zoom), int(pt.y() / self.zoom))

    # -- history -----------------------------------------------------------
    def push(self, item):
        self.items.append(item)
        self.redo_stack.clear()
        self.changed.emit()

    def undo(self):
        if self.items:
            self.redo_stack.append(self.items.pop())
            self.changed.emit()
            self.update()

    def redo(self):
        if self.redo_stack:
            self.items.append(self.redo_stack.pop())
            self.changed.emit()
            self.update()

    def clear(self):
        if self.items:
            self.items = []
            self.redo_stack.clear()
            self.changed.emit()
            self.update()

    # -- input -------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pt = self.to_image(e.position().toPoint())
        if self.tool == MOVE:
            for it in reversed(self.items):
                if it.hit(pt):
                    self.drag_item, self.drag_from = it, pt
                    return
            return
        if self.tool == TEXT:
            self.editor.start_text(pt)
            return
        if self.tool == STEP:
            self.push(Item(STEP, self.color, self.width, [pt], step=self.step_no))
            self.step_no += 1
            self.update()
            return
        if self.tool == CROP:
            self.crop_rect = QRect(pt, pt)
            return
        self.draft = Item(self.tool, self.color, self.width, [pt, pt])

    def mouseMoveEvent(self, e):
        pt = self.to_image(e.position().toPoint())
        if self.drag_item is not None:
            self.drag_item.translate(pt.x() - self.drag_from.x(), pt.y() - self.drag_from.y())
            self.drag_from = pt
            self.update()
        elif self.crop_rect is not None:
            self.crop_rect = QRect(self.crop_rect.topLeft(), pt).normalized()
            self.update()
        elif self.draft is not None:
            if self.draft.kind in (PEN, HIGHLIGHT):
                self.draft.pts.append(pt)
            else:
                self.draft.pts[-1] = pt
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag_item is not None:
            self.drag_item = None
            self.changed.emit()
        elif self.draft is not None:
            d, self.draft = self.draft, None
            if d.kind in (PEN, HIGHLIGHT) or d.rect().width() > 2 or d.rect().height() > 2:
                self.push(d)
            self.update()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15))
            e.accept()
        else:
            e.ignore()

    def apply_crop(self):
        if not self.crop_rect or self.crop_rect.width() < 4:
            return
        r = self.crop_rect.intersected(self.base.rect())
        self.base = self.base.copy(r)
        for it in self.items:
            it.translate(-r.x(), -r.y())
        self.crop_rect = None
        self.changed.emit()
        self.set_zoom(self.zoom)

    # -- render ------------------------------------------------------------
    def _invalidate(self):
        self._composite = None

    def render_image(self):
        """Flatten annotations onto the base image.

        Blur and pixelate must obscure whatever is already beneath them --
        including earlier annotations -- so they sample the partially composited
        result rather than the pristine base.  QPainter holds an exclusive lock
        on its target, so the painter is closed to take each snapshot.
        """
        out = QImage(self.base)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for it in self.items:
            if it.kind in (BLUR, PIXELATE):
                p.end()
                snapshot = out.copy()      # deep copy; out is about to change
                p = QPainter(out)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                it.draw(p, snapshot)
            else:
                it.draw(p, self.base)
        p.end()
        return out

    def composite(self):
        if self._composite is None:
            self._composite = self.render_image()
        return self._composite

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.scale(self.zoom, self.zoom)
        composed = self.composite()
        p.drawImage(0, 0, composed)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.draft is not None:
            # The in-progress mark samples the composite, so a blur being
            # dragged previews exactly what it will look like once committed.
            self.draft.draw(p, composed)
        if self.crop_rect is not None:
            p.setPen(QPen(QColor("#3daee9"), max(1, int(1 / self.zoom)), Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.crop_rect)


class EditorWindow(QMainWindow):
    """Toolbar + canvas.  Closing it does not quit the resident daemon."""

    closed = pyqtSignal()

    def __init__(self, image, save_dir, auto_copy=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snipt")
        self.setWindowIcon(QIcon.fromTheme("applets-screenshooter"))
        self.save_dir = Path(save_dir)
        self.auto_copy = auto_copy
        self.saved_path = None
        self.text_edit = None
        self._copied = False

        self.canvas = Canvas(image)
        self.canvas.editor = self
        self.canvas.changed.connect(self._on_changed)

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(self.scroll)

        self._build_toolbar()
        self.status = self.statusBar()
        self._size_to_image(image)
        # The auto-copy deliberately does NOT happen here.  On Wayland only a
        # focused client may set the selection, so copying before the window is
        # shown silently leaves the clipboard empty.  main.py calls
        # copy_when_ready() once the window is up and activated.

    # -- construction ------------------------------------------------------
    def _size_to_image(self, image):
        scr = QGuiApplication.primaryScreen().availableGeometry()
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        w = min(int(image.width() / dpr) + 60, scr.width() - 40)
        h = min(int(image.height() / dpr) + 150, scr.height() - 40)
        self.resize(max(w, 560), max(h, 420))
        self.canvas.fit_to(QSize(w - 40, h - 130))

    def _build_toolbar(self):
        # Two rows, not one: on a narrow/portrait screen a single toolbar
        # pushes Copy and Save into an overflow chevron, which is exactly the
        # wrong thing to hide in a snipping tool.
        tb = QToolBar("Tools")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        group = QActionGroup(self)
        group.setExclusive(True)
        tools = [(MOVE, "transform-move", "V"), (PEN, "draw-freehand", "P"),
                 (HIGHLIGHT, "draw-highlight", "H"), (ARROW, "draw-arrow", "A"),
                 (LINE, "draw-line", "L"), (RECT, "draw-rectangle", "R"),
                 (ELLIPSE, "draw-ellipse", "E"), (TEXT, "draw-text", "T"),
                 (STEP, "chronometer", "N"), (BLUR, "blurfx", "B"),
                 (PIXELATE, "pixelate", "X"), (CROP, "transform-crop", "C")]
        for kind, icon, key in tools:
            act = QAction(QIcon.fromTheme(icon), "%s (%s)" % (TOOL_NAMES[kind], key), self)
            act.setCheckable(True)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(lambda _, k=kind: self.set_tool(k))
            group.addAction(act)
            tb.addAction(act)
            if kind == PEN:
                act.setChecked(True)
        tb.addSeparator()

        self.swatches = QWidget()
        lay = QHBoxLayout(self.swatches)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(3)
        for hexcol in PALETTE:
            b = QLabel()
            b.setFixedSize(18, 18)
            b.setStyleSheet("background:%s;border:1px solid #888;border-radius:3px" % hexcol)
            b.mousePressEvent = lambda _, c=hexcol: self.set_color(QColor(c))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(b)
        tb.addWidget(self.swatches)

        pick = QAction(QIcon.fromTheme("color-picker"), "More colours…", self)
        pick.triggered.connect(self.pick_color)
        tb.addAction(pick)

        self.width_box = QSpinBox()
        self.width_box.setRange(1, 60)
        self.width_box.setValue(self.canvas.width)
        self.width_box.setPrefix("size ")
        self.width_box.valueChanged.connect(lambda v: setattr(self.canvas, "width", v))
        tb.addWidget(self.width_box)

        self.addToolBarBreak()
        tb2 = QToolBar("Actions")
        tb2.setMovable(False)
        tb2.setIconSize(QSize(18, 18))
        tb2.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb2)

        for text, icon, seq, slot in [
            ("Undo", "edit-undo", QKeySequence.StandardKey.Undo, self.canvas.undo),
            ("Redo", "edit-redo", QKeySequence.StandardKey.Redo, self.canvas.redo),
            ("Copy", "edit-copy", QKeySequence.StandardKey.Copy, self.copy),
            ("Save", "document-save", QKeySequence.StandardKey.Save, self.save),
            ("Save As…", "document-save-as", QKeySequence("Ctrl+Shift+S"), self.save_as),
        ]:
            act = QAction(QIcon.fromTheme(icon), text, self)
            act.setShortcut(seq)
            act.triggered.connect(slot)
            tb2.addAction(act)

        for seq, slot in [("Return", self.canvas.apply_crop),
                          ("Ctrl+Shift+Z", self.canvas.redo),
                          ("Ctrl+0", lambda: self.canvas.set_zoom(1.0)),
                          ("Ctrl++", lambda: self.canvas.set_zoom(self.canvas.zoom * 1.25)),
                          ("Ctrl+-", lambda: self.canvas.set_zoom(self.canvas.zoom / 1.25)),
                          ("Escape", self.close)]:
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(slot)
            self.addAction(a)

    # -- tool state --------------------------------------------------------
    def set_tool(self, kind):
        self.canvas.tool = kind
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor if kind == MOVE
                              else Qt.CursorShape.CrossCursor)
        self.statusBar().showMessage(
            "Crop: drag a box, then press Enter" if kind == CROP else TOOL_NAMES[kind], 4000)

    def set_color(self, color):
        self.canvas.color = color

    def pick_color(self):
        c = QColorDialog.getColor(self.canvas.color, self, "Annotation colour")
        if c.isValid():
            self.set_color(c)

    # -- inline text -------------------------------------------------------
    def start_text(self, image_pt):
        if self.text_edit is not None:
            self.commit_text()
        self.text_edit = QLineEdit(self.canvas)
        self._text_pt = image_pt
        px = max(self.canvas.width * 6, 14) * self.canvas.zoom
        self.text_edit.setStyleSheet(
            "background:rgba(0,0,0,150);color:%s;border:1px solid %s;font-size:%dpx"
            % (self.canvas.color.name(), self.canvas.color.name(), max(10, int(px))))
        self.text_edit.move(int(image_pt.x() * self.canvas.zoom),
                            int(image_pt.y() * self.canvas.zoom) - int(px))
        self.text_edit.resize(240, int(px * 1.6))
        self.text_edit.returnPressed.connect(self.commit_text)
        self.text_edit.show()
        self.text_edit.setFocus()

    def commit_text(self):
        if self.text_edit is None:
            return
        txt = self.text_edit.text()
        self.text_edit.deleteLater()
        self.text_edit = None
        if txt.strip():
            self.canvas.push(Item(TEXT, self.canvas.color, self.canvas.width,
                                  [self._text_pt], text=txt))
            self.canvas.update()

    # -- output ------------------------------------------------------------
    def copy_when_ready(self):
        """Auto-copy, but only once we actually hold focus.

        A Wayland client may only set the selection while focused.  A window
        opened by the resident daemon carries no xdg-activation token, so it
        can come up unfocused; in that case we defer to changeEvent() and copy
        the moment activation arrives.
        """
        if not self.auto_copy or self._copied:
            return
        if not self.isActiveWindow():
            if _debug():
                print("snipt: not active yet, deferring auto-copy", file=sys.stderr)
            return
        self._copied = True
        self.copy(quiet=True)
        self.statusBar().showMessage("Copied to clipboard — paste anywhere", 4000)
        if _debug():
            print("snipt: auto-copied while active", file=sys.stderr)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange:
            self.copy_when_ready()

    def _on_changed(self):
        if self.auto_copy:
            self.copy(quiet=True)

    def copy(self, quiet=False):
        self.commit_text()
        QApplication.clipboard().setImage(self.canvas.render_image())
        if not quiet and self.statusBar():
            self.statusBar().showMessage("Copied to clipboard", 3000)

    def _default_path(self):
        self.save_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.save_dir / ("Snip_%s.png" % stamp)

    def save(self):
        self.commit_text()
        path = self.saved_path or self._default_path()
        if self.canvas.render_image().save(str(path)):
            self.saved_path = path
            self.statusBar().showMessage("Saved to %s" % path, 6000)
            self.setWindowTitle("Snipt — %s" % path.name)
        else:
            QMessageBox.warning(self, "Snipt", "Could not write %s" % path)

    def save_as(self):
        self.commit_text()
        start = str(self.saved_path or self._default_path())
        path, _ = QFileDialog.getSaveFileName(self, "Save snip", start,
                                              "PNG image (*.png);;JPEG image (*.jpg)")
        if path:
            if self.canvas.render_image().save(path):
                self.saved_path = Path(path)
                self.statusBar().showMessage("Saved to %s" % path, 6000)
                self.setWindowTitle("Snipt — %s" % Path(path).name)
            else:
                QMessageBox.warning(self, "Snipt", "Could not write %s" % path)

    def closeEvent(self, e):
        super().closeEvent(e)
        self.closed.emit()
