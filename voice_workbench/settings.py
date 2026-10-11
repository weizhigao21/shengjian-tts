"""Compact model and generated-audio management inside Settings."""
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QWheelEvent
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSpinBox, QFormLayout)

from .ui import button, icon, label

DOWNLOAD_STATES = {"available": "可下载", "downloading": "下载中", "verifying": "校验中",
                   "pausing": "正在暂停", "paused": "已暂停", "failed": "下载失败", "done": "已下载"}


def size_text(size):
    return f"{size / (1024 ** 3):.2f} GB" if size >= 1024 ** 3 else f"{size / (1024 ** 2):.1f} MB"


def preset_text(item):
    source = item.get("provenance")
    if source is None:
        return "旧记录 · 来源未记录"
    if not source.get("preset_used"):
        return "未使用预设"
    return (source.get("preset_name") or source.get("preset_id") or "预设") + ("（已调整）" if source.get("preset_modified") else "")


class ModelTable(QTableWidget):
    """Show every row and let the enclosing page handle wheel scrolling."""
    def __init__(self, rows, columns, scroll_area):
        super().__init__(rows, columns)
        self.scroll_area = scroll_area
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(70)
        self.horizontalHeader().viewport().installEventFilter(self)

    def wheelEvent(self, event):
        viewport = self.scroll_area.viewport()
        forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                                event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                event.buttons(), event.modifiers(), event.phase(), event.inverted())
        QCoreApplication.sendEvent(viewport, forwarded)
        event.accept()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            self.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)

    def fit_rows(self):
        self.setFixedHeight(max(70, self.horizontalHeader().sizeHint().height()
                                + self.verticalHeader().length() + 2 * self.frameWidth()))


