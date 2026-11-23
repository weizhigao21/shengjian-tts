"""局域网分发控件：主电脑这边只需要「开始」和「把地址给另一台电脑」。

界面不直接搬文件，全部通过本地服务完成；这里只负责展示状态、复制地址、
下载引导包。清单包含 1.8 万个文件时哈希要算一阵，所以准备阶段也要有反馈。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from ...handoff.manifest import SCOPES
from ...ui import button, label
from ..api_client import checked


def _size(value):
    return f"{value / 1024 ** 3:.1f} GB" if value >= 1024 ** 3 else f"{value / 1024 ** 2:.0f} MB"


class HandoffControls(QWidget):
    def __init__(self, settings):
        super().__init__(settings)
        self.settings, self.owner = settings, settings.owner
        self.state = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.status = label("把程序、模型和声音直接交给同网段的另一台电脑，不必重新下载。", "hint")
        self.status.setWordWrap(True)
        self.scope = QComboBox()
        for key, value in SCOPES.items():
            self.scope.addItem(value["label"], key)
        self.scope.setFixedWidth(200)
        self.start_button = button("开始分发", self.start, "link")
        self.stop_button = button("结束", self.stop, "close", "ghost")
        self.stop_button.hide()
        layout.addWidget(settings.settings_group([
            settings.settings_row("局域网分发", self.status, (self.scope, self.start_button, self.stop_button))]))

        self.detail = QWidget()
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(10)
        self.address_row = QWidget()
        address_layout = QHBoxLayout(self.address_row)
        address_layout.setContentsMargins(0, 0, 0, 0)
        address_layout.setSpacing(8)
        self.address = label("", "muted")
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        address_layout.addWidget(self.address, 1)
        self.copy_button = button("复制地址", self.copy, "folder", "ghost")
        address_layout.addWidget(self.copy_button)
        self.bootstrap_button = button("下载引导包", self.download_bootstrap, "import", "ghost")
        address_layout.addWidget(self.bootstrap_button)
        detail.addWidget(self.address_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setFormat("等待另一台电脑连接")
        detail.addWidget(self.progress)
        # 同网段的另一台声笺广播找到这里时出现在这一行：发现是公开的，
        # 但要不要把模型和声音交出去，得在这台电脑上点一下。
        self.pending_row = QWidget()
        pending = QHBoxLayout(self.pending_row)
        pending.setContentsMargins(0, 0, 0, 0)
        pending.setSpacing(8)
        self.pending_label = label("", "muted")
        self.pending_label.setWordWrap(True)
        pending.addWidget(self.pending_label, 1)
        self.allow_button = button("允许接入", self.allow, "check", "primary")
        pending.addWidget(self.allow_button)
        pending.addWidget(button("拒绝", self.deny, "close", "ghost"))
        self.pending_row.hide()
        detail.addWidget(self.pending_row)
        self.pending_token = ""
        self.hint = label("", "hint")
        self.hint.setWordWrap(True)
        detail.addWidget(self.hint)
        self.detail.hide()
        layout.addWidget(self.detail)

        self.timer = QTimer(self)
        self.timer.setInterval(1200)
        self.timer.timeout.connect(self.poll)

    # -- actions -----------------------------------------------------------
    def start(self):
        self.start_button.setEnabled(False)
        self.status.setText("正在整理清单：统计文件并计算校验值，首次需要一两分钟…")
        action = lambda client: checked(client.post("/handoff/start", json={"scope": self.scope.currentData()}))
        self.owner.call(action, self.apply, self.failed)

    def stop(self):
        self.owner.call(lambda client: checked(client.post("/handoff/stop")), self.apply, self.failed)

    def poll(self):
        self.owner.call(lambda client: checked(client.get("/handoff")), self.apply, lambda error: None)

    def copy(self):
        QGuiApplication.clipboard().setText(self.address.text())
        self.hint.setText("地址已复制，粘到另一台电脑的浏览器里就能下载引导包。")

    def allow(self):
        self.decide("approve")

    def deny(self):
        self.decide("deny")

    def decide(self, action):
        token = self.pending_token
        if not token:
            return
        self.allow_button.setEnabled(False)
        self.owner.call(lambda client: checked(client.post(f"/handoff/pair/{token}/{action}")),
                        lambda result: (self.pending_row.hide(), self.poll()),
                        self.failed)

    def download_bootstrap(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        # The browser handles the save dialog; the archive is a few kilobytes.
        QDesktopServices.openUrl(QUrl(self.owner.url + "/handoff/bootstrap"))

    def failed(self, message):
        self.start_button.setEnabled(True)
        self.status.setText(str(message))
        self.timer.stop()

    # -- rendering ---------------------------------------------------------
    def apply(self, state):
        self.state = state
        phase = state.get("phase", "idle")
        ready = phase == "ready"
        busy = phase == "preparing"
        self.start_button.setEnabled(not ready and not busy)
        self.stop_button.setVisible(ready or busy)
        self.detail.setVisible(ready or busy)
        if busy:
            self.status.setText("正在整理清单：统计文件并计算校验值，首次需要一两分钟…")
            self.progress.setRange(0, 0)
        elif ready:
            total = state.get("bytes", 0)
            self.status.setText(f"已就绪：{state.get('scope_label', '')} · {state.get('files', 0)} 个文件 / {_size(total)}")
            self.address.setText(state.get("url", ""))
            self.progress.setRange(0, 100)
            done = state.get("sent_bytes", 0)
            percent = min(100, int(done * 100 / total)) if total else 0
            self.progress.setValue(percent)
            self.progress.setFormat(f"已传 {_size(done)} / {_size(total)}  ·  配对码 {state.get('code', '')}")
            self.hint.setText("把地址粘到另一台电脑的浏览器，下载引导包后解压，双击「开始接收.bat」。"
                              "接收期间请保持声笺打开。")
            pending = state.get("pending_pairs") or []
            if pending:
                item = pending[0]
                self.pending_token = item.get("token", "")
                self.pending_label.setText(
                    f"{item.get('address') or '同网段的另一台电脑'} 想接收这台电脑的模型与声音"
                    + (f"（还有 {len(pending) - 1} 个请求在排队）" if len(pending) > 1 else ""))
                self.allow_button.setEnabled(True)
                self.pending_row.show()
            else:
                self.pending_token = ""
                self.pending_row.hide()
        elif phase == "error":
            self.status.setText(state.get("error", "准备失败"))
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        else:
            self.status.setText("把程序、模型和声音直接交给同网段的另一台电脑，不必重新下载。")
            self.detail.hide()
        if ready or busy:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    def refresh(self):
        """Called by the settings page tick; cheap no-op until something is running."""
        if self.state and self.state.get("phase") in ("ready", "preparing"):
            self.poll()
