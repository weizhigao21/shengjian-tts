"""Paragraph-start @ completion with persistent preset identity."""
from __future__ import annotations

from PyQt6.QtCore import QModelIndex, Qt, QSize
from PyQt6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import QCompleter, QStyledItemDelegate, QStyle

from ...ui import ScriptEditor
from ...services.script_voices import parse_script, utf16_length
from ..model_labels import model_label

IDENTITY = QTextFormat.Property.UserProperty.value + 41
CAPTION = IDENTITY + 1
VOICE_ROLE = Qt.ItemDataRole.UserRole.value + 41


class PresetDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(320, 54)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect.adjusted(9, 5, -9, -5)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#f2f2f1"))
        voice = index.data(VOICE_ROLE) or {}
        painter.setFont(option.font)
        painter.setPen(QColor("#202020"))
        painter.drawText(rect.adjusted(0, 0, 0, -20), Qt.AlignmentFlag.AlignVCenter,
                         option.fontMetrics.elidedText(voice.get("name", index.data()), Qt.TextElideMode.ElideRight, rect.width()))
        small = QFont(option.font)
        if small.pixelSize() > 0:
            small.setPixelSize(max(10, small.pixelSize() - 2))
        else:
            small.setPointSizeF(max(8, small.pointSizeF() - 1))
        painter.setFont(small)
        painter.setPen(QColor("#888888"))
        detail = index.data(Qt.ItemDataRole.UserRole.value + 42) if voice else "可先在声音库保存声音预设"
        if voice.get("error"):
            detail += " · 配置需修复"
        painter.drawText(rect.adjusted(0, 22, 0, 0), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, rect.width()))
        painter.restore()


