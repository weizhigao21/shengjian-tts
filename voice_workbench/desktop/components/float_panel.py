"""「边剪边配」悬浮窗：打完一句就生成，抓住六点直接拖进剪辑软件的时间线。"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QMimeData, QPoint, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QSlider, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...library import friendly_name
from ...ui import AudioDrag, DragHandle, ORANGE
from ..constants import STATES

PANEL_WIDTH = 430
ROW_HEIGHT = 28
TABLE_MAX_HEIGHT = 196
CHARS_PER_SECOND = 4.2


def estimate_seconds(texts, speed=1.0):
    """Clip work needs a length before generation, not after; 4.2 汉字/秒 plus a beat per line."""
    characters = sum(len("".join(text.split())) for text in texts)
    return characters / (CHARS_PER_SECOND * max(0.5, float(speed))) + 0.35 * len(texts)


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    return f"{int(seconds // 60)} 分 {seconds % 60:.0f} 秒"


class FloatEditor(QPlainTextEdit):
    """Ctrl+Enter generates without reaching for the mouse."""

    submit = pyqtSignal()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.submit.emit()
            return
        super().keyPressEvent(event)


class FloatTitleBar(QWidget):
    """A frameless window still has to be draggable."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # A styled QWidget subclass paints its own background only with this attribute.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.press = None
        self.origin = None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.press = event.globalPosition().toPoint()
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemMove():
            self.press = None
            event.accept()
            return
        self.origin = self.window().pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(self.origin + event.globalPosition().toPoint() - self.press)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.press = self.origin = None
        super().mouseReleaseEvent(event)


class FloatTasks(AudioDrag, QTableWidget):
    """A row leaves as a file named after the line that produced it."""

    def __init__(self, owner):
        super().__init__(0, 4)
        self.owner = owner
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.enable_audio_drag()

    def _index(self, row):
        item = self.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def resolve_audio(self, row):
        index = self._index(row)
        path = self.owner.audio_path(index) if index is not None else None
        return (path,) if path else ()

    def _text(self, row):
        index = self._index(row)
        return str((self.owner.segment(index) or {}).get("text", "")) if index is not None else ""

    def describe_audio(self, row):
        return self._text(row)

    def audio_row_texts(self, row):
        return [self._text(row)]


