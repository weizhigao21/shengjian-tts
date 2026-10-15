"""DocumentController responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QInputDialog, QMenu, QPushButton, QTableWidgetItem
from ...subtitles import parse_text
from ...naming import display_title, work_type
from datetime import datetime
from ...ui import icon
from ..api_client import checked
from ..constants import STATES
from ..model_labels import model_key


class DocumentController:
    def load_saved_jobs(self):
        from ...store import Store
        try:
            self.render_jobs(Store.read_recent(self.data_dir))
        except Exception as exc:
            self.service_log.append("启动时读取作品记录失败，服务连接后将重试：" + str(exc))

    @staticmethod
    def update_table_cell(table, row, col, text, tooltip=None, color=None):
        cell = table.item(row, col)
        if cell is None:
            cell = QTableWidgetItem(text)
            table.setItem(row, col, cell)
        elif cell.text() != text:
            cell.setText(text)
        tooltip = text if tooltip is None else tooltip
        if cell.toolTip() != tooltip:
            cell.setToolTip(tooltip)
        if color is not None and cell.foreground().color() != color:
            cell.setForeground(color)
        return cell

    def render_jobs(self, jobs):
        jobs = [j for j in jobs if j["id"] not in self.deleted_job_ids]
        self.jobs_empty.setVisible(not jobs)
        self.jobs_table.blockSignals(True)
        if jobs != self.jobs:
            self.jobs_table.setUpdatesEnabled(False)
            self.jobs_table.setRowCount(len(jobs))
            for row, value in enumerate(jobs):
                category = "对话" if work_type(value) == "dialogue" else "文章"
                stamp = datetime.fromtimestamp(value["created"]).strftime("%Y-%m-%d %H:%M:%S")
                tooltip = f"{category} · {display_title(value)}\n创建时间：{stamp}\n{STATES[value['status']]} · {value['completed']}/{value['total']}"
                for col, text in enumerate((display_title(value), STATES[value["status"]], f"{value['completed']}/{value['total']}")):
                    cell = self.update_table_cell(self.jobs_table, row, col, text, tooltip)
                    if cell.data(256) != value["id"]:
                        cell.setData(256, value["id"])
                    if cell.data(257) != value:
                        cell.setData(257, value)
            self.jobs = jobs
            self.jobs_table.setUpdatesEnabled(True)
        selected_row = next((row for row, value in enumerate(jobs) if value["id"] == self.selected_job), -1)
        if self.jobs_table.currentRow() != selected_row:
            if selected_row >= 0:
                self.jobs_table.selectRow(selected_row)
            else:
                self.jobs_table.clearSelection()
                self.jobs_table.setCurrentCell(-1, -1)
        self.jobs_table.blockSignals(False)

    def select_job(self):
        if self.submitting:
            return
        row = self.jobs_table.currentRow()
        if row >= 0 and self.jobs_table.item(row, 0):
            selected = self.jobs_table.item(row, 0).data(256)
            if selected != self.selected_job:
                self.save_document()
                self.document_dirty = False
                self.clear_autoplay()
                self.job_data = None
                self.segments_table.setRowCount(0)
                self.segments_table.setCurrentCell(-1, -1)
                self.auto_select_audio = True
                self.clip_list.hide()
                self.preview_segment()
            self.selected_job = selected
            self.set_page(0)
            self.call(lambda c: checked(c.get(f"/jobs/{selected}")), self.open_job_data)

    def open_job_data(self, job):
        from ..model_labels import model_key
        if job["id"] != self.selected_job:
            return
        self.capture_document_save()
        pending = self.draft_saves.pending.get(job["id"])
        if pending:
            job = {**job, **pending.body, "display_title": pending.body["title"]}
        self.loading_document = True
        self.title_edit.setText(display_title(job))
        self.text_edit.setPlainText(job.get("script") if job.get("script") is not None else
            "\n".join((f"[{s['timestamp']}]" if s["timestamp"] else "") + s["text"] for s in job["segments"]))
        self.text_edit.restore_mentions(job.get("script_mentions"))
        self.load_editor_state(job)
        self.document_dirty = bool(pending)
        base = next((v for v in self.voices if v["id"] == job["voice"].get("id") and model_key(v) == model_key(job["voice"])), None)
        base = base or next((v for v in self.voices if job["voice"] and model_key(v) == model_key(job["voice"])), None)
        if base:
            self.refresh_generation_models(base["id"])
            self.active_voice_snapshot = {**job["voice"], "model_name": "job:" + job["id"]}
            provenance = (job["segments"][-1].get("provenance") or {}) if job["segments"] else {}
            if base["id"] != job["voice"].get("id") or provenance.get("preset_used") is False:
                self.voice_combo.blockSignals(True)
                self.voice_combo.setCurrentIndex(0)
                self.voice_combo.blockSignals(False)
            self.update_voice_summary(reapply=True)
        self.loading_document = False
        if self.editor_mode == "dialogue":
            self.select_dialogue_entry(self.dialogue_editor.selected_id)
        self.render_job(job)
        if pending:
            self.document_timer.start(650)

    def job_menu(self, row):
        from ...ui import icon
        item = self.jobs_table.item(row, 0)
        if not item:
            return None
        value = item.data(257)
        menu = QMenu(self)
        def open_task():
            self.jobs_table.blockSignals(True)
            self.jobs_table.selectRow(row)
            self.jobs_table.blockSignals(False)
            self.select_job()
        menu.addAction(icon("pen"), "打开任务", open_task)
        menu.addAction(icon("pen"), "重命名", lambda: self.rename_job(value))
        menu.addSeparator()
        action = menu.addAction(icon("trash", "#b24932"), "取消并删除任务" if value["status"] in ("running", "queued") else "删除任务",
                               lambda: self.delete_job(value["id"]))
        action.setObjectName("deleteJob")
        return menu

    def job_context_menu(self, position):
        menu = self.job_menu(self.jobs_table.rowAt(position.y()))
        if menu:
            menu.exec(self.jobs_table.viewport().mapToGlobal(position))

    def delete_job(self, job_id):
        self.call(lambda c: checked(c.delete(f"/jobs/{job_id}")), lambda r: self.job_deleted(job_id))

    def job_deleted(self, job_id):
        self.draft_saves.discard(job_id)
        self.deleted_job_ids.add(job_id)
        if self.selected_job == job_id:
            self.clear_autoplay()
            self.selected_job = self.job_data = None
            self.active_voice_snapshot = None
            self.segments_table.setRowCount(0)
            self.segments_table.setCurrentCell(-1, -1)
            self.clip_list.hide()
            self.job_summary.setText("还没有生成记录")
            self.job_progress.setValue(0)
            self.job_progress.hide()
            for control in (self.retry_button, self.cancel_button, self.export_button):
                control.setEnabled(False)
            self.preview_segment()
        self.statusBar().showMessage("任务已删除，可撤销。音频与已导出文件保留。", 10000)
        self.show_undo(lambda: self.restore_job(job_id))
        self.poll()

    def show_undo(self, action):
        if not hasattr(self, "undo_button"):
            self.undo_button = QPushButton("撤销删除")
            self.toast_layout.addWidget(self.undo_button)
            self.undo_timer = QTimer(self)
            self.undo_timer.setSingleShot(True)
            self.undo_timer.timeout.connect(self.undo_button.hide)
        else:
            try:
                self.undo_button.clicked.disconnect()
            except TypeError:
                pass
        self.undo_button.clicked.connect(action)
        self.undo_button.show()
        self.undo_timer.start(10000)
        self.show_toast(self.statusBar().currentMessage())

    def restore_job(self, job_id):
        def restored(value):
            self.deleted_job_ids.discard(job_id)
            self.undo_button.hide()
            self.statusBar().showMessage("任务已恢复；已取消的片段可手动重试。", 6000)
            self.poll()
        self.call(lambda c: checked(c.post(f"/jobs/{job_id}/restore")), restored)

    def rename_job(self, job):
        name, accepted = QInputDialog.getText(self, "重命名任务", "任务名称", text=job["title"])
        if accepted and name.strip():
            self.call(lambda c: checked(c.patch(f"/jobs/{job['id']}", json={"title": name.strip()})), lambda r: self.poll())

    def document_changed(self, *_):
        if self.loading_document:
            return
        self.document_dirty = True
        if self.selected_job and self.ready:
            self.document_timer.start(650)

    def save_document(self):
        self.document_timer.stop()
        self.capture_document_save()
        if not self.draft_saves.pending or not self.ready or self.document_save_running or self.submitting:
            return
        snapshot = next(iter(self.draft_saves.pending.values()))
        target, body = snapshot.task_id, snapshot.body
        self.document_save_running = True
        def finished(_=None):
            self.capture_document_save()
            self.draft_saves.acknowledge(snapshot)
            if self.selected_job == target and self.document_payload() == body:
                self.document_dirty = False
            if isinstance(_, dict) and _.get("title") and self.selected_job == target and self.title_edit.text().strip() == body["title"]:
                self.title_edit.blockSignals(True)
                self.title_edit.setText(_.get("display_title", _["title"]))
                self.title_edit.blockSignals(False)
            self.document_save_running = False
            callbacks, self.after_save = self.after_save, []
            for callback in callbacks:
                callback()
            if not self.submitting:
                self.save_document()
        def failed(message):
            self.draft_saves.retry_later(snapshot)
            if self.selected_job == target:
                self.document_dirty = True
            self.statusBar().showMessage("草稿保存失败：" + message, 8000)
            self.document_save_running = False
            callbacks, self.after_save = self.after_save, []
            for callback in callbacks:
                callback()
            self.document_timer.start(5000)
        self.call(lambda c: checked(c.patch(f"/jobs/{target}/document", json=body)), finished, failed)

    def capture_document_save(self):
        if self.document_dirty and self.selected_job and not self.loading_document:
            snapshot = self.draft_saves.stage(self.selected_job, self.document_payload())
            if self.draft_saves.write_error:
                self.statusBar().showMessage("本地草稿暂存失败：" + self.draft_saves.write_error, 8000)
            return snapshot

    def set_page(self, index):
        self.tabs.setCurrentIndex(index)

    def sync_navigation(self, index):
        self.transport.setVisible(index != 0 or self.creation_pages.currentIndex() not in (2, 3))
        if index != 0:
            self.audio_editor.controller.save()
            self.audio_editor.controller.stop()
            self.recognition.controller.pause()
        names = ("语音创作", "声音库", "工作流联动", "设置")
        self.page_title.setText("工作台  /  " + names[index])
        for i, control in enumerate(self.nav_buttons):
            control.setChecked(index == i)
        self.workspace_header.setVisible(index != 0)
        if index == 3:
            self.clip_list.hide()
            self.settings.refresh()
        else:
            self.settings.player.pause()

    def new_document(self):
        if getattr(self, "submitting", False):
            return
        self.save_document()
        self.clear_autoplay()
        self.set_page(0)
        self.selected_job = self.job_data = None
        self.active_voice_snapshot = None
        self.jobs_table.blockSignals(True)
        self.jobs_table.clearSelection()
        self.jobs_table.blockSignals(False)
        self.segments_table.setRowCount(0)
        self.segments_table.setCurrentCell(-1, -1)
        self.clip_list.hide()
        self.auto_select_audio = True
        self.job_summary.setText("还没有生成记录")
        self.job_progress.setValue(0)
        self.job_progress.hide()
        self.preview_segment()
        for control in (self.retry_button, self.cancel_button, self.export_button):
            control.setEnabled(False)
        self.loading_document = True
        self.title_edit.setText("未命名配音")
        self.text_edit.clear()
        self.dialogue_editor.load([])
        self.editor_mode = "article"
        self.document_dirty = False
        self.loading_document = False
        self.open_creation_home()

    def update_script_stats(self):
        from ...subtitles import parse_text
        text = self.document_text()
        try:
            segments = self.document_segments() if text.strip() else []
            count = len(segments)
            self.script_stats.setText(f"{len(text.strip())} 字  ·  {count} 个片段")
            self.script_stats.setToolTip("")
        except ValueError as exc:
            self.script_stats.setText(f"{len(text.strip())} 字  ·  请检查音色或文本")
            self.script_stats.setToolTip(str(exc))
        self.update_generation_actions()
