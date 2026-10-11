"""Installed into workflow src/widgets; a link connects all saved Shengjian voices."""
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QComboBox, QGridLayout, QLabel, QLineEdit, QPushButton, QWidget

from ..steps.local_voice_client import bind_discovered_voice, discover_voices, workflow_base_url


class DiscoverySignals(QObject):
    done = pyqtSignal(int, object, str)


class DiscoveryTask(QRunnable):
    def __init__(self, token, link):
        super().__init__()
        self.token, self.link = token, link
        self.signals = DiscoverySignals()

    def run(self):
        catalog, error = None, ""
        try:
            import requests
            with requests.Session() as session:
                session.trust_env = False
                catalog = discover_voices(session, self.link)
        except Exception as exc:
            error = "读取音色失败：" + str(exc)
        self.signals.done.emit(self.token, catalog, error)


class VoicePicker(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.catalog = None
        self.serial = 0
        self.tasks = {}
        self.applying = False
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(QLabel("声笺链接:"), 0, 0)
        self.link = QLineEdit()
        self.link.setPlaceholderText("粘贴声笺的接入链接")
        self.link.setToolTip("在声笺「工作流联动」复制接入链接；只需添加一次。")
        self.link.returnPressed.connect(self.load_link)
        layout.addWidget(self.link, 0, 1)
        self.connect_button = QPushButton("读取音色")
        self.connect_button.clicked.connect(self.load_link)
        layout.addWidget(self.connect_button, 0, 2)
        layout.addWidget(QLabel("音色:"), 1, 0)
        self.voices = QComboBox()
        self.voices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.voices.setMinimumContentsLength(18)
        self.voices.currentIndexChanged.connect(self.select_voice)
        layout.addWidget(self.voices, 1, 1)
        self.refresh_button = QPushButton("刷新音色")
        self.refresh_button.clicked.connect(self.refresh_current)
        self.refresh_button.setEnabled(False)
        layout.addWidget(self.refresh_button, 1, 2)
        self.hint = QLabel("连接后选择已保存的音色，再按上方批量设置生成。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.hint, 2, 0, 1, 3)
        self.clear_voices()

    def current_api(self):
        cfg = self.owner.config.tts_cfg
        index = cfg.get("current_api_index", 0)
        apis = cfg.get("api_configs", [])
        return index, apis[index] if 0 <= index < len(apis) else {}

    def selection_token(self):
        index, api = self.current_api()
        return index, api.get("url"), api.get("model")

    def clear_voices(self):
        self.voices.blockSignals(True)
        self.voices.clear()
        self.voices.addItem("尚未读取音色", None)
        self.voices.blockSignals(False)
        self.voices.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_from_config()

    def sync_from_config(self):
        if self.applying:
            return
        _, api = self.current_api()
        if not api.get("voice_discovery"):
            self.catalog = None
            self.clear_voices()
            self.refresh_button.setEnabled(False)
            self.hint.setText("粘贴声笺接入链接，读取后在这里选择音色。")
            return
        self.link.setText(api.get("discovery_url") or api.get("url", "").rstrip("/") + "/workflow")
        self.refresh_button.setEnabled(True)
        if self.catalog and self.catalog["base_url"] == api.get("url"):
            self.populate_voices(api)
        elif self.isVisible() and not any(t[1]["selection"] == self.selection_token() for t in self.tasks.values()):
            self.catalog = None
            self.clear_voices()
            self.refresh_current()

    def load_link(self):
        self.start_request(self.link.text(), importing=True)

    def refresh_current(self):
        _, api = self.current_api()
        if api.get("voice_discovery"):
            self.start_request(api["url"], importing=False)

    def start_request(self, link, importing):
        try:
            base = workflow_base_url(link)
        except ValueError as exc:
            self.hint.setText(str(exc))
            return
        self.serial += 1
        task = DiscoveryTask(self.serial, base)
        context = {"importing": importing, "selection": self.selection_token()}
        self.tasks[self.serial] = task, context
        task.signals.done.connect(self.request_done)
        self.connect_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.voices.setEnabled(False)
        self.hint.setText("正在读取音色…")
        QThreadPool.globalInstance().start(task)

    @pyqtSlot(int, object, str)
    def request_done(self, token, catalog, error):
        _, context = self.tasks.pop(token)
        if token != self.serial:
            return
        self.connect_button.setEnabled(True)
        _, api = self.current_api()
        self.refresh_button.setEnabled(bool(api.get("voice_discovery")))
        if context["selection"] != self.selection_token():
            # A result for a previously selected server must not overwrite the new selection.
            self.sync_from_config()
            return
        if error:
            self.hint.setText(error)
            self.owner.log_message.emit("[声笺音色] " + error)
            return
        self.applying = True
        try:
            cfg = self.owner.config.tts_cfg
            apis = cfg.setdefault("api_configs", [])
            if context["importing"]:
                index = next((i for i, a in enumerate(apis) if a.get("voice_discovery")
                              and a.get("url") == catalog["base_url"]), None)
                if index is None:
                    index = len(apis)
                    apis.append({"name": "声笺 · 本地音色", "url": catalog["base_url"],
                                 "model": "", "model_tag": "", "status": "unknown"})
                cfg["current_api_index"] = index
                apis[index].update(voice_discovery=True, discovery_url=catalog["discovery_url"])
                self.owner._refresh_api_combo()
            self.catalog = catalog
            _, api = self.current_api()
            self.populate_voices(api)
            if self.voices.currentData() is not None:
                self.select_voice()
            else:
                self.owner.config.save()
                self.owner.config_changed.emit()
            self.refresh_button.setEnabled(True)
        finally:
            self.applying = False

    def populate_voices(self, api):
        self.voices.blockSignals(True)
        self.voices.clear()
        selected = api.get("voice_id") or api.get("model", "").partition("@")[0]
        first_available = -1
        for voice in self.catalog["voices"]:
            caption = voice["name"] + " · " + voice.get("mode_name", "")
            if not voice["available"]:
                caption += "（不可用）"
            self.voices.addItem(caption, voice["id"])
            row = self.voices.count() - 1
            error = voice.get("error") or {}
            tooltip = " · ".join(filter(None, (voice["name"], voice.get("model"), voice.get("mode_name"),
                                                  voice.get("language_name"), voice.get("native_voice"))))
            if error:
                tooltip += "\n" + error.get("message", "") + "\n" + error.get("hint", "")
            self.voices.setItemData(row, tooltip, Qt.ItemDataRole.ToolTipRole)
            self.voices.model().item(row).setEnabled(voice["available"])
            if voice["available"] and first_available < 0:
                first_available = row
        row = self.voices.findData(selected) if selected else first_available
        if selected and row < 0:
            self.voices.addItem((api.get("voice_name") or selected) + "（已删除，请重新选择）", selected)
            row = self.voices.count() - 1
            self.voices.model().item(row).setEnabled(False)
        if row < 0:
            self.voices.addItem("没有可用音色，请先在声笺保存声音预设", None)
            row = self.voices.count() - 1
        self.voices.setCurrentIndex(row)
        self.voices.blockSignals(False)
        self.voices.setEnabled(first_available >= 0)
        self.hint.setText(f"已读取 {len(self.catalog['voices'])} 个音色；可用 {sum(v['available'] for v in self.catalog['voices'])} 个。")

    def select_voice(self, *_):
        if self.catalog is None or self.voices.currentData() is None:
            return
        index, api = self.current_api()
        try:
            updated = bind_discovered_voice(api, self.catalog, self.voices.currentData())
        except ValueError as exc:
            self.hint.setText(str(exc))
            return
        self.owner.config.tts_cfg["api_configs"][index] = updated
        self.owner.config.save()
        self.owner.config_changed.emit()
        voice = next(v for v in self.catalog["voices"] if v["id"] == updated["voice_id"])
        self.hint.setText("已选择：" + " · ".join(filter(None, (voice["name"], voice.get("model"),
                                                                      voice.get("mode_name"), voice.get("language_name")))))
        self.voices.setToolTip(self.voices.currentText())