class FloatPanel(QWidget):
    """Borderless, always on top, and it never hides itself when another program takes focus."""

    generate_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    new_session_requested = pyqtSignal()
    play_requested = pyqtSignal(int)
    copy_requested = pyqtSignal()
    restore_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, state_path, parent=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.state_path = Path(state_path)
        self.audio_root = None
        self.records = {}
        self.voice_speeds = {}
        self.last_voice = None
        self.signature = None
        self.collapsed = False
        self.busy = False
        self.setObjectName("floatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("边剪边配")
        self.setFixedWidth(PANEL_WIDTH)
        self._build()
        self._restore()
        self._refresh_stats()

    # ---------- construction ----------

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(0)
        self.card = QFrame()
        self.card.setObjectName("floatCard")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = FloatTitleBar(self.card)
        bar.setObjectName("floatBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 8, 8, 8)
        bar_layout.setSpacing(8)
        mark = QLabel()
        mark.setObjectName("floatMark")
        mark.setFixedSize(8, 8)
        bar_layout.addWidget(mark)
        self.brand = QLabel("边剪边配")
        self.brand.setObjectName("floatBrand")
        bar_layout.addWidget(self.brand)
        self.state = QLabel("待机")
        self.state.setObjectName("floatState")
        bar_layout.addWidget(self.state)
        bar_layout.addStretch()
        self.restore_button = self._flat("主窗口", "回到主窗口", self.restore_requested.emit)
        bar_layout.addWidget(self.restore_button)
        self.collapse_button = self._flat("收起", "收成一条，继续剪片", self.toggle)
        bar_layout.addWidget(self.collapse_button)
        self.close_button = self._flat("×", "退出边剪边配", self.close)
        self.close_button.setObjectName("floatClose")
        bar_layout.addWidget(self.close_button)
        layout.addWidget(bar)
        self.bar = bar

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(12, 2, 12, 12)
        body_layout.setSpacing(8)

        voices = QHBoxLayout()
        voices.setSpacing(8)
        voice_label = QLabel("音色")
        voice_label.setObjectName("floatHint")
        voices.addWidget(voice_label)
        self.voice_combo = QComboBox()
        self.voice_combo.setObjectName("floatVoice")
        self.voice_combo.setMinimumContentsLength(10)
        self.voice_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.voice_combo.currentIndexChanged.connect(self._voice_changed)
        voices.addWidget(self.voice_combo, 1)
        speed_label = QLabel("语速")
        speed_label.setObjectName("floatHint")
        voices.addWidget(speed_label)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setObjectName("floatSpeed")
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        self.speed_slider.setValue(100)
        self.speed_slider.setFixedWidth(96)
        self.speed_slider.valueChanged.connect(self._speed_changed)
        voices.addWidget(self.speed_slider)
        self.speed_label = QLabel("1.00x")
        self.speed_label.setObjectName("floatSpeedValue")
        self.speed_label.setFixedWidth(38)
        voices.addWidget(self.speed_label)
        body_layout.addLayout(voices)

        self.editor = FloatEditor()
        self.editor.setObjectName("floatEditor")
        self.editor.setPlaceholderText("输入台词，回车换行…")
        self.editor.setFixedHeight(84)
        self.editor.textChanged.connect(self._refresh_stats)
        self.editor.submit.connect(self._request_generate)
        body_layout.addWidget(self.editor)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.stats = QLabel("0 行")
        self.stats.setObjectName("floatHint")
        meta.addWidget(self.stats)
        meta.addStretch()
        self.per_line = QCheckBox("每行一条")
        self.per_line.setChecked(True)
        self.per_line.setToolTip("每一行单独生成一条音频，适合逐句对轴；关掉则整段合成一条。")
        self.per_line.toggled.connect(self._refresh_stats)
        meta.addWidget(self.per_line)
        body_layout.addLayout(meta)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.generate_button = QPushButton("生成")
        self.generate_button.setObjectName("floatGenerate")
        self.generate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.generate_button.setToolTip("Ctrl+Enter 也可以生成")
        self.generate_button.clicked.connect(self._request_generate)
        actions.addWidget(self.generate_button, 1)
        self.copy_button = QPushButton("复制最新")
        self.copy_button.setObjectName("floatFlat")
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.setToolTip("把最新一条音频放进剪贴板，在剪辑软件里 Ctrl+V 直接粘贴")
        self.copy_button.clicked.connect(self.copy_requested.emit)
        actions.addWidget(self.copy_button)
        body_layout.addLayout(actions)

        separator = QFrame()
        separator.setObjectName("floatSeparator")
        separator.setFixedHeight(1)
        body_layout.addWidget(separator)

        tasks = QHBoxLayout()
        tasks.setSpacing(6)
        self.count = QLabel("本次 0 条")
        self.count.setObjectName("floatHint")
        tasks.addWidget(self.count)
        tasks.addStretch()
        self.retry_button = self._flat("重试", "重新生成失败的片段", self.retry_requested.emit)
        self.retry_button.hide()
        tasks.addWidget(self.retry_button)
        self.all_handle = DragHandle(lambda: self.completed_files(), lambda: "全部片段",
                                     self, size=20, texts=lambda: self.completed_texts())
        self.all_handle.setToolTip("拖出全部已完成的片段")
        tasks.addWidget(self.all_handle)
        self.new_button = self._flat("新会话", "清空列表，开始下一个视频", self.new_session_requested.emit)
        tasks.addWidget(self.new_button)
        body_layout.addLayout(tasks)

        self.table = FloatTasks(self)
        self.table.setObjectName("floatTasks")
        self.table.setFixedHeight(0)
        self.table.cellDoubleClicked.connect(self._play_row)
        body_layout.addWidget(self.table)

        self.empty = QLabel("生成的音频会出现在这里\n抓住每行右侧的六点，拖进时间线")
        self.empty.setObjectName("floatEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_layout.addWidget(self.empty)

        self.error = QLabel("")
        self.error.setObjectName("floatError")
        self.error.setWordWrap(True)
        self.error.hide()
        body_layout.addWidget(self.error)

        layout.addWidget(self.body)
        self.setStyleSheet(PANEL_STYLE)

    def _flat(self, text, tip, callback):
        control = QPushButton(text)
        control.setObjectName("floatFlat")
        control.setToolTip(tip)
        control.setCursor(Qt.CursorShape.PointingHandCursor)
        control.setFixedHeight(24)
        control.clicked.connect(callback)
        return control

    # ---------- state ----------

    def set_voices(self, voices):
        """Only saved presets: the panel must not fork the inspector's parameter state."""
        entries = [(voice["id"], friendly_name(voice)) for voice in voices if not voice.get("is_template")]
        self.voice_speeds = {voice["id"]: float(voice.get("speed") or 1.0) for voice in voices
                             if not voice.get("is_template")}
        if [identity for identity, _ in entries] == [self.voice_combo.itemData(i) for i in range(self.voice_combo.count())]:
            return
        current = self.voice_combo.currentData() or getattr(self, "last_voice", None)
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for identity, name in entries:
            self.voice_combo.addItem(name, identity)
        if entries:
            position = self.voice_combo.findData(current) if current else -1
            self.voice_combo.setCurrentIndex(max(0, position))
        else:
            self.voice_combo.addItem("先在声音库保存一个预设", None)
        self.voice_combo.blockSignals(False)
        self.voice_combo.setEnabled(bool(entries))
        self._voice_changed()

    def voice_id(self):
        return self.voice_combo.currentData()

    def speed(self):
        return self.speed_slider.value() / 100

    def _speed_changed(self, value):
        self.speed_label.setText(f"{value / 100:.2f}x")
        self._refresh_stats()

    def segments(self):
        text = self.editor.toPlainText()
        if not self.per_line.isChecked():
            stripped = text.strip()
            return [stripped] if stripped else []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _voice_changed(self, *args):
        """The slider starts on the preset's own speed; the panel only nudges it."""
        speed = self.voice_speeds.get(self.voice_id())
        if speed:
            self.speed_slider.setValue(int(round(float(speed) * 100)))
        self._refresh_stats()

    def _refresh_stats(self, *args):
        segments = self.segments()
        characters = sum(len(text) for text in segments)
        seconds = estimate_seconds(segments, self.speed())
        self.stats.setText(f"{len(segments)} 行 · {characters} 字 · 约 {format_duration(seconds)}")
        self.generate_button.setText(f"生成 {len(segments)} 条" if segments else "生成")
        self.generate_button.setEnabled(bool(segments) and not self.busy and bool(self.voice_id()))
        if self.voice_id():
            self.last_voice = self.voice_id()

    def _request_generate(self):
        self.error.hide()
        if not self.segments():
            self.show_error("先写一句台词")
            return
        if not self.voice_id():
            self.show_error("还没有可用的音色预设，请先在声音库保存一个")
            return
        self.generate_requested.emit()

    def set_busy(self, busy):
        self.busy = busy
        self.generate_button.setEnabled(not busy and bool(self.segments()) and bool(self.voice_id()))
        self.generate_button.setText("提交中…" if busy else
                                     f"生成 {len(self.segments())} 条" if self.segments() else "生成")

    def set_state(self, text):
        self.state.setText(text)

    def show_error(self, message):
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def clear_editor(self):
        self.editor.clear()
        self.editor.setFocus()

    # ---------- tasks ----------

    def segment(self, index):
        return self.records.get(index)

    def audio_path(self, index):
        item = self.segment(index) or {}
        name = item.get("audio")
        if not name or item.get("status") != "done" or self.audio_root is None:
            return None
        try:
            folder = Path(self.audio_root).resolve()
            path = (folder / name).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.parent == folder and path.is_file() else None

    def completed_files(self):
        return tuple(path for path in (self.audio_path(key) for key in sorted(self.records, reverse=True)) if path)

    def completed_texts(self):
        return [str(self.segment(key).get("text", "")) for key in sorted(self.records, reverse=True)
                if self.audio_path(key)]

    def show_job(self, job):
        items = list((job or {}).get("segments") or [])
        self.records = {item["idx"]: item for item in items}
        signature = tuple((item["idx"], item["status"], item.get("audio")) for item in items)
        if signature != self.signature:
            self.signature = signature
            self._rebuild()
        done = sum(1 for item in items if item["status"] == "done")
        failed = sum(1 for item in items if item["status"] == "failed")
        self.count.setText(f"本次 {len(items)} 条 · 完成 {done}" + (f" · 失败 {failed}" if failed else ""))
        self.retry_button.setVisible(bool(failed))
        self.all_handle.refresh()
        if not items:
            self.set_state("待机")

    def reset(self):
        """Next video: forget the previous job so the list starts empty."""
        self.records = {}
        self.signature = None
        self._rebuild()
        self.count.setText("本次 0 条")
        self.retry_button.hide()
        self.set_state("待机")
        self.show_error("")

    def _rebuild(self):
        rows = sorted(self.records.values(), key=lambda item: item["idx"], reverse=True)
        self.table.blockSignals(True)
        # Dropping the rows also drops their grip widgets; clearContents would leak them.
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            index = item["idx"]
            label = QTableWidgetItem(item["text"])
            label.setData(Qt.ItemDataRole.UserRole, index)
            label.setToolTip(item["text"])
            status = QTableWidgetItem(STATES.get(item["status"], item["status"]))
            status.setToolTip((item.get("error") or {}).get("message", "") if item.get("error") else "")
            if item["status"] == "done":
                status.setForeground(QColor("#1d7a55"))
            elif item["status"] == "failed":
                status.setForeground(QColor("#b4453f"))
            elif item["status"] in ("queued", "running"):
                status.setForeground(QColor(ORANGE))
            duration = item.get("duration")
            length = QTableWidgetItem(f"{duration:.1f}s" if duration else "")
            length.setForeground(QColor("#8b8b90"))
            for column, entry in enumerate((label, status, length)):
                self.table.setItem(row, column, entry)
            handle = DragHandle(lambda i=index: (self.audio_path(i),) if self.audio_path(i) else (),
                                lambda i=index: str((self.segment(i) or {}).get("text", "")),
                                self.table, size=20,
                                texts=lambda i=index: [str((self.segment(i) or {}).get("text", ""))])
            self.table.setCellWidget(row, 3, handle)
        self.table.setColumnWidth(1, 62)
        self.table.setColumnWidth(2, 48)
        self.table.setColumnWidth(3, 26)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.blockSignals(False)
        height = min(TABLE_MAX_HEIGHT, len(rows) * ROW_HEIGHT + 4)
        self.table.setFixedHeight(height)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self._resize()

    def _play_row(self, row, column):
        index = self.table._index(row)
        if index is not None and self.audio_path(index):
            self.play_requested.emit(index)

    # ---------- window shape ----------

    def toggle(self):
        self.collapsed = not self.collapsed
        self.body.setVisible(not self.collapsed)
        self.collapse_button.setText("展开" if self.collapsed else "收起")
        self.brand.setText("边剪边配" if not self.collapsed else f"边剪边配 · {self.state.text()}")
        self._resize()

    def _resize(self):
        self.setMaximumHeight(16777215)
        self.setMinimumHeight(0)
        if self.collapsed:
            self.setFixedHeight(self.bar.sizeHint().height() + 24)
            return
        self.adjustSize()

    def present(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.editor.setFocus()

    def copy_latest(self):
        files = [path for path in self.completed_files() if path]
        if not files:
            return False
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(files[0]))])
        QApplication.clipboard().setMimeData(data)
        return True

    # ---------- persistence ----------

    def _restore(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        self.last_voice = state.get("voice")
        self.per_line.setChecked(bool(state.get("per_line", True)))
        if state.get("collapsed"):
            self.toggle()
        point = QPoint(int(state.get("x", 0)), int(state.get("y", 0)))
        if self._on_screen(point):
            self.move(point)
        else:
            self._place_default()

    def _place_default(self):
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is not None:
            self.move(area.right() - PANEL_WIDTH - 40, area.top() + 60)

    @staticmethod
    def _on_screen(point):
        return any(screen.availableGeometry().contains(point) for screen in QGuiApplication.screens())

    def save_state(self):
        state = {"x": self.x(), "y": self.y(), "collapsed": self.collapsed,
                 "per_line": self.per_line.isChecked(), "speed": self.speed_slider.value(),
                 "voice": self.voice_combo.currentData()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def closeEvent(self, event):
        self.save_state()
        self.closed.emit()
        event.accept()


PANEL_STYLE = """
QFrame#floatCard { background: #fffdfb; border: 1px solid #ded7cd; border-radius: 13px; }
QWidget#floatBar { background: #f7f1e9; border-top-left-radius: 13px; border-top-right-radius: 13px; }
QLabel#floatMark { background: #e0651c; border-radius: 4px; }
QLabel#floatBrand { font-size: 13px; font-weight: 600; color: #18181a; }
QLabel#floatState { font-size: 11px; color: #8b8b90; }
QLabel#floatHint { font-size: 11px; color: #77777c; }
QLabel#floatSpeedValue { font-size: 11px; color: #55555a; }
QLabel#floatEmpty { font-size: 11px; color: #a3a3a8; padding: 14px 0; line-height: 1.6; }
QLabel#floatError { font-size: 11px; color: #b4453f; padding: 2px 0; }
QFrame#floatSeparator { background: #efe8df; border: none; }
QPushButton { font-family: inherit; }
QPushButton#floatFlat { border: 1px solid #e2dbd1; border-radius: 6px; background: #ffffff;
                        font-size: 11px; color: #55555a; padding: 0 8px; }
QPushButton#floatFlat:hover { background: #f6f0ea; border-color: #cfc5b8; }
QPushButton#floatClose { border: none; background: transparent; font-size: 14px; color: #8b8b90; }
QPushButton#floatClose:hover { background: #f0d9cd; color: #b4453f; }
QComboBox#floatVoice { border: 1px solid #e2dbd1; border-radius: 7px; background: #ffffff;
                       padding: 3px 8px; font-size: 12px; color: #18181a; }
QComboBox#floatVoice:disabled { color: #a3a3a8; }
QComboBox#floatVoice::drop-down { border: none; width: 16px; }
QPlainTextEdit#floatEditor { border: 1px solid #e2dbd1; border-radius: 8px; background: #ffffff;
                             padding: 5px 7px; font-size: 13px; color: #18181a;
                             selection-background-color: #f4d9c5; selection-color: #18181a; }
QPlainTextEdit#floatEditor:focus { border-color: #e0a173; }
QSlider#floatSpeed::groove:horizontal { height: 3px; background: #e8e1d8; border-radius: 2px; }
QSlider#floatSpeed::sub-page:horizontal { background: #e0651c; border-radius: 2px; }
QSlider#floatSpeed::handle:horizontal { background: #ffffff; border: 1px solid #d8a273;
                                        width: 11px; height: 11px; margin: -5px 0; border-radius: 6px; }
QCheckBox { font-size: 11px; color: #6f6f74; spacing: 5px; }
QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid #cfc5b8; border-radius: 3px; background: #ffffff; }
QCheckBox::indicator:checked { background: #e0651c; border-color: #e0651c; }
QPushButton#floatGenerate { background: #e0651c; color: #ffffff; border: none; border-radius: 8px;
                            font-size: 12px; font-weight: 600; padding: 6px 0; }
QPushButton#floatGenerate:hover { background: #cc5a17; }
QPushButton#floatGenerate:disabled { background: #e9dcd0; color: #a89c90; }
QTableWidget#floatTasks { border: none; background: transparent; font-size: 12px; color: #2a2a2c; }
QTableWidget#floatTasks::item { border-bottom: 1px solid #f1ebe3; padding: 0 2px; }
QTableWidget#floatTasks::item:selected { background: #f4e7db; color: #18181a; }
"""
