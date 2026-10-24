"""Paper-style desktop controls, inspired by yovoice's workbench layout."""
from __future__ import annotations

import array
import os
import re
import shutil
import sys
import time
import wave
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import (QByteArray, QCoreApplication, QMimeData, QObject, QPoint, QPointF, QRect, QSize, Qt, QUrl,
                           QEvent, pyqtSignal)
from PyQt6.QtGui import (QColor, QDrag, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QTextBlockFormat,
                         QTextCursor, QWheelEvent)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox,
                            QDoubleSpinBox, QFrame, QLabel, QPushButton, QSizePolicy, QSlider, QSpinBox,
                            QStyledItemDelegate, QStyle, QStyleOptionViewItem, QTableWidget, QTextEdit, QWidget)


INK = "#18181a"
ORANGE = "#e0651c"


def sorted_audio(records, order="newest"):
    return sorted(records, key=lambda item: (item.get("created") or 0, item.get("idx") or 0,
                                             str(item.get("id", ""))), reverse=order == "newest")


def audio_sort_combo():
    control = NoWheelComboBox()
    control.addItem("最新生成在前", "newest")
    control.addItem("最早生成在前", "oldest")
    control.setAccessibleName("生成音频排序")
    control.setToolTip("按生成时间排序；相同时间按片段顺序排列。")
    control.setFixedWidth(142)
    return control


class ModelDescriptionDelegate(QStyledItemDelegate):
    """Two readable text levels without adding widgets to every model row."""
    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(text, dict):
            return super().paint(painter, option, index)
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget)
        painter.save()
        painter.setClipRect(option.rect)
        font = QFont(option.font)
        for key, size, weight, color, top, height in (
            ("title", 14, QFont.Weight.Medium, INK, 10, 23),
            ("description", 12, QFont.Weight.Normal, "#77777c", 34, 20),
        ):
            font.setPixelSize(size)
            font.setWeight(weight)
            painter.setFont(font)
            painter.setPen(QColor(color))
            area = QRect(option.rect.x() + 10, option.rect.y() + top, max(0, option.rect.width() - 20), height)
            line = QFontMetrics(font).elidedText(text[key], Qt.TextElideMode.ElideRight, area.width())
            painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
        painter.restore()


def application_icon():
    return QIcon(str(Path(__file__).parent / "assets" / "app.svg"))


ICONS = {
    "audio": '<path d="M3 10v4m4-8v12m5-15v18m5-15v12m4-8v4"/>',
    "pen": '<path d="m15 4 5 5M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z"/>',
    "article": '<path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h6"/>',
    "dialogue": '<path d="M3 4h18v13H9l-6 4zM7 8h10M7 12h7"/>',
    "recognize": '<path d="M3 8v8m4-12v16m4-13v10m4-11h6m-6 6h6m-6 6h6"/>',
    "back": '<path d="M19 12H5m6-6-6 6 6 6"/>',
    "voices": '<rect x="8" y="3" width="8" height="12" rx="4"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
    "link": '<path d="m10 13 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(2 -1)"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "import": '<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
    "export": '<path d="M12 15V3m-4 4 4-4 4 4M4 16v5h16v-5"/>',
    "play": '<path d="m8 5 11 7-11 7z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "previous": '<path d="M5 5v14m14-14L8 12l11 7z"/>',
    "next": '<path d="M19 5v14M5 5l11 7-11 7z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "retry": '<path d="M3 10a9 9 0 1 1 2 8M3 3v7h7"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
    "folder": '<path d="M3 7V4h6l2 3h10v13H3z"/>',
    "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="white"/><circle cx="15" cy="17" r="3" fill="white"/>',
    "chevron": '<path d="m9 5 7 7-7 7"/>',
    "chevron-down": '<path d="m5 9 7 7 7-7"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "float": '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 8h18"/><path d="m10 11 4 2.5-4 2.5z"/>',
}


@lru_cache(maxsize=256)
def icon(name, color=INK, size=20):
    """Rasterise an icon once: every row and every refresh asks for the same few.

    Rendering was measurable on its own: a task switch asked for the same play,
    retry and trash glyphs hundreds of times, and each call re-parsed the SVG and
    painted a fresh pixmap. QIcon is shared by value here, so the cache hands the
    same glyph to every button instead of rasterising it again.
    """
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    # Render at 2x for crisp icons on high-density displays.
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def button(text, callback, symbol=None, kind=None):
    control = QPushButton(text)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    if symbol:
        control.setIcon(icon(symbol, "#ffffff" if kind == "primary" else INK))
        control.setIconSize(QSize(18, 18))
    if kind:
        control.setProperty("kind", kind)
    control.clicked.connect(callback)
    return control


