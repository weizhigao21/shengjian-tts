"""One-click Qwen runtime and paired model setup with progress polling."""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QProgressBar, QVBoxLayout, QWidget
from ...ui import button, label
from ..api_client import checked
from .common import size_text
from .model_transfers import speed_text


class BatchSetupControls(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.busy = self.loading = False
        self.selected = ''
        self.device_dirty = False
        self.data = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.source = QComboBox()
        self.source.setMinimumWidth(150)
        self.source.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.download_source = QComboBox()
        self.download_source.addItem('HF 镜像', 'mirror')
        self.download_source.addItem('Hugging Face', 'huggingface')
        self.device = QComboBox()
        self.device.addItem('NVIDIA CUDA', 'cuda')
        self.device.addItem('CPU', 'cpu')
        form.addRow('自动关联模型', self.source)
        form.addRow('模型下载来源', self.download_source)
        form.addRow('安装计算环境', self.device)
        layout.addLayout(form)
        self.plan = label('正在检测本地模型…', 'hint')
        self.plan.setWordWrap(True)
        layout.addWidget(self.plan)
        row = QHBoxLayout()
        self.install = button('一键安装并关联', lambda: self.start('all'), 'import')
        self.detect = button('自动检测 / 刷新', lambda: self.start('detect'), 'retry', 'ghost')
        self.pause = button('暂停', self.pause_setup, 'pause', 'ghost')
        self.pause.hide()
        row.addWidget(self.install); row.addWidget(self.detect); row.addWidget(self.pause); row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        self.runtime_download = button('下载运行环境', lambda: self.start('runtime'), 'import', 'ghost')
        self.model_download = button('下载关联模型', lambda: self.start('model'), 'import', 'ghost')
        row.addWidget(self.runtime_download); row.addWidget(self.model_download); row.addStretch()
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = label('', 'hint')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(panel.refresh)
        self.source.currentIndexChanged.connect(self.changed)
        self.device.currentIndexChanged.connect(lambda _: setattr(self, 'device_dirty', True))

    def changed(self, *_):
        self.selected = self.source.currentData() or ''
        self.source.setToolTip(self.selected)
        selected = next((m for m in self.data.get('sources', []) if m['path'] == self.selected), None)
        text = (f"自动匹配 {selected['size']} Base 原始权重（约 " + ('2.34 GB' if selected['size'] == '0.6B' else '4.23 GB') + '），已有完整文件优先复用。'
                if selected else '请先在“模型”页面下载或导入 Qwen3-TTS Base GGUF。运行环境可提前安装。')
        self.plan.setText(text + f"\n独立 Python 约 {size_text(self.data.get('python_download_size', 0))}，PyTorch 等依赖另需数 GB。保存位置：{self.data.get('directory', '')}")

    def bind(self, data, settings):
        self.data = data
        if not self.device_dirty:
            self.device.blockSignals(True)
            self.device.setCurrentIndex(max(0, self.device.findData(settings.get('device', 'cuda'))))
            self.device.blockSignals(False)
        previous = self.selected or settings.get('source_model') or (data.get('selected') or {}).get('path')
        self.source.blockSignals(True)
        self.source.clear()
        for source in data['sources']:
            self.source.addItem(source['name'] + ' · ' + source['size'] + ' Base', source['path'])
        if not data['sources']:
            self.source.addItem('请先添加 Qwen3-TTS Base GGUF', '')
        self.source.setCurrentIndex(max(0, self.source.findData(previous)))
        self.source.blockSignals(False)
        self.changed()
        task = data['task']
        self.busy = task['busy']
        enabled = not self.busy and not self.loading
        for control in (self.source, self.device, self.download_source, self.detect):
            control.setEnabled(enabled)
        self.install.setEnabled(enabled and data['supported'] and bool(self.selected))
        self.model_download.setEnabled(self.install.isEnabled())
        self.runtime_download.setEnabled(enabled and data['supported'])
        self.pause.setVisible(self.busy)
        self.pause.setEnabled(task['status'] != 'pausing')
        self.install.setText('继续安装并关联' if task['status'] in ('paused', 'failed') else '一键安装并关联')
        self.panel.manual.setEnabled(not self.busy)
        self.panel.apply.setEnabled(not self.busy)
        self.progress.setVisible(self.busy)
        received, total = task.get('received', 0), task.get('size', 0)
        self.progress.setRange(0, 1000 if total else 0)
        if total: self.progress.setValue(min(1000, int(received * 1000 / total)))
        message = task.get('message', '')
        if self.busy and total:
            message += f"\n{size_text(received)} / {size_text(total)}"
        if task.get('speed_bps'):
            message += ' · ' + speed_text(task['speed_bps'])
        self.status.setText(message)
        self.timer.start() if self.busy else self.timer.stop()

    def start(self, action):
        if self.loading or self.busy:
            return
        self.loading = True
        for control in (self.install, self.detect, self.runtime_download, self.model_download):
            control.setEnabled(False)
        payload = {'action': action, 'source_model': self.selected,
                   'source': self.download_source.currentData(), 'device': self.device.currentData()}
        def done(_):
            self.loading = False
            self.panel.dirty = False
            self.panel.refresh()
        def failed(message):
            self.loading = False
            self.status.setText(message)
            self.panel.refresh()
        self.panel.owner.call(lambda c: checked(c.post('/inference/setup', json=payload)), done, failed)

    def pause_setup(self):
        self.panel.owner.call(lambda c: checked(c.post('/inference/setup/pause')),
                              lambda _: self.panel.refresh(), self.panel.failed)
