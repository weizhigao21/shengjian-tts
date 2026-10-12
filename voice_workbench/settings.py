"""Compact model and generated-audio management inside Settings."""
from datetime import datetime
import json
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QWheelEvent
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
    QGridLayout, QHBoxLayout, QHeaderView, QLineEdit, QMenu, QMessageBox, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSpinBox, QFormLayout, QProgressBar, QSizePolicy)

from .ui import ElidedLabel, button, icon, label, generation_elapsed

DOWNLOAD_STATES = {"available": "可下载", "downloading": "下载中", "verifying": "校验中",
                   "pausing": "正在暂停", "paused": "已暂停", "failed": "下载失败", "done": "已下载",
                   "extracting": "正在安装", "waiting": "已安装，等待当前生成结束后启用"}
ARCHIVE_STATES = {**DOWNLOAD_STATES, "available": "待下载", "downloaded": "下载完成",
                  "pending_verification": "待校验", "done": "已拥有"}


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
        self.setObjectName("modelTable")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalHeader().setObjectName("modelSectionHeader")
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.horizontalHeader().setFixedHeight(28)
        self.setFixedHeight(28)
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
        self.setFixedHeight(self.horizontalHeader().height()
                            + self.verticalHeader().length() + 2 * self.frameWidth())
        self.setVisible(self.rowCount() > 0)


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
        self.engine_catalog = json.loads((Path(__file__).parent / "assets" / "engine_catalog.json").read_text(encoding="utf-8"))
        self.engine_release_url = self.engine_catalog["release"]
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
        self.data_location = ElidedLabel("数据目录 · " + str(self.owner.data_dir))
        self.data_location.setProperty("role", "muted")
        self.data_location.setToolTip(f"数据库、声音预设、音频和下载文件保存在这里\n{self.owner.data_dir}")
        heading.addWidget(self.data_location, 1)
        self.open_files_button = button("打开本地文件", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.owner.data_dir))), "folder", "ghost")
        self.open_files_button.setToolTip(f"打开配置、任务、模型和音频所在的数据目录\n{self.owner.data_dir}")
        heading.addWidget(self.open_files_button)
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
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(6)
        layout.addWidget(self._engine_panel())
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
        layout.addLayout(tools)
        self.installed_label = label("正在读取已添加的模型…", "hint")
        layout.addWidget(self.installed_label)
        self.installed_table = self.table(("已添加的模型", "文件大小", "位置", "操作"), whole_page=True)
        self.installed_table.hide()
        for col, width in ((1, 100), (2, 90), (3, 90)):
            self.installed_table.setColumnWidth(col, width)
        layout.addWidget(self.installed_table)
        self.catalog_label = label("正在读取模型目录…", "hint")
        layout.addWidget(self.catalog_label)
        self.catalog_table = self.table(("下载其他模型", "精度", "文件大小", "状态", "操作"), whole_page=True)
        self.catalog_table.hide()
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

    def _engine_panel(self):
        panel = QWidget()
        panel.setObjectName("engineManagement")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(label("推理引擎", "section"))
        self.engine_backend = QComboBox()
        self.engine_backend.addItem("NVIDIA CUDA 12.4", "cuda")
        self.engine_backend.addItem("CPU 便携版", "cpu")
        self.engine_backend.currentIndexChanged.connect(self.render_engine)
        header.addWidget(self.engine_backend)
        self.engine_status = ElidedLabel("等待服务连接")
        self.engine_status.setProperty("role", "muted")
        header.addWidget(self.engine_status, 1)
        self.engine_verify = button("验证", self.verify_engine, "check", "ghost")
        self.engine_verify.setToolTip("检查本地推理程序、计算后端及运行库；缺失文件会明确列出。")
        self.engine_verify.setEnabled(False)
        header.addWidget(self.engine_verify)
        self.engine_download = button("下载并安装", self.toggle_engine_install, "import")
        self.engine_download.setFixedWidth(136)
        self.engine_download.setEnabled(False)
        header.addWidget(self.engine_download)
        self.engine_more = button("更多", lambda: None, kind="ghost")
        self.engine_menu = QMenu(self.engine_more)
        self.local_engine_action = self.engine_menu.addAction("使用本地引擎…", self.choose_runtime)
        self.local_engine_action.setEnabled(False)
        self.engine_release_action = self.engine_menu.addAction("官方下载", lambda: QDesktopServices.openUrl(QUrl(self.engine_release_url)))
        self.engine_more.setMenu(self.engine_menu)
        header.addWidget(self.engine_more)
        layout.addLayout(header)
        note = QHBoxLayout()
        self.engine_description = ElidedLabel()
        self.engine_description.setProperty("role", "hint")
        note.addWidget(self.engine_description, 1)
        self.runtime_label = ElidedLabel()
        self.runtime_label.setProperty("role", "muted")
        self.runtime_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        note.addWidget(self.runtime_label, 1)
        layout.addLayout(note)
        self.engine_assets = QWidget()
        grid = QGridLayout(self.engine_assets)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(2, 1)
        self.engine_asset_rows = []
        for index in range(max(len(e["archives"]) for e in self.engine_catalog["backends"])):
            title, amount, state = (ElidedLabel() for _ in range(3))
            for text in (title, amount, state):
                text.setProperty("role", "muted")
                text.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            progress = QProgressBar()
            progress.setTextVisible(False)
            progress.setFixedHeight(3)
            progress.setObjectName("engineAssetProgress")
            amount.setMinimumWidth(156)
            amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            title.setFixedWidth(84)
            state.setFixedWidth(100)
            state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for col, widget in enumerate((title, amount, progress, state)):
                grid.addWidget(widget, index, col)
            self.engine_asset_rows.append((title, amount, progress, state))
        layout.addWidget(self.engine_assets)
        self.engine_progress = QProgressBar()
        self.engine_progress.setTextVisible(False)
        self.engine_progress.setFixedHeight(3)
        policy = self.engine_progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.engine_progress.setSizePolicy(policy)
        self.engine_progress.hide()
        layout.addWidget(self.engine_progress)
        self.engine_error = label("", "hint")
        self.engine_error.setObjectName("engineError")
        self.engine_error.setWordWrap(True)
        self.engine_error.hide()
        layout.addWidget(self.engine_error)
        self.render_engine()
        return panel

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
        self.audio_table = self.table(("", "作品 / 生成文本", "模型 / 模式", "声音预设", "生成时间 / 时长", "生成耗时", "试听", "详情", "删除"))
        self.audio_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.audio_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.audio_table.horizontalHeader().setMinimumSectionSize(28)
        for col, width in ((0, 32), (2, 165), (3, 120), (4, 150), (5, 96), (6, 42), (7, 42), (8, 42)):
            self.audio_table.setColumnWidth(col, width)
        self.audio_table.itemChanged.connect(self.checked_item)
        self.audio_table.cellDoubleClicked.connect(lambda row, _: self.audio_details(self.visible_audio[row]))
        self.audio_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.audio_table.customContextMenuRequested.connect(self.audio_context_menu)
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
        self.render_engine()
        self.installed_table.horizontalHeaderItem(0).setText(f"已添加的模型 · {len(installed)}")
        self.catalog_table.horizontalHeaderItem(0).setText(f"下载其他模型 · {len(catalog)}")
        self.installed_label.setText("已添加的模型 · 0  · " + ("没有匹配的模型" if text else "可从下方下载，或点击「导入模型」"))
        self.catalog_label.setText("下载其他模型 · 0  · " + ("没有匹配的模型" if text else "暂无可下载的模型"))
        self.installed_label.setVisible(not installed)
        self.catalog_label.setVisible(not catalog)
        table = self.installed_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in installed])
        for row, m in enumerate(installed):
            title = m["name"] + (" · " + m["precision"] if m["precision"] else "")
            self.cell(table, row, 0, title + "\n" + m["description"], title + "\n" + m["description"] + "\n" + m["path"] + f"\n关联预设：{m['preset_count']} 个")
            self.cell(table, row, 1, "目录模型" if m.get("is_directory") else size_text(m["size"]) if m["exists"] else "文件缺失")
            self.cell(table, row, 2, "程序目录" if m["managed"] else "外部文件", m["path"])
            self.action_cell(table, row, 3, m["id"], lambda v=m: button("删除", lambda: self.remove_model(v), "trash", "ghost"))
            table.setRowHeight(row, 50)
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
            table.setRowHeight(row, 50)
        table.fit_rows()
        table.setUpdatesEnabled(True)

    def render_engine(self, *_):
        data = self.model_data or {}
        engine = data.get("engine", {})
        runtime = engine.get("path") or data.get("runtime")
        ready = engine.get("ready", bool(runtime and Path(runtime).is_file()))
        self.runtime_label.setText("当前 · " + Path(runtime).name if ready and runtime else "当前未配置引擎")
        self.runtime_label.setToolTip(runtime or engine.get("directory") or str(self.owner.data_dir / "runtime"))
        backend = self.engine_backend.currentData()
        entry = next(e for e in self.engine_catalog["backends"] if e["id"] == backend)
        option = next((e for e in engine.get("options", []) if e["backend"] == backend), {})
        self.engine_description.setText(f"audio.cpp {option.get('version', self.engine_catalog['version'])} · " +
                                       ("程序与 CUDA 运行库一起安装" if backend == "cuda" else "包含 CPU 所需运行依赖"))
        status = option.get("status", "available")
        busy = status in ("downloading", "verifying", "extracting", "waiting", "pausing")
        any_busy = any(e.get("status") in ("downloading", "verifying", "extracting", "waiting", "pausing") for e in engine.get("options", []))
        active = bool(ready and option.get("path") == runtime)
        caption = ("正在暂停" if status == "pausing" else "暂停下载" if status == "downloading" else
                   "暂停安装" if busy else "继续安装" if status == "paused" else "已拥有" if active else
                   "启用引擎" if option.get("installed") else "重试安装" if status == "failed" else
                   "补全缺失组件" if any(a.get("verified") for a in option.get("archives", [])) else "下载并安装")
        self.engine_download.setText(caption)
        self.engine_download.setIcon(icon("pause" if busy else "check" if active else "import"))
        self.engine_download.setEnabled(self.owner.ready and engine.get("supported", True) and status != "pausing"
                                        and (busy or (not any_busy and not active)))
        self.local_engine_action.setEnabled(self.owner.ready and not any_busy)
        self.engine_verify.setEnabled(self.owner.ready and not any_busy)
        self.engine_download.setToolTip(option.get("description", "完成后自动更新模型的引擎路径。") +
                                       "\n保存目录：" + str(self.owner.data_dir / "runtime"))
        self.engine_status.setText("等待服务连接" if not self.owner.ready else DOWNLOAD_STATES[status] if busy else
                                   "已拥有 · 当前使用" if active else "已拥有" if option.get("installed") else DOWNLOAD_STATES.get(status, status))
        self.engine_status.setToolTip(self.engine_status.text() + "\n" + option.get("archive", "") + "\n" + option.get("description", entry["description"]))
        archives = option.get("archives") or [{**a, "received": a["size"] if option.get("installed") else 0,
                                               "status": "done" if option.get("installed") else "available"}
                                              for a in entry["archives"]]
        for index, widgets in enumerate(self.engine_asset_rows):
            for widget in widgets:
                widget.setVisible(index < len(archives))
            if index >= len(archives):
                continue
            archive = archives[index]
            title, amount, progress, state = widgets
            title.setText("推理程序" if index == 0 else "CUDA 运行库")
            title.setToolTip(archive["name"])
            received, total = archive.get("received", 0), archive["size"]
            amount.setText(size_text(total) if archive["status"] == "done" else f"{size_text(received)} / {size_text(total)}")
            progress.setRange(0, 1000)
            progress.setValue(min(1000, int(received * 1000 / (total or 1))))
            state.setText(ARCHIVE_STATES.get(archive["status"], archive["status"]))
            state.setToolTip(archive["name"] + "\n" + state.text() + "\n" + "\n".join(archive.get("issues", [])))
        self.engine_progress.setVisible(busy or status == "paused")
        self.engine_progress.setToolTip(f"整体进度：{size_text(option.get('received', 0))} / {size_text(option.get('size', sum(a['size'] for a in archives)))}")
        if status in ("extracting", "waiting"):
            self.engine_progress.setRange(0, 0)
        else:
            self.engine_progress.setRange(0, 1000)
            self.engine_progress.setValue(min(1000, int(option.get("received", 0) * 1000 / (option.get("size") or 1))))
        validation = option.get("verification", {})
        issues = validation.get("issues", []) if validation.get("path") and Path(validation["path"]).is_file() else []
        error = option.get("error") or ("验证未通过：" + "；".join(issues) if issues and not busy else "")
        self.engine_error.setText(error)
        self.engine_error.setVisible(bool(error))

    def verify_engine(self):
        from .gui import checked
        backend = self.engine_backend.currentData()
        self.engine_verify.setEnabled(False)
        def done(value):
            self.model_data = {**(self.model_data or {}), "engine": value["engine"]}
            self.render_engine()
            result = value["verification"]
            self.owner.statusBar().showMessage("验证通过，推理引擎和运行库已拥有。" if result["valid"] else "验证未通过：" + "；".join(result["issues"]), 6000)
        self.owner.call(lambda c: checked(c.post("/models/runtime/verify", json={"backend": backend})), done,
                        lambda message: (self.render_engine(), self.owner.statusBar().showMessage(str(message), 6000)))

    def toggle_engine_install(self):
        backend = self.engine_backend.currentData()
        option = next((e for e in (self.model_data or {}).get("engine", {}).get("options", []) if e["backend"] == backend), {})
        if option.get("status") in ("downloading", "verifying", "extracting", "waiting"):
            self.pause_engine()
        else:
            self.download_engine()

    def download_engine(self, _checked=False, *, backend=None):
        backend = backend or self.engine_backend.currentData()
        self.engine_backend.setCurrentIndex(self.engine_backend.findData(backend))
        engine = (self.model_data or {}).get("engine", {})
        option = next((e for e in engine.get("options", []) if e["backend"] == backend), {})
        action = "use" if option.get("installed") and option.get("status") not in ("paused", "failed") else "download"
        self.mutate(lambda c: c.post(f"/models/runtime/{action}", json={"backend": backend}),
                    "已启用推理引擎。" if action == "use" else "引擎开始下载，完成后自动配置；设置中可查看进度。")

    def pause_engine(self):
        backend = self.engine_backend.currentData()
        self.mutate(lambda c: c.post("/models/runtime/pause", json={"backend": backend}))

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
            elapsed, elapsed_tip = generation_elapsed(a)
            timing = self.cell(table, row, 5, elapsed, elapsed_tip)
            timing.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            play = self.action_cell(table, row, 6, a["id"], lambda v=a: button("", lambda: self.play_audio(v), "play", "ghost"))
            play.setToolTip("试听 / 暂停")
            play.setEnabled(a["available"])
            details = self.action_cell(table, row, 7, a["id"], lambda v=a: button("", lambda: self.audio_details(v), "info", "ghost"))
            details.setToolTip("查看音频、完整文本与生成配置")
            delete = self.action_cell(table, row, 8, a["id"], lambda v=a: button("", lambda: self.delete_audio([v["id"]]), "trash", "ghost"))
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

    def audio_context_menu(self, position):
        row = self.audio_table.rowAt(position.y())
        if not 0 <= row < len(self.visible_audio):
            return
        # Keep the clicked record even if polling or filtering changes the table.
        item = dict(self.visible_audio[row])
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        reference = menu.addAction(icon("voices"), "添加为参考音频…")
        reference.setEnabled(item["available"] and self.owner.ready and not self.deleting)
        hint = ("音频文件已不存在，请刷新列表。" if not item["available"] else
                "本地服务未连接，请连接后再添加。" if not self.owner.ready else
                "带入音频与生成文本，保存到声音库；参考音频需要 1–60 秒。")
        reference.setToolTip(hint)
        details = menu.addAction(icon("info"), "查看详情")
        chosen = menu.exec(self.audio_table.viewport().mapToGlobal(position))
        menu.deleteLater()
        if chosen is reference:
            self.add_audio_reference(item)
        elif chosen is details:
            self.audio_details(item)

    def add_audio_reference(self, item):
        folder = (self.owner.data_dir / "audio").resolve()
        path = (folder / item["audio"]).resolve()
        if path.parent != folder or not path.is_file():
            self.owner.show_error("音频文件已不存在，请刷新列表后重试。")
            return
        self.stop()
        source = {"reference_audio": str(path),
                  "name": f"{item['work_title']} · 片段 {item['idx']}"[:100],
                  "reference_text": item["text"]}
        return self.owner.import_reference(source=source, selected=lambda _: self.owner.statusBar().showMessage(
            "已添加到声音库 → 参考声音，原生成音频保留。", 5000))

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
            control = self.audio_table.cellWidget(row, 6)
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
                   f"时长：{duration}\n生成耗时：{generation_elapsed(item)[0]}\n字幕时间：{item.get('timestamp') or '无'}\n\n生成文本：\n{item['text']}")
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
