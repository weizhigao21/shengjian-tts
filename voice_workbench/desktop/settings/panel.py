"""Settings page assembly, refresh coordination and lifecycle."""
import json
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from ...ui import ElidedLabel, button, label
from ...runtime import resource_root
from ..audio_output import SystemAudioOutput
from .common import SettingsComponents
from .models import ModelSettings
from .engine import EngineSettings
from .audio import AudioSettings
from .preferences import PreferencesSettings


class SettingsPanel(ModelSettings, EngineSettings, AudioSettings, PreferencesSettings, SettingsComponents, QWidget):
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
        self.engine_catalog = json.loads((resource_root() / "voice_workbench/assets/engine_catalog.json").read_text(encoding="utf-8"))
        self.engine_release_url = self.engine_catalog["release"]
        self.output = SystemAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self.update_play_buttons)
        self.player.errorOccurred.connect(lambda _, text: self.owner.show_error("音频播放失败：" + text))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 18)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        title = label("设置", "settingsTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.data_location = ElidedLabel("数据目录 · " + str(self.owner.data_dir))
        self.data_location.setProperty("role", "muted")
        self.data_location.setToolTip(f"数据库、声音预设、音频和下载文件保存在这里\n{self.owner.data_dir}")
        self.open_files_button = button("打开本地文件", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.owner.data_dir))), "folder", "ghost")
        self.open_files_button.setToolTip(f"打开配置、任务、模型和音频所在的数据目录\n{self.owner.data_dir}")
        heading.addWidget(button("刷新", self.refresh, "retry", "ghost"))
        layout.addLayout(heading)
        tabs = QHBoxLayout()
        self.tab_buttons = []
        for index, title in ((3, "常规"), (0, "模型"), (4, "推理引擎"), (1, "生成音频"), (2, "批量性能")):
            control = button(title, lambda _, i=index: self.set_section(i), kind="settingsTab")
            control.setProperty("section_index", index)
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
        self._general_page()
        self._engine_page()
        self.set_section(0)
        self.timer = QTimer(self)
        self.timer.setInterval(1800)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def set_section(self, index):
        self.pages.setCurrentIndex(index)
        for i, control in enumerate(self.tab_buttons):
            control.setChecked(control.property("section_index") == index)
        if hasattr(self, "timer"):
            self.refresh()

    def refresh(self, *_):
        if not self.isVisible() or not self.owner.ready or self.loading or self.deleting:
            return
        from ..api_client import checked
        section = self.pages.currentIndex()
        if section == 3:
            return
        self.loading = True
        if section == 2:
            def performance_loaded(value):
                self.loading = False
                if not self.performance_dirty:
                    for control, key in ((self.workflow_workers, "workflow_workers"), (self.cpu_threads, "cpu_threads")):
                        control.blockSignals(True)
                        control.setValue(value[key])
                        control.blockSignals(False)
                self.performance_notice.setText(value.get("notice", ""))
                self.batch_inference.refresh()
            def performance_failed(message):
                self.loading = False
                self.owner.statusBar().showMessage("性能设置读取失败：" + message, 6000)
            self.owner.call(lambda c: checked(c.get("/settings/performance")), performance_loaded, performance_failed)
            return
        def fetched(values):
            self.loading = False
            if section in (0, 4) and values != self.model_data:
                self.model_data = values
                self.render_models()
            if section == 1 and values != self.audio_data:
                audio = values
                self.audio_data = audio
                self.selected_ids.intersection_update(a["id"] for a in audio)
                if self.playing_id and self.playing_id not in {a["id"] for a in audio}:
                    self.stop()
                self.render_audio()
        def failed(message):
            self.loading = False
            self.owner.statusBar().showMessage("管理列表刷新失败：" + message, 6000)
        endpoint = "/generated-audio" if section == 1 else "/models"
        self.owner.call(lambda c: checked(c.get(endpoint)), fetched, failed)

    def mutate(self, action, message=""):
        from ..api_client import checked
        def done(value):
            self.owner.active_voice_snapshot = None
            self.refresh()
            self.owner.poll()
            if message:
                self.owner.statusBar().showMessage(message, 4500)
        self.owner.call(lambda c: checked(action(c)), done)

    def close_management(self):
        self.app_updates.close_updates()
        self.timer.stop()
        self.stop()
