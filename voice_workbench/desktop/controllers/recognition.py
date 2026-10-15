"""Speech recognition: import audio, follow one session, and play a single line at a time."""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import QFileDialog

from ..api_client import checked
from ..audio_output import SystemAudioOutput
from ...subtitles import normalize_timestamp
from ...recent_works import external_work

AUDIO_FILES = "音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.wma *.aiff *.aif)"
# Formats the native reader and the Qt player both accept; kept in step with the service.
DROP_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
STATUSES = {"queued": "等待识别", "running": "识别中", "done": "识别完成",
            "failed": "识别失败", "cancelled": "已取消"}
BUSY = ("queued", "running")


class RecognitionController:
    def __init__(self, owner, panel):
        self.owner, self.panel = owner, panel
        self.options = {}
        self.sessions = []
        self.session = None
        self.refreshing = False
        self.submitting = False
        self.applying = False
        self.open_sequence = 0
        self.play_range = None
        self.playing_line = None
        self.seek_pending = False
        self.last_refresh = 0.0
        self.continuous = False
        self.rendered_session = None
        self.rendered_lines = None
        self.selection_sequence = 0
        self.save_sequence = 0
        self.importing = None

    # ---------------------------------------------------------------- lifecycle

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
        self.name_timer = QTimer(self.panel)
        self.name_timer.setSingleShot(True)
        self.name_timer.timeout.connect(self.save_name)
        self.update_actions()

    def pause(self):
        """Called when the page is left: release the audio device and stop polling."""
        self.stop()
        if hasattr(self, "timer"):
            self.timer.stop()
        if hasattr(self, "name_timer") and self.name_timer.isActive():
            self.name_timer.stop()
            self.save_name()

    def resume(self):
        if hasattr(self, "timer"):
            self.timer.start(1200)
        self.refresh()

    # ------------------------------------------------------------------ polling

    def state(self):
        return (self.session or {}).get("state") or {}

    def busy(self):
        active = self.options.get("active") or {}
        return self.submitting or bool(active) or self.state().get("status") in BUSY

    def refresh(self, force=True):
        if not self.panel.isVisible() or not self.owner.ready or self.refreshing:
            self.update_actions()
            return
        if not force and not self.busy() and time.monotonic() - self.last_refresh < 8:
            return
        self.refreshing = True
        self.last_refresh = time.monotonic()
        identity = self.session["id"] if self.session else None

        def fetch(client):
            response = client.get("/recognition/options")
            if response.status_code == 404:
                raise RuntimeError("当前本地服务版本不支持语音识别，请关闭旧声笺及其服务后重新启动。")
            return (checked(response), checked(client.get("/recognition/sessions")),
                    checked(client.get(f"/recognition/sessions/{identity}")) if identity else None)

        def done(value):
            self.refreshing = False
            self.options, self.sessions, detail = value
            if detail and self.session and detail["id"] == self.session["id"]:
                self.session = detail
            self.render_sessions()
            self.render_options()
            self.render_session()
            self.update_actions()

        def failed(message):
            self.refreshing = False
            self.panel.status.setText(str(message))
            self.update_actions()

        self.owner.call(fetch, done, failed)

    def failure(self, message):
        self.submitting = False
        if self.importing:
            self.importing = None
            self.rendered_session = None
            self.render_sessions()
            self.render_session()
            self.owner.render_jobs(self.owner.jobs)
        self.panel.status.setText(str(message))
        self.update_actions()

    # -------------------------------------------------------------------- combos

    @staticmethod
    def fill(control, choices, empty=None, preferred=None):
        """Replace a combo's entries only when they actually changed."""
        if not choices and empty:
            choices = [(empty, None)]
        current = preferred if preferred is not None else control.currentData()
        if [(control.itemText(i), control.itemData(i)) for i in range(control.count())] == choices:
            if preferred is not None:
                control.blockSignals(True)
                control.setCurrentIndex(max(0, control.findData(preferred)))
                control.blockSignals(False)
            return
        control.blockSignals(True)
        control.clear()
        for text, value in choices:
            control.addItem(text, value)
            control.setItemData(control.count() - 1, text, 3)
        index = control.findData(current)
        control.setCurrentIndex(index if index >= 0 else 0)
        control.blockSignals(False)

    def render_options(self):
        selection = self.options.get("selection") or {}
        self.fill(self.panel.asr, [(f"{m['name']} · {m['family']}", m["path"]) for m in self.options.get("asr", [])],
                  "未安装识别模型，请到设置 → 模型下载中获取", selection.get("asr_model"))
        self.fill(self.panel.aligner, [(f"{m['name']} · {m['family']}", m["path"]) for m in self.options.get("align", [])],
                  "未安装对齐器（时间估算）", selection.get("align_model"))
        language = ((self.panel.language.currentData() or self.state().get("language")) if self.session and self.session["id"] == self.rendered_session
                    else selection.get("language"))
        self.fill(self.panel.language, list(self.options.get("languages") or []), preferred=language)

    def render_sessions(self):
        if self.importing and self.importing.get("preview", True):
            self.fill(self.panel.history, [(self.importing["name"] + " · 导入中", None)])
            return
        choices = [(s["name"] + " · " + STATUSES.get(s["status"], s["status"]), s["id"]) for s in self.sessions]
        self.fill(self.panel.history, [("选择识别记录", None)] + choices,
                  preferred=self.session["id"] if self.session else None)

    def apply_selection(self):
        """Persist the model and language defaults used by the next run."""
        if self.applying:
            return
        body = {"asr_model": self.panel.asr.currentData(), "align_model": self.panel.aligner.currentData(),
                "language": self.panel.language.currentData()}
        self.selection_sequence += 1
        sequence = self.selection_sequence
        self.owner.call(lambda c: checked(c.post("/recognition/selection", json=body)),
                        lambda value: self.options.update({"selection": value}) if sequence == self.selection_sequence else None,
                        lambda message: self.panel.status.setText("识别设置保存失败：" + str(message)))
        if self.session:
            self.name_timer.start(400)

    # ------------------------------------------------------------------- session

    def render_session(self):
        if self.importing and self.importing.get("preview", True):
            self.render_importing()
            return
        state = self.state()
        lines = state.get("lines") or []
        self.panel.drop_zone.setVisible(not lines)
        self.panel.lines.setVisible(bool(lines))
        identity = self.session["id"] if self.session else None
        switched = identity != self.rendered_session
        if switched or self.session is None:
            self.applying = True
            self.panel.title.blockSignals(True)
            self.panel.title.setText(state.get("name") or "未命名音频")
            self.panel.title.blockSignals(False)
            if self.session:
                self.fill(self.panel.language, list(self.options.get("languages") or []), preferred=state.get("language"))
            self.applying = False
        signature = tuple((line["index"], line["start"], line["end"], line["text"]) for line in lines)
        if switched or signature != self.rendered_lines:
            self.stop()
            self.panel.lines.blockSignals(True)
            self.panel.lines.clear()
            for line in lines:
                self.panel.lines.addItem(self.line_text(line))
            self.panel.lines.blockSignals(False)
            self.panel.clock.setText("00:00.00")
        self.rendered_session, self.rendered_lines = identity, signature
        self.panel.drop_title.setText(state.get("name") or "把音频拖到这里")
        self.panel.drop_hint.setText("正在识别音频，请稍候…" if state.get("status") in BUSY else
                                    "拖入音频后自动识别文字并按标点换行。")
        self.render_details()
        self.panel.drop_zone.setEnabled(not self.busy())

    @staticmethod
    def line_text(line, playing=False):
        clock = f"{RecognitionController.clock(line['start'])} – {RecognitionController.clock(line['end'])}"
        return ("▶  " if playing else "") + f"[{clock}]  " + line["text"]

    @staticmethod
    def clock(seconds):
        minutes, remainder = divmod(max(0.0, float(seconds or 0)), 60)
        return f"{int(minutes):02d}:{remainder:05.2f}"

    def render_details(self):
        state = self.state()
        if not self.session:
            self.panel.audio_name.setText("尚未导入音频")
            self.panel.audio_name.setToolTip("")
            self.panel.info.setText("把音频拖到下方区域，或点击「导入音频」。支持 WAV / FLAC / MP3 / M4A / OGG 等格式。")
            self.panel.status.setText("导入后会自动识别；识别完成后点击任意一行即可播放那一段。")
            return
        duration = state.get("duration") or 0.0
        source = state.get("source_path") or state.get("audio") or ""
        self.panel.audio_name.setText("当前音频：" + Path(source).name)
        self.panel.audio_name.setToolTip(source)
        details = f"{duration:.2f} 秒"
        if state.get("recognized_language"):
            details += f" · 识别语言 {state['recognized_language']}"
        if state.get("processing_seconds"):
            details += f" · 耗时 {state['processing_seconds']:.1f} 秒"
        if not state.get("lines"):
            details += f" · {len(state.get('text') or '')} 字"
        self.panel.info.setText(details)
        self.panel.info.setToolTip(state.get("source_path") or "")
        status = STATUSES.get(state.get("status"), "")
        if state.get("status") == "done":
            lines = state.get("lines") or []
            status = f"识别完成，共 {len(lines)} 行。点击任意一行播放对应的音频片段。"
        elif state.get("status") in BUSY:
            status = "正在识别，请稍候…" + ("首次运行需要加载模型。" if state.get("status") == "queued" else "")
        self.panel.status.setText(state.get("error") or state.get("warning") or status)

    def render_progress(self, row):
        for index in range(self.panel.lines.count()):
            item = self.panel.lines.item(index)
            line = (self.state().get("lines") or [])[index] if index < len(self.state().get("lines") or []) else None
            if line:
                item.setText(self.line_text(line, playing=index == row))

    # ---------------------------------------------------------------------- drops

    @staticmethod
    def dropped_path(mime):
        """The first dropped item that is a local audio file, if any."""
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = Path(url.toLocalFile()) if url.isLocalFile() else None
            if path and path.is_file() and path.suffix.lower() in DROP_SUFFIXES:
                return str(path)
        return None

    def accepts(self, mime):
        return self.dropped_path(mime) is not None

    # -------------------------------------------------------------------- actions

    def open_model_download(self, task=None):
        """Use the existing verified downloader, focused on this mode's models."""
        settings = self.owner.settings
        settings.model_filter.setCurrentIndex(settings.model_filter.findData("stt"))
        settings.model_search.setText({"asr": "qwen3_asr", "align": "qwen3_forced_aligner"}.get(task, ""))
        self.owner.set_page(3)
        settings.set_section(0)

    def render_importing(self):
        value = self.importing
        self.panel.title.blockSignals(True)
        self.panel.title.setText(value["name"])
        self.panel.title.blockSignals(False)
        self.panel.audio_name.setText("当前音频：" + Path(value["path"]).name)
        self.panel.audio_name.setToolTip(value["path"])
        self.panel.info.setText("正在导入音频…")
        self.panel.drop_title.setText(Path(value["path"]).name)
        self.panel.drop_hint.setText("正在读取音频并准备识别，请稍候…")
        self.panel.drop_zone.show()
        self.panel.drop_zone.setEnabled(False)
        self.panel.lines.hide()
        self.panel.clock.setText("00:00.00")
        self.panel.status.setText("正在导入音频，完成后自动识别。")

    def import_audio(self, path=None):
        if not self.owner.ready:
            self.panel.status.setText("请先连接本地服务。")
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self.panel, "选择要识别的音频", "", AUDIO_FILES)
        if not path:
            return
        if not self.options.get("asr"):
            self.panel.status.setText("还没有识别模型：请在设置 → 模型下载中安装 Qwen3-ASR 后重试。")
            return
        if self.busy():
            self.panel.status.setText("已有音频正在识别，请等待完成或先取消。")
            return
        self.open_sequence += 1
        sequence = self.open_sequence
        self.name_timer.stop()
        self.save_name()
        body = {"path": str(path), "name": Path(path).stem[:200],
                "language": self.panel.language.currentData() or "auto"}
        work = external_work("recognition", "importing-" + str(sequence), body["name"], time.time(), status="importing")
        work["pending_import"] = True
        self.stop()
        self.importing = {"path": str(path), "name": body["name"], "work": work}
        self.submitting = True
        self.render_sessions()
        self.render_importing()
        self.update_actions()
        self.owner.render_jobs(self.owner.jobs)

        def done(session):
            self.submitting = False
            self.importing = None
            if sequence == self.open_sequence:
                self.sessions = [{"id": session["id"], "name": session["state"]["name"], "status": session["state"]["status"]},
                                 *[s for s in self.sessions if s["id"] != session["id"]]]
                self.open_session(session, force=True)
            else:
                self.render_session()
                self.update_actions()
            state = session["state"]
            work = external_work("recognition", session["id"], state["name"], session["created"], session["updated"], state["status"])
            self.owner.render_jobs([work, *[j for j in self.owner.jobs if j["id"] != work["id"]]])

        self.owner.call(lambda c: checked(c.post("/recognition/sessions", json=body, timeout=150)), done, self.failure)

    def open_selected(self):
        identity = self.panel.history.currentData()
        if not identity or (self.session and self.session["id"] == identity):
            return
        self.name_timer.stop()
        self.save_name()
        self.stop()
        self.open_sequence += 1
        sequence = self.open_sequence
        self.owner.call(lambda c: checked(c.get(f"/recognition/sessions/{identity}")),
                        lambda session: self.open_session(session) if sequence == self.open_sequence else None,
                        self.failure)

    def open_session(self, session, force=False):
        self.name_timer.stop()
        self.stop()
        self.session = session
        self.render_sessions()
        self.render_session()
        self.update_actions()
        if force or self.state().get("status") in BUSY:
            self.refresh()

    def save_name(self):
        if self.applying or not self.session or not self.owner.ready:
            return
        identity = self.session["id"]
        body = {"name": self.panel.title.text().strip() or "未命名音频",
                "language": self.panel.language.currentData() or "auto"}
        if body["name"] == (self.state().get("name") or "未命名音频") and body["language"] == self.state().get("language", "auto"):
            return
        self.save_sequence += 1
        sequence = self.save_sequence
        def done(_):
            if sequence == self.save_sequence and self.session and self.session["id"] == identity:
                self.session["state"].update(body)
        self.owner.call(lambda c: checked(c.patch(f"/recognition/sessions/{identity}", json=body)),
                        done, lambda message: self.panel.status.setText("识别记录保存失败：" + str(message)))

    def recognize(self):
        if not self.session or self.busy():
            return
        if not self.options.get("asr"):
            self.panel.status.setText("还没有识别模型：请在设置 → 模型下载中安装 Qwen3-ASR 后重试。")
            return
        identity = self.session["id"]
        sequence = self.open_sequence
        self.submitting = True
        self.update_actions()
        body = {"name": self.panel.title.text().strip() or "未命名音频",
                "language": self.panel.language.currentData() or "auto"}

        def accepted(session):
            self.submitting = False
            if sequence == self.open_sequence:
                self.open_session(session, force=True)
            else:
                self.update_actions()

        def launch():
            self.owner.call(lambda c: checked(c.post(f"/recognition/sessions/{identity}/retry", timeout=30)),
                            accepted, self.failure)

        self.panel.status.setText("正在重新识别…")
        self.owner.call(lambda c: checked(c.patch(f"/recognition/sessions/{identity}", json=body)),
                        lambda _: launch(), self.failure)

    def cancel(self):
        if self.session:
            identity = self.session["id"]
            self.owner.call(lambda c: checked(c.post(f"/recognition/sessions/{identity}/cancel")),
                            lambda _: self.refresh(), self.failure)

    def remove(self):
        if not self.session:
            return
        identity = self.session["id"]
        self.stop()
        self.session = None

        def done(_):
            self.session = None
            self.render_session()
            self.refresh()

        self.owner.call(lambda c: checked(c.delete(f"/recognition/sessions/{identity}")), done, self.failure)

    def export_subtitles(self):
        lines = self.state().get("lines") or []
        if not lines:
            self.panel.status.setText("还没有可导出的识别结果。")
            return
        name = (self.state().get("name") or "识别结果") + ".srt"
        path, _ = QFileDialog.getSaveFileName(self.panel, "导出字幕", name, "SRT 字幕 (*.srt);;纯文本 (*.txt)")
        if not path:
            return
        target = Path(path)
        try:
            if target.suffix.lower() == ".txt":
                target.write_text("\n".join(line["text"] for line in lines), encoding="utf-8")
            else:
                blocks = []
                for line in lines:
                    start = normalize_timestamp("", round(line["start"] * 1000)).replace(".", ",")
                    end = normalize_timestamp("", round(line["end"] * 1000)).replace(".", ",")
                    blocks.append(f"{line['index']}\n{start} --> {end}\n{line['text']}")
                target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
            self.panel.status.setText("已导出：" + str(target))
        except OSError as exc:
            self.owner.show_error(str(exc))

    def copy_text(self):
        lines = self.state().get("lines") or []
        if lines:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText("\n".join(line["text"] for line in lines))
            self.panel.status.setText("已复制识别文字。")

    # ------------------------------------------------------------------ playback

    def audio_source(self):
        state = self.state()
        if not state.get("audio"):
            return None
        local = Path(self.owner.data_dir) / "recognition" / state["audio"]
        return QUrl.fromLocalFile(str(local)) if local.is_file() else QUrl(self.owner.url + (state.get("audio_url") or ""))

    def play_line(self, row):
        lines = self.state().get("lines") or []
        if not 0 <= row < len(lines):
            return
        if self.playing_line == row:
            self.stop()
            return
        source = self.audio_source()
        if source is None:
            self.panel.status.setText("音频文件已不存在，请重新导入。")
            return
        line = lines[row]
        self.play_range = (line["start"], line["end"])
        self.playing_line = row
        self.seek_pending = True
        self.render_progress(row)
        if self.player.source() != source:
            self.player.setSource(source)
        else:
            self.player.setPosition(round(line["start"] * 1000))
            self.player.play()
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()
        self.panel.clock.setText(f"{line['start']:.2f} – {line['end']:.2f} 秒 · 第 {line['index']} 行")
        self.panel.toggle.setEnabled(True)

    def media_ready(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.seek_pending:
            self.seek_pending = False
            if self.play_range:
                self.player.setPosition(round(self.play_range[0] * 1000))
            self.player.play()

    def position_changed(self, position):
        if not self.play_range:
            return
        if position >= round(self.play_range[1] * 1000):
            lines = self.state().get("lines") or []
            if self.continuous and self.playing_line is not None and self.playing_line + 1 < len(lines):
                following = self.playing_line + 1
                self.playing_line = None
                self.play_line(following)
                return
            self.stop()

    def toggle(self):
        if self.playing_line is not None:
            self.stop()
        else:
            self.play_line(0)

    def stop(self):
        self.play_range = None
        self.seek_pending = False
        if self.playing_line is not None:
            previous = self.playing_line
            self.playing_line = None
            self.render_progress(-1)
            self.panel.clock.setText(f"{previous + 1} 行已停止")
        if hasattr(self, "player"):
            self.player.stop()

    # -------------------------------------------------------------------- state

    def update_actions(self):
        ready = self.owner.ready
        state = self.state()
        busy = self.busy()
        importing = bool(self.importing and self.importing.get("preview", True))
        lines = state.get("lines") or []
        self.panel.title.setReadOnly(importing)
        self.panel.history.setEnabled(ready and not importing)
        self.panel.import_button.setEnabled(ready and not busy)
        self.panel.recognize_button.setEnabled(bool(ready and self.session and self.options.get("asr") and not busy))
        self.panel.cancel_button.setEnabled(bool(busy and self.options.get("active")
                                                 and self.session and self.options["active"]["session_id"] == self.session["id"]))
        self.panel.remove_button.setEnabled(bool(ready and self.session and not busy))
        self.panel.export_button.setEnabled(bool(lines) and not importing)
        self.panel.copy_button.setEnabled(bool(lines) and not importing)
        self.panel.toggle.setEnabled(bool(lines) and not importing)
        self.panel.continuous.setEnabled(bool(lines) and not importing)
        self.panel.setup_button.setEnabled(ready and not importing)
        for button, key in ((self.panel.asr_download, "asr"), (self.panel.aligner_download, "align")):
            button.setEnabled(ready and not importing)
            button.setText("管理" if self.options.get(key) else "下载")
            button.setToolTip("打开对应模型的下载列表，可选择精度、来源并查看下载进度。")
        for control in (self.panel.asr, self.panel.aligner, self.panel.language):
            control.setEnabled(ready and not busy)

    def close(self):
        if hasattr(self, "timer"):
            self.timer.stop()
        self.name_timer.stop()
        self.stop()
        self.player.setSource(QUrl())
