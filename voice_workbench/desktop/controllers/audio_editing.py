"""Audio editor navigation, asynchronous processing and single-clip playback."""
import shutil
import time
from datetime import datetime
from pathlib import Path
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import QFileDialog, QListWidgetItem
from ..api_client import checked
from ..audio_output import SystemAudioOutput
from ..draft_saves import DraftSaveQueue

OPERATIONS = {"import": "原音频", "render": "裁剪 / 调整", "append": "拼接", "vc": "更换音色", "edit": "修改台词"}
STATUSES = {"queued": "等待处理", "running": "处理中", "done": "已完成", "failed": "失败", "cancelled": "已取消"}


class AudioEditController:
    def __init__(self, owner, panel):
        self.owner, self.panel = owner, panel
        self.project = None
        self.current = None
        self.options = {}
        self.refreshing = False
        self.submitting = False
        self.pending = None
        self.play_range = None
        self.open_sequence = 0
        self.save_running = False
        self.draft_saves = DraftSaveQueue(owner.data_dir / "pending-audio-edits.json", title_field="name")
        self.save_callbacks = []
        self.last_refresh = 0

    def connect(self):
        self.output = SystemAudioOutput(self.panel)
        self.player = QMediaPlayer(self.panel)
        self.player.setAudioOutput(self.output)
        self.player.positionChanged.connect(self.position_changed)
        self.player.mediaStatusChanged.connect(self.media_ready)
        self.player.errorOccurred.connect(lambda _, message: self.panel.status.setText("播放失败：" + message))
        self.timer = QTimer(self.panel)
        self.timer.timeout.connect(lambda: self.refresh(force=False))
        self.timer.start(1200)
        self.update_actions()

    def refresh(self, force=True):
        if not self.panel.isVisible() or not self.owner.ready or self.refreshing:
            self.update_actions()
            return
        if not force and not self.pending and not self.options.get("active") and time.monotonic() - self.last_refresh < 10:
            return
        self.refreshing = True
        self.last_refresh = time.monotonic()
        identity = self.project["id"] if self.project else None
        def fetch(client):
            response = client.get("/audio-editor/options")
            if response.status_code == 404:
                raise RuntimeError("当前本地服务版本不支持音频编辑，请关闭旧声笺及其服务后重新启动。")
            return checked(response), checked(client.get("/audio-editor/projects")), \
                   checked(client.get(f"/audio-editor/projects/{identity}")) if identity else None
        def done(value):
            self.refreshing = False
            self.options, projects, project = value
            self._combo(self.panel.models, [(m["name"], m["path"]) for m in self.options["models"]], "未安装 Vevo2，请下载或导入")
            reference = self.panel.reference.currentData()
            refs = [("沿用原音频音色（台词修改）", None)] + [(r["name"], r["reference_audio"]) for r in self.options["references"]]
            if reference and reference not in [v for _, v in refs]:
                refs.append((Path(reference).stem, reference))
            self._combo(self.panel.reference, refs)
            self._combo(self.panel.projects, [("选择已保存的项目", None)] + [(p["name"], p["id"]) for p in projects],
                        preferred=self.project["id"] if self.project else None)
            if project and self.project and project["id"] == self.project["id"]:
                self.project = project
                self.render_history()
                if self.pending:
                    result = next((v for v in project["versions"] if v["id"] == self.pending), None)
                    if result and result["status"] in ("done", "failed", "cancelled"):
                        self.pending = None
                        if result["status"] == "done":
                            if self.current and result["parent_id"] == self.current["id"]:
                                self.select_version(result)
                            clipped = (result.get("details") or {}).get("clipped_samples", 0)
                            self.panel.status.setText("处理完成，新版本已保留。" + ("部分采样发生削波，请降低音量后重试。" if clipped else ""))
                        else:
                            self.panel.status.setText(result.get("error") or STATUSES[result["status"]])
            self.update_actions()
            self._save_next()
        def failed(message):
            self.refreshing = False
            self.panel.status.setText(str(message))
            self.update_actions()
        self.owner.call(fetch, done, failed)

    @staticmethod
    def _combo(control, choices, empty=None, preferred=None):
        if not choices and empty:
            choices = [(empty, None)]
        desired = preferred if preferred is not None else control.currentData()
        if [(control.itemText(i), control.itemData(i)) for i in range(control.count())] == choices:
            if preferred is not None:
                control.blockSignals(True)
                control.setCurrentIndex(control.findData(preferred))
                control.blockSignals(False)
            return
        control.blockSignals(True)
        control.clear()
        for text, value in choices:
            control.addItem(text, value)
            control.setItemData(control.count() - 1, text, 3)
        if not choices and empty:
            control.addItem(empty, None)
        index = control.findData(desired)
        control.setCurrentIndex(index if index >= 0 else 0)
        control.blockSignals(False)

    def state(self):
        return {"name": self.panel.title.text().strip() or "未命名音频",
                "original_text": self.panel.original_text.toPlainText(), "target_text": self.panel.target_text.toPlainText(),
                "selected_version": self.current["id"] if self.current else None}

    def save(self, after=None):
        if not self.project:
            if callable(after):
                after()
            return
        self.capture_save()
        if callable(after):
            self.save_callbacks.append(after)
        self._save_next()

    def capture_save(self):
        if self.project:
            self.draft_saves.stage(self.project["id"], self.state())
            if self.draft_saves.write_error:
                self.panel.status.setText("本地编辑草稿暂存失败：" + self.draft_saves.write_error)

    def _save_next(self):
        if self.save_running or not self.draft_saves.pending or not self.owner.ready:
            return
        snapshot = next(iter(self.draft_saves.pending.values()))
        identity, body = snapshot.task_id, snapshot.body
        self.save_running = True
        def done(_):
            self.save_running = False
            self.draft_saves.acknowledge(snapshot)
            if self.draft_saves.pending:
                self._save_next()
            else:
                callbacks, self.save_callbacks = self.save_callbacks, []
                for callback in callbacks:
                    callback()
                self.panel.status.setText("名称与台词已保存。")
        def failed(message):
            self.save_running = False
            self.submitting = False
            self.draft_saves.retry_later(snapshot)
            self.save_callbacks.clear()
            self.panel.status.setText("保存失败：" + str(message))
            self.update_actions()
        self.owner.call(lambda c: checked(c.patch(f"/audio-editor/projects/{identity}", json=body)), done, failed)

    def import_audio(self, path=None, text="", name=""):
        if not self.owner.ready:
            self.panel.status.setText("请先连接本地服务。")
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self.panel, "导入音频", "", "PCM WAV (*.wav)")
        if path:
            self.save()
            self.open_sequence += 1
            sequence = self.open_sequence
            self.owner.call(lambda c: checked(c.post("/audio-editor/projects", json={"path": str(path), "original_text": text, "name": name}, timeout=30)),
                            lambda p: self.open_project(p) if sequence == self.open_sequence else None)

    def open_selected_project(self):
        identity = self.panel.projects.currentData()
        if not identity or self.project and self.project["id"] == identity:
            return
        self.save()
        self.open_sequence += 1
        sequence = self.open_sequence
        self.owner.call(lambda c: checked(c.get(f"/audio-editor/projects/{identity}")),
                        lambda p: self.open_project(p) if sequence == self.open_sequence else None)

    def open_project(self, project):
        self.stop()
        self.project = project
        self.current = None
        self.pending = next((v["id"] for v in project["versions"] if v["status"] in ("queued", "running")), None)
        pending = self.draft_saves.pending.get(project["id"])
        state = pending.body if pending else project["state"]
        self.panel.title.setText(state["name"])
        self.panel.original_text.setPlainText(state["original_text"])
        self.panel.target_text.setPlainText(state["target_text"])
        versions = [v for v in project["versions"] if v["status"] == "done"]
        self.select_version(next((v for v in versions if v["id"] == state.get("selected_version")), versions[-1]), persist=False)
        self.refresh()

    def select_version(self, version, persist=True):
        self.stop()
        self.current = version
        info = version["details"]
        self.panel.waveform.duration = info["duration"]
        self.panel.waveform.load(Path(version["path"]))
        self.panel.start.setMaximum(info["duration"])
        self.panel.end.setMaximum(info["duration"])
        self.set_selection(0, info["duration"])
        self.panel.info.setText(f"{info['duration']:.3f} 秒 · {info['sample_rate'] / 1000:g} kHz · {info['channels']} 声道 · {info['sample_width'] * 8} 位\n"
                                "拖动波形选择范围；处理结果是该选区的新音频。")
        self.panel.info.setToolTip(f"片段 ID：{self.project['clip_id']}\n版本 ID：{version['id']}\n{version['path']}")
        if persist:
            saved = version["parameters"].get("state", self.project["state"])
            original_text = saved["target_text"] if version["operation"] == "edit" else saved["original_text"]
            self.panel.original_text.setPlainText(original_text)
        self.render_history()
        self.update_actions()
        if persist:
            self.save()

    def render_history(self):
        signature = (self.project["id"], self.current["id"] if self.current else None,
                     tuple((v["id"], v["status"], v["error"]) for v in self.project["versions"]))
        if signature == getattr(self, "history_signature", None):
            return
        self.history_signature = signature
        self.panel.history.blockSignals(True)
        self.panel.history.clear()
        selected = -1
        for i, version in enumerate(self.project["versions"]):
            duration = f" · {version['details']['duration']:.2f} 秒" if version["details"] else ""
            stamp = datetime.fromtimestamp(version["created"]).strftime("%m-%d %H:%M:%S")
            cell = QListWidgetItem(f"{OPERATIONS[version['operation']]} · {STATUSES[version['status']]}{duration} · {stamp}")
            cell.setToolTip(f"片段 ID：{self.project['clip_id']}\n版本 ID：{version['id']}\n来源版本：{version['parent_id'] or '原始导入'}\n"
                            + (version["error"] or str(version["parameters"])))
            self.panel.history.addItem(cell)
            if self.current and version["id"] == self.current["id"]:
                selected = i
        self.panel.history.setCurrentRow(selected)
        self.panel.history.blockSignals(False)

    def choose_version(self, row):
        if self.project and 0 <= row < len(self.project["versions"]):
            version = self.project["versions"][row]
            if version["status"] == "done":
                self.select_version(version)
            else:
                self.panel.status.setText(version["error"] or STATUSES[version["status"]])

    def set_selection(self, start, end):
        for control, value in ((self.panel.start, start), (self.panel.end, end)):
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        self.selection_changed()

    def selection_changed(self):
        self.panel.waveform.select(self.panel.start.value(), self.panel.end.value())
        self.update_actions()

    def select_all(self):
        if self.current:
            self.set_selection(0, self.current["details"]["duration"])

    def process(self, operation, append=None):
        if not self.current or self.submitting:
            return
        try:
            seed = int(self.panel.seed.text())
            if not -1 <= seed <= 4294967295:
                raise ValueError()
        except ValueError:
            self.panel.status.setText("随机种子需为 -1 或 0–4294967295 的整数。")
            return
        identity = self.project["id"]
        duration = self.current["details"]["duration"]
        end = None if abs(self.panel.end.value() - duration) < 0.000501 else self.panel.end.value()
        body = {"source_version": self.current["id"], "operation": operation, "start": self.panel.start.value(),
                "end": end, "gain_db": self.panel.gain.value(),
                "fade_in": self.panel.fade_in.value(), "fade_out": self.panel.fade_out.value(),
                "append_path": append, "model_path": self.panel.models.currentData(), "reference_path": self.panel.reference.currentData(),
                "device": self.panel.device.currentData(), "seed": seed, "steps": self.panel.steps.value(), "state": self.state()}
        self.submitting = True
        self.update_actions()
        def launch():
            def done(project):
                self.submitting = False
                if self.project and project["id"] == self.project["id"]:
                    self.project = project
                    self.pending = project["versions"][0]["id"]
                    self.panel.status.setText("正在处理选区…")
                    self.render_history()
                self.update_actions()
            def failed(message):
                self.submitting = False
                self.panel.status.setText(str(message))
                self.update_actions()
            self.owner.call(lambda c: checked(c.post(f"/audio-editor/projects/{identity}/process", json=body, timeout=30)), done, failed)
        if self.save_running or self.draft_saves.pending:
            self.save(after=launch)
        else:
            launch()

    def append_audio(self):
        path, _ = QFileDialog.getOpenFileName(self.panel, "选择拼接音频（格式需一致）", "", "PCM WAV (*.wav)")
        if path:
            self.process("append", path)

    def import_model(self):
        path, _ = QFileDialog.getOpenFileName(self.panel, "选择 Vevo2 模型", "", "Vevo2 GGUF (*.gguf)")
        if path:
            self.owner.call(lambda c: checked(c.post("/audio-editor/models", json={"path": path}, timeout=30)), lambda _: self.refresh())

    def choose_reference(self):
        path, _ = QFileDialog.getOpenFileName(self.panel, "目标音色参考（1–60 秒）", "", "PCM WAV (*.wav)")
        if path:
            self.panel.reference.addItem(Path(path).stem, path)
            self.panel.reference.setCurrentIndex(self.panel.reference.count() - 1)
            self.update_actions()

    def cancel(self):
        if self.project:
            identity = self.project["id"]
            self.owner.call(lambda c: checked(c.post(f"/audio-editor/projects/{identity}/cancel")), lambda _: self.refresh())

    def undo(self):
        if self.current and self.current["parent_id"]:
            self.select_version(next(v for v in self.project["versions"] if v["id"] == self.current["parent_id"]))

    def update_actions(self):
        ready = self.owner.ready
        active = self.options.get("active")
        editable = bool(self.current and ready and not self.submitting and not active and not self.pending)
        selection = bool(self.panel.end.value() > self.panel.start.value())
        for control in (self.panel.render_button, self.panel.append_button):
            control.setEnabled(editable and selection)
        ai = bool(editable and selection and self.panel.models.currentData() and self.options.get("runtime"))
        self.panel.vc_button.setEnabled(ai)
        self.panel.edit_button.setEnabled(ai)
        self.panel.cancel_button.setEnabled(bool(self.pending or active and self.project and active["project_id"] == self.project["id"]))
        for control in (self.panel.preview_button, self.panel.original_button, self.panel.export_button):
            control.setEnabled(bool(self.current))
        self.panel.undo_button.setEnabled(bool(self.current and self.current["parent_id"]))
        self.panel.import_button.setEnabled(ready)

    def play(self, version, start=0, end=None):
        self.owner.clear_autoplay()
        self.owner.player.stop()
        self.owner.reference_player.stop()
        self.stop()
        self.play_range = (start, end if end is not None else version["details"]["duration"])
        self.seek_pending = True
        self.playing_version = version["id"]
        self.player.setSource(QUrl.fromLocalFile(version["path"]))
        self.player.play()

    def media_ready(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.play_range and self.seek_pending:
            self.seek_pending = False
            self.player.setPosition(round(self.play_range[0] * 1000))

    def preview_selection(self):
        if self.current:
            self.play(self.current, self.panel.start.value(), self.panel.end.value())

    def preview_original(self):
        if self.project:
            self.play(next(v for v in self.project["versions"] if v["operation"] == "import"))

    def position_changed(self, position):
        if self.play_range:
            end = self.play_range[1]
            self.panel.clock.setText(f"{position / 1000:.2f} / {end:.2f} 秒")
            if self.current and self.playing_version == self.current["id"]:
                self.panel.waveform.set_progress(position / 1000 / max(0.001, self.panel.waveform.duration))
            if position >= round(end * 1000):
                self.stop()

    def stop(self):
        self.play_range = None
        if hasattr(self, "player"):
            self.player.stop()

    def export(self):
        if not self.current:
            return
        path, _ = QFileDialog.getSaveFileName(self.panel, "导出当前版本", self.state()["name"] + ".wav", "WAV (*.wav)")
        if path:
            target = Path(path)
            if target.resolve() == Path(self.current["path"]).resolve() or target.resolve().is_relative_to((self.owner.data_dir / "audio-editor").resolve()):
                self.panel.status.setText("请选择编辑数据目录以外的导出位置，以保护版本文件。")
                return
            try:
                shutil.copyfile(self.current["path"], target)
                self.panel.status.setText("已导出当前版本：" + str(target))
            except OSError as exc:
                self.owner.show_error(str(exc))

    def close(self):
        self.timer.stop()
        self.stop()
        self.player.setSource(QUrl())
