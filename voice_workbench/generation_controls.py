"""Compact controls rendered from the selected model's request-option definitions."""
import secrets

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractSpinBox, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QSizePolicy, QWidget

from .ui import (NoWheelComboBox, NoWheelDoubleSpinBox, NoWheelSlider,
                 NoWheelSpinBox, button, label)


class SeedControl(QWidget):
    """An explicit random mode with a remembered, directly editable fixed seed."""
    changed = pyqtSignal()

    def __init__(self, spec, value):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.random = QCheckBox("随机")
        self.random.setToolTip("每次生成使用新的种子；取消勾选可输入固定种子。")
        # Native engine seeds are unsigned 32-bit; QSpinBox only accepts signed
        # 32-bit bounds. Doubles represent this entire integer range exactly.
        self.number = NoWheelDoubleSpinBox() if spec["max"] > 2147483647 else NoWheelSpinBox()
        if isinstance(self.number, NoWheelDoubleSpinBox):
            self.number.setDecimals(0)
            self.number.setSingleStep(1)
        self.number.setRange(0, spec["max"])
        self.number.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number.setFixedWidth(112)
        self.number.setKeyboardTracking(False)
        self.number.setAccessibleName("固定随机种子")
        self.number.setToolTip(f"固定种子：0–{spec['max']}。同样的种子有助于复现生成。")
        self.new_seed = button("", self.randomize, "retry", "icon")
        self.new_seed.setFixedWidth(28)
        self.new_seed.setToolTip("生成一个新的固定种子")
        self.new_seed.setAccessibleName("生成新的固定种子")
        layout.addWidget(self.random)
        layout.addWidget(self.number)
        layout.addWidget(self.new_seed)
        self.random.toggled.connect(self.random_changed)
        self.number.valueChanged.connect(self.changed)
        self.setValue(value)

    def random_changed(self, checked):
        self.number.setEnabled(not checked)
        self.changed.emit()

    def value(self):
        return -1 if self.random.isChecked() else int(self.number.value())

    def setValue(self, value):
        before = self.value()
        blocked = self.blockSignals(True)
        if value != -1:
            self.number.setValue(value)
        self.random.setChecked(value == -1)
        self.number.setEnabled(value != -1)
        self.blockSignals(blocked)
        if self.value() != before:
            self.changed.emit()

    def randomize(self):
        self.setValue(secrets.randbelow(int(self.number.maximum()) + 1))


class GenerationControls(QWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("generationControls")
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setVerticalSpacing(14)
        self.form.setHorizontalSpacing(8)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fields, self.specs = {}, []
        self.extra = {}

    def bind(self, specs, values):
        self.blockSignals(True)
        while self.form.rowCount():
            row = self.form.takeRow(0)
            for item in (row.labelItem, row.fieldItem):
                if item and item.widget():
                    # Finish queued layout/focus events before destroying controls.
                    item.widget().hide()
                    item.widget().deleteLater()
        self.fields, self.specs = {}, specs
        self.extra = {k: v for k, v in values.items() if k not in {s['name'] for s in specs}}
        # Put the seed first so it is visible as soon as advanced settings open.
        for spec in sorted(specs, key=lambda s: s["name"] != "seed"):
            name, type_ = spec["name"], spec["type"]
            value = values.get(name, spec["default"])
            caption = label(spec["label"], "muted")
            caption.setToolTip(spec.get("help") or spec.get("description", ""))
            if name == "seed" and type_ == "int":
                field = SeedControl(spec, value)
                field.changed.connect(self.changed)
                self.add_compact_row(caption, field)
            elif type_ == "enum":
                field = NoWheelComboBox()
                choices = spec["values"]
                for choice in choices:
                    field.addItem(choices[choice] if isinstance(choices, dict) else choice, choice)
                field.setCurrentIndex(max(0, field.findData(value)))
                field.currentIndexChanged.connect(self.changed)
                self.form.addRow(caption)
                self.form.addRow(field)
            elif type_ == "bool":
                field = QCheckBox()
                field.setChecked(bool(value))
                field.stateChanged.connect(self.changed)
                self.add_compact_row(caption, field)
            else:
                field = NoWheelSpinBox() if type_ == "int" else NoWheelDoubleSpinBox()
                field.setRange(spec["min"], spec["max"])
                field.setSingleStep(spec["step"])
                field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                field.setAlignment(Qt.AlignmentFlag.AlignCenter)
                if type_ == "float":
                    field.setDecimals(2 if spec["step"] < .1 else 1)
                field.setValue(value)
                field.setFixedWidth(86)
                field.valueChanged.connect(self.changed)
                if spec.get("slider"):
                    row = QWidget()
                    line = QHBoxLayout(row)
                    line.setContentsMargins(0, 0, 0, 0)
                    slider = NoWheelSlider(Qt.Orientation.Horizontal)
                    scale = round(1 / spec["step"])
                    slider.setRange(round(spec["min"] * scale), round(spec["max"] * scale))
                    slider.setValue(round(value * scale))
                    slider.setAccessibleName(spec["label"])
                    slider.valueChanged.connect(lambda v, f=field, s=scale: f.setValue(v / s))
                    field.valueChanged.connect(lambda v, s=slider, c=scale: s.setValue(round(v * c)))
                    field.setFixedWidth(64)
                    line.addWidget(slider, 1)
                    line.addWidget(field)
                    self.form.addRow(caption)
                    self.form.addRow(row)
                else:
                    self.add_compact_row(caption, field)
            field.setObjectName("option_" + name)
            field.setToolTip(spec.get("help") or spec.get("description", ""))
            self.fields[name] = field
        if not specs:
            self.form.addRow(label("此模型没有额外生成参数", "hint"))
        else:
            reset = button("恢复默认参数", self.reset, "retry", "ghost")
            self.form.addRow(reset)
        self.blockSignals(False)

    def add_compact_row(self, caption, field):
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        # Each row sizes independently: the widest label and widest field may
        # belong to different rows and must not combine into a larger form.
        caption.setWordWrap(True)
        layout.addWidget(caption)
        layout.addStretch()
        layout.addWidget(field)
        self.form.addRow(row)

    def values(self):
        values = dict(self.extra)
        for name, field in self.fields.items():
            if isinstance(field, QComboBox):
                value = field.currentData()
            elif isinstance(field, QCheckBox):
                value = field.isChecked()
            else:
                value = field.value()
            values[name] = value
        return values

    def reset(self):
        self.bind(self.specs, {})
        self.changed.emit()
