"""Dedicated batch-recognition workspace: one batch is one task, not many loose sessions."""
from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame,
                             QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar,
                             QSizePolicy, QSplitter, QStackedWidget, QTabWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWIDGETSIZE_MAX, QWidget)

from ...speech_recognition.exporting import is_chinese, srt_text, translated_lines
from ...speech_recognition.formats import ALLOWED_SUFFIXES, dialog_filter
from ...speech_recognition.languages import LANGUAGE_CHOICES
from ...ui import DANGER, MUTED, SUCCESS, button, label
from ..api_client import checked
from ..audio_output import SystemAudioOutput

STATUS_TEXT = {"pending": "等待", "importing": "导入中", "running": "识别中", "done": "完成",
               "failed": "失败", "cancelled": "已跳过"}
BUSY = ("queued", "running", "cancelling")
FORMATS = (("SRT 字幕", "srt"), ("SRT + TXT", "srt+txt"), ("TXT 文本", "txt"))


class SubtitlePreview(QPlainTextEdit):
    """Read-only SRT preview that turns a click into the clicked subtitle line."""

    lineClicked = pyqtSignal(object)

    def __init__(self, text=""):
        self._ranges = []
        super().__init__()
        self.setReadOnly(True)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        if text:
            self.setPlainText(text)

    def setPlainText(self, text):
        super().setPlainText(text)
        self._ranges = []

    def clear(self):
        super().clear()
        self._ranges = []

    def set_subtitle_text(self, text):
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        super().setPlainText(text)
        self._ranges = self._parse_ranges(text)

    def _parse_ranges(self, text):
        ranges = []
        offset = 0
        for position, block in enumerate(re.split(r"\n\s*\n", text.strip()), start=1):
            if not block.strip():
                continue
            start_char = text.find(block, offset)
            if start_char < 0:
                continue
            end_char = start_char + len(block)
            offset = end_char
            lines = block.splitlines()
            timing_index = next((i for i, value in enumerate(lines) if "-->" in value), None)
            if timing_index is None:
                continue
            match = re.search(
                r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})",
                lines[timing_index])
            if not match:
                continue
            index = position
            if lines[0].strip().isdigit():
                index = int(lines[0].strip())
            ranges.append((start_char, end_char, {
                "index": index, "start": self._seconds(match.group(1)),
                "end": self._seconds(match.group(2))}))
        return ranges

    @staticmethod
    def _seconds(value):
        parts = value.replace(",", ".").split(":")
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) >= 3 else 0
        return hours * 3600 + minutes * 60 + seconds

    def line_at(self, position):
        for start, end, line in getattr(self, "_ranges", []):
            if start <= position <= end:
                return line
        return None

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        line = self.line_at(self.cursorForPosition(event.position().toPoint()).position())
        if line is not None:
            self.lineClicked.emit(line)


