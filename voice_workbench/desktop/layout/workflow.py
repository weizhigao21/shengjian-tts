"""WorkflowLayout responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QTextEdit, QVBoxLayout, QHBoxLayout, QLineEdit, QWidget
from PyQt6.QtGui import QDesktopServices
from ...ui import button, label
from ..api_client import checked
from .common import card


class WorkflowLayout:
    def _service_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 28, 22)
        layout.setSpacing(16)
        layout.addWidget(label("连接你的双语工作流", "title"))
        layout.addWidget(label("字幕 → 配音片段 → 混音。仅保留生成记录，音频接收后自动清理。", "muted"))
        config_card, config_layout = card("音色自动发现", "复制接入链接到双语工作流，读取声音预设后选择音色并批量生成。")
        row = QHBoxLayout()
        badge = label("仅本机访问")
        badge.setObjectName("badge")
        row.addWidget(badge)
        self.workflow_link = QLineEdit(self.url + "/workflow")
        self.workflow_link.setReadOnly(True)
        self.workflow_link.setToolTip("在双语工作流的 API 参数中粘贴此链接，点击「读取音色」。")
        row.addWidget(self.workflow_link, 1)
        row.addWidget(button("复制接入链接", lambda: QApplication.clipboard().setText(self.workflow_link.text()), "copy", "primary"))
        row.addWidget(button("复制 API 配置", lambda: QApplication.clipboard().setText(self.link_config.toPlainText()), "copy", "ghost"))
        config_layout.addLayout(row)
        config_layout.addWidget(label("音色来自声音库中的已保存预设。新增或修改预设后，在工作流点击「刷新音色」；批量开始前自动核对版本。", "hint"))
        self.link_config = QTextEdit()
        self.link_config.setObjectName("code")
        self.link_config.setReadOnly(True)
        self.link_config.setFixedHeight(150)
        config_layout.addWidget(self.link_config)
        layout.addWidget(config_card)
        log_card, log_layout = card()
        tools = QHBoxLayout()
        tools.addWidget(label("服务日志", "section"), 1)
        from ..api_client import checked
        self.reconnect_button = button("重新连接", self.reconnect_service, "retry", "ghost")
        self.reconnect_button.setToolTip("连接已有服务；服务已退出时在同一端口重新启动，不中断正在运行的服务。")
        tools.addWidget(self.reconnect_button)
        tools.addWidget(button("释放引擎 / 显存", lambda: self.call(lambda c: checked(c.post("/engine/release")), lambda r: self.statusBar().showMessage(r["msg"], 8000)), "cpu"))
        tools.addWidget(button("打开日志目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir / "logs"))), "folder"))
        log_layout.addLayout(tools)
        self.service_log = QTextEdit()
        self.service_log.setObjectName("code")
        self.service_log.setReadOnly(True)
        self.service_log.document().setMaximumBlockCount(2000)
        self.service_log.setPlaceholderText("启动日志会显示在这里。片段错误可在生成结果中双击查看。")
        log_layout.addWidget(self.service_log, 1)
        layout.addWidget(log_card, 1)
        layout.addWidget(label("双语工作流：模式选择「API 服务器」→ 粘贴链接 → 读取音色 → 选择音色。使用更新后的源码或重新打包版本。", "hint"))
        self.tabs.addWidget(page)
