"""Model-dependent controls shared by the editor and creation inspector."""
import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                            QButtonGroup, QHBoxLayout, QLineEdit, QPlainTextEdit, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .model_info import KOKORO_LANGUAGES, LANGUAGES, MODES, model_ui, selected_mode
from .ui import NoWheelComboBox, NoWheelSpinBox, button, icon, label
from .generation_controls import GenerationControls
from .generation_options import compatible_options
from .index_emotion_controls import IndexEmotionControls
from .index_emotion import EMOTION_DEFAULTS


class DescriptionInput(QPlainTextEdit):
    def setText(self, text):
        self.setPlainText(text)

    def text(self):
        return self.toPlainText()


def populate(combo, entries, current=None):
    combo.blockSignals(True)
    combo.clear()
    for value, name in entries:
        combo.addItem(name, value)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    combo.blockSignals(False)


def voice_label(value):
    qwen_names = {"serena": "Serena", "vivian": "Vivian", "uncle_fu": "Uncle Fu", "ryan": "Ryan",
                  "aiden": "Aiden", "ono_anna": "Ono Anna", "sohee": "Sohee", "eric": "Eric", "dylan": "Dylan"}
    if value in qwen_names:
        return qwen_names[value]
    gender = "女声" if len(value) > 1 and value[1] == "f" else "男声" if len(value) > 1 and value[1] == "m" else ""
    names = {"zf_xiaobei": "小北", "zf_xiaoni": "小妮", "zf_xiaoxiao": "小小", "zf_xiaoyi": "小伊",
             "zm_yunjian": "云健", "zm_yunxi": "云希", "zm_yunxia": "云夏", "zm_yunyang": "云扬"}
    return " · ".join(part for part in (names.get(value, value), gender, value if value in names else "") if part)


def populate_voices(combo, info, language, current=None):
    voices = info.get("builtin_voices", [])
    if info["family"] == "kokoro_tts":
        voices = [v for v in voices if KOKORO_LANGUAGES.get(v[:1]) == language]
    populate(combo, [(v, voice_label(v)) for v in voices], current)
    return voices