class SettingsPanel(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.model_data = None
        self.audio_data = []
        self.loading = False
        self.deleting = False
        self.selected_ids = set()
        self.playing_id = None
        self.performance_dirty = False
        self.output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self.update_play_buttons)
        self.player.errorOccurred.connect(lambda _, text: self.owner.show_error("音频播放失败：" + text))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 18)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading.addWidget(label("设置", "section"))
        heading.addStretch()
        heading.addWidget(button("刷新", self.refresh, "retry", "ghost"))
        layout.addLayout(heading)
        tabs = QHBoxLayout()
        self.tab_buttons = []
        for index, title in enumerate(("模型管理", "生成音频管理", "批量性能")):
            control = button(title, lambda _, i=index: self.set_section(i), kind="nav")
            control.setCheckable(True)
            tabs.addWidget(control)
            self.tab_buttons.append(control)
        tabs.addStretch()
        layout.addLayout(tabs)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self._models_page()
        self._audio_page()
        self._performance_page()
        self.set_section(0)
        self.timer = QTimer(self)
        self.timer.setInterval(1800)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def set_section(self, index):
        self.pages.setCurrentIndex(index)
        for i, control in enumerate(self.tab_buttons):
            control.setChecked(i == index)
        if hasattr(self, "timer"):
            self.refresh()

    def table(self, headers, whole_page=False):
        table = (ModelTable(0, len(headers), self.models_scroll) if whole_page
                 else QTableWidget(0, len(headers)))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return table

    def _models_page(self):
        self.models_scroll = QScrollArea()
        self.models_scroll.setWidgetResizable(True)
        self.models_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.models_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("modelManagementContent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        tools = QHBoxLayout()
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("搜索模型名称、能力或精度…")
        self.model_search.textChanged.connect(self.render_models)
        tools.addWidget(self.model_search, 1)
        self.source_combo = QComboBox()
        for text, value in (("HF 镜像", "mirror"), ("Hugging Face", "huggingface"), ("ModelScope", "modelscope")):
            self.source_combo.addItem(text, value)
        self.source_combo.setToolTip("下载源；更换源后可继续未完成的下载")
        tools.addWidget(self.source_combo)
        tools.addWidget(button("导入模型", self.import_model, "import"))
        tools.addWidget(button("推理引擎", self.choose_runtime, "cpu", "ghost"))
        layout.addLayout(tools)
        self.runtime_label = label("", "muted")
        layout.addWidget(self.runtime_label)
        self.installed_label = label("已添加的模型", "hint")
        layout.addWidget(self.installed_label)
        self.installed_table = self.table(("模型 / 功能", "文件大小", "位置", "操作"), whole_page=True)
        for col, width in ((1, 100), (2, 90), (3, 90)):
            self.installed_table.setColumnWidth(col, width)
        layout.addWidget(self.installed_table)
        self.catalog_label = label("下载其他模型", "hint")
        layout.addWidget(self.catalog_label)
        self.catalog_table = self.table(("模型 / 功能", "精度", "文件大小", "状态", "操作"), whole_page=True)
        for col, width in ((1, 65), (2, 100), (3, 175), (4, 90)):
            self.catalog_table.setColumnWidth(col, width)
        self.catalog_table.cellDoubleClicked.connect(self.download_details)
        layout.addWidget(self.catalog_table)
        foot = QHBoxLayout()
        foot.addWidget(label("Q8 通常更省内存；下载后校验文件，功能以模型配置为准。", "muted"))
        foot.addStretch()
        foot.addWidget(button("模型来源", lambda: QDesktopServices.openUrl(QUrl("https://github.com/leemysw/yovoice")), "link", "ghost"))
        layout.addLayout(foot)
        layout.addStretch()
        self.models_scroll.setWidget(page)
        self.pages.addWidget(self.models_scroll)

    def _audio_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        tools = QHBoxLayout()
        self.select_all = QCheckBox("全选")
        self.select_all.clicked.connect(self.toggle_all)
        tools.addWidget(self.select_all)
        self.audio_search = QLineEdit()
        self.audio_search.setPlaceholderText("搜索作品、文本、模型或预设…")
        self.audio_search.textChanged.connect(self.render_audio)
        tools.addWidget(self.audio_search, 1)
        self.selection_label = label("", "muted")
        tools.addWidget(self.selection_label)
        self.delete_selected = button("删除所选", lambda: self.delete_audio(), "trash")
        self.delete_all = button("全部删除", lambda: self.delete_audio(delete_all=True), "trash", "ghost")
        tools.addWidget(self.delete_selected)
        tools.addWidget(self.delete_all)
        layout.addLayout(tools)
        self.audio_table = self.table(("", "作品 / 生成文本", "模型 / 模式", "声音预设", "生成时间 / 时长", "试听", "详情", "删除"))
        self.audio_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.audio_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.audio_table.horizontalHeader().setMinimumSectionSize(28)
        for col, width in ((0, 32), (2, 175), (3, 135), (4, 150), (5, 42), (6, 42), (7, 42)):
            self.audio_table.setColumnWidth(col, width)
        self.audio_table.itemChanged.connect(self.checked_item)
        self.audio_table.cellDoubleClicked.connect(lambda row, _: self.audio_details(self.visible_audio[row]))
        layout.addWidget(self.audio_table, 1)
        footer = QHBoxLayout()
        self.audio_count = label("还没有生成的音频", "muted")
        footer.addWidget(self.audio_count)
        footer.addStretch()
        self.playback_label = label("", "muted")
        footer.addWidget(self.playback_label)
        layout.addLayout(footer)
        self.pages.addWidget(page)
        self.visible_audio = []
        self.update_selection()

    def refresh(self, *_):
        if not self.isVisible() or not self.owner.ready or self.loading or self.deleting:
            return
        from .gui import checked
        self.loading = True
        if self.pages.currentIndex() == 2:
            def performance_loaded(value):
                self.loading = False
                if not self.performance_dirty:
                    for control, key in ((self.workflow_workers, "workflow_workers"), (self.cpu_threads, "cpu_threads")):
                        control.blockSignals(True)
                        control.setValue(value[key])
                        control.blockSignals(False)
                self.performance_notice.setText(value.get("notice", ""))
            def performance_failed(message):
                self.loading = False
                self.owner.statusBar().showMessage("性能设置读取失败：" + message, 6000)
            self.owner.call(lambda c: checked(c.get("/settings/performance")), performance_loaded, performance_failed)
            return
        def fetched(values):
            self.loading = False
            models, audio = values
            if models != self.model_data:
                self.model_data = models
                self.render_models()
            if audio != self.audio_data:
                self.audio_data = audio
                self.selected_ids.intersection_update(a["id"] for a in audio)
                if self.playing_id and self.playing_id not in {a["id"] for a in audio}:
                    self.stop()
                self.render_audio()
        def failed(message):
            self.loading = False
            self.owner.statusBar().showMessage("管理列表刷新失败：" + message, 6000)
        self.owner.call(lambda c: (checked(c.get("/models")), checked(c.get("/generated-audio"))), fetched, failed)

    def _performance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label("双语工作流批量生成", "section"))
        description = label("并发数决定统一引擎同时生成多少个字幕片段。每个实例会额外占用内存和显存；大模型建议从 1 开始，比较生成时间后再调整。", "muted")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.workflow_workers = QSpinBox()
        self.workflow_workers.setRange(1, 4)
        self.workflow_workers.setValue(1)
        self.workflow_workers.setFixedWidth(140)
        self.workflow_workers.setSuffix(" 个实例")
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(0, 128)
        self.cpu_threads.setSpecialValueText("自动")
        self.cpu_threads.setFixedWidth(140)
        form.addRow("批量并发数", self.workflow_workers)
        form.addRow("每个实例的 CPU 线程数", self.cpu_threads)
        layout.addLayout(form)
        hint = label("CPU 线程数为 0 时自动分配。线程越多不一定越快；并发遇到显存不足时，当前任务自动改为单实例并重试一次。调整设置前请完成或取消正在生成的任务。", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.performance_apply = button("应用设置", self.save_performance, "check")
        self.performance_apply.setEnabled(False)
        row.addWidget(self.performance_apply)
        row.addStretch()
        layout.addLayout(row)
        self.performance_notice = label("", "hint")
        self.performance_notice.setWordWrap(True)
        layout.addWidget(self.performance_notice)
        layout.addStretch()
        def changed():
            self.performance_dirty = True
            self.performance_apply.setEnabled(True)
        self.workflow_workers.valueChanged.connect(changed)
        self.cpu_threads.valueChanged.connect(changed)
        self.pages.addWidget(page)

    def save_performance(self):
        from .gui import checked
        value = {"workflow_workers": self.workflow_workers.value(), "cpu_threads": self.cpu_threads.value()}
        self.performance_apply.setEnabled(False)
        self.workflow_workers.setEnabled(False)
        self.cpu_threads.setEnabled(False)
        def saved(_):
            self.workflow_workers.setEnabled(True)
            self.cpu_threads.setEnabled(True)
            self.performance_dirty = False
            self.performance_notice.setText("")
            self.owner.statusBar().showMessage("批量性能设置已应用", 5000)
        def failed(message):
            self.workflow_workers.setEnabled(True)
            self.cpu_threads.setEnabled(True)
            self.performance_apply.setEnabled(True)
            self.owner.show_error(message)
        self.owner.call(lambda c: checked(c.put("/settings/performance", json=value)), saved, failed)

    def cell(self, table, row, col, text, tooltip=None):
        return self.owner.update_table_cell(table, row, col, text, tooltip)

    def table_rows(self, table, ids):
        previous = getattr(table, "record_ids", None)
        if previous != ids:
            table.clearContents()
            table.setRowCount(len(ids))
            table.record_ids = ids

    def action_cell(self, table, row, col, key, create):
        control = table.cellWidget(row, col)
        if control is None or control.property("action_key") != key:
            control = create()
            control.setProperty("action_key", key)
            table.setCellWidget(row, col, control)
            if isinstance(table, ModelTable):
                control.installEventFilter(table)
        return control

    def render_models(self, *_):
        if self.model_data is None:
            return
        text = self.model_search.text().strip().casefold()
        matches = lambda m: not text or text in " ".join(str(m.get(k, "")) for k in ("name", "description", "precision", "family")).casefold()
        installed = [m for m in self.model_data["installed"] if matches(m)]
        catalog = [m for m in self.model_data["catalog"] if matches(m) and not m["installed"]]
        runtime = self.model_data.get("runtime")
        self.runtime_label.setText("推理引擎：" + (Path(runtime).name if runtime else "尚未设置，下载后请选择 audiocpp_server.exe"))
        self.runtime_label.setToolTip(runtime or "")
        self.installed_label.setText(f"已添加的模型 · {len(installed)}")
        self.catalog_label.setText(f"下载其他模型 · {len(catalog)}")
        table = self.installed_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in installed])
        for row, m in enumerate(installed):
            title = m["name"] + (" · " + m["precision"] if m["precision"] else "")
            self.cell(table, row, 0, title + "\n" + m["description"], title + "\n" + m["description"] + "\n" + m["path"] + f"\n关联预设：{m['preset_count']} 个")
            self.cell(table, row, 1, "目录模型" if m.get("is_directory") else size_text(m["size"]) if m["exists"] else "文件缺失")
            self.cell(table, row, 2, "程序目录" if m["managed"] else "外部文件", m["path"])
            self.action_cell(table, row, 3, m["id"], lambda v=m: button("删除", lambda: self.remove_model(v), "trash", "ghost"))
            table.setRowHeight(row, 64)
        table.fit_rows()
        table.setUpdatesEnabled(True)
        table = self.catalog_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in catalog])
        self.visible_catalog = catalog
        for row, m in enumerate(catalog):
            self.cell(table, row, 0, m["name"] + "\n" + m["description"])
            self.cell(table, row, 1, m["precision"])
            self.cell(table, row, 2, size_text(m["size"]))
            status = m["status"]
            summary = DOWNLOAD_STATES.get(status, status)
            if status in ("downloading", "paused", "pausing", "verifying"):
                summary += f" · {min(100, m.get('received', 0) * 100 / m['size']):.0f}%"
            if m.get("error"):
                summary += "\n" + m["error"]
            self.cell(table, row, 3, summary, m.get("error") or summary)
            if status in ("downloading", "verifying", "pausing"):
                action = self.action_cell(table, row, 4, m["id"] + "-pause", lambda v=m: button("暂停", lambda: self.pause_download(v), "pause", "ghost"))
                action.setEnabled(status != "pausing")
            else:
                caption = "继续" if status == "paused" else "重试" if status == "failed" else "下载" if m["downloadable"] else "仅导入"
                action = self.action_cell(table, row, 4, m["id"] + "-download", lambda v=m: button("下载", lambda: self.download_model(v), "import", "ghost"))
                action.setText(caption)
                action.setEnabled(m["downloadable"])
            table.setRowHeight(row, 66)
        table.fit_rows()
        table.setUpdatesEnabled(True)

    def mutate(self, action, message=""):
        from .gui import checked
        def done(value):
            self.owner.active_voice_snapshot = None
            self.refresh()
            self.owner.poll()
            if message:
                self.owner.statusBar().showMessage(message, 4500)
        self.owner.call(lambda c: checked(action(c)), done)

    def download_model(self, model):
        source = self.source_combo.currentData()
        self.mutate(lambda c: c.post(f"/models/{model['id']}/download", json={"source": source}), "模型开始下载，可在列表中暂停和查看进度。")

    def pause_download(self, model):
        self.mutate(lambda c: c.post(f"/models/{model['id']}/pause"))

    def download_details(self, row, _):
        m = self.visible_catalog[row]
        self.owner.show_details(m["name"] + "\n" + m["description"] + f"\n精度：{m['precision']}\n大小：{size_text(m['size'])}\nSHA-256：{m['sha256']}\n" + m.get("error", ""))

    def import_model(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入本地模型", "", "语音模型 (*.gguf)")
        if path:
            self.mutate(lambda c: c.post("/models/import", json={"path": path}), "模型已添加，语音创作中可以直接选择。")

    def choose_runtime(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 audio.cpp 推理引擎", "", "推理引擎 (audiocpp_server.exe);;程序 (*.exe)")
        if path:
            self.mutate(lambda c: c.put("/models/runtime", json={"path": path}), "已更新统一推理引擎。")

    def remove_model(self, model):
        model = next((m for m in self.model_data["installed"] if m["id"] == model["id"]), model)
        box = QMessageBox(self)
        box.setWindowTitle("删除模型")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("移除模型：" + model["name"])
        box.setInformativeText(f"关联的 {model['preset_count']} 个声音预设会一起移除。\n{model['path']}")
        checkbox = QCheckBox("同时删除模型文件（无法撤销）")
        checkbox.setChecked(model["managed"] and model["can_delete_file"])
        checkbox.setEnabled(model["can_delete_file"])
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("删除")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Yes:
            delete_file = checkbox.isChecked()
            self.mutate(lambda c: c.post(f"/models/{model['id']}/remove", json={"delete_file": delete_file}), "模型已移除。")

    def render_audio(self, *_):
        text = self.audio_search.text().strip().casefold()
        self.visible_audio = [a for a in self.audio_data if not text or text in " ".join((a["work_title"], a["text"], a["model"], a["mode"], preset_text(a))).casefold()]
        table = self.audio_table
        scroll = table.verticalScrollBar().value()
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        self.table_rows(table, [a["id"] for a in self.visible_audio])
        for row, a in enumerate(self.visible_audio):
            item = self.cell(table, row, 0, "")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, a["id"])
            item.setCheckState(Qt.CheckState.Checked if a["id"] in self.selected_ids else Qt.CheckState.Unchecked)
            name = f"{a['work_title']} · 第 {a['generation']} 次 / {a['idx']}" + ("（作品已移除）" if a.get("work_deleted") else "")
            self.cell(table, row, 1, name + "\n" + a["text"], name + "\n" + a["text"] + "\n" + a["audio"])
            self.cell(table, row, 2, a["model"] + "\n" + a["mode"])
            self.cell(table, row, 3, preset_text(a))
            stamp = datetime.fromtimestamp(a["created"]).strftime("%Y-%m-%d %H:%M:%S") if a.get("created") else "时间未记录"
            duration = f"{a['duration']:.2f} 秒" if a.get("duration") is not None else "时长未知"
            date, _, clock = stamp.partition(" ")
            self.cell(table, row, 4, date + "\n" + (clock + " · " if clock else "") + duration, stamp + "\n" + duration)
            play = self.action_cell(table, row, 5, a["id"], lambda v=a: button("", lambda: self.play_audio(v), "play", "ghost"))
            play.setToolTip("试听 / 暂停")
            play.setEnabled(a["available"])
            details = self.action_cell(table, row, 6, a["id"], lambda v=a: button("", lambda: self.audio_details(v), "info", "ghost"))
            details.setToolTip("查看音频、完整文本与生成配置")
            delete = self.action_cell(table, row, 7, a["id"], lambda v=a: button("", lambda: self.delete_audio([v["id"]]), "trash", "ghost"))
            delete.setToolTip("删除此音频")
            table.setRowHeight(row, 64)
        table.verticalScrollBar().setValue(scroll)
        table.blockSignals(False)
        table.setUpdatesEnabled(True)
        self.audio_count.setText(f"共 {len(self.audio_data)} 条音频" + (f" · 搜索结果 {len(self.visible_audio)} 条" if text else ""))
        self.update_selection()
        self.update_play_buttons()

    def checked_item(self, item):
        if item.column() != 0:
            return
        identity = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_ids.add(identity)
        else:
            self.selected_ids.discard(identity)
        self.update_selection()

    def toggle_all(self, checked):
        ids = {a["id"] for a in self.visible_audio}
        self.selected_ids.update(ids) if checked else self.selected_ids.difference_update(ids)
        self.render_audio()

    def update_selection(self):
        visible_ids = {a["id"] for a in self.visible_audio}
        count = len(self.selected_ids)
        self.selection_label.setText(f"已选 {count} 条" if count else "")
        self.delete_selected.setEnabled(count > 0 and not self.deleting)
        self.delete_all.setEnabled(bool(self.audio_data) and not self.deleting)
        self.select_all.setChecked(bool(visible_ids) and visible_ids <= self.selected_ids)

    def play_audio(self, item):
        self.owner.player.pause()
        self.owner.reference_player.stop()
        if self.playing_id == item["id"]:
            self.player.pause() if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else self.player.play()
            return
        self.player.stop()
        self.playing_id = item["id"]
        self.player.setSource(QUrl(self.owner.url + item["audio_url"]))
        self.playback_label.setText(item["work_title"])
        self.player.play()

    def update_play_buttons(self, *_):
        for row, a in enumerate(self.visible_audio):
            control = self.audio_table.cellWidget(row, 5)
            if control:
                active = a["id"] == self.playing_id and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                control.setIcon(icon("pause" if active else "play"))

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.playing_id = None
        self.playback_label.clear()

    def audio_details(self, item):
        voice = item["voice"]
        stamp = datetime.fromtimestamp(item["created"]).strftime("%Y-%m-%d %H:%M:%S") if item.get("created") else "未知"
        duration = f"{item['duration']:.2f} 秒" if item.get("duration") is not None else "未知"
        details = (f"作品：{item['work_title']}\n生成：第 {item['generation']} 次，第 {item['idx']} 段\n"
                   f"音频：{item['audio']}\n模型：{item['model']}\n模型路径：{voice.get('model_dir', '')}\n"
                   f"模式：{item['mode']}\n预设：{preset_text(item)}\n时间：{stamp}\n"
                   f"时长：{duration}\n字幕时间：{item.get('timestamp') or '无'}\n\n生成文本：\n{item['text']}")
        import json
        self.owner.show_details(details + "\n\n生成时配置：\n" + json.dumps(voice, ensure_ascii=False, indent=2))

    def delete_audio(self, ids=None, delete_all=False):
        if self.deleting:
            return
        ids = [a["id"] for a in self.audio_data] if delete_all else ids if ids is not None else sorted(self.selected_ids)
        count = len(self.audio_data) if delete_all else len(ids)
        if count == 0:
            return
        result = QMessageBox.question(self, "删除生成音频", (f"删除全部 {count} 条音频？" if delete_all else f"删除所选 {count} 条音频？") + "\n对应生成记录和不再使用的音频文件会删除，无法撤销。作品文本会保留。",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if result != QMessageBox.StandardButton.Yes:
            return
        # Release the Qt media source before Windows removes the WAV file.
        self.stop()
        self.owner.player.stop()
        self.owner.player.setSource(QUrl())
        self.deleting = True
        self.update_selection()
        from .gui import checked
        def done(value):
            self.deleting = False
            self.selected_ids.difference_update(value["deleted"])
            self.refresh()
            self.owner.poll()
            self.owner.statusBar().showMessage(f"已删除 {len(value['deleted'])} 条音频。", 5000)
            if value["errors"]:
                self.owner.show_error("部分音频未删除：\n" + "\n".join(value["errors"]))
        def failed(message):
            self.deleting = False
            self.update_selection()
            self.owner.show_error(message)
        self.owner.call(lambda c: checked(c.post("/generated-audio/delete", json={"ids": ids})), done, failed)

    def close_management(self):
        self.timer.stop()
        self.stop()
