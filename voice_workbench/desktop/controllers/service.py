"""ServiceController responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from PyQt6.QtCore import QTimer
from ...config import PROJECT_ROOT
from ...runtime import service_command
from ...service_status import startup_failure
from ..api_client import checked


class ServiceController:
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
            if not {"generation_options", "work_generations", "asset_management", "dialogue_editor", "dialogue_audio_scope", "fixed_creation_type"}.issubset(health.get("capabilities", [])):
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
        was_ready = self.ready
        self.ready = True
        self.last_connection_error = None
        health, voices, jobs, job = values[:4]
        self.references = values[4] if len(values) > 4 else [{"id": v["id"], "name": v["name"],
            "reference_audio": v["reference_audio"], "reference_text": v.get("reference_text", ""), "duration": 0}
            for v in voices if v.get("reference_audio")]
        # Work history must not wait for model/library widgets to bind.
        self.render_jobs(jobs)
        current = health.get("current")
        self.set_service_state("●  正在处理编辑音频" if health.get("audio_editing") else f"●  正在生成第 {current['index']} 段" if current else
                               f"●  本地服务在线 · 队列 {health['queue']}" if health['queue'] else "●  本地服务在线", "ready")
        self.banner.setToolTip(f"{self.banner.text()}\n{self.url} · 队列 {health['queue']} 个任务")
        voice_ids = [(v["id"], v["name"], v.get("model_name")) for v in voices]
        old_ids = [(v["id"], v["name"], v.get("model_name")) for v in self.voices]
        self.voices = voices
        self.text_edit.set_voices(voices)
        self.dialogue_editor.set_voices(voices)
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
        if not was_ready:
            self.save_document()

    def set_service_state(self, text, state):
        self.banner.setText(text)
        self.banner.setToolTip(f"{text}\n{self.url}")
        self.banner.setProperty("state", state)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.update_generation_actions()
