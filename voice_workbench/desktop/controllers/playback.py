"""PlaybackController responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
import json
import re
import shutil
from pathlib import Path
from datetime import datetime
from PyQt6.QtCore import QUrl, Qt, QPoint
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import QFileDialog
from ...ui import generation_elapsed, sorted_audio, icon
from ...subtitles import normalize_timestamp
from ...runner import validate_wave
from PyQt6.QtGui import QColor
from ..constants import STATES
from ...services.dialogue import audio_name, dialogue_id
from ...naming import display_title


class PlaybackController:
    def edit_current_audio(self):
        item = self.current_segment()
        if item and item["status"] == "done" and item.get("audio"):
            self.open_audio_editor(self.data_dir / "audio" / item["audio"], item.get("text", ""),
                                   name=display_title(self.job_data) if self.job_data else "音频编辑")
        else:
            self.statusBar().showMessage("请先选中已完成的音频片段。", 5000)

    def dialogue_audio_name(self, item):
        row = next((r for r in self.dialogue_editor.rows if r.identity == item.get("source_id")), None)
        return audio_name({**item, "source_name": row.role_name} if row else item)

    def render_job(self, job, force=False):
        if job["id"] != self.selected_job:
            return
        scoped = self.scoped_segments(job)
        if not force and job == self.job_data and self.segments_table.rowCount() == len(scoped):
            if self.auto_select_audio and self.playable_rows():
                self.segments_table.selectRow(self.playable_rows()[0])
                self.auto_select_audio = False
            self.preview_segment()
            self.maybe_autoplay(job)
            return
        selected_idx = (self.current_segment() or {}).get("idx")
        self.job_data = job
        self.update_dialogue_records()
        self.visible_segments = sorted_audio(scoped, self.clip_sort.currentData())
        selected_row = next((row for row, s in enumerate(self.visible_segments) if s["idx"] == selected_idx), -1)
        self.segments_table.blockSignals(True)
        self.segments_table.setUpdatesEnabled(False)
        if self.segments_table.rowCount() != len(scoped):
            self.segments_table.setRowCount(len(scoped))
        for row, item in enumerate(self.visible_segments):
            error = item["error"] or {}
            status = STATES[item["status"]] + (" · 缓存" if item["cached"] else "")
            created = item.get("created") or job.get("created")
            stamp = datetime.fromtimestamp(created).strftime("%m-%d %H:%M:%S") if created else item.get("timestamp", "")
            name = self.dialogue_audio_name(item) if item.get("source_id") else display_title(job) + f" · 第 {item.get('generation', 1)} 次 · {item['text']}"
            elapsed, elapsed_tip = generation_elapsed(item)
            for col, text in enumerate((str(item["idx"]), stamp, name, elapsed, status, error.get("message", ""))):
                tooltip = ("生成时间：" + stamp + ("\n字幕时间：" + item["timestamp"] if item["timestamp"] else "")) if col == 1 else text
                if col == 3:
                    tooltip = elapsed_tip
                elif col == 2 and item.get("source_id"):
                    tooltip = name + "\n对白 ID：" + item["source_id"] + "\n正文：" + item["text"] + f"\n生成批次：{item.get('generation', 1)}"
                cell = self.update_table_cell(self.segments_table, row, col, text, tooltip)
                if col in (0, 1, 3) and cell.textAlignment() != Qt.AlignmentFlag.AlignCenter:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if 0 <= selected_row < len(scoped) and self.segments_table.currentRow() != selected_row:
            self.segments_table.selectRow(selected_row)
        elif selected_row < 0:
            self.segments_table.setCurrentCell(-1, -1)
            self.auto_select_audio = True
        if self.auto_select_audio and self.playable_rows():
            self.segments_table.selectRow(self.playable_rows()[0])
            self.auto_select_audio = False
        self.segments_table.blockSignals(False)
        self.decorate_job(job)
        self.segments_table.setUpdatesEnabled(True)
        self.preview_segment()
        self.maybe_autoplay(job)

    def current_segment(self):
        row = self.segments_table.currentRow()
        return self.visible_segments[row] if self.job_data and 0 <= row < len(self.visible_segments) else None

    def play(self, automatic=False):
        if not automatic:
            self.clear_autoplay()
        self.settings.player.pause()
        item = self.current_segment()
        if item and item["status"] == "done":
            self.stop_reference()
            self.preview_segment()
            if self.play_button.isEnabled():
                self.player.play()
        else:
            self.statusBar().showMessage("请选中一个已完成的片段。", 5000)

    def details(self):
        item = self.current_segment()
        if item is None:
            return
        error = item["error"]
        if error:
            self.call(lambda c: c.get(error["log"]).text, lambda text: self.show_details(text))
        else:
            self.show_details(json.dumps(item, ensure_ascii=False, indent=2))

    def export(self):
        if not self.job_data:
            return
        current = self.current_segment()
        generation = (current or self.job_data["segments"][-1]).get("generation", 1) if self.job_data["segments"] else 1
        items = [s for s in self.visible_segments if s.get("generation", 1) == generation and s["status"] == "done"]
        if not items:
            self.show_error("当前生成批次没有已完成片段。")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not folder:
            return
        base = Path(folder) / self.job_data["id"]
        if self.editor_mode == "dialogue" and self.dialogue_editor.selected_id:
            base = base / dialogue_id(self.dialogue_editor.selected_id)
        target = base / f"生成-{generation:03d}"
        target.mkdir(parents=True, exist_ok=True)
        manifest = []
        cursor_ms = 0
        try:
            for item in items:
                if item["status"] == "done":
                    stamp = normalize_timestamp(item["timestamp"], cursor_ms)
                    short_text = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", item["text"])[:2] or "音频"
                    filename = f"{item['idx']:04d}_{stamp.replace(':', '-')}_{short_text}.wav"
                    shutil.copy2(self.data_dir / "audio" / item["audio"], target / filename)
                    manifest.append({"file": filename, "text": item["text"], "timestamp": stamp,
                                     "generation": generation, "created": item.get("created"), "voice": item.get("voice", self.job_data["voice"]),
                                     "content_type": item.get("content_type", "article"), "source_id": item.get("source_id")})
                    cursor_ms += round(validate_wave(self.data_dir / "audio" / item["audio"])["duration"] * 1000)
            (target / "segments.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self.statusBar().showMessage(f"已导出 {len(manifest)} 个片段：{target}", 8000)
        except Exception as exc:
            self.show_error(str(exc))

    def toggle_clip_list(self):
        if self.clip_list.isVisible():
            self.clip_list.hide()
            return
        width = min(860, self.transport.width() - 36)
        height = min(420, max(220, 140 + self.segments_table.rowCount() * 36))
        self.clip_list.resize(width, height)
        self.clip_list.move(self.transport.mapTo(self, QPoint(18, -height - 6)))
        self.clip_list.show()
        self.segments_table.setFocus()
        if self.segments_table.currentRow() >= 0:
            self.segments_table.scrollToItem(self.segments_table.item(self.segments_table.currentRow(), 0))

    def choose_clip(self, *_):
        self.auto_select_audio = False
        self.clear_autoplay()
        item = self.current_segment()
        if item and item["status"] == "done" and self.play_button.isEnabled():
            self.player.setPosition(0)
            self.play()
            self.clip_list.hide()

    def clip_details(self, *_):
        self.clip_list.hide()
        self.details()

    def playable_rows(self):
        return [row for row, item in enumerate(self.visible_segments if self.job_data else [])
                if item["status"] == "done" and item.get("audio") and (self.data_dir / "audio" / item["audio"]).is_file()]

    def adjacent_row(self, direction, indices=None):
        row = self.segments_table.currentRow()
        playable = [i for i in self.playable_rows() if indices is None or self.visible_segments[i]["idx"] in indices]
        if indices is not None:
            current = self.current_segment()
            if not current:
                return None
            rows = [i for i in playable if (self.visible_segments[i]["idx"] - current["idx"]) * direction > 0]
            return min(rows, key=lambda i: abs(self.visible_segments[i]["idx"] - current["idx"])) if rows else None
        rows = [i for i in playable if i < row] if direction < 0 else [i for i in playable if i > row]
        return (rows[-1] if direction < 0 else rows[0]) if rows else None

    def play_adjacent(self, direction, automatic=False):
        if not automatic:
            self.clear_autoplay()
        row = self.adjacent_row(direction, self.autoplay_indices if automatic else None)
        if row is not None:
            self.segments_table.selectRow(row)
            self.player.setPosition(0)
            self.play(automatic=automatic)

    def playback_finished(self, status):
        if not self.closing and status == QMediaPlayer.MediaStatus.EndOfMedia and self.autoplay_indices:
            self.play_adjacent(1, automatic=True)

    def clear_autoplay(self):
        self.pending_autoplay = None
        self.autoplay_indices = None

    def maybe_autoplay(self, job):
        pending = self.pending_autoplay
        if not pending or pending["job_id"] != job["id"]:
            return
        items = [s for s in job["segments"] if s["idx"] in pending["indices"]]
        if len(items) != len(pending["indices"]) or any(s["status"] in ("queued", "running") for s in items):
            return
        self.pending_autoplay = None
        rows = sorted((row for row in self.playable_rows() if self.visible_segments[row]["idx"] in pending["indices"]),
                      key=lambda row: self.visible_segments[row]["idx"])
        if rows:
            self.autoplay_indices = pending["indices"]
            self.auto_select_audio = False
            self.segments_table.selectRow(rows[0])
            self.segments_table.scrollToItem(self.segments_table.item(rows[0], 0))
            self.player.setPosition(0)
            self.play(automatic=True)

    def sort_segments(self):
        if self.job_data:
            self.render_job(self.job_data, force=True)
            row = self.segments_table.currentRow()
            if row >= 0:
                self.segments_table.scrollToItem(self.segments_table.item(row, 0))

    def preview_segment(self):
        item = self.current_segment()
        playable = bool(item and item["status"] == "done" and item.get("audio") and (self.data_dir / "audio" / item["audio"]).is_file())
        source = self.data_dir / "audio" / item["audio"] if playable else None
        media_url = QUrl.fromLocalFile(str(source)) if source else QUrl()
        if source != self.waveform.path or self.player.source() != media_url:
            self.player.stop()
            self.player.setSource(media_url)
            self.waveform.load(source)
            self.clip_duration = 0
            if source:
                from ...runner import validate_wave
                try:
                    self.clip_duration = round(validate_wave(source)["duration"] * 1000)
                except Exception as exc:
                    self.statusBar().showMessage("音频文件无法读取：" + str(exc), 8000)
                    playable = False
        self.play_button.setEnabled(playable)
        if playable and not getattr(self, "clip_duration", 0):
            self.play_button.setEnabled(False)
        self.details_button.setEnabled(bool(item))
        self.previous_button.setEnabled(self.adjacent_row(-1) is not None)
        self.next_button.setEnabled(self.adjacent_row(1) is not None)
        self.clip_title.setEnabled(bool(self.visible_segments))
        title = ((self.dialogue_audio_name(item) if item.get("source_id") else display_title(self.job_data)) + " · " + item["text"] if item else
                 "本框还没有生成音频" if self.editor_mode == "dialogue" else "还没有生成记录")
        self.clip_title.setText(self.clip_title.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 184))
        self.clip_title.setToolTip(title + ("\n对白 ID：" + item["source_id"] if item and item.get("source_id") else "") + "\n点击展开生成列表")
        self.update_transport()

    def toggle_playback(self):
        self.clear_autoplay()
        self.settings.player.pause()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.play()

    def update_play_icon(self, state):
        self.play_button.setIcon(icon("pause" if state == QMediaPlayer.PlaybackState.PlayingState else "play", "#ffffff"))
        text = "暂停" if state == QMediaPlayer.PlaybackState.PlayingState else "播放"
        self.play_button.setToolTip(text)
        self.play_button.setAccessibleName(text)

    def update_transport(self, *_):
        duration, position = self.player.duration() or getattr(self, "clip_duration", 0), self.player.position()
        def clock(ms):
            seconds = ms // 1000
            return f"{seconds // 60:02d}:{seconds % 60:02d}.{ms % 1000 // 100}"
        self.clip_time.setText(f"{clock(position)} / {clock(duration)}")
        self.seek_slider.setRange(0, duration)
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self.waveform.set_progress(position / duration if duration else 0)

    def decorate_job(self, job):
        from ..constants import STATES
        counts = {state: sum(s["status"] == state for s in self.visible_segments) for state in STATES}
        done, total = counts["done"], len(self.visible_segments)
        self.job_summary.setVisible(total > 0)
        status = next((s for s in ("running", "queued", "failed", "cancelled", "done") if counts[s]), "draft")
        self.job_summary.setText(f"{STATES[status]} · {done}/{total}" + (f" · {counts['failed']} 段失败" if counts["failed"] else ""))
        self.job_summary.setToolTip(job["title"])
        selected = self.dialogue_editor.selected_row() if self.editor_mode == "dialogue" else None
        source = (selected.role_name or dialogue_id(selected.identity)) if selected else display_title(job)
        self.clip_list_title.setText(source + " · 生成记录")
        self.clip_list_title.setToolTip("对白 ID：" + selected.identity if selected else display_title(job))
        self.job_progress.setRange(0, max(1, total))
        self.job_progress.setValue(done)
        self.job_progress.setVisible(status in ("running", "queued"))
        self.export_button.setEnabled(done > 0)
        self.retry_button.setEnabled(counts["failed"] + counts["cancelled"] > 0)
        self.cancel_button.setEnabled(job["status"] in ("running", "queued"))
        self.retry_button.setVisible(self.editor_mode == "article")
        self.cancel_button.setVisible(self.editor_mode == "article")
        self.segments_table.setColumnHidden(5, not counts["failed"])
        self.result_hint.setText("双击失败片段，查看具体原因、修复建议和原始异常。" if counts["failed"] else
            "仅显示当前对白框的音频 · 上一个 / 下一个切换本框历史版本" if self.editor_mode == "dialogue" else
            "点击片段播放 · 上一个 / 下一个切换 · 每次生成保留在当前作品")
        for row, item in enumerate(self.visible_segments):
            color = "#ad5427" if item["status"] == "failed" else "#3c7a51" if item["status"] == "done" else "#88888e"
            cell = self.segments_table.item(row, 4)
            if cell.foreground().color() != QColor(color):
                cell.setForeground(QColor(color))
        self.preview_segment()