class PresetScriptEditor(ScriptEditor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.voices = []
        self.suppress_completion = False
        self.dismissed_prefix = None
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        popup = self.completer.popup()
        popup.setObjectName("voiceMentionPopup")
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setMaximumHeight(270)
        popup.setStyleSheet("QListView { background: white; border: 1px solid #dedede; padding: 3px; }"
                            "QListView::item { border: none; }")
        self.preset_delegate = PresetDelegate(popup)
        popup.setItemDelegate(self.preset_delegate)
        self.completer.activated[QModelIndex].connect(self.insert_preset)
        self.textChanged.connect(self.refresh_completion)
        self.cursorPositionChanged.connect(self.refresh_completion)

    def set_voices(self, voices):
        presets = [dict(v) for v in voices if not v.get("is_template")]
        if presets == self.voices and self.model.rowCount():
            return
        self.voices = presets
        self.model.clear()
        for voice in presets:
            from ...model_info import MODES, selected_mode
            detail = model_label(voice) + " · " + MODES.get(selected_mode(voice), "系统声音")
            if sum(v["name"] == voice["name"] and model_label(v) == model_label(voice) for v in presets) > 1:
                detail += " · " + voice["id"]
            item = QStandardItem(voice["name"] + "\n" + detail)
            item.setData(voice, VOICE_ROLE)
            item.setData(detail, Qt.ItemDataRole.UserRole.value + 42)
            item.setToolTip((voice.get("error") or {}).get("message") or "使用此预设的模型、模式和已保存参数")
            self.model.appendRow(item)
        if not presets:
            item = QStandardItem("还没有声音预设")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.model.appendRow(item)
        if self.completer.popup().isVisible():
            self.refresh_completion()

    def setPlainText(self, text):
        # Loading a saved work is not a request to open completion.
        if not hasattr(self, "completer"):
            return super().setPlainText(text)
        self.suppress_completion = True
        self.completer.popup().hide()
        super().setPlainText(text)
        self.suppress_completion = False
        self.dismissed_prefix = None

    def mention_prefix(self):
        cursor = self.textCursor()
        block = cursor.block()
        position = cursor.position() - block.position()
        # Slice by UTF-16 offsets rather than Python code-point offsets.
        prefix = block.text().encode("utf-16-le")[:position * 2].decode("utf-16-le")
        query = prefix.lstrip()
        if not query.startswith("@") or query.startswith("@@") or cursor.hasSelection():
            return None
        start = block.position() + utf16_length(prefix[:len(prefix) - len(query)])
        probe = QTextCursor(self.document())
        probe.setPosition(start)
        probe.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        caption = probe.charFormat().property(CAPTION)
        if probe.charFormat().property(IDENTITY) and caption and query.startswith("@" + caption):
            return None  # A chosen mention remains a label, even before its trailing space.
        return start, cursor.position(), query[1:]

    def refresh_completion(self):
        if self.suppress_completion or not self.hasFocus():
            return
        prefix = self.mention_prefix()
        if prefix is None or prefix == self.dismissed_prefix:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix[2])
        if self.completer.completionCount() == 0:
            self.completer.popup().hide()
            return
        rect = self.cursorRect()
        rect.setWidth(min(420, max(320, self.viewport().width() // 2)))
        self.completer.popup().setMinimumHeight(min(270, self.completer.completionCount() * 54 + 8))
        self.completer.complete(rect)
        self.completer.popup().setCurrentIndex(self.completer.completionModel().index(0, 0))

    def insert_preset(self, index):
        voice = index.data(VOICE_ROLE)
        prefix = self.mention_prefix()
        if not voice or prefix is None:
            return
        cursor = self.textCursor()
        cursor.beginEditBlock()
        self.suppress_completion = True
        cursor.setPosition(prefix[0])
        cursor.setPosition(prefix[1], QTextCursor.MoveMode.KeepAnchor)
        format_ = QTextCharFormat()
        format_.setProperty(IDENTITY, voice["id"])
        format_.setProperty(CAPTION, voice["name"])
        format_.setForeground(QColor("#555555"))
        cursor.insertText("@" + voice["name"], format_)
        cursor.insertText(" ", QTextCharFormat())
        cursor.setCharFormat(QTextCharFormat())
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.completer.popup().hide()
        self.suppress_completion = False
        self.dismissed_prefix = None
        self.textChanged.emit()  # The complete binding, rather than intermediate edits, is now available.

    def keyPressEvent(self, event):
        if self.completer.popup().isVisible():
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab):
                self.insert_preset(self.completer.popup().currentIndex())
                event.accept()
                return
            if event.key() == Qt.Key.Key_Escape:
                self.dismissed_prefix = self.mention_prefix()
                self.completer.popup().hide()
                event.accept()
                return
        super().keyPressEvent(event)
        self.refresh_completion()

    def inputMethodEvent(self, event):
        super().inputMethodEvent(event)
        self.refresh_completion()

    def mentions(self):
        result = []
        block = self.document().begin()
        while block.isValid():
            raw = block.text()
            start = block.position() + utf16_length(raw[:len(raw) - len(raw.lstrip())])
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            identity, caption = fmt.property(IDENTITY), fmt.property(CAPTION)
            if identity and caption and raw.lstrip().startswith("@" + caption):
                end = start + utf16_length("@" + caption)
                probe = QTextCursor(self.document())
                probe.setPosition(start)
                probe.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                if probe.selectedText() == "@" + caption and all(
                        self._character_identity(position) == identity for position in range(start, end)):
                    result.append({"start": start, "end": end, "preset_id": identity, "label": caption})
            block = block.next()
        return result

    def _character_identity(self, position):
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
        return cursor.charFormat().property(IDENTITY)

    def restore_mentions(self, mentions):
        self.suppress_completion = True
        for mention in mentions or ():
            start, end = mention["start"], mention["end"]
            if not 0 <= start < end < self.document().characterCount():
                continue
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            if cursor.selectedText() != "@" + mention["label"]:
                continue
            fmt = QTextCharFormat()
            fmt.setProperty(IDENTITY, mention["preset_id"])
            fmt.setProperty(CAPTION, mention["label"])
            fmt.setForeground(QColor("#555555"))
            cursor.mergeCharFormat(fmt)
        self.suppress_completion = False

    def segments(self):
        return parse_script(self.toPlainText(), self.voices, self.mentions())
