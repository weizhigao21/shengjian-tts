"""One-click Qwen runtime and paired model setup with progress polling.

The model itself is chosen in the batch model card above; this section only
installs the isolated Python runtime and the Base weights that match it.
"""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QProgressBar, QVBoxLayout, QWidget
from ...ui import button, label
from ...batch_inference.sources import MODEL_SOURCES, RUNTIME_SOURCES
from ..api_client import checked
from .common import settings_group, settings_row, size_text
from .model_transfers import speed_text


class BatchSetupControls(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.busy = self.loading = False
        self.selected = ''
        self.data = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.state = label('正在检测本地环境…', 'hint')
        self.state.setWordWrap(True)
        self.install = button('一键安装并关联', lambda: self.start('all'), 'import')
        self.install.setToolTip('安装独立 Python、PyTorch、Qwen SDK，并下载与所选模型匹配的 Base 原始权重。')
        self.download_source = QComboBox()
        for key, name in MODEL_SOURCES.items():
            self.download_source.addItem(name, key)
        self.download_source.setFixedWidth(200)
        self.download_source.setToolTip('模型文件均按内置大小和 SHA-256 校验；暂停后可换源继续下载。')
        self.runtime_source = QComboBox()
        for key, name in RUNTIME_SOURCES.items():
            self.runtime_source.addItem(name, key)
        self.runtime_source.setFixedWidth(200)
        self.runtime_source.setToolTip('国内加速覆盖独立 Python、PyTorch 和 Qwen SDK；失败后尝试备用镜像及官方来源。已有环境和下载文件优先复用。')
        sources = QWidget()
        line = QHBoxLayout(sources)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        line.addWidget(self.download_source)
        line.addWidget(self.runtime_source)
        layout.addWidget(settings_group([
            settings_row('独立运行环境', self.state, (self.install,)),
            settings_row('安装来源', '模型与运行环境分开选择；已有文件优先复用。', (sources,)),
        ]))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.detect = button('重新检测', lambda: self.start('detect'), 'retry', 'ghost')
        self.runtime_download = button('仅下载运行环境', lambda: self.start('runtime'), 'import', 'ghost')
        self.model_download = button('仅下载关联模型', lambda: self.start('model'), 'import', 'ghost')
        self.pause = button('暂停', self.pause_setup, 'pause', 'ghost')
        self.pause.hide()
        row.addWidget(self.detect)
        row.addWidget(self.runtime_download)
        row.addWidget(self.model_download)
        row.addWidget(self.pause)
        row.addStretch()
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

    def changed(self, *_):
        self.selected = self.panel.model_picker.currentData() or ''

    def bind(self, data, settings):
        self.data = data
        self.selected = settings.get('source_model') or (data.get('selected') or {}).get('path') or ''
        chosen = next((m for m in data.get('sources', []) if m['path'] == self.selected), None)
        task = data['task']
        self.busy = task['busy']
        enabled = not self.busy and not self.loading
        for control in (self.download_source, self.runtime_source, self.detect, self.install):
            control.setEnabled(enabled)
        self.install.setEnabled(enabled and data['supported'] and bool(self.selected))
        self.model_download.setEnabled(self.install.isEnabled())
        self.runtime_download.setEnabled(enabled and data['supported'])
        self.pause.setVisible(self.busy)
        self.pause.setEnabled(task['status'] != 'pausing')
        self.install.setText('继续安装并关联' if task['status'] in ('paused', 'failed') else '一键安装并关联')
        # A benchmark run locks the same fields; keep both states consistent.
        locked = self.busy or getattr(getattr(self.panel, 'benchmark', None), 'busy', False)
        self.panel.manual.setEnabled(not locked)
        self.panel.apply.setEnabled(not locked)
        if chosen:
            weights = '约 2.34 GB' if chosen['size'] == '0.6B' else '约 4.23 GB'
            self.state.setText(f"匹配 {chosen['size']} Base 原始权重（{weights}），独立 Python "
                               f"{size_text(data.get('python_download_size', 0))}，PyTorch 等依赖另需数 GB。")
        else:
            self.state.setText('请先在「模型」页下载或导入 Qwen3-TTS Base 模型，或在上方选择已有模型。')
        self.state.setToolTip(data.get('directory', ''))
        self.progress.setVisible(self.busy)
        received, total = task.get('received', 0), task.get('size', 0)
        self.progress.setRange(0, 1000 if total else 0)
        if total: self.progress.setValue(min(1000, int(received * 1000 / total)))
        message = task.get('message', '')
        if self.busy and total:
            message += f"\n{size_text(received)} / {size_text(total)}"
        if task.get('speed_bps'):
            message += ' · ' + speed_text(task['speed_bps'])
        if self.busy and task.get('download_source'):
            message += '\n来源：' + task['download_source']
        self.status.setText(message)
        self.timer.start() if self.busy else self.timer.stop()

    def start(self, action):
        if self.loading or self.busy:
            return
        self.loading = True
        self.changed()
        for control in (self.install, self.detect, self.runtime_download, self.model_download,
                        self.download_source, self.runtime_source):
            control.setEnabled(False)
        payload = {'action': action, 'source_model': self.selected,
                   'source': self.download_source.currentData(), 'device': self.panel.device.currentData(),
                   'runtime_source': self.runtime_source.currentData()}
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
