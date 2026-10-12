"""Compact controls rendered from the selected model's request-option definitions."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractSpinBox, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QSizePolicy, QWidget

from .ui import (NoWheelComboBox, NoWheelDoubleSpinBox, NoWheelSlider,
                 NoWheelSpinBox, button, label)


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
            self.form.removeRow(0)
        self.fields, self.specs = {}, specs
        self.extra = {k: v for k, v in values.items() if k not in {s['name'] for s in specs}}
        for spec in specs:
            name, type_ = spec["name"], spec["type"]
            value = values.get(name, spec["default"])
            caption = label(spec["label"], "muted")
            caption.setToolTip(spec.get("help") or spec.get("description", ""))
            if type_ == "enum":
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
                if name == "seed":
                    field.setSpecialValueText("随机")
                field.setValue(value)
                field.setFixedWidth(86 if name != "seed" else 120)
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
        layout.addStretch()
        layout.addWidget(field)
        self.form.addRow(caption, row)

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
