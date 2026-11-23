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


# One palette for every surface: warm paper, ink text, a single vermilion accent.
INK = "#1c1b19"
INK_SOFT = "#3d3a35"
MUTED = "#6f6b64"
FAINT = "#9a948a"
LINE = "#e5e0d8"
LINE_SOFT = "#f0ece5"
PAPER = "#f6f3ee"
PAPER_DEEP = "#efeae1"
ORANGE = "#d9601f"
ORANGE_DARK = "#bc4f15"
ORANGE_TINT = "#fbeee4"
SUCCESS = "#4f7351"
DANGER = "#a5432b"


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
    "sparkle": '<path d="M12 3.5 14 9l5.5 2-5.5 2-2 5.5-2-5.5L4.5 11 10 9z"/><path d="M18.5 16.5 19.2 18l1.5.7-1.5.7-.7 1.5-.7-1.5-1.5-.7 1.5-.7z"/>',
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
def icon_pixmap(name, color=INK, size=20):
    """Render one glyph once; every button and row asks for the same few."""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    # Render at 2x for crisp icons on high-density displays.
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


@lru_cache(maxsize=256)
def icon(name, color=INK, size=20):
    return QIcon(icon_pixmap(name, color, size))


@lru_cache(maxsize=64)
def button_icon(name, color, disabled="#cfc8bc", size=18):
    """A glyph that fades with the button instead of staying bright white."""
    result = QIcon()
    result.addPixmap(icon_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(icon_pixmap(name, disabled, size), QIcon.Mode.Disabled, QIcon.State.Off)
    return result


def button(text, callback, symbol=None, kind=None):
    control = QPushButton(text)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    if symbol:
        color = "#ffffff" if kind in ("primary", "transportPlay") else INK_SOFT
        control.setIcon(button_icon(symbol, color))
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
        format_.setLineHeight(175, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(format_)


class RecentJobDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        value = index.data(257) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rectangle = option.rect.adjusted(2, 2, -4, -2)
        if selected or hover:
            painter.setBrush(QColor("#ffffff" if selected else "#fbf9f5"))
            painter.setPen(QPen(QColor(LINE if selected else "#efebe3"), 1))
            painter.drawRoundedRect(rectangle.adjusted(0, 0, -1, -1), 7, 7)
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(ORANGE))
                painter.drawRoundedRect(QRect(rectangle.left() + 1, rectangle.top() + 5, 2,
                                              rectangle.height() - 10), 1, 1)
        color = {"done": SUCCESS, "failed": "#b4563a", "running": ORANGE, "importing": ORANGE,
                 "queued": "#b08a4f"}.get(value.get("status"), "#b5ada1")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(rectangle.left() + 11, rectangle.center().y() - 2, 5, 5)
        painter.setPen(QColor(INK if selected else MUTED))
        painter.setFont(option.font)
        from .recent_works import task_type, WORK_ICONS
        category = task_type(value)
        colors = {"dialogue": "#8a6a3f", "audio": "#4f7f78", "recognition": "#626a98", "float": "#b8713f"}
        own_page = category in ("article", "dialogue", "float")
        icon(WORK_ICONS.get(category, "article"), colors.get(category, FAINT), 15).paint(
            painter, rectangle.left() + 24, rectangle.center().y() - 7, 15, 15)
        text_rect = rectangle.adjusted(47, 0, -30 if own_page else -14, 0)
        text = option.fontMetrics.elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)
        if (selected or hover) and own_page:
            icon("trash", FAINT, 13).paint(painter, rectangle.right() - 22, rectangle.center().y() - 6, 13, 13)
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


# Where the pointer sits inside the drag preview. On the leading edge, so the
# card hangs to the right of the cursor: that is where a target program paints
# its own insertion preview (CapCut drops the clip from the pointer rightwards),
# so the pointer marks the exact start and the timeline in front of it -- the
# clips you are lining up against -- stays visible.
DRAG_HOTSPOT_X = 6