def label(text, role=None):
    control = QLabel(text)
    if role:
        control.setProperty("role", role)
    return control


def generation_elapsed(item):
    """Use one elapsed-time presentation for both audio lists."""
    if item.get("cached"):
        return "缓存", "复用已有音频，本次未进行推理。"
    if item.get("status") in ("queued", "running", "draft", "cancelled"):
        return "—", "生成完成后记录耗时，不包含排队时间。"
    seconds = item.get("generation_seconds")
    if seconds is None:
        return "未记录", "此记录没有生成耗时数据，无法从音频时长推算。"
    # Round before splitting to avoid displaying 1分 60.0秒 at a boundary.
    tenths = round(max(0, seconds) * 10)
    minutes, remainder = divmod(tenths, 600)
    text = f"{remainder / 10:.1f} 秒" if not minutes else f"{minutes}分 {remainder / 10:04.1f}秒"
    return text, f"生成耗时：{seconds:.3f} 秒\n从调用推理到音频校验完成，包含首次加载，不包含排队。重试显示本次尝试的耗时。"


class ElidedLabel(QLabel):
    """A single status line that keeps long errors from resizing its layout."""
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def text(self):
        return self.full_text

    def setText(self, text):
        self.full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._fit_text()

    def _fit_text(self):
        margins = self.contentsMargins()
        self.setFixedHeight(self.fontMetrics().height() + margins.top() + margins.bottom() + 2)
        super().setText(self.fontMetrics().elidedText(
            self.full_text.replace("\n", " "), Qt.TextElideMode.ElideRight, self.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "full_text") and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit_text()


class SpinWheelFilter(QObject):
    """Intercept only wheels, leaving Qt's native spin-box event filter intact."""
    def __init__(self, spin):
        super().__init__(spin)
        import weakref
        self.spin = weakref.ref(spin)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        from PyQt6 import sip
        spin = self.spin()
        if spin is None or sip.isdeleted(spin) or sip.isdeleted(watched):
            return False
        spin.wheelEvent(event)
        return True


class IgnoreWheel:
    """Leave wheel events to the containing page, even when the control has focus."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self, QAbstractSpinBox):
            self._wheel_filter = SpinWheelFilter(self)
            self.lineEdit().installEventFilter(self._wheel_filter)

    def wheelEvent(self, event):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                                        event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                        event.buttons(), event.modifiers(), event.phase(), event.inverted())
                QCoreApplication.sendEvent(viewport, forwarded)
                event.accept()
                return
            parent = parent.parentWidget()
        event.ignore()


class NoWheelSpinBox(IgnoreWheel, QSpinBox):
    pass


class NoWheelDoubleSpinBox(IgnoreWheel, QDoubleSpinBox):
    pass


class NoWheelComboBox(IgnoreWheel, QComboBox):
    pass


class NoWheelSlider(IgnoreWheel, QSlider):
    pass


class AnchoredPopover(QFrame):
    """An in-window list; the anchor click toggles it without Qt popup replay."""
    visibility_changed = pyqtSignal(bool)

    def __init__(self, owner, anchor):
        super().__init__(owner)
        self.owner, self.anchor = owner, anchor
        self.previous_focus = None
        self.held = False
        self.hide()

    def eventFilter(self, watched, event):
        if not self.isVisible():
            return False
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            position = event.globalPosition().toPoint()
            inside = self.rect().contains(self.mapFromGlobal(position))
            anchor = self.anchor.rect().contains(self.anchor.mapFromGlobal(position))
            if not inside and not anchor:
                self.hide()
        elif kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hide()
            return True
        elif watched is self.owner and kind == QEvent.Type.WindowDeactivate and not self.held:
            self.hide()
        return False

    def showEvent(self, event):
        self.previous_focus = QApplication.focusWidget()
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)
        self.raise_()
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)
        focus = QApplication.focusWidget()
        if focus and self.isAncestorOf(focus) and self.previous_focus and self.owner.isActiveWindow():
            self.previous_focus.setFocus()
        self.visibility_changed.emit(False)


class ScriptEditor(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_spacing()

    def setPlainText(self, text):
        super().setPlainText(text)
        self._line_spacing()

    def _line_spacing(self):
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        format_ = QTextBlockFormat()
        format_.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(format_)


class RecentJobDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        value = index.data(257) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rectangle = option.rect.adjusted(0, 2, -4, -2)
        if selected or hover:
            painter.setBrush(QColor("#ebebe9" if selected else "#ffffff"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rectangle, 6, 6)
        color = {"done": "#829581", "failed": "#c26344", "running": "#e0651c", "importing": "#e0651c", "queued": "#b7a178"}.get(value.get("status"), "#b5b5b3")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(rectangle.left() + 9, rectangle.center().y() - 2, 4, 4)
        painter.setPen(QColor("#282829" if selected else "#777778"))
        painter.setFont(option.font)
        from .recent_works import task_type, WORK_ICONS
        category = task_type(value)
        colors = {"dialogue": "#967248", "audio": "#5c8f8a", "recognition": "#6b719d", "float": "#c07a46"}
        own_page = category in ("article", "dialogue", "float")
        icon(WORK_ICONS.get(category, "article"), colors.get(category, "#85858a"), 16).paint(
            painter, rectangle.left() + 20, rectangle.center().y() - 8, 16, 16)
        text_rect = rectangle.adjusted(43, 0, -30 if own_page else -12, 0)
        text = option.fontMetrics.elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)
        if (selected or hover) and own_page:
            icon("trash", "#8d8d8b", 14).paint(painter, rectangle.right() - 23, rectangle.center().y() - 7, 14, 14)
        painter.restore()


def existing_audio_files(paths):
    """Keep unique, readable audio files in their original order."""
    result, seen = [], set()
    for path in paths or ():
        try:
            path = Path(path).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        result.append(path)
    return result


def audio_mime_data(paths):
    """A real file drop, so any program that takes a dropped file can take the clip."""
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return data


DRAG_NAME_CHARS = 60
DRAG_CACHE_DAYS = 30
DRAG_CACHE_ENV = "SHENGJIAN_DRAG_CACHE"
DRAG_CACHE_NAME = "drag-cache"
_ILLEGAL_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def audio_drag_name(text, when=None):
    """「生成的文本_生成时间」: the name another program shows once the clip lands there."""
    stem = " ".join(_ILLEGAL_NAME.sub(" ", text or "").split()).strip(" .")
    if len(stem) > DRAG_NAME_CHARS:
        stem = stem[:DRAG_NAME_CHARS].rstrip(" .")
    moment = datetime.fromtimestamp(time.time() if when is None else when)
    return (stem or "音频") + "_" + moment.strftime("%Y%m%d-%H%M%S")


def drag_cache_dir():
    """Where a dropped clip waits for the program that received it.

    Deliberately not %TEMP%: Windows and disk cleaners empty it, and a video
    editor stores the handed-over path in its project file — reopening that
    project then reports the media as missing.  `SHENGJIAN_DRAG_CACHE` relocates
    the folder (the test suite points it at a throwaway directory).
    """
    override = os.environ.get(DRAG_CACHE_ENV)
    if override:
        folder = Path(override)
    else:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        folder = local / "声笺" / DRAG_CACHE_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def drag_session_dir(folder=None):
    """One folder per drag, so a drop and the files it handed out stay together."""
    folder = folder or drag_cache_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate, serial = folder / stamp, 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate, serial = folder / f"{stamp}-{serial}", serial + 1
        except OSError:
            # No rights or no space left: a flat drop under the cache root still works.
            return folder


def prune_drag_cache(folder, days=DRAG_CACHE_DAYS):
    """Drop drag sessions handed out long ago; recent ones may still be open somewhere.

    Age is read from the session folder, never from the clip inside it: copies keep
    the audio's own mtime, so a clip generated last week would otherwise look
    stale the moment it was dropped and vanish on the very next drag.
    """
    deadline = time.time() - days * 86400
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime >= deadline:
                continue
        except OSError:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
            continue
        try:
            entry.unlink(missing_ok=True)
        except OSError:
            continue


def prepare_drag_files(files, texts=None):
    """Copies each clip under its text and generation time.

    Windows hands the target a path, so a readable name has to be a real file: the
    original stays untouched under its opaque id, the copy carries the name and
    lives in a per-drag folder that outlives the session.
    """
    files = existing_audio_files(files)
    if not files:
        return []
    folder = drag_cache_dir()
    prune_drag_cache(folder)
    session = drag_session_dir(folder)
    prepared = []
    for index, path in enumerate(files):
        try:
            when = path.stat().st_mtime
        except OSError:
            prepared.append(path)
            continue
        text = texts[index] if texts and index < len(texts) else ""
        target = session / (audio_drag_name(text or path.stem, when) + path.suffix.lower())
        try:
            shutil.copy2(path, target)
            prepared.append(target)
            continue
        except OSError:
            pass
        # A stale copy is written by the target program: land next to it instead.
        candidate, serial = target, 2
        while candidate.exists():
            candidate = target.with_name(f"{target.stem}-{serial}{target.suffix}")
            serial += 1
        try:
            shutil.copy2(path, candidate)
        except OSError:
            # Out of space or a locked file: a drop under the original name still works.
            prepared.append(path)
            continue
        prepared.append(candidate)
    return prepared


def audio_drag_pixmap(count, title, when=None):
    """Carry the clip name under the cursor instead of a screenshot of the row."""
    width, height = 268, 46
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(2, 2)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(255, 255, 255, 244))
    painter.setPen(QPen(QColor("#e6e6e8"), 1))
    painter.drawRoundedRect(QRect(0, 0, width - 1, height - 1), 10, 10)
    painter.fillRect(QRect(1, 11, 3, height - 22), QColor(ORANGE))
    icon("audio", ORANGE, 18).paint(painter, QRect(16, 13, 20, 20))
    font = QFont()
    font.setPixelSize(12)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor(INK))
    painter.drawText(QRect(44, 7, width - 84, 20), Qt.AlignmentFlag.AlignVCenter,
                     QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight, width - 84))
    font.setPixelSize(11)
    font.setWeight(QFont.Weight.Normal)
    painter.setFont(font)
    painter.setPen(QColor("#8b8b90"))
    stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else ""
    detail = f"{count} 个音频" if count > 1 else "拖到其他程序即可使用"
    caption = stamp + " · " + detail if stamp else detail
    painter.drawText(QRect(44, 25, width - 60, 16), Qt.AlignmentFlag.AlignVCenter,
                     QFontMetrics(font).elidedText(caption, Qt.TextElideMode.ElideRight, width - 60))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


def start_audio_drag(source, files, title="", texts=None):
    """Hand one drag session to the OS; nothing readable means no drag at all."""
    files = existing_audio_files(files)
    if not files:
        return False
    handed = prepare_drag_files(files, texts) or files
    try:
        when = handed[0].stat().st_mtime
    except OSError:
        when = None
    drag = QDrag(source)
    drag.setMimeData(audio_mime_data(handed))
    pixmap = audio_drag_pixmap(len(handed), title or handed[0].stem, when)
    drag.setPixmap(pixmap)
    size = pixmap.deviceIndependentSize()
    drag.setHotSpot(QPoint(int(size.width()) - 22, int(size.height()) // 2))
    # The target program may activate mid-drag; a hidden source list cancels the drop.
    held, parent = [], source.parentWidget()
    while parent is not None:
        if isinstance(parent, AnchoredPopover):
            parent.held = True
            held.append(parent)
        parent = parent.parentWidget()
    try:
        drag.exec(Qt.DropAction.CopyAction)
    finally:
        for popover in held:
            popover.held = False
    return True


class DragHandle(QWidget):
    """A six-dot grip: pulling it hands the row's latest audio to another program."""

    def __init__(self, files, title=None, parent=None, size=18, texts=None):
        super().__init__(parent)
        self.files, self.title, self.texts = files, title or (lambda: ""), texts
        self.press, self.active = None, False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName("拖动音频到其他程序")
        self.refresh()

    def audio_files(self):
        return existing_audio_files(self.files() if self.files else ())

    def drag_title(self):
        return self.title() or self.toolTip()

    def refresh(self):
        self.active = bool(self.audio_files())
        self.setCursor(Qt.CursorShape.OpenHandCursor if self.active else Qt.CursorShape.ArrowCursor)
        self.setToolTip("按住拖到其他程序，带走最新生成的音频" if self.active
                        else "生成之后，可以按住这里把音频拖到其他程序")
        self.update()

    def begin_drag(self):
        return start_audio_drag(self, self.files() if self.files else (), self.title(),
                                self.texts() if self.texts else None)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.active:
            self.press = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.press is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self.press).manhattanLength() >= QApplication.startDragDistance()):
            self.press = None
            self.begin_drag()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.press = None
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f4e7db"))
            painter.drawRoundedRect(self.rect(), 5, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ORANGE if self.active else "#cfcfd4"))
        center = QPointF(self.width() / 2, self.height() / 2)
        for dx in (-3.5, 3.5):
            for dy in (-4.0, 0.0, 4.0):
                painter.drawEllipse(QPointF(center.x() + dx, center.y() + dy), 1.3, 1.3)
        painter.end()


