"""Workbench layout; API, task and service lifecycles remain in gui.py."""
from pathlib import Path
import json
import os
import re

from PyQt6.QtCore import Qt, QSize, QUrl, QPoint
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDoubleSpinBox,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QProgressBar,
    QScrollArea, QSlider, QSpinBox, QStackedWidget, QTableWidget,
    QTextEdit, QVBoxLayout, QWidget)

from . import __version__
from .ui import STYLE, ORANGE, AnchoredPopover, RecentTasks, ScriptEditor, Waveform, application_icon, button, icon, label

from .model_info import KOKORO_LANGUAGES, LANGUAGES, MODES, model_ui, selected_mode
from .model_controls import ModelControls, populate, populate_voices
FAMILIES = {"index_tts2": "IndexTTS", "voxcpm2": "VoxCPM2", "kokoro_tts": "Kokoro", "qwen3_tts": "Qwen3-TTS"}


def model_key(voice):
    return json.dumps([voice["backend"], voice.get("family"),
                       os.path.normcase(os.path.abspath(voice.get("model_dir") or ".")),
                       os.path.normcase(os.path.abspath(voice.get("engine_executable") or voice.get("repo_dir") or ".")),
                       voice.get("index_version") if voice["backend"] == "index_tts" else None])


def model_label(voice):
    if voice["backend"] == "windows_sapi":
        return "Windows 系统声音"
    if voice["backend"] == "index_tts":
        return "IndexTTS " + voice.get("index_version", "2") + " · Python"
    stem = Path(voice.get("model_dir") or "").stem
    name = FAMILIES.get(voice.get("family"), stem)
    if voice.get("family") == "index_tts2":
        name += " 2.5" if "2_5" in stem or "2.5" in stem else " 2"
    elif voice.get("family") == "kokoro_tts" and "82m" in stem.lower():
        name += " 82M"
    precision = re.search(r"(?:q\d+(?:[_-][a-z0-9]+)?|bf16|f16|orig)$", stem, re.I)
    return name + (" · " + precision.group().upper().replace("-", "_") if precision else "")


def card(title=None, subtitle=None):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(14)
    if title:
        layout.addWidget(label(title, "section"))
    if subtitle:
        text = label(subtitle, "hint")
        text.setWordWrap(True)
        layout.addWidget(text)
    return frame, layout


