"""PreferencesSettings responsibilities, composed by the owning Qt widget."""
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSpinBox, QFormLayout
from ...ui import button, label


class PreferencesSettings:
    def _general_page(self):
        scroll, layout = self.scroll_page("generalSettings")
        layout.addWidget(label("数据与本地服务", "hint"))
        layout.addWidget(self.settings_group([
            self.settings_row("数据保存位置", self.data_location, (self.open_files_button,)),
            self.settings_row("本地服务", "双语工作流通过这个地址接入。", (label(f"127.0.0.1:{self.owner.port}", "hint"),))]))
        from .app_updates import AppUpdateControls
        self.app_updates = AppUpdateControls(self)
        layout.addWidget(self.app_updates)
        layout.addStretch()
        self.pages.addWidget(scroll)

    def _performance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label("双语工作流批量生成", "section"))
        description = label("并发数决定统一引擎同时生成多少个字幕片段。每个实例会额外占用内存和显存；大模型建议从 1 开始，比较生成时间后再调整。", "muted")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.workflow_workers = QSpinBox()
        self.workflow_workers.setRange(1, 4)
        self.workflow_workers.setValue(1)
        self.workflow_workers.setFixedWidth(140)
        self.workflow_workers.setSuffix(" 个实例")
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(0, 128)
        self.cpu_threads.setSpecialValueText("自动")
        self.cpu_threads.setFixedWidth(140)
        form.addRow("批量并发数", self.workflow_workers)
        form.addRow("每个实例的 CPU 线程数", self.cpu_threads)
        layout.addLayout(form)
        hint = label("CPU 线程数为 0 时自动分配。线程越多不一定越快；并发遇到显存不足时，当前任务自动改为单实例并重试一次。调整设置前请完成或取消正在生成的任务。", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.performance_apply = button("应用设置", self.save_performance, "check")
        self.performance_apply.setEnabled(False)
        row.addWidget(self.performance_apply)
        row.addStretch()
        layout.addLayout(row)
        self.performance_notice = label("", "hint")
        self.performance_notice.setWordWrap(True)
        layout.addWidget(self.performance_notice)
        layout.addStretch()
        def changed():
            self.performance_dirty = True
            self.performance_apply.setEnabled(True)
        self.workflow_workers.valueChanged.connect(changed)
        self.cpu_threads.valueChanged.connect(changed)
        self.pages.addWidget(page)

    def save_performance(self):
        from ..api_client import checked
        value = {"workflow_workers": self.workflow_workers.value(), "cpu_threads": self.cpu_threads.value()}
        self.performance_apply.setEnabled(False)
        self.workflow_workers.setEnabled(False)
        self.cpu_threads.setEnabled(False)
        def saved(_):
            self.workflow_workers.setEnabled(True)
            self.cpu_threads.setEnabled(True)
            self.performance_dirty = False
            self.performance_notice.setText("")
            self.owner.statusBar().showMessage("批量性能设置已应用", 5000)
        def failed(message):
            self.workflow_workers.setEnabled(True)
            self.cpu_threads.setEnabled(True)
            self.performance_apply.setEnabled(True)
            self.owner.show_error(message)
        self.owner.call(lambda c: checked(c.put("/settings/performance", json=value)), saved, failed)