class AudioDrag:
    """Row-level file drag shared by every generated-audio list."""

    def enable_audio_drag(self):
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDragDropOverwriteMode(False)

    def resolve_audio(self, row):
        """A missing resolver must keep the widget click-only."""
        return ()

    def describe_audio(self, row):
        return ""

    def audio_row_texts(self, row):
        """The text each dropped file should be named after; None keeps the stored name."""
        return None

    def audio_drag_files(self, row):
        return existing_audio_files(self.resolve_audio(row))

    def startDrag(self, supported_actions):
        row = self.currentRow()
        # Nothing readable under the cursor keeps the press an ordinary click.
        start_audio_drag(self, self.audio_drag_files(row), self.describe_audio(row), self.audio_row_texts(row))


class AudioTable(AudioDrag, QTableWidget):
    """A table of generated audio; rows leave as files once a resolver is set."""

    def __init__(self, rows, columns):
        super().__init__(rows, columns)


class RecentTasks(AudioDrag, QTableWidget):
    delete_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__(0, 3)
        self.setMouseTracking(True)
        self.setItemDelegate(RecentJobDelegate(self))
        self.enable_audio_drag()

    def mousePressEvent(self, event):
        row = self.rowAt(int(event.position().y()))
        item = self.item(row, 0) if row >= 0 else None
        from .recent_works import task_type
        deletable = item is not None and task_type(item.data(257) or {}) in ("article", "dialogue", "float")
        if deletable and event.button() == Qt.MouseButton.LeftButton and event.position().x() > self.viewport().width() - 32:
            self.delete_requested.emit(item.data(256))
            event.accept()
            return
        super().mousePressEvent(event)