class UiLayout:
    def _build(self):
        self.setStyleSheet(STYLE)
        self.setMinimumSize(1080, 720)
        self.resize(1380, 900)
        shell = QWidget()
        shell.setObjectName("shell")
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(12, 16, 12, 10)
        nav.setSpacing(8)
        brand = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(application_icon().pixmap(QSize(28, 28)))
        brand.addWidget(mark)
        brand.addWidget(label("声笺", "brand"))
        brand.addStretch()
        nav.addLayout(brand)
        nav.addSpacing(12)
        self.new_document_button = button("新建配音", self.new_document, "plus")
        nav.addWidget(self.new_document_button)
        nav.addSpacing(12)
        self.nav_buttons = []
        for index, (text, symbol) in enumerate((("语音创作", "pen"), ("声音库", "voices"), ("工作流联动", "link"))):
            control = button(text, lambda _, i=index: self.set_page(i), symbol, "nav")
            control.setCheckable(True)
            self.nav_buttons.append(control)
            nav.addWidget(control)
        nav.addSpacing(24)
        nav.addWidget(label("最近作品", "muted"))
        self.jobs_table = RecentTasks()
        self.jobs_table.setObjectName("recentJobs")
        self.configure_table(self.jobs_table)
        self.jobs_table.horizontalHeader().hide()
        self.jobs_table.setColumnHidden(1, True)
        self.jobs_table.setColumnHidden(2, True)
        self.jobs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.jobs_table.itemSelectionChanged.connect(self.select_job)
        self.jobs_table.delete_requested.connect(self.delete_job)
        self.jobs_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.jobs_table.customContextMenuRequested.connect(self.job_context_menu)
        nav.addWidget(self.jobs_table, 1)
        self.jobs_empty = label("新建作品后，生成记录保存在作品里", "muted")
        self.jobs_empty.setWordWrap(True)
        nav.addWidget(self.jobs_empty)
        settings_button = button("设置", lambda: self.set_page(3), "settings", "nav")
        settings_button.setCheckable(True)
        self.nav_buttons.append(settings_button)
        nav.addWidget(settings_button)
        nav.addWidget(button("打开本地文件", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))), "folder", "ghost"))
        self.version_label = label(f"v{__version__}", "muted")
        self.version_label.setObjectName("appVersion")
        self.version_label.setToolTip("声笺 · 本地配音工作台")
        nav.addWidget(self.version_label)
        outer.addWidget(sidebar)

        sheet = QWidget()
        sheet.setObjectName("sheet")
        main = QVBoxLayout(sheet)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self.workspace_header = QWidget()
        header = QHBoxLayout(self.workspace_header)
        header.setContentsMargins(18, 8, 16, 6)
        self.page_title = label("工作台  /  语音创作", "muted")
        header.addWidget(self.page_title)
        header.addStretch()
        self.banner = QLabel("连接本地服务…")
        self.banner.setObjectName("banner")
        self.banner.setToolTip(self.url)
        nav.insertWidget(nav.count() - 2, self.banner)
        main.addWidget(self.workspace_header)
        self.tabs = QStackedWidget()
        # Connect after all pages exist.
        main.addWidget(self.tabs, 1)
        self._generation_tab()
        self._voices_tab()
        self._service_tab()
        from .settings import SettingsPanel
        self.settings = SettingsPanel(self)
        self.tabs.addWidget(self.settings)
        self.tabs.currentChanged.connect(self.sync_navigation)
        main.addWidget(self._transport())
        outer.addWidget(sheet, 1)
        self.setCentralWidget(shell)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().hide()
        self.toast = QFrame(self)
        self.toast.setObjectName("toast")
        self.toast_layout = QHBoxLayout(self.toast)
        self.toast_layout.setContentsMargins(12, 7, 12, 7)
        self.toast_text = label("", "hint")
        self.toast_text.setWordWrap(True)
        self.toast_layout.addWidget(self.toast_text)
        self.toast.hide()
        self.statusBar().messageChanged.connect(self.show_toast)
        self.sync_navigation(0)
        self.text_edit.setFocus()

    @staticmethod
    def configure_table(table):
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_page(self, index):
        self.tabs.setCurrentIndex(index)

    def sync_navigation(self, index):
        names = ("语音创作", "声音库", "工作流联动", "设置")
        self.page_title.setText("工作台  /  " + names[index])
        for i, control in enumerate(self.nav_buttons):
            control.setChecked(index == i)
        self.workspace_header.setVisible(index != 0)
        if index == 3:
            self.clip_list.hide()
            self.settings.refresh()
        else:
            self.settings.player.pause()

    def new_document(self):
        from .gui import checked
        if getattr(self, "submitting", False):
            return
        self.save_document()
        self.set_page(0)
        self.selected_job = self.job_data = None
        self.active_voice_snapshot = None
        self.jobs_table.blockSignals(True)
        self.jobs_table.clearSelection()
        self.jobs_table.blockSignals(False)
        self.segments_table.setRowCount(0)
        self.segments_table.setCurrentCell(-1, -1)
        self.clip_list.hide()
        self.auto_select_audio = True
        self.job_summary.setText("还没有生成记录")
        self.job_progress.setValue(0)
        self.job_progress.hide()
        self.preview_segment()
        for control in (self.retry_button, self.cancel_button, self.export_button):
            control.setEnabled(False)
        self.title_edit.setText("未命名配音")
        self.text_edit.clear()
        self.text_edit.setFocus()
        if self.ready:
            self.submitting = True
            self.update_generation_actions()
            def created(job):
                self.submitting = False
                self.selected_job = job["id"]
                self.render_job(job)
                self.update_generation_actions()
                self.document_changed()
                self.poll()
            self.call(lambda c: checked(c.post("/jobs/draft", json={"title": "未命名配音", "script": ""})), created, self.submission_failed)

    def _generation_tab(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        document = QWidget()
        doc = QVBoxLayout(document)
        doc.setContentsMargins(0, 0, 0, 0)
        doc.setSpacing(0)
        writing = QWidget()
        editor = QVBoxLayout(writing)
        editor.setContentsMargins(18, 12, 18, 10)
        editor.setSpacing(12)
        overline = QHBoxLayout()
        self.title_edit = QLineEdit("未命名配音")
        self.title_edit.setObjectName("documentTitle")
        self.title_edit.setPlaceholderText("为这段声音起个名字")
        self.title_edit.setMaxLength(200)
        self.title_edit.textChanged.connect(self.document_changed)
        overline.addWidget(self.title_edit, 1)
        import_control = button("", self.import_text, "import", "icon")
        import_control.setToolTip("导入字幕或文本")
        overline.addWidget(import_control)
        editor.addLayout(overline)
        self.text_edit = ScriptEditor()
        self.text_edit.setObjectName("scriptEditor")
        self.text_edit.setAcceptRichText(False)
        self.text_edit.setPlaceholderText("从第一句话开始…")
        self.text_edit.textChanged.connect(self.update_script_stats)
        self.text_edit.textChanged.connect(self.document_changed)
        editor.addWidget(self.text_edit, 1)
        footer = QHBoxLayout()
        self.script_stats = label("0 字  ·  0 个片段", "muted")
        footer.addWidget(self.script_stats)
        footer.addStretch()
        footer.addWidget(label("TXT / LRC / SRT / VTT", "eyebrow"))
        editor.addLayout(footer)
        doc.addWidget(writing)
        layout.addWidget(document, 1)

        inspector = QWidget()
        inspector.setObjectName("inspector")
        inspector.setFixedWidth(352)
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        inspector_scroll = QScrollArea()
        self.inspector_scroll = inspector_scroll
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector_body = QWidget()
        inspector_body.setObjectName("inspectorBody")
        side = QVBoxLayout(inspector_body)
        side.setContentsMargins(18, 4, 18, 16)
        side.setSpacing(10)
        side.addWidget(label("模型", "section"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumContentsLength(10)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.currentIndexChanged.connect(self.select_generation_model)
        side.addWidget(self.model_combo)
        self.voice_family = label("连接服务后显示本地模型", "hint")
        self.voice_family.setWordWrap(True)
        side.addWidget(self.voice_family)
        self.voice_family.hide()
        self.voice_name = label("声音预设", "muted")
        side.addWidget(self.voice_name)
        self.voice_combo = QComboBox()
        self.voice_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.voice_combo.setMinimumContentsLength(10)
        self.voice_combo.currentIndexChanged.connect(self.update_link_config)
        self.voice_combo.currentIndexChanged.connect(self.update_voice_summary)
        side.addWidget(self.voice_combo)
        self.model_controls = ModelControls()
        self.model_controls.changed.connect(self.refresh_main_settings)
        self.model_controls.preview_requested.connect(self.preview_reference)
        self.model_controls.choose_requested.connect(lambda: self.choose_reference(self.model_controls))
        self.model_controls.audio_changed.connect(self.stop_reference)
        side.addWidget(self.model_controls)
        self.voice_summary = QLabel()
        self.voice_summary.setWordWrap(True)
        self.voice_summary.setProperty("role", "hint")
        side.addWidget(self.voice_summary)
        self.voice_summary.hide()
        self.save_generation_button = button("保存声音预设", self.save_generation_voice, "check", "ghost")
        self.save_generation_button.setEnabled(False)
        self.voice_health = label("等待服务连接", "hint")
        self.voice_health.setWordWrap(True)
        side.addStretch()
        inspector_scroll.setWidget(inspector_body)
        inspector_layout.addWidget(inspector_scroll, 1)
        actions = QWidget()
        action_layout = QVBoxLayout(actions)
        action_layout.setContentsMargins(18, 12, 18, 12)
        action_layout.setSpacing(10)
        action_layout.addWidget(self.voice_health)
        configuration_actions = QHBoxLayout()
        configuration_actions.setSpacing(8)
        configuration_actions.addWidget(self.save_generation_button, 1)
        action_layout.addLayout(configuration_actions)
        self.warmup_button = button("", self.warmup, "audio", "icon")
        self.warmup_button.setFixedWidth(32)
        self.warmup_button.setToolTip("用一句短文本试听当前声音设置")
        self.warmup_button.setAccessibleName("声音试音")
        self.generate_button = button("开始生成", self.generate, "play", "primary")
        self.generate_button.setMinimumHeight(24)
        self.warmup_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        configuration_actions.addWidget(self.warmup_button)
        action_layout.insertWidget(1, self.generate_button)
        inspector_layout.insertWidget(0, actions)
        layout.addWidget(inspector)
        self.tabs.addWidget(page)

    def _voices_tab(self):
        from .library import LibraryPanel
        self.library = LibraryPanel(self)
        self.tabs.addWidget(self.library)

    def import_yovoice_assets(self):
        from .gui import checked
        self.call(lambda c: checked(c.post("/import/yovoice")), lambda r: (self.statusBar().showMessage(f"已导入 {r['added']} 个声音预设", 5000), self.poll()))

    def _service_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 28, 22)
        layout.setSpacing(16)
        layout.addWidget(label("连接你的双语工作流", "title"))
        layout.addWidget(label("字幕 → 配音片段 → 混音。继续使用你熟悉的工作流。", "muted"))
        config_card, config_layout = card("音色自动发现", "复制接入链接到双语工作流，读取声音预设后选择音色并批量生成。")
        row = QHBoxLayout()
        badge = label("仅本机访问")
        badge.setObjectName("badge")
        row.addWidget(badge)
        self.workflow_link = QLineEdit(self.url + "/workflow")
        self.workflow_link.setReadOnly(True)
        self.workflow_link.setToolTip("在双语工作流的 API 参数中粘贴此链接，点击「读取音色」。")
        row.addWidget(self.workflow_link, 1)
        row.addWidget(button("复制接入链接", lambda: QApplication.clipboard().setText(self.workflow_link.text()), "copy", "primary"))
        row.addWidget(button("复制 API 配置", lambda: QApplication.clipboard().setText(self.link_config.toPlainText()), "copy", "ghost"))
        config_layout.addLayout(row)
        config_layout.addWidget(label("音色来自声音库中的已保存预设。新增或修改预设后，在工作流点击「刷新音色」；批量开始前自动核对版本。", "hint"))
        self.link_config = QTextEdit()
        self.link_config.setObjectName("code")
        self.link_config.setReadOnly(True)
        self.link_config.setFixedHeight(150)
        config_layout.addWidget(self.link_config)
        layout.addWidget(config_card)
        log_card, log_layout = card()
        tools = QHBoxLayout()
        tools.addWidget(label("服务日志", "section"), 1)
        from .gui import checked
        tools.addWidget(button("释放引擎 / 显存", lambda: self.call(lambda c: checked(c.post("/engine/release")), lambda r: self.statusBar().showMessage(r["msg"], 8000)), "cpu"))
        tools.addWidget(button("打开日志目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir / "logs"))), "folder"))
        log_layout.addLayout(tools)
        self.service_log = QTextEdit()
        self.service_log.setObjectName("code")
        self.service_log.setReadOnly(True)
        self.service_log.document().setMaximumBlockCount(2000)
        self.service_log.setPlaceholderText("启动日志会显示在这里。片段错误可在生成结果中双击查看。")
        log_layout.addWidget(self.service_log, 1)
        layout.addWidget(log_card, 1)
        layout.addWidget(label("双语工作流：模式选择「API 服务器」→ 粘贴链接 → 读取音色 → 选择音色。使用更新后的源码或重新打包版本。", "hint"))
        self.tabs.addWidget(page)

    def _transport(self):
        transport = QWidget()
        self.transport = transport
        transport.setObjectName("transport")
        transport.setFixedHeight(72)
        layout = QHBoxLayout(transport)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(10)
        self.previous_button = button("", lambda: self.play_adjacent(-1), "previous", "icon")
        self.previous_button.setToolTip("播放上一个")
        self.previous_button.setAccessibleName("播放上一个")
        self.previous_button.setFixedSize(28, 32)
        self.previous_button.setEnabled(False)
        layout.addWidget(self.previous_button)
        self.play_button = button("", self.toggle_playback, "play", "primary")
        self.play_button.setFixedSize(34, 34)
        self.play_button.setToolTip("播放 / 暂停选中片段")
        self.play_button.setAccessibleName("播放或暂停选中片段")
        self.play_button.setEnabled(False)
        layout.addWidget(self.play_button)
        self.next_button = button("", lambda: self.play_adjacent(1), "next", "icon")
        self.next_button.setToolTip("播放下一个")
        self.next_button.setAccessibleName("播放下一个")
        self.next_button.setFixedSize(28, 32)
        self.next_button.setEnabled(False)
        layout.addWidget(self.next_button)
        names = QVBoxLayout()
        names.setSpacing(3)
        self.clip_title = button("还没有生成记录", self.toggle_clip_list, "chevron-down", "clipTitle")
        self.clip_title.setAccessibleName("展开生成列表")
        self.clip_title.setCheckable(True)
        self.clip_title.setFixedWidth(220)
        self.clip_title.setEnabled(False)
        self.clip_time = label("00:00 / 00:00", "muted")
        names.addWidget(self.clip_title)
        names.addWidget(self.clip_time)
        layout.addLayout(names)
        track = QVBoxLayout()
        track.setSpacing(1)
        self.waveform = Waveform()
        self.waveform.setFixedHeight(24)
        track.addWidget(self.waveform)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setAccessibleName("音频播放进度")
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        track.addWidget(self.seek_slider)
        layout.addLayout(track, 1)
        summary = QVBoxLayout()
        summary.setSpacing(6)
        self.job_summary = label("还没有生成记录", "muted")
        summary.addWidget(self.job_summary)
        self.job_progress = QProgressBar()
        self.job_progress.setTextVisible(False)
        self.job_progress.setValue(0)
        self.job_progress.hide()
        summary.addWidget(self.job_progress)
        layout.addLayout(summary)
        self.export_button = button("", self.export, "export", "icon")
        self.export_button.setToolTip("导出选中的生成批次")
        self.export_button.setAccessibleName("导出音频")
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)
        self.details_button = button("", self.details, "info", "icon")
        self.details_button.setToolTip("片段详情 / 错误原因")
        self.details_button.setAccessibleName("片段详情")
        self.details_button.setEnabled(False)
        layout.addWidget(self.details_button)
        self.player.positionChanged.connect(self.update_transport)
        self.player.durationChanged.connect(self.update_transport)
        self.player.playbackStateChanged.connect(self.update_play_icon)
        self.player.mediaStatusChanged.connect(self.playback_finished)
        self.player.errorOccurred.connect(lambda error, message: self.statusBar().showMessage("音频播放失败：" + message, 8000))
        self._clip_list()
        return transport

    def _clip_list(self):
        self.clip_list = AnchoredPopover(self, self.clip_title)
        self.clip_list.setObjectName("clipList")
        self.clip_list.visibility_changed.connect(self.clip_title.setChecked)
        result = QVBoxLayout(self.clip_list)
        result.setContentsMargins(14, 12, 14, 10)
        result.setSpacing(8)
        header = QHBoxLayout()
        self.clip_list_title = label("生成记录", "section")
        header.addWidget(self.clip_list_title, 1)
        self.retry_button = button("重试失败", self.retry, "retry", "ghost")
        self.cancel_button = button("取消生成", self.cancel, "close", "ghost")
        for control in (self.retry_button, self.cancel_button):
            control.setEnabled(False)
            header.addWidget(control)
        close = button("", self.clip_list.hide, "close", "icon")
        close.setToolTip("收起列表")
        header.addWidget(close)
        result.addLayout(header)
        self.segments_table = QTableWidget(0, 5)
        self.configure_table(self.segments_table)
        self.segments_table.setHorizontalHeaderLabels(["#", "生成时间", "名称 / 文本", "状态", "错误原因"])
        for col, width in ((0, 38), (1, 135), (3, 88)):
            self.segments_table.setColumnWidth(col, width)
        self.segments_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.segments_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.segments_table.itemDoubleClicked.connect(self.clip_details)
        self.segments_table.itemSelectionChanged.connect(self.preview_segment)
        self.segments_table.itemClicked.connect(self.choose_clip)
        result.addWidget(self.segments_table, 1)
        self.result_hint = label("点击片段播放；失败片段可查看具体原因。", "muted")
        result.addWidget(self.result_hint)
        self.clip_list.hide()

    def toggle_clip_list(self):
        if self.clip_list.isVisible():
            self.clip_list.hide()
            return
        width = min(860, self.transport.width() - 36)
        height = min(420, max(220, 140 + self.segments_table.rowCount() * 36))
        self.clip_list.resize(width, height)
        self.clip_list.move(self.transport.mapTo(self, QPoint(18, -height - 6)))
        self.clip_list.show()
        self.segments_table.setFocus()
        if self.segments_table.currentRow() >= 0:
            self.segments_table.scrollToItem(self.segments_table.item(self.segments_table.currentRow(), 0))

    def choose_clip(self, *_):
        self.auto_select_audio = False
        item = self.current_segment()
        if item and item["status"] == "done" and self.play_button.isEnabled():
            self.player.setPosition(0)
            self.play()
            self.clip_list.hide()

    def clip_details(self, *_):
        self.clip_list.hide()
        self.details()

    def playable_rows(self):
        return [row for row, item in enumerate((self.job_data or {}).get("segments", []))
                if item["status"] == "done" and item.get("audio") and (self.data_dir / "audio" / item["audio"]).is_file()]

    def adjacent_row(self, direction):
        row = self.segments_table.currentRow()
        rows = [i for i in self.playable_rows() if i < row] if direction < 0 else [i for i in self.playable_rows() if i > row]
        return (rows[-1] if direction < 0 else rows[0]) if rows else None

    def play_adjacent(self, direction):
        row = self.adjacent_row(direction)
        if row is not None:
            self.segments_table.selectRow(row)
            self.player.setPosition(0)
            self.play()

    def playback_finished(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_adjacent(1)

    def show_toast(self, message):
        self.toast_text.setText(message)
        self.toast.setVisible(bool(message))
        if message:
            self.toast_text.setMaximumWidth(max(400, self.width() - 300))
            self.toast.adjustSize()
            self.toast.move(max(218, self.width() - self.toast.width() - 18), self.height() - self.transport.height() - self.toast.height() - 8)
            self.toast.raise_()

    def update_script_stats(self):
        from .subtitles import parse_text
        text = self.text_edit.toPlainText()
        try:
            count = len(parse_text(text)) if text.strip() else 0
            self.script_stats.setText(f"{len(text.strip())} 字  ·  {count} 个片段")
        except ValueError:
            self.script_stats.setText(f"{len(text.strip())} 字  ·  请检查字幕格式")
        self.update_generation_actions()

    def current_voice(self):
        return next((v for v in self.voices if v["id"] == self.voice_combo.currentData()), None)

    def refresh_generation_models(self, preferred=None):
        preferred = preferred or self.voice_combo.currentData()
        voice = next((v for v in self.voices if v["id"] == preferred), None)
        voice = voice or next((v for v in self.voices if v["backend"] == "audio_cpp"), self.voices[0] if self.voices else None)
        entries, seen = [], set()
        for value in self.voices:
            key = model_key(value)
            if key not in seen:
                entries.append((key, model_label(value)))
                seen.add(key)
        populate(self.model_combo, entries, model_key(voice) if voice else None)
        self.model_controls.set_references(self.references)
        self.populate_generation_presets(voice["id"] if voice else None)

    def populate_generation_presets(self, preferred=None):
        from .library import friendly_name
        voices = [v for v in self.voices if model_key(v) == self.model_combo.currentData()]
        populate(self.voice_combo, [(v["id"], friendly_name(v)) for v in voices], preferred)
        self.update_voice_summary()
        self.update_link_config()

    def select_generation_model(self):
        self.stop_reference()
        self.active_voice_snapshot = None
        self.populate_generation_presets()

    def generation_voice(self):
        voice = self.current_voice()
        if not voice:
            return None
        return {**voice, **self.model_controls.overrides()}

    def update_generation_actions(self):
        if not hasattr(self, "generate_button"):
            return
        from .runner import check_voice
        voice = self.generation_voice()
        usable = self.ready and bool(voice)
        if usable:
            try:
                check_voice(voice)
            except Exception as exc:
                usable = False
                self.voice_health.setText(str(exc))
        elif not self.ready:
            self.voice_health.setText("等待本地服务连接")
        self.voice_health.setVisible(not usable)
        self.new_document_button.setEnabled(not getattr(self, "submitting", False))
        self.jobs_table.setEnabled(not getattr(self, "submitting", False))
        self.warmup_button.setEnabled(usable and not getattr(self, "submitting", False))
        self.generate_button.setEnabled(usable and not getattr(self, "submitting", False) and bool(self.text_edit.toPlainText().strip()))
        self.save_generation_button.setEnabled(usable)
        self.model_controls.advanced_toggle.setEnabled(bool(voice))

    def update_voice_summary(self, *args):
        if args:
            self.active_voice_snapshot = None
        voice = getattr(self, "active_voice_snapshot", None) or self.current_voice()
        if voice:
            changed = self.model_controls.key is None or self.model_controls.key[0] != voice.get("model_name", voice["id"])
            if changed:
                self.stop_reference()
            self.model_controls.bind(voice)
            if changed:
                self.inspector_scroll.verticalScrollBar().setValue(0)
        self.refresh_main_settings()

    def refresh_main_settings(self):
        voice = self.generation_voice()
        if voice:
            info = self.model_controls.info
            self.voice_family.setText(("audio.cpp 统一引擎" if voice["backend"] == "audio_cpp" else "官方 Python 引擎" if voice["backend"] == "index_tts" else "系统语音引擎") +
                                      ("  ·  CUDA" if voice.get("device") == "cuda" and voice["backend"] != "windows_sapi" else ""))
            self.model_combo.setToolTip(voice.get("model_dir") or "Windows 系统声音")
            mode = self.model_controls.mode.currentData()
            if mode == "builtin":
                summary = "使用模型内置音色" if info["builtin_voices"] else "使用 Windows 系统声音"
            elif mode == "design":
                summary = "按声音描述设计音色，无需参考音频"
            else:
                summary = "精细克隆需填写参考原文" if mode == "continuation" else "使用参考音频克隆声音"
            if info["language_auto"]:
                summary += "\n自动识别文本语言"
            summary += "\n当前设置可直接生成；保存预设后可用于双语工作流。"
            self.voice_summary.setText(summary)
            self.voice_health.setText("路径检查通过 · 可短句试音")
            self.voice_health.setToolTip(voice.get("reference_audio") or "")
        self.update_generation_actions()

    def set_service_state(self, text, state):
        self.banner.setText(text)
        self.banner.setProperty("state", state)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.update_generation_actions()

    def preview_segment(self):
        item = self.current_segment()
        playable = bool(item and item["status"] == "done" and item.get("audio") and (self.data_dir / "audio" / item["audio"]).is_file())
        source = self.data_dir / "audio" / item["audio"] if playable else None
        media_url = QUrl.fromLocalFile(str(source)) if source else QUrl()
        if source != self.waveform.path or self.player.source() != media_url:
            self.player.stop()
            self.player.setSource(media_url)
            self.waveform.load(source)
            self.clip_duration = 0
            if source:
                from .runner import validate_wave
                try:
                    self.clip_duration = round(validate_wave(source)["duration"] * 1000)
                except Exception as exc:
                    self.statusBar().showMessage("音频文件无法读取：" + str(exc), 8000)
                    playable = False
        self.play_button.setEnabled(playable)
        if playable and not getattr(self, "clip_duration", 0):
            self.play_button.setEnabled(False)
        self.details_button.setEnabled(bool(item))
        self.previous_button.setEnabled(self.adjacent_row(-1) is not None)
        self.next_button.setEnabled(self.adjacent_row(1) is not None)
        self.clip_title.setEnabled(bool(self.job_data and self.job_data["segments"]))
        title = f"{item.get('batch_title') or self.job_data['title']} · {item['text']}" if item else "生成记录" if self.job_data and self.job_data["segments"] else "还没有生成记录"
        self.clip_title.setText(self.clip_title.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 184))
        self.clip_title.setToolTip(title + "\n点击展开生成列表")
        self.update_transport()

    def toggle_playback(self):
        self.settings.player.pause()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.play()

    def update_play_icon(self, state):
        self.play_button.setIcon(icon("pause" if state == QMediaPlayer.PlaybackState.PlayingState else "play", "#ffffff"))
        text = "暂停" if state == QMediaPlayer.PlaybackState.PlayingState else "播放"
        self.play_button.setToolTip(text)
        self.play_button.setAccessibleName(text)

    def update_transport(self, *_):
        duration, position = self.player.duration() or getattr(self, "clip_duration", 0), self.player.position()
        def clock(ms):
            seconds = ms // 1000
            return f"{seconds // 60:02d}:{seconds % 60:02d}.{ms % 1000 // 100}"
        self.clip_time.setText(f"{clock(position)} / {clock(duration)}")
        self.seek_slider.setRange(0, duration)
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self.waveform.set_progress(position / duration if duration else 0)

    def decorate_job(self, job):
        from .gui import STATES
        counts = {state: sum(s["status"] == state for s in job["segments"]) for state in STATES}
        done, total = counts["done"], len(job["segments"])
        self.job_summary.setVisible(total > 0)
        self.job_summary.setText(f"{STATES[job['status']]} · {done}/{total}" + (f" · {counts['failed']} 段失败" if counts["failed"] else ""))
        self.job_summary.setToolTip(job["title"])
        self.clip_list_title.setText(job["title"] + " · 生成记录")
        self.job_progress.setRange(0, max(1, total))
        self.job_progress.setValue(done)
        self.job_progress.setVisible(job["status"] in ("running", "queued"))
        self.export_button.setEnabled(done > 0)
        self.retry_button.setEnabled(counts["failed"] + counts["cancelled"] > 0)
        self.cancel_button.setEnabled(job["status"] in ("running", "queued"))
        self.segments_table.setColumnHidden(4, not counts["failed"])
        self.result_hint.setText("双击失败片段，查看具体原因、修复建议和原始异常。" if counts["failed"] else "点击片段播放 · 上一个 / 下一个切换 · 每次生成保留在当前作品")
        for row, item in enumerate(job["segments"]):
            color = "#ad5427" if item["status"] == "failed" else "#3c7a51" if item["status"] == "done" else "#88888e"
            cell = self.segments_table.item(row, 3)
            if cell.foreground().color() != QColor(color):
                cell.setForeground(QColor(color))
        self.preview_segment()