class ModelControls(QWidget):
    changed = pyqtSignal()
    preview_requested = pyqtSignal(str)
    audio_changed = pyqtSignal()
    choose_requested = pyqtSignal()
    engine_download_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modelControls")
        self.info = None
        self.voice = None
        self.key = None
        self.references = []
        self.reference_path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.form_body = QWidget()
        self.form_body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.form = QFormLayout(self.form_body)
        layout.addWidget(self.form_body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(8)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode = QComboBox(self)
        self.mode.hide()
        self.mode_strip = QWidget()
        self.mode_strip.setObjectName("segmented")
        self.mode_layout = QHBoxLayout(self.mode_strip)
        self.mode_layout.setContentsMargins(3, 3, 3, 3)
        self.mode_layout.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.language = QComboBox()
        self.native_voice = QComboBox()
        for combo in (self.mode, self.language, self.native_voice):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 2)
        self.speed.setSingleStep(.1)
        self.speed.setSuffix(" ×")
        self.speed.hide()
        self.speed_row = QWidget()
        speed_layout = QHBoxLayout(self.speed_row)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setAccessibleName("语速")
        speed_layout.addWidget(self.speed_slider, 1)
        self.speed_value = button("1.00×", lambda: self.speed.setValue(1), kind="ghost")
        self.speed_value.setToolTip("重置为正常语速")
        speed_layout.addWidget(self.speed_value)
        self.speed_slider.valueChanged.connect(lambda value: self.speed.setValue(value / 100))
        self.speed.valueChanged.connect(self.sync_speed)
        self.index_emotion = IndexEmotionControls()
        self.emotion = self.index_emotion.text_input
        self.index_emotion.changed.connect(self.changed)
        self.index_emotion.audio_changed.connect(self.audio_changed)
        self.description = DescriptionInput()
        self.description.setFixedHeight(88)
        self.description.setPlaceholderText("例如：温柔女声，语速舒缓，自然演绎")
        self.reference = QWidget()
        reference_layout = QVBoxLayout(self.reference)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        reference_layout.setSpacing(8)
        self.reference_combo = QComboBox()
        self.reference_combo.setMinimumContentsLength(8)
        self.reference_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.reference_combo.hide()
        reference_actions = QHBoxLayout()
        reference_actions.setSpacing(4)
        self.reference_choose = button("选择参考声音…", self.choose_requested, "voices", "referenceChoice")
        reference_actions.addWidget(self.reference_choose, 1)
        self.reference_play = button("", lambda: self.preview_requested.emit(self.reference_path), "play", "icon")
        self.reference_play.setFixedWidth(32)
        self.reference_play.setToolTip("试听参考声音")
        reference_actions.addWidget(self.reference_play)
        reference_layout.addLayout(reference_actions)
        self.reference_hint = label("选择 1–60 秒的 WAV 音频", "hint")
        self.reference_hint.setWordWrap(True)
        reference_layout.addWidget(self.reference_hint)
        self.reference_text = QPlainTextEdit()
        self.reference_text.setFixedHeight(72)
        self.reference_text.setPlaceholderText("填写参考音频中的原文")
        for text, widget in (("生成方式", self.mode_strip), ("生成语言", self.language), ("内置音色", self.native_voice),
                             ("参考声音", self.reference), ("参考原文", self.reference_text),
                             ("声音描述", self.description), ("语速", self.speed_row), ("表达方式", self.index_emotion)):
            self.form.addRow(text, widget)
            self.form.labelForField(widget).setProperty("role", "muted")
        self.advanced_toggle = button("高级设置", lambda: None, "chevron", "disclosure")
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(12)
        self.generation_controls = GenerationControls()
        self.generation_controls.changed.connect(self.changed)
        from .parameter_preset_controls import ParameterPresetControls
        self.parameter_presets = ParameterPresetControls(self)
        advanced_layout.addWidget(self.parameter_presets)
        advanced_layout.addWidget(self.generation_controls)
        self.compatibility_hint = label("", "hint")
        self.compatibility_hint.setWordWrap(True)
        self.compatibility_hint.hide()
        advanced_layout.addWidget(self.compatibility_hint)
        self.runtime_toggle = button("模型与运行环境", lambda: None, "chevron", "disclosure")
        self.runtime_toggle.setCheckable(True)
        advanced_layout.addWidget(self.runtime_toggle)
        self.runtime = QWidget()
        self.advanced_form = QFormLayout(self.runtime)
        self.advanced_form.setContentsMargins(0, 0, 0, 0)
        self.advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.advanced_form.setSpacing(10)
        self.device = NoWheelComboBox()
        populate(self.device, [("cuda", "NVIDIA CUDA"), ("cpu", "CPU")], "cuda")
        self.timeout = NoWheelSpinBox()
        self.timeout.setRange(10, 3600)
        self.timeout.setSuffix(" 秒")
        self.advanced_form.addRow("计算设备", self.device)
        self.advanced_form.addRow("片段超时", self.timeout)
        self.assets, self.asset_rows = {}, {}
        for key, title in (("model_dir", "模型文件 / 目录"), ("engine_executable", "统一推理引擎"),
                           ("python", "Python 环境"), ("repo_dir", "官方源码目录")):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            field = QLineEdit()
            field.setMaxLength(4096)
            self.assets[key], self.asset_rows[key] = field, row
            row_layout.addWidget(field, 1)
            picker = button("", lambda _, k=key: self.browse_asset(k), "folder")
            picker.setFixedWidth(34)
            picker.setToolTip("选择" + title)
            row_layout.addWidget(picker)
            if key == "engine_executable":
                self.engine_download_button = button("下载", lambda: self.engine_download_requested.emit(self.device.currentData()), "import", "small")
                self.engine_download_button.setToolTip("未安装引擎时，下载到程序数据目录并自动配置。")
                row_layout.addWidget(self.engine_download_button)
                field.textChanged.connect(lambda: self.update_engine_download())
            self.advanced_form.addRow(title, row)
            field.textChanged.connect(self.changed)
            field.editingFinished.connect(self.refresh_capabilities)
        self.asset_hint = label("模型能力随文件自动识别；保存预设后可再次选择。", "hint")
        self.asset_hint.setWordWrap(True)
        self.advanced_form.addRow(self.asset_hint)
        advanced_layout.addWidget(self.runtime)
        self.runtime.hide()
        self.runtime_toggle.toggled.connect(self.runtime.setVisible)
        layout.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda checked: self.advanced_toggle.setIcon(icon("chevron-down" if checked else "chevron")))
        self.runtime_toggle.toggled.connect(lambda checked: self.runtime_toggle.setIcon(icon("chevron-down" if checked else "chevron")))
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.language.currentIndexChanged.connect(self.language_changed)
        self.native_voice.currentIndexChanged.connect(self.changed)
        self.speed.valueChanged.connect(self.changed)
        self.description.textChanged.connect(self.description_changed)
        self.reference_combo.currentIndexChanged.connect(self.reference_selected)
        self.reference_text.textChanged.connect(self.changed)
        self.device.currentIndexChanged.connect(self.changed)
        self.device.currentIndexChanged.connect(lambda: self.update_engine_download())
        self.timeout.valueChanged.connect(self.changed)
        self.hide()

    def bind(self, voice, preserve_edits=True, custom=False):
        info = voice.get("model_ui") or model_ui(voice)
        key = (voice.get("model_name", voice["id"]), json.dumps(info, sort_keys=True))
        if preserve_edits and key == self.key:
            self.parameter_presets.bind()
            return
        preset = voice
        keep_editing = False
        same_model = self.voice and self.voice.get("backend") == voice.get("backend") and self.voice.get("model_dir") == voice.get("model_dir")
        if preserve_edits and self.voice and (self.voice["id"] == voice["id"] or (custom and same_model)):
            from .schemas import VoiceConfig
            exclude = {"engine_executable", "device"}
            same_preset = VoiceConfig(**self.voice).model_dump(exclude=exclude) == VoiceConfig(**voice).model_dump(exclude=exclude)
            edits = self.overrides()
            if custom or (same_preset and edits.get("model_dir") == self.voice.get("model_dir")):
                # Installing an engine must not discard text/style/reference edits made during download.
                voice = {**voice, **{k: v for k, v in edits.items() if k not in exclude}}
                voice.update(reference_audio=self.reference_path, reference_text=self.reference_text.toPlainText(),
                             voice_description=self.description.text())
                if custom:
                    info = model_ui(voice)
                    key = (preset.get("model_name", preset["id"]), json.dumps(info, sort_keys=True))
                keep_editing = True
        advanced_open, runtime_open = self.advanced_toggle.isChecked(), self.runtime_toggle.isChecked()
        self.key, self.voice, self.info = key, preset, info
        self.blockSignals(True)
        populate(self.mode, [(mode, MODES[mode]) for mode in info["modes"]], selected_mode(voice, info))
        self.rebuild_modes()
        self.mode.setEnabled(len(info["modes"]) > 1)
        native = voice.get("native_voice", "")
        language = KOKORO_LANGUAGES.get(native[:1], voice.get("language")) if info["family"] == "kokoro_tts" else voice.get("language")
        populate(self.language, [(v, LANGUAGES.get(v, v)) for v in info["languages"]], language)
        populate_voices(self.native_voice, info, self.language.currentData(), native)
        self.speed.setValue(voice.get("speed", 1) if info["speed"] else 1)
        self.index_emotion.bind(voice if info["emotion"] else {**EMOTION_DEFAULTS, "emotion": ""})
        self.description.setText(voice.get("voice_description", ""))
        self.reference_path = voice.get("reference_audio", "")
        self.refresh_references()
        self.reference_text.setPlainText(voice.get("reference_text", ""))
        self.bind_generation_options(voice, info)
        self.device.setCurrentIndex(max(0, self.device.findData(voice.get("device", "cuda"))))
        self.timeout.setValue(voice.get("timeout", 600))
        for name, field in self.assets.items():
            field.setText(voice.get(name, ""))
            field.setToolTip(voice.get(name, ""))
        self.advanced_toggle.setChecked(keep_editing and advanced_open)
        self.runtime_toggle.setChecked(keep_editing and runtime_open)
        self.apply_visibility()
        self.blockSignals(False)
        self.show()
        self.parameter_presets.bind()

    def apply_visibility(self):
        if not self.info:
            return
        mode = self.mode.currentData()
        self.form.setRowVisible(self.mode_strip, len(self.info["modes"]) > 1)
        builtin = mode == "builtin" and bool(self.info["builtin_voices"])
        self.form.setRowVisible(self.language, not self.info["language_auto"])
        self.form.setRowVisible(self.native_voice, builtin)
        cloning = mode in ("clone", "continuation")
        self.form.setRowVisible(self.reference, cloning)
        self.form.setRowVisible(self.reference_text, cloning and self.info["reference_text"])
        self.reference_text.setPlaceholderText("必须与参考音频原文一致" if mode == "continuation" else "可选：参考音频中的原文")
        self.form.setRowVisible(self.speed_row, self.info["speed"])
        self.form.setRowVisible(self.index_emotion, self.info["emotion"])
        self.emotion.setVisible(self.info["emotion"])
        describe = mode == "design" or (self.info["family"] == "voxcpm2" and mode == "clone") or (self.info["family"] == "qwen3_tts" and mode == "builtin")
        self.form.setRowVisible(self.description, describe)
        instruction = self.info["family"] == "qwen3_tts" and mode == "builtin"
        self.form.labelForField(self.description).setText("风格指令（可选）" if instruction else "声音描述")
        self.description.setPlaceholderText("例如：用温柔、平静的语气朗读" if instruction else "例如：温柔女声，语速舒缓，自然演绎")
        backend = self.voice.get("backend")
        self.advanced_form.setRowVisible(self.device, backend != "windows_sapi")
        for name, row in self.asset_rows.items():
            visible = (name == "model_dir" and backend != "windows_sapi") or (name == "engine_executable" and backend == "audio_cpp") or (name in ("python", "repo_dir") and backend == "index_tts")
            self.advanced_form.setRowVisible(row, visible)
        self.asset_hint.setVisible(backend != "windows_sapi")

    def bind_generation_options(self, voice, info):
        options, notes = compatible_options(voice, info)
        self.generation_controls.bind(info.get("generation_options", []), options)
        self.compatibility_hint.setText("\n".join(notes))
        self.compatibility_hint.setVisible(bool(notes))
        self.advanced_toggle.setToolTip("\n".join(notes))

    def description_changed(self):
        if len(self.description.text()) > 500:
            cursor = self.description.textCursor()
            position = cursor.position()
            self.description.blockSignals(True)
            self.description.setText(self.description.text()[:500])
            cursor = self.description.textCursor()
            cursor.setPosition(min(position, 500))
            self.description.setTextCursor(cursor)
            self.description.blockSignals(False)
        self.changed.emit()

    def update_engine_download(self, engine=None):
        if engine is not None:
            self.engine_state = engine
        state = getattr(self, "engine_state", {})
        path = self.assets["engine_executable"].text()
        missing = not path or not Path(path).is_file()
        option = next((e for e in state.get("options", []) if e["backend"] == self.device.currentData()), {})
        busy = option.get("status") in ("downloading", "verifying", "extracting", "waiting", "pausing")
        self.engine_download_button.setVisible(missing)
        self.engine_download_button.setEnabled(not busy and state.get("supported", True))
        self.engine_download_button.setText("安装中" if busy else "下载")
        self.engine_download_button.setToolTip(option.get("error") or "下载到程序的 data/runtime/，完成后自动配置。")

    def mode_changed(self):
        for b in self.mode_group.buttons():
            b.setChecked(b.property("mode") == self.mode.currentData())
        self.apply_visibility()
        self.changed.emit()

    def language_changed(self):
        if self.info and self.info["family"] == "kokoro_tts":
            self.blockSignals(True)
            populate_voices(self.native_voice, self.info, self.language.currentData(), self.native_voice.currentData())
            self.blockSignals(False)
            self.changed.emit()
        else:
            self.changed.emit()

    def set_references(self, voices):
        self.references = [dict(v) for v in voices if v.get("reference_audio")]
        self.index_emotion.set_references(self.references)
        self.refresh_references()

    def refresh_references(self):
        entries = [("", "选择参考声音…")]
        seen = set()
        for reference in self.references:
            path, name = reference["reference_audio"], reference["name"]
            if path not in seen:
                entries.append((path, name))
                seen.add(path)
        if self.reference_path and self.reference_path not in seen:
            entries.append((self.reference_path, Path(self.reference_path).name))
        populate(self.reference_combo, entries, self.reference_path)
        self.reference_combo.setToolTip(self.reference_path)
        title = next((name for path, name in entries if path == self.reference_path), "选择参考声音…") if self.reference_path else "选择参考声音…"
        self.reference_choose.setText(title[:24] + "…" if len(title) > 24 else title)
        self.reference_choose.setToolTip(self.reference_path or "从声音库选择或导入参考音频")
        self.reference_play.setEnabled(bool(self.reference_path))
        if not self.reference_path:
            self.reference_hint.setText("选择 1–60 秒的 WAV 音频")
        else:
            from .runner import validate_wave
            try:
                data = validate_wave(Path(self.reference_path))
                self.reference_hint.setText(f"{data['duration']:.1f} 秒  ·  {data['sample_rate'] / 1000:g} kHz  ·  WAV")
            except Exception:
                self.reference_hint.setText("音频无法读取，请重新选择")

    def reference_selected(self):
        self.reference_path = self.reference_combo.currentData() or ""
        reference = next((r for r in self.references if r["reference_audio"] == self.reference_path), None)
        if reference:
            self.reference_text.setPlainText(reference.get("reference_text", ""))
        self.refresh_references()
        self.audio_changed.emit()
        self.changed.emit()

    def select_reference(self, reference):
        self.reference_path = reference["reference_audio"]
        if not any(r["reference_audio"] == self.reference_path for r in self.references):
            self.references.append(reference)
        self.reference_text.setPlainText(reference.get("reference_text", ""))
        self.refresh_references()
        self.audio_changed.emit()
        self.changed.emit()

    def sync_speed(self, value):
        self.speed_slider.blockSignals(True)
        self.speed_slider.setValue(round(value * 100))
        self.speed_slider.blockSignals(False)
        self.speed_value.setText(f"{value:.2f}×")

    def rebuild_modes(self):
        while self.mode_layout.count():
            item = self.mode_layout.takeAt(0)
            if item.widget():
                self.mode_group.removeButton(item.widget())
                item.widget().hide()
                item.widget().deleteLater()
        short = {"clone": "音色克隆", "continuation": "精细克隆", "design": "声音设计", "builtin": "内置音色"}
        order = sorted(range(self.mode.count()), key=lambda i: ["design", "clone", "continuation", "builtin"].index(self.mode.itemData(i)))
        for index in order:
            mode = self.mode.itemData(index)
            control = button(short[mode], lambda _, m=mode: self.mode.setCurrentIndex(self.mode.findData(m)), kind="segment")
            control.setProperty("mode", mode)
            control.setCheckable(True)
            control.setChecked(mode == self.mode.currentData())
            self.mode_group.addButton(control)
            self.mode_layout.addWidget(control, 1)

    def browse_reference(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择参考声音（1–60 秒）", "", "WAV 音频 (*.wav)")
        if path:
            self.reference_path = path
            self.refresh_references()
            self.audio_changed.emit()
            self.changed.emit()

    def browse_asset(self, name):
        if name == "repo_dir" or (name == "model_dir" and self.voice.get("backend") == "index_tts"):
            path = QFileDialog.getExistingDirectory(self, "选择目录")
        else:
            pattern = "GGUF 模型 (*.gguf)" if name == "model_dir" else "程序 (*.exe);;所有文件 (*)"
            path, _ = QFileDialog.getOpenFileName(self, "选择模型或引擎", "", pattern)
        if path:
            self.assets[name].setText(path)
            self.refresh_capabilities()

    def refresh_capabilities(self):
        if not self.voice:
            return
        voice = {**self.voice, **self.overrides()}
        info = model_ui(voice)
        # A replacement package identifies its own family; users don't need to guess it.
        actual = info.get("actual_family")
        if actual in ("index_tts2", "voxcpm2", "kokoro_tts", "qwen3_tts") and actual != voice.get("family"):
            voice["family"] = actual
            info = model_ui(voice)
        self.blockSignals(True)
        old_family = self.info["family"]
        self.info = info
        populate(self.mode, [(v, MODES[v]) for v in info["modes"]], voice.get("voice_mode"))
        self.rebuild_modes()
        self.mode.setEnabled(len(info["modes"]) > 1)
        populate(self.language, [(v, LANGUAGES.get(v, v)) for v in info["languages"]], voice.get("language"))
        populate_voices(self.native_voice, info, self.language.currentData(), voice.get("native_voice"))
        if old_family != info["family"]:
            self.description.clear()
            self.index_emotion.bind({**EMOTION_DEFAULTS, "emotion": ""})
            self.speed.setValue(1)
        self.bind_generation_options(voice if old_family == info["family"] else {**voice, "request_options": {}}, info)
        self.apply_visibility()
        self.blockSignals(False)
        self.parameter_presets.bind()
        self.changed.emit()

    def overrides(self):
        if not self.info:
            return {}
        mode = self.mode.currentData()
        result = {"voice_mode": mode, "speed": self.speed.value() if self.info["speed"] else 1,
                  "emotion": self.emotion.text().strip() if self.info["emotion"] else "",
                  "voice_description": self.description.text().strip() if not self.description.isHidden() else ""}
        result.update(self.index_emotion.values() if self.info["emotion"] else EMOTION_DEFAULTS)
        if mode == "builtin" and self.info["builtin_voices"]:
            result["native_voice"] = self.native_voice.currentData()
        result["language"] = self.language.currentData() or self.voice.get("language", "zh")
        cloning = mode in ("clone", "continuation")
        result["reference_audio"] = self.reference_path if cloning else ""
        result["reference_text"] = self.reference_text.toPlainText().strip() if cloning and self.info["reference_text"] else ""
        result["device"] = self.device.currentData()
        result["timeout"] = self.timeout.value()
        result["request_options"] = self.generation_controls.values()
        if self.voice.get("backend") == "audio_cpp":
            result["family"] = self.info["family"]
        for name, field in self.assets.items():
            if not self.asset_rows[name].isHidden():
                result[name] = field.text().strip()
        return result
