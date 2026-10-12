from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime

import httpx
from PyQt6.QtCore import QLibraryInfo, QLocale, QThread, QTimer, QTranslator, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QInputDialog, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QTableWidgetItem, QTextEdit, QVBoxLayout)

from .config import PROJECT_ROOT
from . import __version__
from .ui import application_icon, generation_elapsed, sorted_audio
from .subtitles import parse_text, normalize_timestamp
from .runner import validate_wave
from .runtime import service_command
from .service_status import startup_failure
from .gui_layout import UiLayout

STATES = {"draft": "草稿", "queued": "排队中", "running": "生成中", "done": "已完成", "failed": "失败", "cancelled": "已取消"}
BACKENDS = {"audio_cpp": "audio.cpp 统一引擎", "windows_sapi": "Windows 系统声音（基础验证）", "index_tts": "官方 Python 环境（备用）"}


class ApiWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, url, action):
        super().__init__()
        self.url, self.action = url, action

    def run(self):
        try:
            with httpx.Client(base_url=self.url, timeout=5, trust_env=False) as client:
                self.result.emit(self.action(client))
        except Exception as exc:
            self.error.emit(str(exc))


def checked(response):
    if response.is_error:
        try:
            body = response.json()
            error = body.get("error", {})
            details = "\n".join(f"{item['field']}: {item['message']}" for item in body.get("errors", []))
            raise RuntimeError(body.get("msg", response.text) + ("\n" + error["hint"] if error.get("hint") else "") + ("\n" + details if details else ""))
        except ValueError:
            response.raise_for_status()
    return response.json()


