"""「边剪边配」模式：主窗口让位，小窗接管打字、生成、拖出。"""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer

from ..api_client import checked
from ..audio_output import SystemAudioOutput
from ..components.float_panel import FloatPanel
from ..hotkey import DEFAULT_LABEL, GlobalHotkey


class FloatModeController:
    def float_setup(self):
        """Called from Window.__init__ before the layout builds."""
        self.float_panel = None
        self.float_job_id = None
        self.float_polling = False
        self.float_submitting = False
        self.float_player = QMediaPlayer(self)
        self.float_output = SystemAudioOutput(self)
        self.float_player.setAudioOutput(self.float_output)
        self.float_timer = QTimer(self)
        self.float_timer.setInterval(1200)
        self.float_timer.timeout.connect(self.float_poll)
        self.float_hotkey = GlobalHotkey(self.toggle_float_mode)
        self.float_hotkey_ready = False

    # ---------- window ----------

    def ensure_float_panel(self):
        if self.float_panel is not None:
            return self.float_panel
        panel = FloatPanel(self.data_dir / "float-panel.json")
        panel.audio_root = self.data_dir / "audio"
        panel.generate_requested.connect(self.float_generate)
        panel.retry_requested.connect(self.float_retry)
        panel.new_session_requested.connect(self.float_new_session)
        panel.play_requested.connect(self.float_play)
        panel.copy_requested.connect(self.float_copy)
        panel.restore_requested.connect(lambda: self.close_float_mode(restore=True))
        panel.closed.connect(lambda: self.close_float_mode(restore=True))
        if self.voices:
            panel.set_voices(self.voices)
        self.float_panel = panel
        return panel

    def open_float_mode(self, minimize=True):
        if self.closing:
            return
        panel = self.ensure_float_panel()
        panel.show_error("")
        panel.present()
        self.float_timer.start()
        if minimize and self.isVisible() and not self.isMinimized():
            # An independent top-level window stays put; a child would minimize with its parent.
            self.showMinimized()
        self.float_poll()

    def close_float_mode(self, restore=True):
        if self.float_panel is not None:
            self.float_panel.save_state()
            self.float_panel.hide()
        self.float_timer.stop()
        if restore and not self.closing:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def toggle_float_mode(self):
        if self.float_panel is not None and self.float_panel.isVisible():
            self.close_float_mode(restore=True)
        else:
            self.open_float_mode()

    def register_float_hotkey(self):
        import os
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return False
        self.float_hotkey_ready = self.float_hotkey.register()
        return self.float_hotkey_ready

    def float_hotkey_hint(self):
        return DEFAULT_LABEL if self.float_hotkey_ready else "热键被占用"

    def sync_float_voices(self, voices):
        if self.float_panel is not None:
            self.float_panel.set_voices(voices)

    # ---------- generation ----------

    def float_generate(self):
        panel = self.ensure_float_panel()
        if self.float_submitting:
            return
        if not self.ready:
            panel.show_error("本地服务还没连上，等状态变成在线再试")
            return
        lines = panel.segments()
        if not lines:
            panel.show_error("先写一句台词")
            return
        if any(len(line) > 5000 for line in lines):
            panel.show_error("单行超过 5000 字，拆成两行再生成")
            return
        voice_id = panel.voice_id()
        if not voice_id:
            panel.show_error("还没有可用的音色预设，请先在声音库保存一个")
            return
        body = {"voice_id": voice_id, "use_preset": True,
                "segments": [{"text": line} for line in lines],
                "overrides": {"speed": round(panel.speed(), 3)}}
        target = self.float_job_id
        if target:
            url = f"/jobs/{target}/generate"
        else:
            url = "/jobs"
            body["title"] = f"边剪边配 {datetime.now():%m-%d %H:%M}"
        self.float_submitting = True
        panel.set_busy(True)
        panel.set_state("提交中")
        panel.show_error("")

        def accepted(result):
            self.float_submitting = False
            self.float_job_id = result["job_id"]
            panel.set_busy(False)
            panel.clear_editor()
            panel.set_state("排队中")
            self.float_poll()

        self.call(lambda client: checked(client.post(url, json=body)), accepted, self.float_failed)

    def float_failed(self, message):
        self.float_submitting = False
        if self.float_panel is not None:
            self.float_panel.set_busy(False)
            self.float_panel.set_state("提交失败")
            self.float_panel.show_error(str(message))

    def float_poll(self):
        panel = self.float_panel
        if panel is None or self.closing or self.float_polling or not self.float_job_id:
            return
        if not panel.isVisible():
            return
        self.float_polling = True
        job_id = self.float_job_id

        def delivered(job):
            self.float_polling = False
            if panel is not self.float_panel or job_id != self.float_job_id:
                return
            panel.show_job(job)
            panel.set_state(self.float_state(job))

        def failed(message):
            self.float_polling = False
            if panel is self.float_panel:
                panel.set_state("连接中断")
                panel.show_error(str(message))

        self.call(lambda client: checked(client.get(f"/jobs/{job_id}")), delivered, failed)

    @staticmethod
    def float_state(job):
        items = (job or {}).get("segments") or []
        if not items:
            return "待机"
        running = sum(1 for item in items if item["status"] in ("queued", "running"))
        failed = sum(1 for item in items if item["status"] == "failed")
        done = sum(1 for item in items if item["status"] == "done")
        if running:
            return f"生成中 {done}/{len(items)}"
        if failed:
            return f"{failed} 条失败"
        return "已就绪"

    # ---------- row actions ----------

    def float_play(self, index):
        path = self.float_panel.audio_path(index) if self.float_panel else None
        if not path:
            return
        self.float_player.stop()
        self.float_player.setSource(QUrl.fromLocalFile(str(path)))
        self.float_player.play()

    def float_retry(self):
        if not self.float_job_id:
            return
        job_id = self.float_job_id

        def refreshed(job):
            if self.float_panel is not None and job_id == self.float_job_id:
                self.float_panel.show_job(job)
                self.float_panel.set_state(self.float_state(job))

        self.call(lambda client: checked(client.post(f"/jobs/{job_id}/retry")), refreshed, self.float_failed)

    def float_new_session(self):
        self.float_job_id = None
        self.float_polling = False
        if self.float_panel is not None:
            self.float_panel.reset()

    def float_copy(self):
        panel = self.ensure_float_panel()
        if panel.copy_latest():
            panel.set_state("已复制 · 去时间线粘贴")
        else:
            panel.show_error("还没有可复制的音频")
