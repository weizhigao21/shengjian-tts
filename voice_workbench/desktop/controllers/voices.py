"""VoiceLibraryController responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
import json
from pathlib import Path
from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import QApplication, QFrame, QInputDialog
from ...runner import validate_wave
from ...ui import icon
from ..api_client import checked


class VoiceLibraryController:
    def stop_reference(self):
        self.reference_preview_origin = None
        self.reference_player.stop()
        self.refresh_reference_icons()

    def update_reference_icon(self, state):
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self.reference_preview_origin = None
        self.refresh_reference_icons(state)

    def refresh_reference_icons(self, state=None):
        if state is None:
            state = self.reference_player.playbackState()
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        origin = self.reference_preview_origin if playing else None
        buttons = [self.model_controls.reference_play, self.model_controls.index_emotion.reference_play]
        for dialog in self.dialogs:
            buttons.extend(getattr(dialog, "preview_buttons", [dialog.preview_button] if hasattr(dialog, "preview_button") else []))
        for button in buttons:
            active = origin is button
            button.setToolTip("停止试听" if active else "试听参考声音")
            button.setIcon(icon("stop" if active else "play"))
        for ticket in self.library.tickets:
            active = origin == ticket.preview_origin
            ticket.cover.set_playing(active)
            ticket.cover.setToolTip("停止试听" if active else "试听参考声音" if ticket.item.get("reference_audio") else "当前预设没有参考音频")

    def reference_playback_error(self, error, message):
        self.stop_reference()
        self.show_error("参考音频播放失败：" + message)

    def open_dialog(self, dialog):
        if not hasattr(self, "dialog_shade"):
            self.dialog_shade = QFrame(self)
            self.dialog_shade.setObjectName("dialogShade")
            self.dialog_shade.setStyleSheet("QFrame#dialogShade {background:rgba(0,0,0,26);border:0;}")
        self.dialog_shade.setGeometry(self.rect())
        self.dialog_shade.show()
        self.dialog_shade.raise_()
        self.dialogs.add(dialog)
        def finished():
            self.dialogs.discard(dialog)
            if not self.dialogs:
                self.dialog_shade.hide()
        dialog.finished.connect(finished)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def choose_reference(self, controls):
        from ...library import ReferencePicker
        self.open_dialog(ReferencePicker(self, controls))

    def import_reference(self, *, selected=None, source=None):
        from ...library import ReferenceDialog
        return self.open_dialog(ReferenceDialog(self, selected=selected, source=source))

    def edit_reference(self, item):
        from ...library import ReferenceDialog
        return self.open_dialog(ReferenceDialog(self, item))

    def edit_library_voice(self, voice=None):
        from ...library import PresetDialog
        return self.open_dialog(PresetDialog(self, voice))

    def use_library_voice(self, voice):
        self.active_voice_snapshot = None
        self.refresh_generation_models(voice["id"])
        self.set_page(0)
        self.text_edit.setFocus()

    def delete_library_voice(self, voice):
        self.call(lambda c: checked(c.delete(f"/voices/{voice['id']}")), lambda r: self.library_deleted("voices", voice))

    def delete_reference(self, item):
        self.call(lambda c: checked(c.delete(f"/references/{item['id']}")), lambda r: self.library_deleted("references", item))

    def library_deleted(self, kind, item):
        if self.reference_preview_origin == (kind, item["id"]):
            self.stop_reference()
        self.statusBar().showMessage(f"已删除「{item['name']}」，已生成的任务保留。", 10000)
        if kind == "voices":
            self.voices = [v for v in self.voices if v["id"] != item["id"]]
        else:
            self.references = [r for r in self.references if r["id"] != item["id"]]
        self.library.bind()
        self.show_undo(lambda: self.call(lambda c: checked(c.post(f"/{kind}/{item['id']}/restore")),
            lambda r: (self.undo_button.hide(), self.poll())))
        self.poll()

    def save_preset(self, voice, success, failure=None):
        def save(client):
            checked(client.put(f"/voices/{voice['id']}", json=voice))
            return next(v for v in checked(client.get("/voices")) if v["id"] == voice["id"])
        self.call(save, success, failure)

    def preset_saved(self, value):
        self.pending_voice_id = value["id"]
        self.active_voice_snapshot = None
        self.update_link_config_for(value)
        QApplication.clipboard().setText(self.link_config.toPlainText())
        self.statusBar().showMessage("声音预设已保存，双语工作流配置已复制。", 8000)
        self.poll()

    def preview_reference(self, path, origin=None):
        origin = self.model_controls.reference_play if origin is None else origin
        source = QUrl.fromLocalFile(str(Path(path).resolve()))
        if (self.reference_preview_origin == origin and self.reference_player.source() == source
                and self.reference_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState):
            self.stop_reference()
            return
        try:
            validate_wave(Path(path))
            self.settings.player.pause()
            self.player.stop()
            self.stop_reference()
            self.reference_player.setSource(source)
            self.reference_preview_origin = origin
            self.reference_player.play()
            self.refresh_reference_icons()
        except Exception as exc:
            self.show_error(str(exc))

    def save_generation_voice(self):
        import uuid
        from ...schemas import VoiceConfig
        from ...runner import check_voice
        current = self.generation_voice()
        if not current:
            return
        name, accepted = QInputDialog.getText(self, "保存声音预设", "预设名称（可直接用于双语工作流）", text=current["name"])
        if not accepted or not name.strip():
            return
        try:
            voice = VoiceConfig(**{**current, "id": "voice-" + uuid.uuid4().hex[:12], "name": name.strip()}).model_dump()
            check_voice(voice)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.save_preset(voice, self.preset_saved)

    def update_link_config(self):
        if not hasattr(self, "link_config"):
            return
        voice = self.selected_preset()
        if voice:
            self.update_link_config_for(voice)
        else:
            self.link_config.clear()
            self.link_config.setPlaceholderText("当前未使用预设；保存声音预设后可复制联动配置。")

    def update_link_config_for(self, voice):
        self.link_config.setPlainText(json.dumps({"name": voice["name"], "url": self.url,
            "model": voice["model_name"], "model_tag": voice["model_tag"], "status": "unknown"}, ensure_ascii=False, indent=2))

    def import_yovoice_assets(self):
        from ..api_client import checked
        self.call(lambda c: checked(c.post("/import/yovoice")), lambda r: (self.statusBar().showMessage(f"已导入 {r['added']} 个声音预设", 5000), self.poll()))