class BatchRecognitionPanel(QWidget):
    """Queue several files and inspect each subtitle without leaving the mode."""

    def __init__(self, owner, controller):
        super().__init__(owner)
        self.owner, self.controller = owner, controller
        self.batch = None
        self.batch_id = None
        self.pending = []
        self.polling = False
        self.active = False
        self.preview_sequence = 0
        self.session_cache = {}
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(button("返回", owner.open_creation_home, "back", "ghost"))
        self.title = label("批量识别", "dialogTitle")
        header.addWidget(self.title)
        header.addStretch()
        self.batch_label = label("尚未开始", "hint")
        self.batch_label.setMaximumWidth(300)
        self.batch_label.setVisible(False)
        header.addWidget(self.batch_label)
        self.copy_id_button = button("复制批次 ID", self.copy_batch_id, "copy", "ghost")
        self.copy_id_button.setVisible(False)
        header.addWidget(self.copy_id_button)
        self.mcp_button = button("复制 MCP 指令", self.copy_mcp_instruction, "copy", "ghost")
        self.mcp_button.setVisible(False)
        header.addWidget(self.mcp_button)
        self.add_button = button("添加音视频", self.add_files, "plus")
        header.addWidget(self.add_button)
        self.new_button = button("新建批次", self.new_list, "retry", "ghost")
        header.addWidget(self.new_button)
        outer.addLayout(header)

        hint = label("拖入音频或视频，或点「添加音视频」；完成后自动导出字幕。"
                     "识别后选中一行可在右侧查看字幕，双击打开完整识别页。", "hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(label("音频语言", "muted"))
        self.language = QComboBox()
        self.language.setMinimumWidth(132)
        for text, value in LANGUAGE_CHOICES:  # 服务还没答复也要能选，不能是个空下拉框
            self.language.addItem(text, value)
        options.addWidget(self.language)
        options.addSpacing(8)
        options.addWidget(label("导出格式", "muted"))
        self.formats = QComboBox()
        for text, value in FORMATS:
            self.formats.addItem(text, value)
        self.formats.setMinimumWidth(118)
        options.addWidget(self.formats)
        options.addSpacing(8)
        self.dedupe = QCheckBox("清理连续重复")
        self.dedupe.setChecked(True)
        self.dedupe.setToolTip("把同一词连续出现 6 次以上的片段折叠为一次，并合并相邻的重复行；"
                               "适合 ASR 在音乐、噪声上反复吐同一句的情况。音频里本来就有意的重复也会被折叠。")
        options.addWidget(self.dedupe)
        options.addStretch()
        outer.addLayout(options)

        body = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left.setMinimumWidth(520)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 12)  # 与右侧预览卡片的内边距齐平，两行操作落在同一高度
        left_layout.setSpacing(8)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文件", "文件夹", "状态", "字幕文件", "译文"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAcceptDrops(False)
        self.table.viewport().setAcceptDrops(False)
        self.table.setWordWrap(False)
        headers = self.table.horizontalHeader()
        headers.setMinimumSectionSize(56)
        headers.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        headers.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        headers.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        headers.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        headers.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        headers.setStretchLastSection(True)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 56)
        self.table.setColumnWidth(3, 108)
        self.table.setColumnWidth(4, 72)
        self.table.setToolTip("单击查看右侧字幕，双击打开这条记录的完整识别页。")
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda row, _column: self.view_result(row))

        self.drop_panel = QFrame()
        self.drop_panel.setObjectName("batchDrop")
        self.drop_panel.setStyleSheet(
            "QFrame#batchDrop { background: #fffdfa; border: 1px dashed #d8d1c6;"
            " border-radius: 12px; }")
        self.drop_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.drop_panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        drop_layout = QVBoxLayout(self.drop_panel)
        drop_layout.setContentsMargins(24, 24, 24, 24)
        drop_layout.setSpacing(6)
        drop_layout.addStretch(1)
        self.drop_icon = QLabel("＋")
        self.drop_icon.setStyleSheet("color: #b18a63; font-size: 34px; font-weight: 300;")
        self.drop_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(self.drop_icon)
        drop_title = label("把音频或视频拖到这里", "groupTitle")
        drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(drop_title)
        drop_hint = label("或点击上方「添加音视频」，添加后点「开始识别」", "hint")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_hint.setWordWrap(True)
        drop_layout.addWidget(drop_hint)
        drop_layout.addStretch(1)

        self.queue = QStackedWidget()
        self.queue.addWidget(self.table)
        self.queue.addWidget(self.drop_panel)
        self.queue.setCurrentIndex(1)
        left_layout.addWidget(self.queue)
        self.fit_to_rows()

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        left_layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(34)
        self.status.setVisible(False)
        left_layout.addWidget(self.status)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_button = button("开始识别", self.start, "recognize", "primary")
        actions.addWidget(self.start_button)
        self.remove_button = button("移除选中", self.remove_selected, "trash", "ghost")
        actions.addWidget(self.remove_button)
        self.stop_button = button("停止", self.cancel, "stop", "ghost")
        actions.addWidget(self.stop_button)
        self.retry_button = button("重试未完成", self.retry, "retry", "ghost")
        actions.addWidget(self.retry_button)
        actions.addStretch()
        left_layout.addLayout(actions)
        body.addWidget(left)

        preview = QFrame()
        preview.setObjectName("batchPreview")
        preview.setMinimumWidth(290)
        preview.setStyleSheet("QFrame#batchPreview { background: #fffdfa; border: 1px solid #e5e0d8;"
                              " border-radius: 10px; }")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(14, 12, 14, 12)
        preview_layout.setSpacing(8)
        preview_header = QHBoxLayout()
        preview_header.addWidget(label("字幕预览", "groupTitle"))
        preview_header.addWidget(label("点击行播放", "hint"))
        preview_header.addStretch()
        self.refresh_button = button("刷新", self.refresh_preview, "retry", "ghost")
        preview_header.addWidget(self.refresh_button)
        preview_layout.addLayout(preview_header)
        self.preview_path = QLabel("尚未选中识别记录")
        self.preview_path.setProperty("role", "hint")
        self.preview_path.setWordWrap(False)
        self.preview_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_layout.addWidget(self.preview_path)
        self.preview_tabs = QTabWidget()
        self.original_view = SubtitlePreview()
        self.original_view.setPlaceholderText("这里显示导出的 SRT/TXT 字幕文件。")
        self.original_view.setToolTip("点击字幕行播放对应音频片段。")
        self.translation_view = SubtitlePreview()
        self.translation_view.setPlaceholderText("这里显示识别记录里的译文。")
        self.translation_view.setToolTip("点击字幕行播放对应音频片段。")
        for view in (self.original_view, self.translation_view):
            view.lineClicked.connect(self.play_preview_line)
        self.preview_tabs.addTab(self.original_view, "字幕文件")
        self.preview_tabs.addTab(self.translation_view, "译文")
        preview_layout.addWidget(self.preview_tabs, 1)
        preview_actions = QHBoxLayout()
        preview_actions.setSpacing(8)
        self.export_translation_button = button(
            "导出译文", self.export_translation, "export", "ghost")
        self.export_translation_button.setToolTip("只导出选中记录的译文，不混入原文。")
        preview_actions.addWidget(self.export_translation_button)
        self.open_file_button = button("打开字幕", self.open_subtitle_file, "import", "ghost")
        self.open_file_button.setToolTip("打开当前记录的 SRT/TXT 字幕文件")
        preview_actions.addWidget(self.open_file_button)
        self.open_folder_button = button("文件夹", self.open_folder, "folder", "ghost")
        self.open_folder_button.setToolTip("打开当前文件所在的文件夹")
        preview_actions.addWidget(self.open_folder_button)
        preview_actions.addStretch()
        self.open_result_button = button("识别页", self.view_selected, "recognize", "ghost")
        self.open_result_button.setToolTip("在完整识别页打开当前记录")
        preview_actions.addWidget(self.open_result_button)
        preview_layout.addLayout(preview_actions)
        body.addWidget(preview)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([640, 340])
        outer.addWidget(body, 1)
        self.body = body
        self.preview = preview
        preview.setVisible(False)

        self.preview_player = QMediaPlayer(self)
        self.preview_output = SystemAudioOutput(self)
        self.preview_player.setAudioOutput(self.preview_output)
        self.preview_player.mediaStatusChanged.connect(self.preview_media_status)
        self.preview_player.positionChanged.connect(self.preview_position)
        self.preview_player.errorOccurred.connect(
            lambda _, message: self.status.setText("播放失败：" + message))
        self.preview_range = None
        self.preview_pending = False
        self.preview_key = None

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.update_actions()

    # ------------------------------------------------------------------ lifecycle

    def open(self, identity=None):
        self.active = True
        self.timer.stop()
        self.polling = False
        self.sync_options()
        if not (self.controller.options or {}).get("languages"):
            # 识别页没打开时控制器不会拉设置，批量页自己补一次，否则下拉框和模型状态都是空的。
            self.controller.load_options(self.sync_options, self.options_failed)
        if identity:
            self.load_batch(identity)
        else:
            self.new_list()
        if self.batch and self.batch.get("status") in BUSY and not self.timer.isActive():
            self.timer.start()
        self.update_actions()

    def options_failed(self, message):
        """取设置失败只在服务已连接时提示；未连接时交给「开始识别」去说明。"""
        if self.active and self.owner.ready:
            self.status.setText(str(message))
            self.status.setVisible(True)

    def resume(self):
        self.active = True
        if self.batch and self.batch.get("status") in BUSY and not self.timer.isActive():
            self.timer.start()

    def pause(self):
        self.active = False
        self.timer.stop()
        if hasattr(self, "preview_player"):
            self.stop_preview()

    def sync_options(self):
        """服务端语言表到了就换上；还没到就保持本地那份，绝不清空下拉框。"""
        options = self.controller.options or {}
        selection = options.get("selection") or {}
        preferred = (self.batch or {}).get("language") or selection.get("language") or "auto"
        choices = [tuple(item) for item in (options.get("languages") or [])]
        if choices:
            self.controller.fill(self.language, choices, preferred=preferred)
            return
        self.language.blockSignals(True)
        self.language.setCurrentIndex(max(0, self.language.findData(preferred)))
        self.language.blockSignals(False)

    def load_batch(self, identity):
        self.batch_id = identity
        self.batch_label.setText("读取批次…")
        self.batch_label.setVisible(True)
        self.status.setText("正在读取批次记录…")
        self.status.setVisible(True)
        self.preview.setVisible(False)
        self.controller.batch_state(identity, self.show_batch, self.show_failure)

    # -------------------------------------------------------------------- files

    def add_files(self):
        if self.busy():
            self.status.setText("批量识别正在进行，请等它完成或先停止再添加文件。")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要批量识别的音频或视频", "",
                                                dialog_filter())
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        if self.busy():
            self.status.setText("批量识别正在进行，请等它完成或先停止再添加文件。")
            return 0
        valid = []
        rejected = 0
        for value in paths:
            source = Path(value)
            try:
                resolved = source.resolve()
            except OSError:
                rejected += 1
                continue
            if not resolved.is_file() or resolved.suffix.lower() not in ALLOWED_SUFFIXES:
                rejected += 1
                continue
            valid.append(str(resolved))
        if not valid:
            self.status.setText("没有可加入的音频或视频文件；支持常见音频和视频格式。")
            self.status.setVisible(True)
            return 0
        if self.batch is not None:
            self.new_list()
        existing = {str(Path(value).resolve()).lower() for value in self.pending}
        accepted = 0
        for value in valid:
            key = str(Path(value)).lower()
            if key in existing:
                continue
            existing.add(key)
            self.pending.append(value)
            accepted += 1
        if accepted:
            self.status.setText(f"已选择 {len(self.pending)} 个文件，点「开始识别」按顺序处理。")
        elif rejected:
            self.status.setText("没有可加入的音频或视频文件；支持常见音频和视频格式。")
        else:
            self.status.setText("这些文件已经在批量列表里了。")
        self.render_table()
        self.status.setVisible(True)
        self.update_actions()
        return accepted

    @staticmethod
    def dropped_paths(mime):
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        paths = self.dropped_paths(event.mimeData())
        if any(Path(value).suffix.lower() in ALLOWED_SUFFIXES and Path(value).is_file()
               for value in paths):
            event.acceptProposedAction()
            self.status.setText("松开即可加入批量列表…")
            self.status.setVisible(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        if self.items() or self.batch:
            self.status.setText("把音频/视频拖到这里，或点「添加音视频」；添加后点「开始识别」。")
        else:
            self.status.setVisible(False)

    def dropEvent(self, event):
        paths = self.dropped_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.add_paths(paths)
        else:
            event.ignore()

    def remove_selected(self):
        if self.busy() or self.batch is not None:
            return
        row = self.table.currentRow()
        if 0 <= row < len(self.pending):
            del self.pending[row]
            self.render_table()
            if self.pending:
                self.status.setText(f"已选择 {len(self.pending)} 个文件，点「开始识别」按顺序处理。")
                self.status.setVisible(True)
            else:
                self.status.setVisible(False)
            self.update_actions()

    def new_list(self):
        if self.busy():
            return
        self.timer.stop()
        self.batch, self.batch_id, self.polling = None, None, False
        self.pending = []
        self.session_cache.clear()
        self.batch_label.setText("尚未开始")
        self.batch_label.setToolTip("")
        self.batch_label.setVisible(False)
        self.copy_id_button.setEnabled(False)
        self.copy_id_button.setVisible(False)
        self.mcp_button.setEnabled(False)
        self.mcp_button.setVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.status.setText("")
        self.status.setVisible(False)
        self.preview.setVisible(False)
        self.render_table()
        self.update_actions()
        self.clear_preview("尚未选中识别记录")

    # ------------------------------------------------------------------ actions

    def start(self):
        if self.busy() or not self.pending:
            if not self.busy():
                self.status.setText("先点「添加音视频」选择要识别的文件。")
            return
        if not self.owner.ready:
            self.status.setText("请先连接本地服务。")
            return
        if not (self.controller.options or {}).get("asr"):
            self.status.setText("还没有识别模型：请在设置 → 模型下载中安装 Qwen3-ASR 后重试。")
            return
        body = {"paths": list(self.pending),
                "language": self.language.currentData() or "auto",
                "format": self.formats.currentData() or "srt",
                "dedupe": self.dedupe.isChecked()}
        self.start_button.setEnabled(False)
        self.status.setText("正在创建批量识别任务…")
        self.controller.start_batch(body, self.started, self.show_failure)

    def started(self, state):
        self.pending = []
        self.show_batch(state)
        self.owner.load_saved_jobs()

    def cancel(self):
        identity = self.batch_id
        if not identity:
            return
        self.stop_button.setEnabled(False)
        self.status.setText("正在停止批量识别…")
        self.controller.cancel_batch(identity, self.show_batch, self.show_failure)

    def retry(self):
        identity = self.batch_id
        if not identity:
            return
        self.retry_button.setEnabled(False)
        self.status.setText("正在继续未完成的文件…")
        self.controller.retry_batch(identity, self.started, self.show_failure)

    def copy_batch_id(self):
        if not self.batch_id:
            return
        QGuiApplication.clipboard().setText(self.batch_id)
        self.status.setText("批次 ID 已复制：" + self.batch_id)

    def copy_mcp_instruction(self):
        if not self.batch_id:
            return
        text = (f"翻译声笺批量识别批次 {self.batch_id}：只处理 status=done 的会话，"
                "逐条 get_subtitle，用你的模型翻成目标语言，保持每行 index，"
                "再逐条 save_translation；不要调用 translate_subtitle。")
        QGuiApplication.clipboard().setText(text)
        self.status.setText("MCP 翻译指令已复制，可直接发给外部 Agent。")

    def view_selected(self):
        row = self.table.currentRow()
        if self.batch and 0 <= row < len(self.batch.get("items") or []):
            self.view_result(row)

    def view_result(self, row):
        items = (self.batch or {}).get("items") or []
        if not 0 <= row < len(items):
            return
        identity = items[row].get("session_id")
        if not identity:
            self.status.setText("这个文件还没有识别结果。")
            return
        self.controller.open_batch_result(identity)

    def open_folder(self):
        path = self.selected_path()
        if not path:
            self.status.setText("先选中一行，再打开它所在的文件夹。")
            return
        folder = Path(path).parent
        if not folder.is_dir():
            self.status.setText(f"文件夹不存在：{folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def selected_path(self):
        item = self.selected_item()
        return (item or {}).get("path")

    def selected_session(self):
        item = self.selected_item()
        return (item or {}).get("session_id")

    def selected_export(self):
        item = self.selected_item()
        for value in (item or {}).get("exported") or []:
            if Path(value).is_file():
                return value
        return None

    def open_subtitle_file(self):
        path = self.selected_export()
        if not path:
            self.status.setText("这一行还没有导出的字幕文件；识别完成后会自动写到原文件夹。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def export_translation(self):
        """Export the selected record's translated text as its own SRT."""
        item = self.selected_item()
        identity = (item or {}).get("session_id")
        if not identity:
            self.status.setText("这一行还没有识别结果，不能导出译文。")
            return
        state = self.session_cache.get(identity)
        if state:
            self.write_translation(state, item)
            return
        self.status.setText("正在读取译文…")
        self.controller.translation_session(
            identity,
            lambda record: self.write_translation(record.get("state") or {}, item),
            lambda message: self.status.setText("读取译文失败：" + str(message)))

    def write_translation(self, state, item):
        translation = state.get("translation") or {}
        lines = translated_lines(translation)
        if not lines:
            self.status.setText("这条记录还没有译文；先在识别页翻译，或让 Agent 回写。")
            return
        target = translation.get("target") or "译文"
        chinese = is_chinese(target)
        label = "中文字幕" if chinese else "译文"
        source = Path((item or {}).get("path") or "")
        folder = source.parent if source.parent.is_dir() else Path.home()
        stem = source.stem or (item or {}).get("name") or "识别结果"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出" + label, str(folder / f"{stem}.{target}.srt"), "SRT 字幕 (*.srt)")
        if not path:
            return
        try:
            Path(path).write_text(srt_text(lines), encoding="utf-8")
        except OSError as exc:
            self.status.setText("导出失败：" + str(exc))
            return
        self.status.setText(label + "已导出：" + path)

    # ------------------------------------------------------------------- render

    def show_batch(self, state):
        previous = self.batch_id
        fresh = self.batch is None or previous != self.batch_id
        selected = self.selected_session()
        self.batch, self.batch_id, self.pending = state, state["id"], []
        if fresh:
            self.stop_preview()
        language, export = state.get("language") or "auto", state.get("format") or "srt"
        # 从最近作品重开时 batch_id 在 load_batch 里已经就位，previous 会相等；
        # 只要下拉框还没显示这批的语言/格式，就摆回去。
        if previous != self.batch_id or self.language.currentData() != language:
            self.set_option(self.language, language)
        if previous != self.batch_id or self.formats.currentData() != export:
            self.set_option(self.formats, export)
        if fresh:
            self.dedupe.setChecked(bool(state.get("dedupe", True)))
        self.batch_label.setText(state.get("name") or f"批量识别 · {state.get('total', 0)} 个文件")
        self.batch_label.setToolTip("批次 ID：" + state["id"])
        self.batch_label.setVisible(True)
        self.copy_id_button.setEnabled(True)
        self.copy_id_button.setVisible(True)
        self.mcp_button.setEnabled(True)
        self.mcp_button.setVisible(True)
        if self.preview.isHidden():
            # 只分一次：轮询每秒都会走这里，不能把拖过的分栏宽弹回去。
            self.preview.setVisible(True)
            self.body.setSizes([640, 340])
        self.progress.setVisible(True)
        self.status.setVisible(True)
        self.render_table(selected)
        if self.table.currentRow() < 0:
            first = next((row for row, item in enumerate(self.items()) if item.get("session_id")), -1)
            if first >= 0:
                self.table.setCurrentCell(first, 0)
        self.render_progress()
        self.update_actions()
        if state.get("status") in BUSY:
            if self.active and not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        current = self.selected_session()
        if current and (selected is None or current != selected):
            self.refresh_preview()

    @staticmethod
    def set_option(combo, value):
        combo.blockSignals(True)
        index = combo.findData(value)
        if index < 0:
            combo.addItem(str(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def items(self):
        if self.batch is not None:
            return self.batch.get("items") or []
        return [{"path": path, "name": Path(path).stem, "status": "pending", "exported": [],
                 "export_error": ""} for path in self.pending]

    def fit_to_rows(self):
        """列表吃满左栏剩余高度，只按行数给出一个下限，短列表不再在下方留白。"""
        items = self.items()
        if not items:
            floor = 380
            self.queue.setCurrentIndex(1)
        else:
            floor = 30 + max(3, min(len(items), 9)) * 36 + 2
            self.queue.setCurrentIndex(0)
        self.queue.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.queue.setMinimumHeight(floor)
        self.queue.setMaximumHeight(QWIDGETSIZE_MAX)

    def render_table(self, selected=None):
        scroll = self.table.verticalScrollBar().value()
        items = self.items()
        self.table.blockSignals(True)
        self.table.setRowCount(len(items))
        self.fit_to_rows()
        for index, item in enumerate(items):
            name = Path(item.get("path") or "").name or item.get("name") or "未命名音频"
            file_item = QTableWidgetItem(self.compact_name(name))
            file_item.setToolTip(item.get("path") or "")
            folder = Path(item.get("path") or "").parent
            folder_item = QTableWidgetItem(self.compact_name(folder.name or str(folder), 14, False))
            folder_item.setToolTip(str(folder))
            folder_item.setForeground(QColor(MUTED))
            status = STATUS_TEXT.get(item.get("status"), item.get("status") or "")
            status_item = QTableWidgetItem(status)
            status_item.setToolTip(self.item_tooltip(item))
            if item.get("status") == "failed":
                status_item.setForeground(QColor(DANGER))
            elif item.get("status") == "done":
                status_item.setForeground(QColor(SUCCESS))
            export_item = QTableWidgetItem(self.export_text(item))
            export_item.setToolTip("\n".join(item.get("exported") or []) or (item.get("export_error") or ""))
            if item.get("export_error"):
                export_item.setForeground(QColor(DANGER))
            translation_item = QTableWidgetItem(self.translation_text(item))
            translation_item.setToolTip(self.translation_tooltip(item))
            for column, value in enumerate((file_item, folder_item, status_item, export_item, translation_item)):
                value.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, column, value)
        if selected:
            row = next((i for i, item in enumerate(items)
                        if item.get("session_id") == selected), -1)
            if row >= 0:
                self.table.setCurrentCell(row, 0)
        elif self.table.currentRow() >= len(items) - 1:
            self.table.setCurrentCell(max(0, len(items) - 1), 0) if items else self.table.clearSelection()
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)

    @staticmethod
    def compact_name(name, limit=30, keep_suffix=True):
        """Keep the extension visible, shorten the middle with an ellipsis."""
        value = str(name or "")
        if len(value) <= limit:
            return value
        suffix = Path(value).suffix if keep_suffix else ""
        head = value[:max(1, limit - len(suffix) - 1)].rstrip(" ._-")
        return f"{head}…{suffix}" if suffix else value[:limit - 1] + "…"

    @staticmethod
    def translation_text(item):
        total = int(item.get("line_count") or 0)
        translated = int(item.get("translated_lines") or 0)
        if not item.get("session_id"):
            return "—"
        return f"{translated}/{total}" if total else "—"

    @staticmethod
    def translation_tooltip(item):
        if not item.get("session_id"):
            return ""
        target = item.get("translation_target") or "目标语言未设置"
        return f"{item.get('translated_lines', 0)} 行已有译文 · {target}"

    @staticmethod
    def export_text(item):
        if item.get("export_error"):
            return "导出失败"
        files = item.get("exported") or []
        if files:
            return "，".join(BatchRecognitionPanel.compact_name(Path(value).name, 24) for value in files)
        return "—"

    @staticmethod
    def item_tooltip(item):
        parts = [item.get("path") or ""]
        if item.get("error"):
            parts.append("错误：" + str(item["error"]))
        if item.get("hint"):
            parts.append("建议：" + str(item["hint"]))
        if item.get("export_error"):
            parts.append("导出：" + str(item["export_error"]))
        return "\n".join(part for part in parts if part)

    def render_progress(self):
        state = self.batch
        if not state:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            return
        total = max(1, int(state.get("total") or 0))
        processed = int(state.get("done") or 0) + int(state.get("failed") or 0) + int(state.get("cancelled") or 0)
        self.progress.setRange(0, total)
        self.progress.setValue(min(total, processed))
        self.status.setText(self.describe(state))

    def describe(self, state):
        status = state.get("status")
        total, done, failed = int(state.get("total") or 0), int(state.get("done") or 0), int(state.get("failed") or 0)
        export_errors = sum(1 for item in state.get("items") or [] if item.get("export_error"))
        if status == "queued":
            return f"准备开始：共 {total} 个文件。"
        if status in ("running", "cancelling"):
            what = "正在停止…" if status == "cancelling" else f"正在识别：{state.get('current_name') or '…'}"
            text = f"{what} · 已完成 {done}/{total}"
            if failed:
                text += f"，失败 {failed}"
            return text
        if status == "done":
            text = f"批量完成：成功 {done}/{total}"
            if failed:
                text += f"，失败 {failed}"
            if export_errors:
                text += f"，{export_errors} 个字幕导出失败"
            elif done:
                text += "，字幕已导出到各文件所在文件夹"
            return text + "。"
        if status == "cancelled":
            return f"批量已停止：完成 {done}/{total}，其余文件未处理。"
        if status == "interrupted":
            return "上次批量识别被中断；点「重试未完成」继续处理剩余文件。"
        if status == "failed":
            return "批量任务出错：" + (state.get("error") or "原因未知") + "。可点「重试未完成」再试。"
        return f"已完成 {done}/{total}。"

    def show_failure(self, message):
        self.status.setText(str(message))
        self.status.setVisible(True)
        self.polling = False
        if not self.batch:
            self.preview.setVisible(False)
            self.progress.setVisible(False)
            self.batch_label.setVisible(bool(self.batch_id))
            self.copy_id_button.setVisible(bool(self.batch_id))
            self.mcp_button.setVisible(bool(self.batch_id))
        self.update_actions()

    # -------------------------------------------------------------------- state

    def busy(self):
        return bool(self.batch and self.batch.get("status") in BUSY)

    def selected_item(self):
        row = self.table.currentRow()
        items = self.items()
        return items[row] if 0 <= row < len(items) else None

    def update_actions(self):
        busy = self.busy()
        state = self.batch or {}
        pending = self.batch is None and self.pending
        retryable = bool(self.batch) and not busy and any(
            item.get("status") in ("failed", "cancelled", "pending") for item in state.get("items") or [])
        item = self.selected_item()
        self.add_button.setEnabled(not busy)
        self.new_button.setVisible(self.batch is not None)
        self.new_button.setEnabled(not busy)

        self.start_button.setVisible(self.batch is None and bool(pending))
        self.start_button.setEnabled(bool(pending) and not busy)
        self.remove_button.setVisible(self.batch is None and bool(pending))
        self.remove_button.setEnabled(bool(pending) and not busy and self.table.currentRow() >= 0)

        self.stop_button.setVisible(bool(self.batch) and state.get("status") in ("queued", "running"))
        self.stop_button.setEnabled(bool(self.batch) and state.get("status") in ("queued", "running"))
        self.retry_button.setVisible(retryable)
        self.retry_button.setEnabled(retryable)

        self.open_file_button.setEnabled(bool(item and item.get("exported")))
        self.open_folder_button.setEnabled(bool(item))
        self.export_translation_button.setEnabled(bool(item and item.get("session_id")))
        self.open_result_button.setEnabled(bool(item and item.get("session_id")))
        self.refresh_button.setEnabled(bool(item))
        for control in (self.language, self.formats):
            control.setEnabled(not busy)
        # 连续重复清理是建批次时的选项：批次一旦存在就按记录里的值走，避免改了下一次却不变。
        self.dedupe.setEnabled(not busy and self.batch is None)

    def poll(self):
        if not self.active or not self.batch_id or self.polling:
            return
        if not self.busy():
            self.timer.stop()
            return
        self.polling = True
        self.controller.batch_state(self.batch_id, self.polled, self.poll_failed)

    def polled(self, state):
        self.polling = False
        self.show_batch(state)
        if state.get("status") not in BUSY:
            self.owner.load_saved_jobs()

    def poll_failed(self, message):
        self.polling = False
        self.status.setText(str(message))
        self.status.setVisible(True)

    # ------------------------------------------------------------------- preview

    def selection_changed(self):
        self.update_actions()
        if self.preview.isVisible():
            self.refresh_preview()

    def clear_preview(self, message="尚未选中识别记录"):
        self.stop_preview()
        self.preview_path.setText(message)
        self.preview_path.setToolTip("")
        self.original_view.clear()
        self.translation_view.clear()

    def preview_audio_source(self, state, item):
        state = state or {}
        audio = state.get("audio")
        if audio:
            local = Path(self.owner.data_dir) / "recognition" / str(audio)
            if local.is_file():
                return QUrl.fromLocalFile(str(local))
            if state.get("audio_url") and getattr(self.owner, "url", ""):
                return QUrl(str(self.owner.url) + str(state["audio_url"]))
        path = Path((item or {}).get("path") or "")
        if path.is_file():
            return QUrl.fromLocalFile(str(path))
        return None

    def play_preview_line(self, line):
        item = self.selected_item()
        if not item or not line:
            return
        source = self.preview_audio_source(self.session_cache.get(item.get("session_id")), item)
        if source is None:
            self.status.setText("找不到这条记录对应的音频，无法播放。")
            return
        start = max(0.0, float(line.get("start") or 0))
        end = max(start, float(line.get("end") or start))
        key = (item.get("session_id"), line.get("index"), round(start, 3))
        if self.preview_key == key and self.preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.stop_preview("已停止播放。")
            return
        self.preview_range = (start, end)
        self.preview_key = key
        self.preview_pending = True
        if self.preview_player.source() != source:
            self.preview_player.setSource(source)
            self.preview_player.play()
        else:
            self.preview_player.setPosition(round(start * 1000))
            self.preview_pending = False
            self.preview_player.play()
        self.status.setText(
            f"播放第 {line.get('index')} 行 · {start:.2f}–{end:.2f} 秒（再点一次停止）")

    def preview_media_status(self, status):
        if not self.preview_pending or not self.preview_range:
            return
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.preview_pending = False
            self.preview_player.setPosition(round(self.preview_range[0] * 1000))
            self.preview_player.play()

    def preview_position(self, position):
        if not self.preview_range:
            return
        if position >= round(self.preview_range[1] * 1000):
            self.stop_preview("片段播放结束。")

    def stop_preview(self, message=""):
        self.preview_range = None
        self.preview_pending = False
        self.preview_key = None
        self.preview_player.stop()
        if message:
            self.status.setText(message)

    def refresh_preview(self):
        item = self.selected_item()
        if not item:
            self.clear_preview()
            return
        export_path = self.selected_export()
        if export_path:
            path = Path(export_path)
            text = f"{path.name} · {path.parent.name}"
            width = max(120, self.preview_path.width() - 2)
            self.preview_path.setText(
                self.preview_path.fontMetrics().elidedText(
                    text, Qt.TextElideMode.ElideRight, width))
            self.preview_path.setToolTip(str(path))
        else:
            self.preview_path.setText("未找到导出的字幕文件")
            self.preview_path.setToolTip("显示识别会话里的字幕内容")
        if export_path:
            try:
                self.original_view.set_subtitle_text(Path(export_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                self.original_view.setPlainText("无法读取字幕文件：" + str(exc))
        else:
            self.original_view.clear()
        identity = item.get("session_id")
        if not identity:
            self.translation_view.clear()
            self.clear_preview("这个文件还没有识别结果")
            self.preview_path.setText("尚未识别")
            return
        cached = self.session_cache.get(identity)
        if cached:
            self.fill_session_preview(cached)
            if int(item.get("translated_lines") or 0) > int(cached.get("_translated") or 0):
                self.session_cache.pop(identity, None)
                self.fetch_preview(identity)
            return
        self.translation_view.setPlainText("正在读取识别记录…")
        self.fetch_preview(identity)

    def fetch_preview(self, identity):
        self.preview_sequence += 1
        sequence = self.preview_sequence
        def done(record):
            if sequence != self.preview_sequence:
                return
            state = record.get("state") or {}
            state["_translated"] = sum(1 for line in (state.get("translation") or {}).get("lines") or []
                                       if str(line.get("translated") or "").strip())
            self.session_cache[identity] = state
            self.fill_session_preview(state)
        def failed(message):
            if sequence == self.preview_sequence:
                self.translation_view.setPlainText("读取失败：" + str(message))
        self.controller.translation_session(identity, done, failed)

    def fill_session_preview(self, state):
        lines = state.get("lines") or []
        if not self.original_view.toPlainText().strip():
            self.original_view.set_subtitle_text(srt_text(lines) if lines else "这条记录没有字幕行。")
        translation = state.get("translation") or {}
        target = translation.get("target") or ""
        self.export_translation_button.setText(
            "导出中文字幕" if is_chinese(target) else "导出译文")
        translated = translated_lines(translation)
        if translated:
            self.translation_view.set_subtitle_text(srt_text(translated))
        else:
            self.translation_view.setPlainText("还没有译文。可在识别页翻译，或让外部 Agent 按批次回写。")
