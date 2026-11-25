"""Batch recognition queue: pick several files, watch each one finish, keep the SRT.

The dialog is a view over the queue stored by the local service. Closing it while
a batch runs only hides the progress; reopening shows the same record, and the
service keeps going until it is finished or stopped.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog,
                             QHBoxLayout, QHeaderView, QLabel, QProgressBar, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

from ...speech_recognition.formats import ALLOWED_SUFFIXES, dialog_filter
from ...ui import DANGER, MUTED, STYLE, SUCCESS, button, label

STATUS_TEXT = {"pending": "等待", "importing": "导入中", "running": "识别中", "done": "完成",
               "failed": "失败", "cancelled": "已跳过"}
BUSY = ("queued", "running", "cancelling")
FORMATS = (("SRT 字幕", "srt"), ("SRT + TXT", "srt+txt"), ("TXT 文本", "txt"))


class BatchRecognitionDialog(QDialog):
    def __init__(self, owner, controller):
        super().__init__(owner)
        self.owner, self.controller = owner, controller
        self.batch = None
        self.batch_id = None
        self.pending = []
        self.polling = False
        self.opened = False
        self.setWindowTitle("批量识别")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(880, 480)
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        self._fit_key = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = label("批量识别", "dialogTitle")
        header.addWidget(self.title)
        header.addStretch()
        outer.addLayout(header)
        hint = label("把音频或视频拖到这里，或点「添加音频/视频」；逐个识别，"
                     "每完成一个自动把字幕导出到它所在的文件夹。"
                     "同名字幕不会被覆盖：内容相同直接复用，内容不同则另存为「文件名.识别字幕」。", "hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.add_button = button("添加音频/视频", self.add_files, "plus")
        toolbar.addWidget(self.add_button)
        self.remove_button = button("移除选中", self.remove_selected, "trash", "ghost")
        toolbar.addWidget(self.remove_button)
        self.new_button = button("新建列表", self.new_list, "retry", "ghost")
        toolbar.addWidget(self.new_button)
        toolbar.addStretch()
        outer.addLayout(toolbar)

        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(label("音频语言", "muted"))
        self.language = QComboBox()
        self.language.setMinimumWidth(132)
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
        self.dedupe.setToolTip("把同一词连续出现 6 次以上的片段折叠为一次，并合并相邻的重复行。")
        options.addWidget(self.dedupe)
        options.addStretch()
        options_hint = label("自动导出到原文件所在文件夹", "hint")
        self.options_hint = options_hint
        options.addWidget(options_hint)
        outer.addLayout(options)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文件", "文件夹", "状态", "字幕导出"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAcceptDrops(False)
        self.table.viewport().setAcceptDrops(False)
        self.table.setWordWrap(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, 210)
        self.table.setColumnWidth(2, 88)
        self.table.setColumnWidth(3, 210)
        self.table.setToolTip("双击已完成的行可以打开该文件的识别结果。")
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.cellDoubleClicked.connect(lambda row, _column: self.view_result(row))
        outer.addWidget(self.table)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)
        self.status = QLabel("把音频/视频拖到这里，或点「添加音频/视频」；添加后点「开始识别」。")
        self.status.setProperty("role", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(36)
        outer.addWidget(self.status)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_button = button("开始识别", self.start, "recognize", "primary")
        actions.addWidget(self.start_button)
        self.stop_button = button("停止", self.cancel, "stop", "ghost")
        actions.addWidget(self.stop_button)
        self.retry_button = button("重试未完成", self.retry, "retry", "ghost")
        actions.addWidget(self.retry_button)
        actions.addStretch()
        self.folder_button = button("打开文件夹", self.open_folder, "folder", "ghost")
        actions.addWidget(self.folder_button)
        self.result_button = button("查看结果", self.view_selected, "audio", "ghost")
        actions.addWidget(self.result_button)
        actions.addWidget(button("关闭", self.close, "close", "ghost"))
        outer.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.update_actions()

    # ------------------------------------------------------------------ opening

    def open_window(self):
        self.sync_options()
        if not self.opened:
            self.opened = True
            self.refresh_latest()
        elif self.batch_id:
            self.batch_state(self.batch_id, self.show_batch, self.show_failure)
        self.show()
        self.raise_()
        self.activateWindow()

    def sync_options(self):
        options = self.controller.options or {}
        selection = options.get("selection") or {}
        preferred = (self.batch or {}).get("language") or selection.get("language") or "auto"
        self.controller.fill(self.language, list(options.get("languages") or []), preferred=preferred)

    def refresh_latest(self):
        self.controller.batches(self.load_latest, self.show_failure)

    def load_latest(self, batches):
        if batches:
            self.batch_state(batches[0]["id"], self.show_batch, self.show_failure)
        else:
            self.new_list()

    def batch_state(self, identity, done, failed=None):
        self.controller.batch_state(identity, done, failed or self.show_failure)

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
        """Add local files from the picker or a drag, with one dedupe/filter path."""
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
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.status.setText("把音频/视频拖到这里，或点「添加音频/视频」；添加后点「开始识别」。")

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
            self.update_actions()

    def new_list(self):
        if self.busy():
            return
        self.timer.stop()
        self.batch, self.batch_id, self.polling = None, None, False
        self.pending = []
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText("添加文件后点「开始识别」。")
        self.render_table()
        self.update_actions()

    # ------------------------------------------------------------------ actions

    def start(self):
        if self.busy() or not self.pending:
            if not self.busy():
                self.status.setText("先点「添加音频/视频」选择要识别的文件。")
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
        self.controller.refresh()

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
        self.close()
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
        row = self.table.currentRow()
        if self.batch and 0 <= row < len(self.batch.get("items") or []):
            return (self.batch["items"][row] or {}).get("path")
        if self.batch is None and 0 <= row < len(self.pending):
            return self.pending[row]
        return None

    # ------------------------------------------------------------------- render

    def show_batch(self, state):
        previous = self.batch_id
        fresh = self.batch is None or previous != self.batch_id
        self.batch, self.batch_id, self.pending = state, state["id"], []
        if previous != self.batch_id:
            self.set_option(self.language, state.get("language") or "auto")
            self.set_option(self.formats, state.get("format") or "srt")
        if fresh:
            self.dedupe.setChecked(bool(state.get("dedupe", True)))
        self.render_table()
        self.render_progress()
        self.update_actions()
        if state.get("status") in BUSY:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    @staticmethod
    def set_option(combo, value):
        combo.blockSignals(True)
        index = combo.findData(value)
        if index < 0:
            combo.addItem(str(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def render_table(self):
        row = self.table.currentRow()
        scroll = self.table.verticalScrollBar().value()
        items = (self.batch or {}).get("items") or [
            {"path": path, "name": Path(path).stem, "status": "pending", "exported": [],
             "export_error": ""} for path in self.pending]
        self.table.setRowCount(len(items))
        for index, item in enumerate(items):
            name = Path(item.get("path") or "").name or item.get("name") or "未命名音频"
            file_item = QTableWidgetItem(name)
            file_item.setToolTip(item.get("path") or "")
            folder_item = QTableWidgetItem(str(Path(item.get("path") or "").parent))
            folder_item.setToolTip(item.get("path") or "")
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
            for column, value in enumerate((file_item, folder_item, status_item, export_item)):
                value.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, column, value)
        if row >= 0:
            self.table.setCurrentCell(min(row, len(items) - 1), 0)
        self.table.verticalScrollBar().setValue(scroll)
        self.fit_to_rows(len(items))

    def fit_to_rows(self, count):
        """Keep the list close to its rows instead of leaving a large blank table."""
        visible = min(max(3, count), 9)
        if (count, visible) == self._fit_key:
            return
        self._fit_key = (count, visible)
        header = self.table.horizontalHeader().sizeHint().height()
        row = self.table.verticalHeader().defaultSectionSize()
        self.table.setFixedHeight(header + visible * row + 2)
        self.layout().activate()
        self.resize(self.width(), max(430, min(650, self.layout().sizeHint().height())))

    @staticmethod
    def export_text(item):
        if item.get("export_error"):
            return "导出失败"
        files = item.get("exported") or []
        if files:
            return "，".join(Path(value).name for value in files)
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
        self.polling = False
        self.update_actions()

    # -------------------------------------------------------------------- state

    def busy(self):
        return bool(self.batch and self.batch.get("status") in BUSY)

    def update_actions(self):
        busy = self.busy()
        state = self.batch or {}
        pending = self.batch is None and self.pending
        retryable = bool(self.batch) and not busy and any(
            item.get("status") in ("failed", "cancelled", "pending") for item in state.get("items") or [])
        self.add_button.setEnabled(not busy)
        self.remove_button.setEnabled(bool(pending) and not busy and self.table.currentRow() >= 0)
        self.new_button.setEnabled(not busy)
        self.start_button.setEnabled(bool(pending) and not busy)
        self.stop_button.setEnabled(bool(self.batch) and state.get("status") in ("queued", "running"))
        self.retry_button.setEnabled(retryable)
        self.folder_button.setEnabled(bool(self.selected_path()))
        self.result_button.setEnabled(self.selected_session() is not None)
        for control in (self.language, self.formats):
            control.setEnabled(not busy)
        self.dedupe.setEnabled(not busy and self.batch is None)
        self.options_hint.setVisible(True)

    def selected_session(self):
        row = self.table.currentRow()
        items = (self.batch or {}).get("items") or []
        if 0 <= row < len(items):
            return items[row].get("session_id")
        return None

    def poll(self):
        if not self.isVisible() or not self.batch_id or self.polling:
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
            self.controller.refresh()

    def poll_failed(self, message):
        self.polling = False
        self.status.setText(str(message))

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
