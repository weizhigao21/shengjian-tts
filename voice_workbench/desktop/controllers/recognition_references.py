"""Recognition context menus and private clip lifetime for the reference library."""
from PyQt6 import sip
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtWidgets import QMenu

from ...ui import icon
from ...speech_recognition.reference_clip import prepare_reference_clip, reference_audio_path


class RecognitionReferences:
    def reference_snapshot(self, row=None, whole=False):
        state = self.state()
        if not self.session or not state.get("audio"):
            return None
        name = state.get("name") or "识别音频"
        if whole:
            start, end, text = 0.0, state.get("duration") or 0.0, state.get("text") or ""
            suffix = "整段录音"
        else:
            lines = state.get("lines") or []
            if row is None or not 0 <= row < len(lines):
                return None
            selected = [self.panel.lines.row(item) for item in self.panel.lines.selectedItems()]
            selected = selected if row in selected else [row]
            first, last = min(selected), max(selected)
            entries = lines[first:last+1]
            start, end = entries[0]["start"], entries[-1]["end"]
            text = "\n".join(line["text"] for line in entries)
            suffix = f"第 {first+1} 行" if first == last else f"第 {first+1}–{last+1} 行"
        return {"audio": state["audio"], "start": start, "end": end, "text": text,
                "name": f"{name} · {suffix}"[:100], "estimated": bool(state.get("warning")) and not whole}

    def reference_unavailable(self, snapshot):
        if self.owner.closing or not self.owner.ready:
            return "本地服务未连接，请连接后再保存。"
        if self.busy() or self.importing:
            return "请等待音频导入或识别完成后再保存。"
        if self.preparing_reference:
            return "正在准备参考声音片段，请稍候。"
        if not snapshot:
            return "请先选择识别音频。"
        try:
            reference_audio_path(self.owner.data_dir, snapshot["audio"])
        except Exception as exc:
            return str(exc)
        if not 1-1e-6 <= snapshot["end"] - snapshot["start"] <= 60+1e-6:
            return "参考音频需要 1–60 秒，请按住 Shift 选择适当的连续识别行。"
        return ""

    def reference_context_menu(self, widget, position, whole=False):
        item = None if whole else self.panel.lines.itemAt(position)
        snapshot = self.reference_snapshot(self.panel.lines.row(item) if item else None, whole)
        if not snapshot:
            return
        reason = self.reference_unavailable(snapshot)
        menu = QMenu(self.panel)
        menu.setToolTipsVisible(True)
        action = menu.addAction(icon("voices"), "保存到声音库…")
        action.setEnabled(not reason)
        action.setToolTip(reason or "带入所选音频及识别文字，保存独立副本到声音库 → 参考声音。" +
                          ("当前为估算时间，请先试听核对。" if snapshot["estimated"] else ""))
        anchor = widget if whole else self.panel.lines.viewport()
        chosen = menu.exec(anchor.mapToGlobal(position))
        menu.deleteLater()
        if chosen is action and action.isEnabled():
            self.save_reference(snapshot)

    def save_reference(self, snapshot):
        snapshot = dict(snapshot)
        reason = self.reference_unavailable(snapshot)
        if reason:
            self.owner.show_error(reason)
            return
        self.stop()
        self.preparing_reference = True
        self.panel.status.setText("正在准备参考声音片段…")
        def prepared(result):
            self.preparing_reference = False
            temporary, path = result
            if self.owner.closing:
                temporary.cleanup()
                return
            try:
                source = {"reference_audio": str(path), "name": snapshot["name"], "reference_text": snapshot["text"],
                          "source_label": "已选择识别音频 · 点击更换", "source_hint":
                          f"{self.clock(snapshot['start'])} – {self.clock(snapshot['end'])}" +
                          (" · 当前时间为估算，请先试听并核对参考原文。" if snapshot["estimated"] else "")}
                dialog = self.owner.import_reference(source=source, selected=lambda _: self.owner.statusBar().showMessage(
                    "已保存到声音库 → 参考声音，原识别音频保留。", 5000))
                def cleanup(attempt=0):
                    player = self.owner.reference_player
                    if not sip.isdeleted(player) and player.source() == QUrl.fromLocalFile(str(path)):
                        player.stop()
                        player.setSource(QUrl())
                    try:
                        temporary.cleanup()
                    except OSError:
                        if attempt < 10:
                            QTimer.singleShot(200, lambda: cleanup(attempt+1))
                dialog.finished.connect(lambda _: QTimer.singleShot(100, cleanup))
                self.render_details()
            except Exception as exc:
                temporary.cleanup()
                self.panel.status.setText("无法打开入库窗口：" + str(exc))
                self.owner.show_error(str(exc))
        def failed(message):
            self.preparing_reference = False
            self.panel.status.setText("准备参考声音失败：" + message)
            self.owner.show_error(message)
        self.owner.call(lambda _: prepare_reference_clip(self.owner.data_dir, snapshot["audio"], snapshot["start"], snapshot["end"]),
                        prepared, failed)
