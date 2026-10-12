"""Paper tickets and small editors for voices and reference audio."""
import hashlib
import math
from pathlib import Path
import uuid
import wave

from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import (QAbstractButton, QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLineEdit, QPlainTextEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .model_controls import ModelControls, populate, voice_label
from .model_info import MODES, selected_mode
from .runner import check_voice, validate_wave
from .schemas import VoiceConfig
from .ui import Waveform, button, label


def friendly_name(voice):
    name = voice["name"]
    return name.split(" · ")[-1] if " · 声音 " in name else name


class RecordButton(QAbstractButton):
    def __init__(self, seed, callback, reference="", ring=False):
        super().__init__()
        self.setObjectName("recordPlay")
        self.setFixedSize(80, 80)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(callback)
        self.seed = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
        self.ring, self.hovered, self.playing, self.angle = ring, False, False, 0
        self.color = ["#D97757", "#81969B", "#B6A06D", "#839578", "#AE86A2"][self.seed % 5]
        self.peaks = []
        if ring and reference:
            try:
                with wave.open(reference, "rb") as stream:
                    import array
                    stride = max(1, stream.getnframes() // 64)
                    for _ in range(64):
                        raw = stream.readframes(min(stride, 512))
                        samples = array.array("h", raw[:len(raw) // 2 * 2]) if stream.getsampwidth() == 2 else []
                        self.peaks.append(max((abs(s) / 32768 for s in samples), default=.2))
                        stream.setpos(min(stream.getnframes(), stream.tell() + max(0, stride - 512)))
            except Exception:
                pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def tick(self):
        self.angle = (self.angle + 2) % 360
        self.update()

    def set_playing(self, playing):
        self.playing = playing
        self.timer.start(40) if playing else self.timer.stop()
        self.update()

    def enterEvent(self, event):
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(.4)
        p.translate(self.width() / 2, self.height() / 2)
        scale = min(self.width(), self.height()) / 80
        p.scale(scale, scale)
        p.rotate(self.angle)
        if self.ring:
            for i in range(64):
                a = i * math.tau / 64
                peak = self.peaks[i % len(self.peaks)] if self.peaks else .1
                p.setPen(QPen(QColor(self.color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                inner, outer = 30, 32 + peak * 6
                p.drawLine(QPointF(math.cos(a) * inner, math.sin(a) * inner), QPointF(math.cos(a) * outer, math.sin(a) * outer))
        else:
            gradient = QRadialGradient(0, 0, 39)
            gradient.setColorAt(0, QColor("#333334"))
            gradient.setColorAt(.7, QColor("#151516"))
            gradient.setColorAt(1, QColor("#09090a"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient)
            p.drawEllipse(QPointF(0, 0), 39, 39)
            for radius in range(21, 38, 2):
                p.setPen(QPen(QColor("#39393a"), .5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(0, 0), radius, radius)
            p.setPen(QPen(QColor("#777774"), 1))
            p.drawArc(QRectF(-36, -36, 72, 72), 35 * 16, 40 * 16)
            p.drawArc(QRectF(-34, -34, 68, 68), 215 * 16, 40 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(QPointF(0, 0), 24 if self.ring else 19, 24 if self.ring else 19)
        # Stable, simple printed geometry gives each voice its own label.
        p.save()
        clip = QPainterPath()
        radius = 24 if self.ring else 19
        clip.addEllipse(QRectF(-radius, -radius, radius * 2, radius * 2))
        p.setClipPath(clip)
        p.setBrush(QColor(255, 255, 255, 80))
        p.drawEllipse(QPointF(-10, -12), 18, 18)
        p.setBrush(QColor(32, 32, 32, 37))
        p.drawRect(QRectF(-24, 8, 48, 12))
        p.restore()
        p.rotate(-self.angle)
        p.setBrush(QColor("#fffdf6"))
        if self.playing:
            p.drawRoundedRect(QRectF(-5, -6, 10, 12), 1, 1)
        else:
            p.drawPolygon(QPolygonF([QPointF(-3, -7), QPointF(8, 0), QPointF(-3, 7)]))


class Ticket(QFrame):
    def __init__(self, owner, item, reference=False):
        super().__init__()
        from .desktop.model_labels import model_label
        self.item, self.owner, self.reference, self.hovered = item, owner, reference, False
        self.setFixedHeight(86)
        self.setMouseTracking(True)
        self.setObjectName("voiceTicket")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 0, 10)
        layout.setSpacing(12)
        path = item.get("reference_audio", "")
        self.cover = RecordButton(item["id"], lambda: owner.preview_reference(path), path, reference)
        self.cover.setFixedSize(56, 56)
        self.cover.setToolTip("试听参考声音" if path else "当前预设没有参考音频")
        self.cover.setEnabled(bool(path) and not (reference and item.get("error")))
        layout.addWidget(self.cover)
        body = QVBoxLayout()
        body.setSpacing(5)
        eyebrow = label("WAV  /  参考音频" if reference else model_label(item), "ticketMeta")
        title = label(item["name"] if reference else friendly_name(item), "ticketTitle")
        title.setToolTip(item["name"])
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body.addWidget(eyebrow)
        body.addWidget(title)
        if reference:
            description = item.get("reference_text") or "添加原文，可用于精细声音克隆"
        else:
            mode = selected_mode(item, item.get("model_ui"))
            description = voice_label(item.get("native_voice", "")) if mode == "builtin" and item["backend"] == "audio_cpp" else item.get("voice_description") or item.get("emotion") or "参考声音克隆"
        excerpt = label(description[:65], "muted")
        excerpt.setToolTip(description)
        excerpt.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body.addWidget(excerpt)
        layout.addLayout(body, 1)
        if not reference:
            layout.addWidget(button("使用", lambda: owner.use_library_voice(item), kind="small"))
        edit = button("", lambda: owner.edit_reference(item) if reference else owner.edit_library_voice(item), "pen", "icon")
        edit.setToolTip("编辑参考声音" if reference else "编辑声音预设")
        edit.setFixedSize(32, 32)
        layout.addWidget(edit)
        remove = button("", lambda: owner.delete_reference(item) if reference else owner.delete_library_voice(item), "trash", "icon")
        remove.setToolTip("删除")
        remove.setFixedSize(32, 32)
        layout.addWidget(remove)
        stamp = QWidget()
        stamp.setFixedWidth(84)
        stamp_layout = QVBoxLayout(stamp)
        stamp_layout.setContentsMargins(10, 0, 10, 0)
        stamp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if reference:
            duration = round(item.get("duration", 0))
            value, unit = f"{duration // 60}:{duration % 60:02d}", "音频时长"
        else:
            value = {"clone": "克隆", "continuation": "精细", "builtin": "内置", "design": "设计"}[selected_mode(item, item.get("model_ui"))]
            unit = "声音预设"
        amount, caption = label(value, "ticketStamp"), label(unit, "ticketMeta")
        amount.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_layout.addWidget(amount)
        stamp_layout.addWidget(caption)
        layout.addWidget(stamp)

    def enterEvent(self, event):
        self.hovered = True
        self.update()

    def leaveEvent(self, event):
        self.hovered = False
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#fafafa" if self.hovered else "#ffffff"))
        p.drawRect(QRectF(self.rect()))
        p.setPen(QPen(QColor("#ededee"), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        x = self.width() - 84
        p.drawLine(x, 12, x, self.height() - 12)


class LibraryPanel(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner, self.key, self.page = owner, None, "presets"
        content = QVBoxLayout(self)
        content.setContentsMargins(24, 8, 24, 16)
        content.setSpacing(14)
        heading = QHBoxLayout()
        heading.addWidget(label("声音库", "title"), 1)
        self.import_button = button("导入参考声音", owner.import_reference, "plus", "small")
        heading.addWidget(self.import_button)
        content.addLayout(heading)
        tabs = QHBoxLayout()
        tabs.setSpacing(24)
        self.buttons = {}
        group = QButtonGroup(self)
        for key, text in (("presets", "声音预设"), ("references", "参考声音")):
            tab = button(text, lambda _, k=key: self.set_page(k), kind="libraryTab")
            tab.setCheckable(True)
            group.addButton(tab)
            self.buttons[key] = tab
            tabs.addWidget(tab)
        self.buttons["presets"].setChecked(True)
        tabs.addStretch()
        self.new_button = button("新建声音预设", lambda: owner.edit_library_voice(), "plus", "ghost")
        tabs.addWidget(self.new_button)
        content.addLayout(tabs)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索声音名称、模型或原文")
        self.search.setClearButtonEnabled(True)
        from .ui import icon
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.rebuild)
        search_row.addWidget(self.search, 1)
        self.count = label("", "muted")
        search_row.addWidget(self.count)
        content.addLayout(search_row)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("libraryBody")
        self.entries = QVBoxLayout(self.body)
        self.entries.setContentsMargins(0, 0, 4, 0)
        self.entries.setSpacing(0)
        self.scroll.setWidget(self.body)
        content.addWidget(self.scroll, 1)
        foot = QHBoxLayout()
        foot.addWidget(label("点击唱片试听，选择「使用」即可开始创作。", "hint"), 1)
        foot.addWidget(button("导入 yovoice 资产", owner.import_yovoice_assets, "import", "ghost"))
        content.addLayout(foot)
        self.tickets = []

    def set_page(self, page):
        self.page = page
        self.buttons[page].setChecked(True)
        self.new_button.setVisible(page == "presets")
        self.rebuild()

    def bind(self):
        import json
        key = json.dumps([self.owner.voices, self.owner.references], sort_keys=True)
        if key != self.key:
            self.key = key
            self.rebuild()

    def rebuild(self, *_):
        while self.entries.count():
            item = self.entries.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.tickets = []
        source = self.owner.references if self.page == "references" else sorted([v for v in self.owner.voices if not v.get("is_template")], key=lambda v: v["backend"] == "windows_sapi")
        query = self.search.text().strip().casefold()
        items = [v for v in source if query in " ".join(str(v.get(key, "")) for key in ("name", "family", "reference_text", "voice_description")).casefold()]
        self.count.setText(f"{len(items)} 个声音")
        for item in items:
            ticket = Ticket(self.owner, item, self.page == "references")
            self.tickets.append(ticket)
            self.entries.addWidget(ticket)
        if not items:
            empty = QWidget()
            layout = QVBoxLayout(empty)
            layout.setContentsMargins(0, 55, 0, 40)
            cover = RecordButton("empty-library", lambda: None, ring=True)
            layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignCenter)
            text = label("没有找到声音" if source else "把第一段声音，放进你的声音库。", "section")
            text.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(text)
            self.entries.addWidget(empty)
        self.entries.addStretch()


class PaperDialog(QDialog):
    def __init__(self, owner, title, width=560):
        super().__init__(owner)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setObjectName("paperDialog")
        self.busy = False
        self.resize(width, 520)
        canvas = QVBoxLayout(self)
        canvas.setContentsMargins(12, 12, 12, 12)
        surface = QFrame()
        surface.setObjectName("dialogSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor("#30000000"))
        surface.setGraphicsEffect(shadow)
        canvas.addWidget(surface)
        self.outer = QVBoxLayout(surface)
        self.outer.setContentsMargins(28, 24, 28, 24)
        self.outer.setSpacing(16)
        header = QHBoxLayout()
        header.addWidget(label(title, "dialogTitle"), 1)
        close = button("", self.reject, "close", "icon")
        self.close_button = close
        close.setFixedSize(28, 28)
        header.addWidget(close)
        self.outer.addLayout(header)
        self.content = QVBoxLayout()
        self.content.setSpacing(12)
        self.outer.addLayout(self.content, 1)
        self.error = label("", "error")
        self.error.setWordWrap(True)
        self.error.hide()
        self.outer.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = button("取消", self.reject)
        actions.addWidget(self.cancel_button)
        self.confirm = button("保存", lambda: None, kind="primary")
        actions.addWidget(self.confirm)
        self.outer.addLayout(actions)

    def show_error(self, error):
        self.set_busy(False)
        self.error.setText(str(error))
        self.error.show()
        self.confirm.setEnabled(True)

    def set_busy(self, busy):
        self.busy = busy
        for control in (self.confirm, self.cancel_button, self.close_button):
            control.setEnabled(not busy)

    def reject(self):
        if not self.busy:
            super().reject()

    def accept(self):
        self.busy = False
        super().accept()


class PresetDialog(PaperDialog):
    def __init__(self, owner, voice=None):
        from .desktop.model_labels import model_key, model_label
        super().__init__(owner, "编辑声音预设" if voice else "新建声音预设", 580)
        self.resize(580, 700 if self.base_requires_reference(voice or owner.current_voice()) else 560)
        self.owner, self.original = owner, voice
        self.base = voice or owner.current_voice() or (owner.voices[0] if owner.voices else None)
        self.name = QLineEdit(friendly_name(voice) if voice else "")
        self.name.setMaxLength(100)
        self.name.setPlaceholderText("例如：旁白、女主角、英文讲解")
        self.content.addWidget(label("声音名称", "muted"))
        self.content.addWidget(self.name)
        self.content.addWidget(label("模型", "muted"))
        self.model = QComboBox()
        self.models = {}
        for value in owner.voices:
            self.models.setdefault(model_key(value), value)
        populate(self.model, [(k, model_label(v)) for k, v in self.models.items()], model_key(self.base) if self.base else None)
        self.content.addWidget(self.model)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.controls = ModelControls()
        self.controls.set_references(owner.references)
        self.controls.preview_requested.connect(owner.preview_reference)
        self.controls.choose_requested.connect(lambda: owner.choose_reference(self.controls))
        self.controls.audio_changed.connect(owner.stop_reference)
        scroll.setWidget(self.controls)
        self.content.addWidget(scroll, 1)
        if self.base:
            self.controls.bind(self.base)
        self.model.currentIndexChanged.connect(self.select_model)
        self.confirm.clicked.connect(self.save)
        self.finished.connect(owner.stop_reference)

    @staticmethod
    def base_requires_reference(voice):
        return bool(voice and selected_mode(voice, voice.get("model_ui")) in ("clone", "continuation"))

    def select_model(self):
        self.base = self.models[self.model.currentData()]
        self.controls.bind(self.base)
        self.resize(580, 700 if self.base_requires_reference(self.base) else 560)

    def save(self):
        try:
            if not self.base:
                raise ValueError("请先导入本地模型")
            if not self.name.text().strip():
                raise ValueError("请填写声音名称")
            voice = VoiceConfig(**{**self.base, **self.controls.overrides(), "name": self.name.text().strip(),
                "id": self.original["id"] if self.original else "voice-" + uuid.uuid4().hex[:12]}).model_dump()
            check_voice(voice)
            self.set_busy(True)
            self.owner.save_preset(voice, lambda v: (self.accept(), self.owner.preset_saved(v)), self.show_error)
        except Exception as exc:
            self.show_error(exc)


class ReferenceDialog(PaperDialog):
    def __init__(self, owner, item=None, selected=None, source=None):
        super().__init__(owner, "编辑参考声音" if item else "添加为参考声音" if source else "导入参考声音")
        self.owner, self.item, self.selected = owner, item, selected
        initial = item or source or {}
        self.path = initial.get("reference_audio", "")
        self.file = button(Path(self.path).name if self.path else "选择 WAV 音频 · 1–60 秒", self.browse, "import")
        if item:
            self.file.setText(f"参考音频 · {item.get('duration', 0):.1f} 秒")
            self.file.setEnabled(False)
        elif source:
            self.file.setText("已选择生成音频 · 点击更换")
        self.file.setToolTip(self.path)
        self.file.setObjectName("audioDrop")
        self.file.setMinimumHeight(64)
        self.content.addWidget(self.file)
        self.waveform = Waveform()
        self.waveform.load(Path(self.path) if self.path else None)
        self.content.addWidget(self.waveform)
        self.name = QLineEdit(initial.get("name", ""))
        self.name.setMaxLength(100)
        self.name.setPlaceholderText("为这段参考声音起个名字")
        self.content.addWidget(label("声音名称", "muted"))
        self.content.addWidget(self.name)
        self.text = QPlainTextEdit(initial.get("reference_text", ""))
        self.text.setPlaceholderText("可选：与音频一致的原文，用于精细克隆")
        self.content.addWidget(label("参考原文", "muted"))
        self.content.addWidget(self.text, 1)
        self.content.addWidget(button("试听参考声音", lambda: owner.preview_reference(self.path), "play", "ghost"))
        self.confirm.clicked.connect(self.save)
        self.finished.connect(owner.stop_reference)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入参考声音", "", "WAV 音频 (*.wav)")
        if path:
            self.path = path
            self.file.setText(Path(path).name)
            self.waveform.load(Path(path))
            if not self.name.text():
                self.name.setText(Path(path).stem[:100])

    def save(self):
        from .desktop.api_client import checked
        try:
            if not self.name.text().strip():
                raise ValueError("请填写声音名称")
            duration = validate_wave(Path(self.path))["duration"]
            if not 1 <= duration <= 60:
                raise ValueError("请选择 1–60 秒的 WAV 音频")
            body = {"name": self.name.text().strip(), "reference_text": self.text.toPlainText().strip()}
            self.set_busy(True)
            def save(client):
                if self.item and self.path == self.item["reference_audio"]:
                    return checked(client.patch(f"/references/{self.item['id']}", json=body))
                return checked(client.post("/references", json={**body, "reference_audio": self.path}))
            def saved(value):
                self.accept()
                if self.selected:
                    self.selected(value)
                self.owner.poll()
            self.owner.call(save, saved, self.show_error)
        except Exception as exc:
            self.show_error(exc)


class ReferencePicker(PaperDialog):
    def __init__(self, owner, controls):
        super().__init__(owner, "选择参考声音", 560)
        self.owner, self.controls, self.selection = owner, controls, None
        self.confirm.setText("使用这个声音")
        self.confirm.setEnabled(False)
        self.content.addWidget(button("导入参考声音", lambda: owner.import_reference(selected=lambda r: (self.choose(r), self.apply())), "import", "small"))
        search = QLineEdit()
        search.setPlaceholderText("搜索参考声音")
        self.content.addWidget(search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("libraryBody")
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.scroll.setWidget(self.body)
        self.content.addWidget(self.scroll, 1)
        self.group = QButtonGroup(self)
        self.reference_buttons = []
        for reference in owner.references:
            row = button(f"{reference['name']}  ·  {reference.get('duration', 0):.1f} 秒", lambda _, r=reference: self.choose(r), "voices", "referenceChoice")
            row.setToolTip(reference.get("reference_text") or reference["reference_audio"])
            row.setCheckable(True)
            row.setEnabled(not reference.get("error"))
            self.group.addButton(row)
            self.rows.addWidget(row)
            self.reference_buttons.append((row, reference))
        self.rows.addStretch()
        search.textChanged.connect(lambda value: [row.setVisible(value.casefold() in item["name"].casefold()) for row, item in self.reference_buttons])
        self.content.addWidget(button("试听选中声音", lambda: owner.preview_reference(self.selection["reference_audio"]) if self.selection else None, "play", "ghost"))
        self.confirm.clicked.connect(self.apply)
        self.finished.connect(owner.stop_reference)

    def choose(self, reference):
        self.selection = reference
        self.confirm.setEnabled(True)
        for row, item in self.reference_buttons:
            row.setChecked(item["id"] == reference["id"])

    def apply(self):
        if self.selection:
            self.controls.select_reference(self.selection)
            self.accept()