def audio_drag_pixmap(count, title, when=None):
    """Carry the clip name beside the cursor instead of a screenshot of the row."""
    width, height = 268, 46
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(2, 2)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(255, 253, 250, 248))
    painter.setPen(QPen(QColor(LINE), 1))
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
    painter.setPen(QColor("#8b8479"))
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
    drag.setHotSpot(QPoint(DRAG_HOTSPOT_X, int(size.height()) // 2))
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
            painter.setBrush(QColor(ORANGE_TINT))
            painter.drawRoundedRect(self.rect(), 6, 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ORANGE if self.active else "#cfc8bc"))
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
            painter.setPen(QPen(QColor("#e8e2d8"), 1))
            painter.drawLine(0, int(center), self.width(), int(center))
            return
        maximum = max(self.peaks) or 1
        count = min(len(self.peaks), max(1, self.width() // 4))
        step = self.width() / count
        for i in range(count):
            peak = self.peaks[int(i * len(self.peaks) / count)]
            height = max(2, peak / maximum * (self.height() - 6))
            painter.setPen(QPen(QColor(ORANGE if i / count <= self.progress else "#ddc8b8"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            x = int(step * i + step / 2)
            painter.drawLine(x, int(center - height / 2), x, int(center + height / 2))


STYLE = """
/* ── surfaces ─────────────────────────────────────────────────────────── */
QMainWindow, QWidget#shell { background:#ffffff; color:#1c1b19; }
QWidget { font-size:13px; color:#1c1b19; }
QWidget#sheet { background:#ffffff; }
QWidget#sidebar { background:#f6f3ee; border-right:1px solid #e5e0d8; }
QWidget#workspaceHeader { background:#ffffff; border-bottom:1px solid #f0ece5; }
QWidget#inspector { background:#ffffff; border-left:1px solid #ece7de; }
QWidget#inspectorBody, QWidget#inspectorActions { background:#ffffff; }
QWidget#libraryBody, QWidget#modelControls, QWidget#dialogueBody { background:#ffffff; }
QWidget#audioEditContent, QWidget#recognitionContent { background:#ffffff; }
QWidget#modelManagementContent, QWidget#performanceSettings, QWidget#engineSettingsContent { background:#ffffff; }
QWidget#engineManagement, QWidget#generalSettings, QWidget#settingsRow { background:#ffffff; }
QWidget#settingsRow { background:transparent; }
QScrollArea { background:transparent; border:0; }
QWidget#qt_scrollarea_viewport { background:transparent; }
QScrollArea > QWidget > QWidget { background:transparent; }
QTableWidget QWidget#qt_scrollarea_viewport { background:#ffffff; }
QLabel { background:transparent; border:none; }

/* ── type ─────────────────────────────────────────────────────────────── */
QLabel[role="brand"] { font-size:19px; font-weight:700; }
QLabel[role="tagline"] { font-size:11px; color:#9a948a; }
QLabel[role="title"] { font-size:22px; font-weight:600; }
QLabel[role="section"] { font-size:13px; font-weight:600; }
QLabel[role="groupTitle"] { font-size:13px; font-weight:600; color:#6f6b64; padding-left:1px; }
QLabel[role="eyebrow"] { font-size:10px; color:#9a948a; }
QLabel[role="muted"] { color:#6f6b64; font-size:12px; }
QLabel[role="hint"] { color:#6f6b64; font-size:12px; }
QLabel[role="crumb"] { color:#9a948a; font-size:12px; }
QLabel[role="dialogTitle"] { font-size:19px; font-weight:600; }
QLabel[role="ticketTitle"] { font-size:15px; font-weight:600; }
QLabel[role="ticketMeta"] { font-family:Consolas; font-size:11px; color:#8b8479; }
QLabel[role="ticketStamp"] { font-size:18px; font-weight:600; color:#1c1b19; }
QLabel[role="settingsTitle"] { font-size:22px; font-weight:600; }
QLabel[role="settingLabel"] { font-size:13px; font-weight:600; }
QLabel[role="error"] { padding:10px 12px; background:#fbece6; border-radius:8px; color:#a5432b; font-size:12px; }
QLabel#appVersion { font-family:Consolas; font-size:11px; color:#9a948a; }
QLabel[role="mono"] { font-family:Consolas; font-size:11px; color:#9a948a; }
QLabel#jobsEmpty { color:#8b8479; border:1px dashed #ddd6ca; border-radius:8px; padding:12px; font-size:11px; }
QLabel#banner { background:transparent; color:#6f6b64; font-size:11px; }
QLabel#banner[state="ready"] { color:#4f7351; }
QLabel#banner[state="failed"] { color:#a5432b; }
QLabel#inferenceStatus { background:transparent; font-size:11px; color:#8b8479; }
QLabel#inferenceStatus[state="running"] { color:#4f7351; }
QLabel#inferenceStatus[state="fallback"] { color:#b04f22; }
QLabel#badge { color:#a84c1c; background:#fbeee4; padding:4px 9px; border-radius:9px; font-size:11px; }
QLabel#voiceAvatar { background:#fbeee4; color:#b5541c; border-radius:12px; font-size:20px; font-weight:600; }
QLabel#engineError { color:#a5432b; font-size:12px; }

/* ── buttons ──────────────────────────────────────────────────────────── */
QPushButton { border:1px solid #e5e0d8; border-radius:8px; background:#ffffff; padding:6px 12px; min-height:18px; color:#1c1b19; }
QPushButton:hover { background:#faf7f2; border-color:#d8d1c6; }
QPushButton:pressed { background:#efeae1; }
QPushButton:disabled { color:#b3ada3; background:#faf8f4; border-color:#eeeae2; }
QPushButton:focus { border-color:#e7b28d; }
QPushButton[kind="primary"] { background:#1c1b19; color:#ffffff; border-color:#1c1b19; font-weight:600; }
QPushButton[kind="primary"]:hover { background:#332f2a; border-color:#332f2a; }
QPushButton[kind="primary"]:pressed { background:#0f0e0d; }
QPushButton[kind="primary"]:focus { border-color:#e7b28d; }
QPushButton[kind="primary"]:disabled { background:#e8e3da; border-color:#e8e3da; color:#a49d92; font-weight:500; }
QPushButton[kind="ghost"] { background:transparent; border-color:transparent; color:#6f6b64; }
QPushButton[kind="ghost"]:hover { background:#f6f3ee; color:#1c1b19; }
QPushButton[kind="ghost"]:pressed { background:#efeae1; }
QPushButton[kind="ghost"]:disabled { background:transparent; border-color:transparent; color:#b3ada3; }
QPushButton[kind="nav"] { text-align:left; background:transparent; border:1px solid transparent; border-left:3px solid transparent; padding:6px 10px; min-height:22px; color:#6f6b64; }
QPushButton[kind="nav"]:hover { background:#fbf9f5; color:#1c1b19; }
QPushButton[kind="nav"]:checked { background:#ffffff; border-color:#e5e0d8; border-left:3px solid #d9601f; color:#1c1b19; font-weight:600; }
QPushButton[kind="newWork"] { background:#1c1b19; color:#ffffff; border-color:#1c1b19; font-weight:600; padding:8px 12px; }
QPushButton[kind="newWork"]:hover { background:#332f2a; border-color:#332f2a; }
QPushButton[kind="small"] { padding:4px 9px; min-height:16px; font-size:12px; background:#ffffff; }
QPushButton[kind="recording"] { padding:4px 9px; min-height:16px; font-size:12px; background:#fbeee4; border-color:#e3b596; color:#b04f22; font-weight:600; }
QPushButton[kind="recording"]:hover { background:#f7e4d6; }
QPushButton[kind="icon"] { border:0; background:transparent; padding:5px; min-width:28px; min-height:28px; border-radius:7px; }
QPushButton[kind="icon"]:hover { background:#f6f3ee; }
QPushButton[kind="icon"]:pressed { background:#efeae1; }
QPushButton[kind="referenceChoice"] { text-align:left; background:#ffffff; padding:6px 10px; }
QPushButton[kind="referenceChoice"]:checked { background:#fffaf5; border-color:#e7b28d; }
QPushButton[kind="libraryTab"] { border:0; border-bottom:2px solid transparent; border-radius:0; background:transparent; padding:5px 2px 7px; color:#8b8479; }
QPushButton[kind="libraryTab"]:checked { color:#1c1b19; border-bottom:2px solid #1c1b19; font-weight:600; }
QPushButton[kind="libraryTab"]:hover { color:#1c1b19; background:transparent; }
QPushButton[kind="settingsTab"] { border:0; border-bottom:2px solid transparent; border-radius:0; background:transparent; padding:9px 14px 8px; color:#8b8479; min-height:20px; }
QPushButton[kind="settingsTab"]:checked { color:#1c1b19; border-bottom:2px solid #1c1b19; font-weight:600; }
QPushButton[kind="settingsTab"]:hover { color:#1c1b19; background:transparent; }
QPushButton[kind="filterChip"] { border:1px solid transparent; border-radius:13px; padding:4px 12px; min-height:18px; background:transparent; color:#6f6b64; font-size:12px; }
QPushButton[kind="filterChip"]:hover { background:#f6f3ee; color:#1c1b19; }
QPushButton[kind="filterChip"]:checked { background:#1c1b19; color:#ffffff; border-color:#1c1b19; font-weight:600; }
QPushButton[kind="segment"] { border:1px solid transparent; border-radius:6px; background:transparent; padding:4px 4px; font-size:12px; color:#6f6b64; min-height:18px; }
QPushButton[kind="segment"]:checked { background:#ffffff; border-color:#e0dad0; color:#1c1b19; font-weight:600; }
QPushButton[kind="segment"]:hover { color:#1c1b19; }
QPushButton#emotionPreset:checked { color:#a84c1c; background:#fbeee4; border-color:#e3b596; }
QPushButton[kind="disclosure"] { text-align:left; border:0; border-top:1px solid #f0ece5; border-radius:0; padding:0 0; min-height:36px; max-height:36px; background:transparent; color:#3d3a35; font-size:12px; }
QPushButton[kind="disclosure"]:hover { color:#1c1b19; background:transparent; }
QPushButton[kind="clipTitle"] { text-align:left; background:transparent; border:0; padding:2px 0; font-weight:600; font-size:13px; }
QPushButton[kind="clipTitle"]:hover { color:#d9601f; }
QPushButton[kind="dashedAdd"] { border:1px dashed #d8d1c6; background:transparent; color:#6f6b64; min-height:34px; }
QPushButton[kind="dashedAdd"]:hover { border-color:#e7b28d; background:#fffaf5; color:#a84c1c; }
QPushButton#audioDrop { background:#fbf8f3; border:1px dashed #d8d1c6; }
QPushButton#transportPlay { border-radius:18px; }
QPushButton[kind="transportPlay"] { background:#1c1b19; color:#ffffff; border:1px solid #1c1b19; border-radius:18px; padding:0; }
QPushButton[kind="transportPlay"]:hover { background:#332f2a; border-color:#332f2a; }
QPushButton[kind="transportPlay"]:pressed { background:#0f0e0d; }
QPushButton[kind="transportPlay"]:disabled { background:#ece6dc; border-color:#ece6dc; }

/* ── inputs ───────────────────────────────────────────────────────────── */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background:#ffffff; border:1px solid #e5e0d8; border-radius:8px; padding:5px 10px; min-height:20px;
    selection-background-color:#fbeee4; selection-color:#1c1b19;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color:#d8d1c6; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color:#e7b28d; background:#fffdfb; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background:#faf8f4; color:#a8a199; border-color:#eeeae2; }
QLineEdit#documentTitle { background:transparent; border:0; border-radius:0; font-size:22px; font-weight:600; padding:0 2px; min-height:34px; }
QLineEdit#documentTitle:focus { background:#fffdfb; }
QComboBox { padding-right:28px; }
QComboBox::drop-down { border:0; width:26px; }
QComboBox QAbstractItemView { background:#ffffff; border:1px solid #e0dad0; selection-background-color:#fbeee4; selection-color:#1c1b19; padding:4px; outline:0; }
QTextEdit, QPlainTextEdit {
    background:#ffffff; border:1px solid #e5e0d8; border-radius:8px; padding:9px 11px;
    selection-background-color:#fbeee4; selection-color:#1c1b19;
}
QTextEdit:hover, QPlainTextEdit:hover { border-color:#d8d1c6; }
QTextEdit:focus, QPlainTextEdit:focus { border-color:#e7b28d; background:#fffdfb; }
QTextEdit:disabled, QPlainTextEdit:disabled { background:#faf8f4; color:#a8a199; }
QTextEdit#scriptEditor { background:#ffffff; border:0; border-radius:0; padding:2px 0; font-size:16px; }
QTextEdit#scriptEditor:focus { background:#ffffff; }
QTextEdit#dialogueText { background:#ffffff; border:1px solid #f0ece5; border-radius:8px; padding:7px 9px; font-size:15px; }
QTextEdit#dialogueText:hover { border-color:#e5e0d8; }
QTextEdit#dialogueText:focus { border-color:#e7b28d; background:#fffdfb; }
QTextEdit#code { font-family:Consolas; font-size:12px; background:#fbf8f3; border-color:#e5e0d8; }
QPlainTextEdit#code { font-family:Consolas; font-size:12px; background:#fbf8f3; }

/* ── tables and lists ─────────────────────────────────────────────────── */
QTableWidget { background:#ffffff; border:0; gridline-color:#f0ece5; selection-background-color:#fbeee4; selection-color:#1c1b19; outline:0; }
QTableWidget::item { padding:6px 10px; border-bottom:1px solid #f0ece5; }
QTableWidget::item:selected { background:#fbeee4; color:#1c1b19; }
QHeaderView::section { background:#ffffff; color:#9a948a; font-size:11px; border:0; border-bottom:1px solid #e5e0d8; padding:8px 8px; }
QTableCornerButton::section { background:#ffffff; border:0; }
QTableWidget#recentJobs { background:transparent; font-size:12px; selection-background-color:transparent; }
QTableWidget#recentJobs::item { border:0; padding:0; }
QTableWidget#recentJobs::item:selected { background:transparent; }
QTableWidget#modelTable { border:1px solid #e5e0d8; border-radius:12px; }
QTableWidget#modelTable::item { padding:5px 10px; }
QHeaderView#modelSectionHeader::section { padding:4px 10px; }
QTableWidget#modelTable QPushButton { padding:4px 10px; }
QListWidget { background:#ffffff; border:1px solid #e5e0d8; border-radius:8px; padding:4px; outline:0; }
QListWidget::item { padding:6px 8px; border-radius:6px; }
QListWidget::item:hover { background:#faf7f2; }
QListWidget::item:selected { background:#fbeee4; color:#1c1b19; }

/* ── structure ────────────────────────────────────────────────────────── */
QFrame#card { background:#ffffff; border:1px solid #e5e0d8; border-radius:12px; }
QFrame#waveCard { background:#ffffff; border:1px solid #e5e0d8; border-radius:12px; }
QFrame#settingsGroup { border:1px solid #e5e0d8; border-radius:12px; background:#ffffff; }
QFrame#settingsDivider { border:0; background:#f0ece5; }
QWidget#segmented { background:#f6f3ee; border:1px solid #e5e0d8; border-radius:8px; }
QSplitter::handle { background:#f0ece5; }
QSplitter::handle:vertical { height:1px; }
QFrame#clipList { background:#ffffff; border:1px solid #e0dad0; border-radius:12px; }
QFrame#toast { background:#ffffff; border:1px solid #e0dad0; border-radius:8px; }
QWidget#transport { background:#ffffff; border-top:1px solid #f0ece5; }
QFrame#voiceTicket { background:transparent; border:0; }
QFrame#dialogSurface { background:#ffffff; border:1px solid #e5e0d8; border-radius:14px; }

/* ── scrollbars, sliders, progress ────────────────────────────────────── */
QScrollBar:vertical { background:transparent; width:8px; margin:2px 0; }
QScrollBar::handle:vertical { background:#ded7cc; min-height:32px; border-radius:4px; }
QScrollBar::handle:vertical:hover { background:#cfc6b9; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QScrollBar:horizontal { height:8px; background:transparent; }
QScrollBar::handle:horizontal { background:#ded7cc; min-width:32px; border-radius:4px; }
QScrollBar::handle:horizontal:hover { background:#cfc6b9; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
QSlider::groove:horizontal { height:3px; background:#ece7de; border-radius:1px; }
QSlider::sub-page:horizontal { background:#d9601f; border-radius:1px; }
QSlider::handle:horizontal { background:#d9601f; width:11px; margin:-4px 0; border-radius:5px; }
QSlider::handle:horizontal:hover { background:#bc4f15; }
QSlider::groove:horizontal:disabled { background:#f2eee7; }
QSlider::handle:horizontal:disabled { background:#ded7cc; }
QProgressBar { border:0; background:#efeae1; border-radius:2px; max-height:4px; min-height:4px; }
QProgressBar::chunk { background:#d9601f; border-radius:2px; }
QProgressBar#benchmarkProgress { min-height:8px; max-height:8px; border-radius:4px; background:#efeae1; }
QProgressBar#benchmarkProgress::chunk { background:#d9601f; border-radius:4px; }
QWidget#engineManagement QProgressBar { min-height:3px; max-height:3px; border-radius:1px; }
QWidget#engineManagement QProgressBar::chunk { background:#8b8479; border-radius:1px; }

/* ── checks, menus, dialogs ───────────────────────────────────────────── */
QCheckBox { spacing:7px; font-size:12px; color:#6f6b64; }
QCheckBox::indicator { width:14px; height:14px; border:1px solid #d8d1c6; border-radius:4px; background:#ffffff; }
QCheckBox::indicator:hover { border-color:#c9b7a5; }
QCheckBox::indicator:checked { background:#d9601f; border-color:#d9601f; }
QCheckBox::indicator:disabled { background:#f6f3ee; border-color:#ece7de; }
QMenu { background:#ffffff; border:1px solid #e0dad0; border-radius:8px; padding:5px; }
QMenu::item { padding:7px 24px 7px 12px; border-radius:5px; }
QMenu::item:selected { background:#f6f3ee; }
QMenu::separator { height:1px; background:#f0ece5; margin:5px; }
QToolTip { background:#1c1b19; color:#ffffff; padding:6px 8px; border:0; border-radius:5px; }
QDialog, QMessageBox { background:#ffffff; }
QDialog#paperDialog { background:transparent; }
QStatusBar { background:#ffffff; color:#9a948a; font-size:11px; }
QStatusBar::item { border:none; }
"""

_assets = (Path(__file__).parent / "assets").as_posix()
STYLE += f"""
QComboBox::down-arrow {{ image:url('{_assets}/arrow-down.svg'); width:12px; height:12px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:20px; border:0; background:transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:20px; border:0; background:transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url('{_assets}/arrow-up.svg'); width:11px; height:11px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url('{_assets}/arrow-down.svg'); width:11px; height:11px; }}
QCheckBox::indicator:checked {{ image:url('{_assets}/check-white.svg'); }}
"""
