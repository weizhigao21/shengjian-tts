"""Dialogue rows: a voice label, growing text editor and per-row actions."""
from __future__ import annotations
import uuid
import math

from PyQt6.QtCore import Qt, QEvent, pyqtSignal
from PyQt6.QtGui import QTextCharFormat
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ...services.dialogue import entry_revision, dialogue_id
from ...ui import button, generation_elapsed, icon, ElidedLabel
from ..model_labels import model_label
from .voice_mentions import PresetScriptEditor, VOICE_ROLE
from .delayed_tooltip import DelayedToolTip


class DialogueInput(PresetScriptEditor):
    next_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setObjectName("dialogueText")
        self.setStyleSheet("QTextEdit#dialogueText { background: white; font-size: 15px; }")
        self.setPlaceholderText("输入 @ 选择音色，再输入这段对白…")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(78)
        self.document().documentLayout().documentSizeChanged.connect(self.fit_content)
        self.fit_content()

    def fit_content(self, *_):
        chrome = max(26, self.height() - self.viewport().height())
        height = min(720, max(78, math.ceil(self.document().size().height()) + chrome + 2))
        if height != self.height():
            self.setFixedHeight(height)

    def insert_preset(self, index):
        voice, prefix = index.data(VOICE_ROLE), self.mention_prefix()
        if not voice or prefix is None:
            return
        cursor = self.textCursor()
        self.suppress_completion = True
        cursor.beginEditBlock()
        cursor.setPosition(prefix[0])
        cursor.setPosition(prefix[1], cursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.setCharFormat(QTextCharFormat())
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.completer.popup().hide()
        self.suppress_completion = False
        self.preset_chosen.emit(voice)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not self.completer.popup().isVisible() and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DialogueRow(QWidget):
    def __init__(self, panel, entry):
        super().__init__(panel.body)
        self.panel, self.identity = panel, entry["id"]
        self.voice_id, self.voice_name = entry.get("voice_id"), entry.get("voice_name", "")
        self.model_id, self.overrides = entry.get("model_id"), entry.get("overrides")
        self.latest = None
        self.record_count = 0
        self.effective_voice = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setObjectName("dialogueRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("QWidget#dialogueRow { border: 1px solid transparent; border-bottom-color: #eeeeee; border-radius: 7px; }"
                          "QWidget#dialogueRow:hover { border-color: #e5d8cd; }"
                          "QWidget#dialogueRow[selected=\"true\"] { border-color: #d9ac8c; background: #fffdfb; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 12)
        layout.setSpacing(9)
        header = QHBoxLayout()
        header.setSpacing(5)
        self.name_edit = QLineEdit(entry.get("role_name") or "")
        self.name_edit.setObjectName("dialogueRoleName")
        self.name_edit.setFixedWidth(140)
        self.name_edit.setMaxLength(100)
        self.name_edit.setPlaceholderText(dialogue_id(self.identity))
        self.name_edit.setAccessibleName("角色名称")
        self.name_edit.setStyleSheet("QLineEdit#dialogueRoleName { border: 1px solid transparent; background: transparent; padding: 5px; }"
                                    "QLineEdit#dialogueRoleName:hover { background: #f7f5f3; }"
                                    "QLineEdit#dialogueRoleName:focus { border-color: #d9ac8c; background: white; }")
        self.name_edit.textChanged.connect(self.name_changed)
        self.name_edit.editingFinished.connect(self.finish_name)
        header.addWidget(self.name_edit)
        self.voice_button = button("", self.choose_voice, "voices")
        self.voice_button.setMinimumWidth(110)
        self.voice_button.setMaximumWidth(330)
        self.voice_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.voice_button.setAccessibleName("对白声音预设")
        header.addWidget(self.voice_button)
        header.addStretch()
        self.status = ElidedLabel("待生成")
        self.status.setFixedWidth(85)
        self.status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        header.addWidget(self.status)
        self.play_button = button("", lambda: panel.play_requested.emit((self.latest or {}).get("idx", -1)), "play", "icon")
        self.play_button.setToolTip("试听这条对白的最新音频")
        self.play_button.setFixedWidth(32)
        self.play_button.setStyleSheet("QPushButton:hover { background: #eef5ef; }")
        self.play_button.setEnabled(False)
        header.addWidget(self.play_button)
        self.generate_button = button("", lambda: panel.submit_requested.emit(self.identity), "play", "primary")
        self.generate_button.setFixedWidth(32)
        self.generate_button.setToolTip("生成这条对白")
        self.generate_button.setAccessibleName("生成这条对白")
        header.addWidget(self.generate_button)
        self.delete_button = button("", lambda: panel.remove_row(self), "trash", "icon")
        self.delete_button.setToolTip("移除这条对白，历史音频保留")
        self.delete_button.setFixedWidth(32)
        self.delete_button.setIcon(icon("trash", "#a2462e"))
        self.delete_button.setStyleSheet("QPushButton:hover { background: #fff0eb; }")
        header.addWidget(self.delete_button)
        layout.addLayout(header)
        self.text = DialogueInput(self)
        self.text.setPlainText(entry.get("text", ""))
        self.text.set_voices(panel.voices)
        self.text.textChanged.connect(self.changed)
        self.text.preset_chosen.connect(self.set_voice)
        self.text.next_requested.connect(lambda: panel.add_after(self))
        layout.addWidget(self.text)
        for control in (self.text, self.text.viewport(), self.name_edit, self.status, self.voice_button,
                        self.generate_button, self.play_button, self.delete_button):
            control.installEventFilter(self)
        self.detail_tip = DelayedToolTip(self, self.detail_text)
        for control in (self, self.name_edit, self.text, self.text.viewport(), self.status):
            self.detail_tip.watch(control)
        for control in (self.voice_button, self.generate_button, self.play_button, self.delete_button):
            self.detail_tip.watch(control, details=False)
        self.refresh_voice()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.panel.select(self.identity)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        self.panel.select(self.identity)
        super().mousePressEvent(event)

    def entry(self):
        return {"id": self.identity, "text": self.text.toPlainText(), "voice_id": self.voice_id, "voice_name": self.voice_name,
                "model_id": self.model_id, "overrides": self.overrides,
                **({"role_name": self.role_name} if self.role_name else {})}

    @property
    def role_name(self):
        return self.name_edit.text().strip()

    def name_changed(self):
        self.changed()
        self.panel.name_changed.emit(self.identity)

    def finish_name(self):
        self.name_edit.setText(self.role_name)
        self.name_edit.setCursorPosition(0)

    def detail_text(self):
        base = next((v for v in self.panel.voices if v["id"] == (self.voice_id or self.model_id)), None)
        voice = {**(base or self.effective_voice or {}), **(self.overrides or {})}
        lines = ["角色：" + (self.role_name or "未命名（点击名称区输入角色名称）"),
                 "对白 ID：" + self.identity, "音色：" + (self.voice_name or "当前参数"),
                 "模型：" + (model_label(voice) if voice.get("backend") else "未选择"), "状态：" + self.status.text(),
                 f"生成记录：{self.record_count} 条 · 正文：{len(self.text.toPlainText())} 字"]
        if self.latest:
            lines.append("最近生成：" + generation_elapsed(self.latest)[0])
        status_detail = self.status.toolTip().partition("\n")[2]
        if status_detail:
            lines.append(status_detail)
        return "\n".join(lines)

    def changed(self):
        self.panel.changed.emit()

    def set_voice(self, voice):
        self.voice_id, self.voice_name = voice["id"], voice["name"]
        self.model_id, self.overrides = None, None
        self.refresh_voice()
        self.panel.changed.emit()
        self.panel.voice_changed.emit(self.identity)
        self.text.setFocus()

    def refresh_voice(self):
        voice = next((v for v in self.panel.voices if v["id"] == self.voice_id), None)
        if voice:
            self.voice_name = voice["name"]
        name = self.voice_name or "默认音色"
        shown = self.voice_button.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, max(0, self.voice_button.width() - 44))
        self.voice_button.setText(shown)
        self.voice_button.setToolTip(name + ("\n" + model_label(voice) if voice else
            "\n预设已删除，请重新选择" if self.voice_id else "\n使用右侧的模型与当前参数；输入 @ 可选择其他音色"))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "voice_button"):
            self.refresh_voice()

    def choose_voice(self):
        menu = QMenu(self)
        menu.addAction("默认音色（右侧设置）", self.use_default)
        menu.addSeparator()
        for voice in self.panel.voices:
            if voice.get("is_template"):
                continue
            action = menu.addAction(icon("voices"), voice["name"] + " · " + model_label(voice),
                                    lambda checked=False, v=voice: self.set_voice(v))
            action.setToolTip(voice["name"] + "\n" + model_label(voice))
        menu.setToolTipsVisible(True)
        menu.exec(self.voice_button.mapToGlobal(self.voice_button.rect().bottomLeft()))

    def use_default(self):
        self.voice_id, self.voice_name = None, ""
        self.model_id, self.overrides = None, None
        self.refresh_voice()
        self.panel.changed.emit()
        self.panel.voice_changed.emit(self.identity)

    def update_status(self, records, default_voice, ready):
        self.effective_voice = default_voice
        self.record_count = sum(s.get("source_id") == self.identity for s in records)
        latest = next((s for s in reversed(records) if s.get("source_id") == self.identity), None)
        self.latest = next((s for s in reversed(records) if s.get("source_id") == self.identity and s["status"] == "done" and s.get("audio")), None)
        self.play_button.setEnabled(bool(self.latest))
        self.generate_button.setEnabled(ready and bool(self.text.toPlainText().strip()) and not (latest and latest["status"] in ("queued", "running")))
        action = "重新生成这条对白" if latest else "生成这条对白"
        self.generate_button.setIcon(icon("retry" if latest else "play", "#ffffff"))
        self.generate_button.setToolTip(action)
        self.generate_button.setAccessibleName(action)
        if self.voice_id and not any(v["id"] == self.voice_id for v in self.panel.voices):
            status = "音色已删除"
        elif latest and latest.get("source_revision") != entry_revision(self.entry(), self.panel.voices, default_voice):
            status = "已修改" if latest["status"] in ("done", "failed", "cancelled") else "生成中 · 已修改"
        elif latest:
            from ..constants import STATES
            status = STATES[latest["status"]]
            if latest["status"] == "done":
                status = "已生成"
        else:
            status = "待生成"
        self.status.setText(status)
        color = "#a2462e" if "失败" in status or "已删除" in status else "#3c7a51" if "已生成" in status else "#c76726" if "修改" in status or "生成中" in status or "排队" in status else "#85858a"
        self.status.setStyleSheet("color: " + color + ";")
        detail = generation_elapsed(latest)[1] if latest and latest["status"] == "done" else ((latest or {}).get("error") or {}).get("message", "")
        self.status.setToolTip(status + ("\n" + detail if detail else ""))