class Window(UiLayout, QMainWindow):
    def __init__(self, port, data_dir, autostart=True):
        super().__init__()
        self.port, self.data_dir = port, Path(data_dir).resolve()
        self.url = f"http://127.0.0.1:{port}"
        self.workers = set()
        self.closing = False
        self.process = None
        self.process_log = None
        self.log_offset = 0
        self.service_output_text = ""
        self.connecting_service = False
        self.service_launch_error = None
        self.owns_service = False
        self.voices = []
        self.references = []
        self.jobs = []
        self.dialogs = set()
        self.deleted_job_ids = set()
        self.active_voice_snapshot = None
        self.generation_base_id = None
        self.pending_autoplay = None
        self.autoplay_indices = None
        self.selected_job = None
        self.job_data = None
        self.visible_segments = []
        self.polling = False
        self.ready = False
        self.submitting = False
        self.loading_document = False
        self.document_dirty = False
        self.document_save_running = False
        self.after_save = []
        self.auto_select_audio = True
        self.document_timer = QTimer(self)
        self.document_timer.setSingleShot(True)
        self.document_timer.timeout.connect(self.save_document)
        self.setWindowTitle(f"声笺 v{__version__} · 本地配音工作台")
        self.setWindowIcon(application_icon())
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.reference_output = QAudioOutput(self)
        self.reference_player = QMediaPlayer(self)
        self.reference_player.setAudioOutput(self.reference_output)
        self._build()
        self.load_saved_jobs()
        self.reference_player.playbackStateChanged.connect(self.update_reference_icon)
        self.reference_player.errorOccurred.connect(lambda error, message: self.show_error("参考音频播放失败：" + message))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1800)
        if autostart:
            self.reconnect_service()

    def call(self, action, success=None, failure=None):
        worker = ApiWorker(self.url, action)
        self.workers.add(worker)
        def guarded(callback):
            def deliver(value):
                from PyQt6 import sip
                if not sip.isdeleted(self) and not self.closing:
                    callback(value)
            return deliver
        if success:
            worker.result.connect(guarded(success))
        worker.error.connect(guarded(failure or self.show_error))
        worker.finished.connect(lambda: self.workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def show_error(self, message):
        QMessageBox.warning(self, "操作未完成", str(message))

    def _existing_service(self, value):
        self.connecting_service = False
        self.reconnect_button.setEnabled(True)
        if value.get("service") != "local-voice-workbench":
            self.show_error(f"端口 {self.port} 被其他程序占用，请换一个端口启动。")
            return
        if Path(value["data_dir"]).resolve() != self.data_dir:
            self.show_error("该端口上的服务使用另一个数据目录。请用对应 --data-dir 启动界面。")
            return
        if not {"reference_library", "generation_options", "work_generations", "asset_management", "voice_discovery", "engine_verification", "workflow_record_only", "engine_updates", "model_updates"}.issubset(value.get("capabilities", [])):
            self.show_error("本地服务仍是旧版本。请关闭旧程序及独立运行的语音服务，再重新启动。")
            return
        self.ready = True
        self.poll()

    def reconnect_service(self):
        if self.connecting_service or self.closing:
            return
        if self.owns_service and self.process and self.process.poll() is None:
            self.poll()  # A running service must not be duplicated or interrupted.
            return
        if self.process and self.process.poll() is not None:
            self.process = None
            self.owns_service = False
        self.connecting_service = True
        self.service_launch_error = None
        self.reconnect_button.setEnabled(False)
        self.set_service_state("正在连接本地服务…", "connecting")
        self.call(lambda c: checked(c.get("/health")), self._existing_service, self._start_service)

    def _start_service(self, message):
        try:
            if self.process and self.process.poll() is None:
                return
            self.process = None
            self.owns_service = False
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "logs").mkdir(exist_ok=True)
            if self.process_log:
                self.process_log.close()
            self.process_log = (self.data_dir / "logs" / "service.log").open("ab")
            self.process_log.seek(0, os.SEEK_END)
            self.log_offset = self.process_log.tell()
            self.service_output_text = ""
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYINSTALLER_RESET_ENVIRONMENT="1")
            self.process = subprocess.Popen(service_command(self.port, self.data_dir),
                cwd=PROJECT_ROOT, stdout=self.process_log, stderr=subprocess.STDOUT, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.owns_service = True
            self.set_service_state("服务启动中…", "connecting")
            QTimer.singleShot(200, self.poll)
        except Exception as exc:
            self.service_launch_error = str(exc)
            self.service_log.append("启动进程失败：" + str(exc))
            self.set_service_state("启动失败 · 查看服务日志", "failed")
            self.banner.setToolTip(str(exc))
            if self.process_log:
                self.process_log.close()
        finally:
            self.connecting_service = False
            self.reconnect_button.setEnabled(True)

    def _service_output(self):
        path = self.data_dir / "logs" / "service.log"
        if self.owns_service and path.is_file():
            with path.open("rb") as stream:
                stream.seek(self.log_offset)
                content = stream.read(65536)
                self.log_offset = stream.tell()
            if content:
                output = content.decode("utf-8", errors="replace")
                self.service_output_text = (self.service_output_text + output)[-32768:]
                self.service_log.append(output.strip())

    def _service_failed(self):
        self.ready = False
        text, detail = startup_failure(self.service_output_text, self.port, self.process.returncode)
        self.set_service_state(text, "failed")
        self.banner.setToolTip(f"{text}\n{detail}\n{self.url}\n工作流联动 → 服务日志")

    def poll(self):
        self._service_output()
        if self.service_launch_error and not self.connecting_service:
            self.ready = False
            self.set_service_state("启动失败 · 查看服务日志", "failed")
            self.banner.setToolTip(self.service_launch_error)
            return
        if self.owns_service and self.process and self.process.poll() is not None:
            self._service_failed()
            return
        if self.polling:
            return
        self.polling = True
        selected = self.selected_job
        def fetch(client):
            health = checked(client.get("/health"))
            if health.get("service") != "local-voice-workbench" or Path(health["data_dir"]).resolve() != self.data_dir:
                raise RuntimeError("端口上的服务与当前项目不匹配")
            if not {"generation_options", "work_generations", "asset_management"}.issubset(health.get("capabilities", [])):
                raise RuntimeError("语音服务版本过旧，请关闭旧窗口及独立运行的旧服务后重新启动。")
            response = client.get(f"/jobs/{selected}") if selected else None
            job = checked(response) if response and response.status_code != 404 else None
            return health, checked(client.get("/voices")), checked(client.get("/jobs")), job, checked(client.get("/references"))
        def failed(message):
            self.polling = False
            if self.owns_service and self.process and self.process.poll() is not None:
                self._service_output()
                self._service_failed()
                return
            self.ready = False
            self.set_service_state("未连接 · 查看服务日志", "failed")
            self.banner.setToolTip(str(message))
            if getattr(self, "last_connection_error", None) != str(message):
                self.service_log.append("连接服务失败：" + str(message))
                self.last_connection_error = str(message)
        self.call(fetch, self.render, failed)

    def render(self, values):
        self.polling = False
        self.ready = True
        self.last_connection_error = None
        health, voices, jobs, job = values[:4]
        self.references = values[4] if len(values) > 4 else [{"id": v["id"], "name": v["name"],
            "reference_audio": v["reference_audio"], "reference_text": v.get("reference_text", ""), "duration": 0}
            for v in voices if v.get("reference_audio")]
        # Work history must not wait for model/library widgets to bind.
        self.render_jobs(jobs)
        current = health.get("current")
        self.set_service_state(f"●  正在生成第 {current['index']} 段" if current else
                               f"●  本地服务在线 · 队列 {health['queue']}" if health['queue'] else "●  本地服务在线", "ready")
        self.banner.setToolTip(f"{self.banner.text()}\n{self.url} · 队列 {health['queue']} 个任务")
        voice_ids = [(v["id"], v["name"], v.get("model_name")) for v in voices]
        old_ids = [(v["id"], v["name"], v.get("model_name")) for v in self.voices]
        self.voices = voices
        if voice_ids != old_ids:
            preferred = getattr(self, "pending_voice_id", None)
            self.pending_voice_id = None
            self.refresh_generation_models(preferred)
            self.update_link_config()
        self.model_controls.set_references(self.references)
        self.model_controls.update_engine_download(health.get("engine", {}))
        self.library.bind()
        self.update_voice_summary()
        if job and job["id"] == self.selected_job:
            self.render_job(job)

    def load_saved_jobs(self):
        from .store import Store
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
                tooltip = f"{value['title']}\n{STATES[value['status']]} · {value['completed']}/{value['total']}"
                for col, text in enumerate((value["title"], STATES[value["status"]], f"{value['completed']}/{value['total']}")):
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
        from .gui_layout import model_key
        if job["id"] != self.selected_job:
            return
        self.loading_document = True
        self.title_edit.setText(job["title"])
        self.text_edit.setPlainText(job.get("script") if job.get("script") is not None else
            "\n".join((f"[{s['timestamp']}]" if s["timestamp"] else "") + s["text"] for s in job["segments"]))
        self.loading_document = False
        self.document_dirty = False
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
        self.render_job(job)

    def job_menu(self, row):
        from .ui import icon
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

    def render_job(self, job, force=False):
        if job["id"] != self.selected_job:
            return
        if not force and job == self.job_data and self.segments_table.rowCount() == len(job["segments"]):
            if self.auto_select_audio and self.playable_rows():
                self.segments_table.selectRow(self.playable_rows()[0])
                self.auto_select_audio = False
            self.preview_segment()
            self.maybe_autoplay(job)
            return
        selected_idx = (self.current_segment() or {}).get("idx")
        self.job_data = job
        self.visible_segments = sorted_audio(job["segments"], self.clip_sort.currentData())
        selected_row = next((row for row, s in enumerate(self.visible_segments) if s["idx"] == selected_idx), -1)
        self.segments_table.blockSignals(True)
        self.segments_table.setUpdatesEnabled(False)
        if self.segments_table.rowCount() != len(job["segments"]):
            self.segments_table.setRowCount(len(job["segments"]))
        for row, item in enumerate(self.visible_segments):
            error = item["error"] or {}
            status = STATES[item["status"]] + (" · 缓存" if item["cached"] else "")
            created = item.get("created") or job.get("created")
            stamp = datetime.fromtimestamp(created).strftime("%m-%d %H:%M:%S") if created else item.get("timestamp", "")
            name = f"{item.get('batch_title') or job['title']} · 第 {item.get('generation', 1)} 次 · {item['text']}"
            elapsed, elapsed_tip = generation_elapsed(item)
            for col, text in enumerate((str(item["idx"]), stamp, name, elapsed, status, error.get("message", ""))):
                tooltip = ("生成时间：" + stamp + ("\n字幕时间：" + item["timestamp"] if item["timestamp"] else "")) if col == 1 else text
                if col == 3:
                    tooltip = elapsed_tip
                cell = self.update_table_cell(self.segments_table, row, col, text, tooltip)
                if col in (0, 1, 3) and cell.textAlignment() != Qt.AlignmentFlag.AlignCenter:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if 0 <= selected_row < len(job["segments"]) and self.segments_table.currentRow() != selected_row:
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

    def import_text(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入字幕或文本", "", "字幕与文本 (*.txt *.lrc *.srt *.vtt);;所有文件 (*)")
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("gb18030")
            parse_text(content)
            self.text_edit.setPlainText(content)
            self.title_edit.setText(Path(filename).stem)
        except Exception as exc:
            self.show_error(str(exc))

    def submit(self, segments, title):
        if self.submitting:
            return
        voice = self.current_voice()
        if voice is None:
            self.show_error("请等待服务连接并选择声音。")
            return
        self.document_timer.stop()
        self.document_dirty = False
        use_preset = self.selected_preset() is not None
        body = {"voice_id": voice["model_name"] if use_preset else voice["id"], "use_preset": use_preset,
                "title": title, "segments": segments,
                "script": self.text_edit.toPlainText(),
                "overrides": self.model_controls.overrides()}
        self.submission_title = title
        target = self.selected_job
        self.submitting = True
        self.update_generation_actions()
        def send():
            self.call(lambda c: checked(c.post(f"/jobs/{target}/generate" if target else "/jobs", json=body)), self.submitted, self.submission_failed)
        if self.document_save_running:
            self.after_save.append(send)
        else:
            send()

    def submitted(self, result):
        self.submitting = False
        if self.selected_job != result["job_id"]:
            self.job_data = None
            self.segments_table.setRowCount(0)
            self.segments_table.setCurrentCell(-1, -1)
            self.auto_select_audio = True
            self.preview_segment()
        self.selected_job = result["job_id"]
        indices = result.get("segment_indices", [])
        self.pending_autoplay = {"job_id": result["job_id"], "indices": set(indices)} if indices else None
        if result.get("title") and self.title_edit.text() == getattr(self, "submission_title", None):
            self.title_edit.blockSignals(True)
            self.title_edit.setText(result["title"])
            self.title_edit.blockSignals(False)
        self.update_generation_actions()
        if self.document_dirty:
            self.document_changed()
        self.statusBar().showMessage("已加入当前作品的生成列表，完成后自动保存并播放本次音频。", 5000)
        self.poll()

    def submission_failed(self, message):
        self.submitting = False
        self.update_generation_actions()
        self.show_error(message)

    def document_changed(self, *_):
        if self.loading_document:
            return
        self.document_dirty = True
        if self.selected_job and self.ready:
            self.document_timer.start(650)

    def save_document(self):
        self.document_timer.stop()
        if not self.document_dirty or not self.selected_job or not self.ready or self.document_save_running or self.submitting:
            return
        target = self.selected_job
        body = {"title": self.title_edit.text().strip() or "未命名配音", "script": self.text_edit.toPlainText()}
        self.document_dirty = False
        self.document_save_running = True
        def finished(_=None):
            if isinstance(_, dict) and _.get("title") and self.selected_job == target and self.title_edit.text().strip() == body["title"]:
                self.title_edit.blockSignals(True)
                self.title_edit.setText(_["title"])
                self.title_edit.blockSignals(False)
            self.document_save_running = False
            callbacks, self.after_save = self.after_save, []
            for callback in callbacks:
                callback()
            if self.document_dirty and not self.submitting:
                self.save_document()
        def failed(message):
            if self.selected_job == target:
                self.document_dirty = True
            self.statusBar().showMessage("草稿保存失败：" + message, 8000)
            self.document_save_running = False
            callbacks, self.after_save = self.after_save, []
            for callback in callbacks:
                callback()
        self.call(lambda c: checked(c.patch(f"/jobs/{target}/document", json=body)), finished, failed)

    def generate(self):
        try:
            self.submit(parse_text(self.text_edit.toPlainText()), self.title_edit.text() or "配音任务")
        except Exception as exc:
            self.show_error(str(exc))

    def warmup(self):
        self.submit([{"text": "你好，这是本地配音工作台的语音测试。"}], self.title_edit.text() or "未命名配音")

    def retry(self):
        if self.selected_job:
            self.call(lambda c: checked(c.post(f"/jobs/{self.selected_job}/retry")), self.render_job)

    def cancel(self):
        if self.selected_job:
            self.call(lambda c: checked(c.post(f"/jobs/{self.selected_job}/cancel")), self.render_job)

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

    def stop_reference(self):
        self.reference_player.stop()

    def update_reference_icon(self, state):
        from .ui import icon
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.model_controls.reference_play.setToolTip("停止试听" if playing else "试听参考声音")
        self.model_controls.reference_play.setIcon(icon("stop" if playing else "play"))
        path = self.reference_player.source().toLocalFile()
        for ticket in self.library.tickets:
            ticket.cover.set_playing(playing and bool(path) and Path(ticket.item.get("reference_audio") or ".").resolve() == Path(path).resolve())

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "dialog_shade"):
            self.dialog_shade.setGeometry(self.rect())
        if hasattr(self, "clip_list"):
            self.clip_list.hide()
        if hasattr(self, "toast") and self.toast.isVisible():
            self.show_toast(self.statusBar().currentMessage())

    def choose_reference(self, controls):
        from .library import ReferencePicker
        self.open_dialog(ReferencePicker(self, controls))

    def import_reference(self, *, selected=None, source=None):
        from .library import ReferenceDialog
        return self.open_dialog(ReferenceDialog(self, selected=selected, source=source))

    def edit_reference(self, item):
        from .library import ReferenceDialog
        return self.open_dialog(ReferenceDialog(self, item))

    def edit_library_voice(self, voice=None):
        from .library import PresetDialog
        return self.open_dialog(PresetDialog(self, voice))

    def use_library_voice(self, voice):
        self.active_voice_snapshot = None
        self.refresh_generation_models(voice["id"])
        self.set_page(0)
        self.text_edit.setFocus()

    def delete_library_voice(self, voice):
        self.call(lambda c: checked(c.delete(f"/voices/{voice['id']}")), lambda r: self.library_deleted("voices", voice))

    def delete_reference(self, item):
        self.stop_reference()
        self.call(lambda c: checked(c.delete(f"/references/{item['id']}")), lambda r: self.library_deleted("references", item))

    def library_deleted(self, kind, item):
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

    def preview_reference(self, path):
        self.settings.player.pause()
        if self.reference_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.stop_reference()
            return
        try:
            validate_wave(Path(path))
            self.player.stop()
            self.reference_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self.reference_player.play()
        except Exception as exc:
            self.show_error(str(exc))

    def save_generation_voice(self):
        import uuid
        from .schemas import VoiceConfig
        from .runner import check_voice
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

    def details(self):
        item = self.current_segment()
        if item is None:
            return
        error = item["error"]
        if error:
            self.call(lambda c: c.get(error["log"]).text, lambda text: self.show_details(text))
        else:
            self.show_details(json.dumps(item, ensure_ascii=False, indent=2))

    def show_details(self, text):
        dialog = QDialog(self)
        dialog.setWindowTitle("片段详情与原始异常")
        dialog.resize(850, 650)
        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)
        button = QPushButton("复制详情")
        button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        layout.addWidget(button)
        dialog.exec()

    def export(self):
        if not self.job_data:
            return
        current = self.current_segment()
        generation = (current or self.job_data["segments"][-1]).get("generation", 1) if self.job_data["segments"] else 1
        items = [s for s in self.job_data["segments"] if s.get("generation", 1) == generation and s["status"] == "done"]
        if not items:
            self.show_error("当前生成批次没有已完成片段。")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not folder:
            return
        target = Path(folder) / self.job_data["id"] / f"生成-{generation:03d}"
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
                                     "generation": generation, "created": item.get("created"), "voice": item.get("voice", self.job_data["voice"])})
                    cursor_ms += round(validate_wave(self.data_dir / "audio" / item["audio"])["duration"] * 1000)
            (target / "segments.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self.statusBar().showMessage(f"已导出 {len(manifest)} 个片段：{target}", 8000)
        except Exception as exc:
            self.show_error(str(exc))

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

    def closeEvent(self, event):
        self.closing = True
        self.clear_autoplay()
        self.timer.stop()
        self.document_timer.stop()
        self.clip_list.hide()
        self.settings.close_management()
        self.player.stop()
        self.player.setSource(QUrl())
        self.reference_player.stop()
        self.reference_player.setSource(QUrl())
        for worker in list(self.workers):
            worker.blockSignals(True)
            worker.wait(30000)
            worker.deleteLater()
        self.workers.clear()
        if self.document_dirty and self.selected_job:
            try:
                with httpx.Client(base_url=self.url, trust_env=False, timeout=2) as client:
                    checked(client.patch(f"/jobs/{self.selected_job}/document", json={
                        "title": self.title_edit.text().strip() or "未命名配音", "script": self.text_edit.toPlainText()}))
            except Exception as exc:
                self.service_log.append("退出时草稿保存失败：" + str(exc))
        if self.owns_service and self.process and self.process.poll() is None:
            try:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    client.post(self.url + "/shutdown")
            except httpx.HTTPError:
                pass
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process_log:
            self.process_log.close()
        event.accept()


def configure_font(app):
    # 显式加载系统中文字体，也支持离屏布局检查。
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))


def configure_language(app):
    """Localize Qt's standard actions and keep the translator alive."""
    QLocale.setDefault(QLocale("zh_CN"))
    if getattr(app, "_chinese_translator", None) is not None:
        return
    translator = QTranslator(app)
    translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load("qtbase_zh_CN", translations):
        raise RuntimeError("缺少 Qt 简体中文资源 qtbase_zh_CN.qm，请保留完整的 _internal 文件夹。")
    app.installTranslator(translator)
    app._chinese_translator = translator


def launch(port, data_dir):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Shengjian.VoiceWorkbench")
    app = QApplication(sys.argv)
    configure_language(app)
    app.setApplicationName("声笺")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Shengjian")
    app.setWindowIcon(application_icon())
    app.setStyle("Fusion")
    configure_font(app)
    window = Window(port, data_dir)
    window.show()
    raise SystemExit(app.exec())
