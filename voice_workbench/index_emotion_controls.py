"""IndexTTS expression panel, shared by the inspector and preset editor."""
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QGridLayout, QHBoxLayout,
                            QLineEdit, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from .index_emotion import EMOTION_NAMES, EMOTION_PRESETS, emotion_mode
from .ui import button, icon, label


class EmotionSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


class IndexEmotionControls(QWidget):
    reference_role = "emotion"
    changed = pyqtSignal()
    audio_changed = pyqtSignal()
    choose_requested = pyqtSignal()
    preview_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "speaker"
        self.reference_path = ""
        self.references = []
        self.binding = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        strip = QWidget()
        strip.setObjectName("segmented")
        row = QHBoxLayout(strip)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, title in (("speaker", "跟随音色"), ("reference", "参考演绎"),
                            ("vector", "情绪调节"), ("text", "文字引导")):
            control = button(title, lambda _, m=mode: self.set_mode(m), kind="segment")
            control.setCheckable(True)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            self.mode_group.addButton(control)
            self.mode_buttons[mode] = control
            row.addWidget(control, 1)
        layout.addWidget(strip)
        self.speaker_hint = label("沿用音色参考中的自然情绪。", "hint")
        self.speaker_hint.setWordWrap(True)
        layout.addWidget(self.speaker_hint)
        self.reference_panel = QWidget()
        reference_layout = QVBoxLayout(self.reference_panel)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.reference_choose = button("添加演绎参考音频", self.choose_requested, "voices", "referenceChoice")
        actions.addWidget(self.reference_choose, 1)
        self.reference_play = button("", lambda: self.preview_requested.emit(self.reference_path), "play", "icon")
        self.reference_play.setFixedWidth(32)
        self.reference_play.setToolTip("试听演绎参考")
        actions.addWidget(self.reference_play)
        reference_layout.addLayout(actions)
        self.reference_hint = label("仅提取情绪；音色仍使用上方参考声音。", "hint")
        self.reference_hint.setWordWrap(True)
        reference_layout.addWidget(self.reference_hint)
        layout.addWidget(self.reference_panel)
        self.text_panel = QWidget()
        text_layout = QVBoxLayout(self.text_panel)
        text_layout.setContentsMargins(0, 0, 0, 0)
        self.text_input = QLineEdit()
        self.text_input.setMaxLength(500)
        self.text_input.setPlaceholderText("例如：平静、温柔，带一点喜悦")
        self.text_input.setToolTip("描述情绪即可；文字描述不会作为台词朗读。")
        self.text_input.textChanged.connect(self.text_changed)
        text_layout.addWidget(self.text_input)
        self.infer_text = QCheckBox("从正文推断情绪")
        self.infer_text.toggled.connect(self.infer_changed)
        text_layout.addWidget(self.infer_text)
        layout.addWidget(self.text_panel)
        self.vector_panel = QWidget()
        vector_layout = QVBoxLayout(self.vector_panel)
        vector_layout.setContentsMargins(0, 0, 0, 0)
        title = QHBoxLayout()
        title.addWidget(label("情绪预设", "muted"), 1)
        title.addWidget(button("重置", lambda: self.set_vector([0.] * 8), kind="small"))
        vector_layout.addLayout(title)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.preset_buttons = {}
        for index, (name, vector) in enumerate(EMOTION_PRESETS.items()):
            control = button(name, lambda _, v=vector: self.set_vector(v), kind="small")
            control.setCheckable(True)
            control.setObjectName("emotionPreset")
            control.setToolTip("使用预设情绪组合；可在微调中继续调整。")
            grid.addWidget(control, index // 3, index % 3)
            self.preset_buttons[name] = control
        vector_layout.addLayout(grid)
        self.tune_toggle = button("微调情绪", lambda: None, "chevron", "disclosure")
        self.tune_toggle.setCheckable(True)
        vector_layout.addWidget(self.tune_toggle)
        self.tune_panel = QWidget()
        sliders_layout = QVBoxLayout(self.tune_panel)
        sliders_layout.setContentsMargins(0, 0, 0, 0)
        self.sliders = []
        for name in EMOTION_NAMES:
            widget, slider = self.slider_row(name)
            self.sliders.append(slider)
            slider.valueChanged.connect(self.vector_changed)
            sliders_layout.addWidget(widget)
        vector_layout.addWidget(self.tune_panel)
        self.tune_panel.hide()
        self.tune_toggle.toggled.connect(self.tune_panel.setVisible)
        self.tune_toggle.toggled.connect(lambda checked: self.tune_toggle.setIcon(icon("chevron-down" if checked else "chevron")))
        layout.addWidget(self.vector_panel)
        self.strength_panel, self.strength = self.slider_row("情绪强度")
        self.strength.setValue(60)
        self.strength.valueChanged.connect(lambda: self.changed.emit() if not self.binding else None)
        layout.addWidget(self.strength_panel)
        self.set_mode("speaker")

    @staticmethod
    def slider_row(name):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        title = label(name, "muted")
        title.setFixedWidth(56)
        row.addWidget(title)
        slider = EmotionSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setSingleStep(5)
        slider.setAccessibleName(name)
        row.addWidget(slider, 1)
        value = label("0%", "hint")
        value.setFixedWidth(36)
        row.addWidget(value)
        slider.value_label = value
        slider.valueChanged.connect(lambda n: value.setText(f"{n}%"))
        return widget, slider

    def set_mode(self, mode):
        self.mode = mode
        for key, control in self.mode_buttons.items():
            control.setChecked(key == mode)
        self.speaker_hint.setVisible(mode == "speaker")
        self.reference_panel.setVisible(mode == "reference")
        self.vector_panel.setVisible(mode == "vector")
        self.text_panel.setVisible(mode == "text")
        self.strength_panel.setVisible(mode != "speaker")
        if not self.binding:
            self.changed.emit()

    def text_changed(self):
        if self.binding:
            return
        # Keep the historical ModelControls.emotion.setText interface working.
        if self.text_input.text().strip() and self.mode == "speaker":
            self.set_mode("text")
        self.changed.emit()

    def infer_changed(self, checked):
        self.text_input.setEnabled(not checked)
        if not self.binding:
            self.changed.emit()

    def set_vector(self, vector):
        for slider, value in zip(self.sliders, vector):
            slider.blockSignals(True)
            slider.setValue(round(value * 100))
            slider.blockSignals(False)
            slider.value_label.setText(f"{slider.value()}%")
        self.vector_changed()

    def vector_changed(self):
        values = [slider.value() / 100 for slider in self.sliders]
        for name, control in self.preset_buttons.items():
            control.setChecked(values == EMOTION_PRESETS[name])
        if not self.binding:
            self.changed.emit()

    def bind(self, voice):
        self.binding = True
        try:
            self.text_input.setText(voice.get("emotion", ""))
            self.infer_text.setChecked(voice.get("emotion_infer_from_text", False))
            self.strength.setValue(round(voice.get("emotion_strength", .6) * 100))
            self.set_vector(voice.get("emotion_vector", [0.] * 8))
            self.reference_path = voice.get("emotion_reference_audio", "")
            self.refresh_reference()
            self.set_mode(emotion_mode(voice))
        finally:
            self.binding = False

    def set_references(self, references):
        self.references = list(references)
        self.refresh_reference()

    def select_reference(self, reference):
        self.reference_path = reference["reference_audio"]
        self.references.append(reference)
        self.refresh_reference()
        self.audio_changed.emit()
        self.changed.emit()

    def refresh_reference(self):
        name = next((r["name"] for r in self.references if r["reference_audio"] == self.reference_path),
                    Path(self.reference_path).name if self.reference_path else "添加演绎参考音频")
        self.reference_choose.setText(name[:22] + "…" if len(name) > 22 else name)
        self.reference_choose.setToolTip(self.reference_path or "选择 1–60 秒 WAV，提取其中的情绪。")
        self.reference_play.setEnabled(bool(self.reference_path))
        detail = "仅提取情绪；音色仍使用上方参考声音。"
        if self.reference_path:
            from .runner import validate_wave
            try:
                data = validate_wave(Path(self.reference_path))
                detail = f"{data['duration']:.1f} 秒 · {data['sample_rate'] / 1000:g} kHz · WAV；仅提取情绪。"
            except Exception:
                detail = "演绎参考无法读取，请重新选择。"
        self.reference_hint.setText(detail)

    def values(self):
        return {"emotion_mode": self.mode, "emotion": self.text_input.text().strip(),
                "emotion_strength": self.strength.value() / 100,
                "emotion_vector": [slider.value() / 100 for slider in self.sliders],
                "emotion_reference_audio": self.reference_path,
                "emotion_infer_from_text": self.infer_text.isChecked()}