class DialogueEditor(QScrollArea):
    changed = pyqtSignal()
    name_changed = pyqtSignal(str)
    submit_requested = pyqtSignal(str)
    play_requested = pyqtSignal(int)
    selection_changed = pyqtSignal(str)
    voice_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.voices, self.rows = [], []
        self.selected_id = None
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("dialogueBody")
        self.body.setStyleSheet("QWidget#dialogueBody { background: white; }")
        self.layout = QVBoxLayout(self.body)
        self.layout.setContentsMargins(0, 0, 0, 8)
        self.layout.setSpacing(0)
        self.add_button = button("添加对白", lambda: self.add_after(self.rows[-1] if self.rows else None), "plus", "ghost")
        self.layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.layout.addStretch()
        self.setWidget(self.body)
        self.load([])

    def load(self, entries):
        for row in self.rows:
            self.layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.rows = []
        self.selected_id = None
        for entry in entries or [{"id": uuid.uuid4().hex, "text": ""}]:
            self._insert(len(self.rows), entry)

    def _insert(self, index, entry):
        row = DialogueRow(self, entry)
        self.rows.insert(index, row)
        self.layout.insertWidget(index, row)
        return row

    def selected_row(self):
        return next((r for r in self.rows if r.identity == self.selected_id), None)

    def select(self, identity):
        if self.selected_id == identity or not any(r.identity == identity for r in self.rows):
            return
        self.selected_id = identity
        for row in self.rows:
            row.setProperty("selected", row.identity == identity)
            row.style().unpolish(row)
            row.style().polish(row)
            row.update()
        self.selection_changed.emit(identity)

    def add_after(self, previous):
        row = self._insert(self.rows.index(previous) + 1 if previous in self.rows else len(self.rows),
                           {"id": uuid.uuid4().hex, "text": ""})
        self.changed.emit()
        self.select(row.identity)
        row.text.setFocus()
        self.ensureWidgetVisible(row)
        return row

    def remove_row(self, row):
        was_selected = self.selected_id == row.identity
        index = self.rows.index(row)
        self.rows.remove(row)
        self.layout.removeWidget(row)
        row.text.completer.popup().hide()
        row.hide()
        row.deleteLater()
        if not self.rows:
            self._insert(0, {"id": uuid.uuid4().hex, "text": ""})
        if was_selected:
            self.select(self.rows[min(index, len(self.rows)-1)].identity)
        self.changed.emit()

    def entries(self):
        return [r.entry() for r in self.rows]

    def set_voices(self, voices):
        self.voices = list(voices)
        for row in self.rows:
            row.text.set_voices(self.voices)
            row.refresh_voice()

    def segments(self, default_voice=None, identity=None, force=False):
        result = []
        for row in self.rows:
            entry = row.entry()
            if identity and identity != entry["id"] or not entry["text"].strip():
                continue
            base_id = entry["voice_id"] or entry["model_id"]
            if base_id and not any(v["id"] == base_id for v in self.voices):
                raise ValueError("声音预设「" + entry["voice_name"] + "」已删除，请点击音色标签重新选择。")
            if len(entry["text"]) > 5000:
                raise ValueError("单条对白超过 5000 字，请分为多条后生成。")
            text = entry["text"].strip()
            if text.startswith("@") and not text.startswith("@@"):
                raise ValueError("请先从 @ 列表选择音色；需要朗读行首 @ 时输入 @@。")
            result.append({"text": text[1:] if text.startswith("@@") else text, "timestamp": "", "voice_id": base_id,
                           "source_id": entry["id"], "source_revision": entry_revision(entry, self.voices, default_voice),
                           "force_generate": force, "use_preset": bool(entry["voice_id"]), "overrides": entry["overrides"]})
        if not result:
            raise ValueError("请在对白文本框中输入要生成的内容。")
        return result

    def script(self):
        return "\n".join(("@" + r.voice_name + " " if r.voice_id else "") + r.text.toPlainText()
                          for r in self.rows if r.text.toPlainText().strip())

    def update_records(self, records, default_voice, ready):
        for row in self.rows:
            row.update_status(records, default_voice, ready)

    def close_popups(self):
        for row in self.rows:
            row.text.completer.popup().hide()