class Waveform(QWidget):
    """A real PCM amplitude preview; blank until a generated clip is selected."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks = []
        self.progress = 0.0
        self.path = None
        self.setMinimumHeight(38)
        self.setMinimumWidth(100)

    def load(self, path: Path | None):
        if self.path == path:
            return
        self.path, self.peaks, self.progress = path, [], 0.0
        if path:
            try:
                with wave.open(str(path), "rb") as stream:
                    width = stream.getsampwidth()
                    stride = max(1, stream.getnframes() // 120)
                    for _ in range(120):
                        # Bound sampling cost even for unusually long clips.
                        raw = stream.readframes(min(stride, 2048))
                        if not raw:
                            break
                        if width == 2:
                            values = array.array("h", raw)
                            if sys.byteorder != "little":
                                values.byteswap()
                            peak = max(abs(v) for v in values) / 32768
                        elif width == 1:
                            peak = max(abs(v - 128) for v in raw) / 128
                        else:
                            peak = max(abs(int.from_bytes(raw[i:i+width], "little", signed=True))
                                       for i in range(0, len(raw), width)) / (2 ** (width * 8 - 1))
                        self.peaks.append(peak)
                        stream.setpos(min(stream.getnframes(), stream.tell() + max(0, stride - 2048)))
            except (OSError, wave.Error, ValueError):
                self.peaks = []
        self.update()

    def set_progress(self, fraction):
        self.progress = max(0, min(1, fraction))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.height() / 2
        if not self.peaks:
            painter.setPen(QPen(QColor("#e5e5e7"), 1))
            painter.drawLine(0, int(center), self.width(), int(center))
            return
        maximum = max(self.peaks) or 1
        count = min(len(self.peaks), max(1, self.width() // 4))
        step = self.width() / count
        for i in range(count):
            peak = self.peaks[int(i * len(self.peaks) / count)]
            height = max(2, peak / maximum * (self.height() - 6))
            painter.setPen(QPen(QColor(ORANGE if i / count <= self.progress else "#d9b69f"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            x = int(step * i + step / 2)
            painter.drawLine(x, int(center - height / 2), x, int(center + height / 2))


STYLE = """
QMainWindow, QWidget#shell { background:#f7f7f7; color:#18181a; }
QWidget { font-size:13px; color:#18181a; }
QWidget#sheet, QFrame#card { background:#ffffff; border:1px solid #e8e8ea; border-radius:14px; }
QWidget#sidebar { background:transparent; }
QWidget#inspector { background:#fcfcfc; border-left:1px solid #ededee; }
QWidget#inspectorBody { background:#fcfcfc; }
QWidget#audioEditContent { background:white; }
QWidget#modelManagementContent { background:white; }
QWidget#performanceSettings { background:white; }
QLabel { background:transparent; border:none; }
QLabel[role="brand"] { font-size:23px; font-weight:700; }
QLabel[role="title"] { font-size:24px; font-weight:600; }
QLabel[role="section"] { font-size:14px; font-weight:600; }
QLabel[role="eyebrow"] { font-size:10px; color:#99999d; letter-spacing:2px; }
QLabel[role="muted"] { color:#85858a; font-size:12px; }
QLabel[role="hint"] { color:#77777c; font-size:12px; line-height:1.6; }
QLabel#banner { background:#efefef; color:#6b6b6e; border-radius:10px; padding:8px 14px; font-size:11px; }
QLabel#banner[state="ready"] { background:#edf5ef; color:#39724e; }
QLabel#banner[state="failed"] { background:#fbe9e0; color:#a34e26; }
QLabel#badge { color:#a75022; background:#fbecdf; padding:5px 9px; border-radius:7px; font-size:11px; }
QLabel#voiceAvatar { background:#fbe6d6; color:#b5541c; border-radius:12px; font-size:20px; font-weight:600; }
QPushButton { border:1px solid #e3e3e5; border-radius:8px; background:white; padding:8px 13px; min-height:18px; }
QPushButton:hover { background:#f3f3f4; border-color:#d6d6d8; }
QPushButton:pressed { background:#eaeaec; }
QPushButton:disabled { color:#b0b0b4; background:#f7f7f7; border-color:#eeeeef; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background:#f6f6f7; color:#a2a2a8; border-color:#ececee; }
QPushButton:focus { border-color:#e0651c; }
QPushButton[kind="primary"] { background:#18181a; color:white; border-color:#18181a; font-weight:600; }
QPushButton[kind="primary"]:hover { background:#343436; }
QPushButton[kind="primary"]:disabled { background:#d6d6d9; border-color:#d6d6d9; }
QPushButton[kind="ghost"] { background:transparent; border-color:transparent; color:#6b6b6e; }
QPushButton[kind="ghost"]:hover { background:#efefef; color:#18181a; }
QPushButton[kind="nav"] { text-align:left; background:transparent; border-color:transparent; padding:11px 14px; color:#6b6b6e; }
QPushButton[kind="nav"]:checked { background:white; border-color:#e7e7e9; color:#18181a; font-weight:600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background:#fafafa; border:1px solid #e5e5e7; border-radius:8px; padding:7px 10px; min-height:20px; selection-background-color:#fbe6d6; selection-color:#18181a; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color:#d9ac8c; }
QLineEdit#documentTitle { background:transparent; border:0; border-radius:0; font-size:27px; font-weight:600; padding:0; min-height:42px; }
QComboBox { padding-right:26px; }
QComboBox::drop-down { border:0; width:24px; }
QComboBox QAbstractItemView { background:white; border:1px solid #e0e0e2; selection-background-color:#f5e9df; selection-color:#18181a; padding:5px; }
QTextEdit { background:#fafafa; border:1px solid #e5e5e7; border-radius:9px; padding:12px; selection-background-color:#fbe6d6; selection-color:#18181a; }
QTextEdit#scriptEditor { background:white; border:0; border-radius:0; padding:4px 0; font-size:17px; }
QTextEdit#code { font-family:Consolas; font-size:12px; background:#f7f7f8; }
QTableWidget { background:white; border:0; gridline-color:#f0f0f1; selection-background-color:#fbede3; selection-color:#18181a; outline:0; }
QTableWidget::item { padding:7px 9px; border-bottom:1px solid #f2f2f3; }
QTableWidget::item:selected { background:#fbede3; color:#18181a; }
QTableWidget#recentJobs { background:transparent; font-size:12px; }
QTableWidget#recentJobs::item { border:0; padding:7px 3px; }
QHeaderView::section { background:#fafafa; color:#88888e; font-size:11px; border:0; border-bottom:1px solid #ededee; padding:9px 6px; }
QTableCornerButton::section { background:#fafafa; border:0; }
QSplitter::handle { background:#f1f1f2; }
QSplitter::handle:vertical { height:5px; }
QScrollArea { background:transparent; border:0; }
QScrollBar:vertical { background:transparent; width:7px; margin:3px 0; }
QScrollBar::handle:vertical { background:#dedee1; min-height:30px; border-radius:3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QScrollBar:horizontal { height:7px; background:transparent; }
QScrollBar::handle:horizontal { background:#dedee1; min-width:30px; border-radius:3px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
QSlider::groove:horizontal { height:3px; background:#e8e8ea; border-radius:1px; }
QSlider::sub-page:horizontal { background:#e0651c; }
QSlider::handle:horizontal { background:#e0651c; width:10px; margin:-4px 0; border-radius:5px; }
QProgressBar { border:0; background:#efeff1; border-radius:3px; max-height:5px; min-height:5px; }
QProgressBar::chunk { background:#e0651c; border-radius:3px; }
QProgressBar#benchmarkProgress { min-height:8px; max-height:8px; border-radius:4px; background:#efeff1; }
QProgressBar#benchmarkProgress::chunk { background:#e0651c; border-radius:4px; }
QStatusBar { background:#f7f7f7; color:#99999d; font-size:11px; }
QStatusBar::item { border:none; }
QDialog, QMessageBox { background:#f7f7f7; }
QToolTip { background:#18181a; color:white; padding:6px; border:0; }
"""

_assets = (Path(__file__).parent / "assets").as_posix()
STYLE += f"""
QComboBox::down-arrow {{ image:url('{_assets}/arrow-down.svg'); width:12px; height:12px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:23px; border:0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:23px; border:0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url('{_assets}/arrow-up.svg'); width:12px; height:12px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url('{_assets}/arrow-down.svg'); width:12px; height:12px; }}
"""

STYLE += """
QWidget#inspector, QWidget#inspectorBody { background:white; }
QWidget#libraryBody, QWidget#modelControls { background:white; }
QWidget#sheet { border-radius:12px; }
QLabel[role="brand"] { font-size:19px; font-weight:600; }
QLabel#appVersion { font-family:Consolas; font-size:11px; color:#99999d; padding:3px 0; }
QLabel[role="title"] { font-size:26px; font-weight:600; }
QLabel[role="dialogTitle"] { font-size:20px; font-weight:600; }
QLabel[role="ticketTitle"] { font-size:17px; font-weight:600; }
QLabel[role="ticketMeta"] { font-family:Consolas; font-size:11px; color:#8a8985; }
QLabel[role="ticketStamp"] { font-size:25px; font-weight:600; color:#252525; }
QLabel[role="error"] { padding:10px; background:#fcf0e9; border-radius:6px; color:#a2462e; }
QLabel#banner { background:transparent; padding:3px 6px; }
QLabel#banner[state="ready"] { background:transparent; color:#83907c; }
QPushButton { border-radius:6px; font-weight:400; }
QPushButton[kind="small"] { padding:5px 11px; min-height:18px; font-size:12px; background:#f7f7f5; }
QPushButton#emotionPreset:checked { color:#a34c1b; background:#fff1e7; border-color:#e3a578; }
QPushButton[kind="icon"] { border:0; background:transparent; padding:4px; min-width:22px; min-height:22px; }
QPushButton[kind="icon"]:hover { background:#e8e8e5; }
QPushButton[kind="referenceChoice"] { text-align:left; background:white; padding:9px 12px; }
QPushButton[kind="referenceChoice"]:checked { background:#faf4ed; border-color:#d9b38e; }
QPushButton[kind="libraryTab"] { border:0; border-radius:0; background:transparent; padding:8px 2px 10px; color:#8a8a86; }
QPushButton[kind="libraryTab"]:checked { color:#18181a; border-bottom:2px solid #18181a; font-weight:600; }
QPushButton#segment { border:0; background:transparent; padding:6px; }
QPushButton[kind="segment"] { border:1px solid transparent; background:transparent; padding:5px 4px; font-size:12px; }
QPushButton[kind="segment"]:checked { background:white; border-color:#e4e4e1; }
QWidget#segmented { background:#f4f4f2; border:1px solid #e9e9e6; border-radius:7px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background:white; border-radius:6px; padding:7px 9px; }
QPlainTextEdit { background:#f7f7f5; border:0; border-radius:8px; padding:10px; selection-background-color:#f6e4d6; }
QLineEdit#documentTitle { font-size:25px; min-height:40px; }
QTextEdit#scriptEditor { font-size:16px; }
QFrame#voiceTicket { background:transparent; border:0; }
QFrame#dialogSurface { background:white; border:1px solid #e6e6e3; border-radius:14px; }
QDialog#paperDialog { background:transparent; }
QPushButton#audioDrop { background:#fafaf8; border:1px dashed #d8d8d4; }
QMenu { background:white; border:1px solid #e5e5e2; border-radius:8px; padding:5px; }
QMenu::item { padding:8px 24px 8px 12px; border-radius:5px; }
QMenu::item:selected { background:#f3f3f0; }
QMenu::separator { height:1px; background:#eeeeeb; margin:5px; }
QTableWidget#recentJobs::item:selected { background:transparent; }
QTableWidget#recentJobs { selection-background-color:transparent; }
QSlider::sub-page:horizontal { background:#8f8f88; }
QSlider::handle:horizontal { background:white; border:1px solid #bdbdb7; width:12px; margin:-5px 0; border-radius:6px; }
QMainWindow, QWidget#shell { background:white; }
QWidget#sheet { background:white; border:0; border-radius:0; }
QWidget#sidebar { background:#fafafa; border-right:1px solid #eeeeee; }
QFrame#card { border-radius:6px; border-color:#eeeeee; }
QLabel[role="title"] { font-size:23px; }
QLineEdit#documentTitle { font-size:23px; min-height:32px; }
QTextEdit#scriptEditor { font-size:15px; }
QLabel[role="ticketTitle"] { font-size:15px; }
QLabel[role="ticketStamp"] { font-size:18px; font-weight:500; }
QPushButton { padding:6px 10px; min-height:16px; border-radius:6px; }
QPushButton[kind="nav"] { padding:8px 10px; }
QPushButton[kind="nav"]:checked { background:#f0f0f0; border-color:transparent; font-weight:500; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding:6px 8px; min-height:18px; border-radius:6px; }
QPlainTextEdit { background:white; border:1px solid #e5e5e7; border-radius:6px; padding:8px; }
QPushButton[kind="disclosure"] { text-align:left; border:0; border-top:1px solid #eeeeee; border-radius:0; padding:10px 0 5px; min-height:20px; max-height:20px; background:white; color:#444448; }
QPushButton[kind="disclosure"]:hover { color:#18181a; }
QWidget#segmented { background:#fafafa; border-color:#eeeeee; }
QTableWidget::item:selected { background:#f1f1f1; }
QHeaderView::section { background:white; border-color:#eeeeee; }
QTableWidget#modelTable::item { padding:4px 10px; }
QHeaderView#modelSectionHeader::section { padding:4px 10px; }
QTableWidget#modelTable QPushButton { padding:4px 10px; }
QLabel[role="settingsTitle"] { font-size:22px; font-weight:600; color:#18181a; }
QLabel[role="settingLabel"] { font-weight:600; color:#18181a; }
QPushButton[kind="settingsTab"] { border:0; border-bottom:2px solid transparent; border-radius:0; padding:10px 12px; background:white; color:#727277; }
QPushButton[kind="settingsTab"]:checked { border-bottom:2px solid #18181a; color:#18181a; font-weight:600; }
QPushButton[kind="settingsTab"]:hover { color:#18181a; }
QFrame#settingsGroup { border:1px solid #eeeeee; border-radius:13px; background:white; }
QFrame#settingsDivider { border:0; background:#eeeeee; }
QWidget#engineManagement { background:white; }
QWidget#engineSettingsContent, QWidget#generalSettings { background:white; }
QWidget#settingsRow { background:transparent; }
QTableWidget#modelTable { border:1px solid #eeeeee; border-radius:12px; }
QWidget#inspectorBody QComboBox { min-height:22px; max-height:22px; }
QWidget#engineManagement QProgressBar { min-height:3px; max-height:3px; border-radius:1px; }
QWidget#engineManagement QProgressBar::chunk { background:#85877f; border-radius:1px; }
QLabel#engineError { color:#a2462e; }
QSplitter::handle { background:#eeeeee; }
QSplitter::handle:vertical { height:1px; }
QStatusBar { background:white; }
QWidget#transport { background:white; border-top:1px solid #eeeeee; }
QPushButton[kind="clipTitle"] { text-align:left; background:transparent; border:0; padding:3px 0; font-weight:500; }
QPushButton[kind="clipTitle"]:hover { color:#e0651c; }
QFrame#clipList { background:white; border:1px solid #dedede; border-radius:8px; }
QFrame#toast { background:white; border:1px solid #e5e5e5; border-radius:6px; }
"""
