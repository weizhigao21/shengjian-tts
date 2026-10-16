"""User-selected optional Qwen runtime; no torch imports in the desktop app."""
from pathlib import Path
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget
from ...runtime import project_root
from ...ui import button, label
from ..api_client import checked


class BatchInferenceControls(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.dirty = self.loading = False
        self.sequence = 0
        self.settings = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(label('Qwen 共享模型批量推理', 'section'))
        description = label('多条兼容短句共用模型和音色特征。自动检测、关联现有文件；可一键下载独立 Python、PyTorch、Qwen SDK 和配套 Base 模型，无需手动安装 Python。', 'muted')
        description.setWordWrap(True)
        layout.addWidget(description)
        from .batch_setup import BatchSetupControls
        self.automatic = BatchSetupControls(self)
        layout.addWidget(self.automatic)
        self.enabled = QCheckBox('符合条件时自动使用批量后端')
        self.enabled.setChecked(True)
        layout.addWidget(self.enabled)
        self.manual_toggle = QCheckBox('高级手动配置')
        layout.addWidget(self.manual_toggle)
        self.manual = QWidget()
        form = QFormLayout(self.manual)
        form.setContentsMargins(0, 0, 0, 0)
        self.manual.hide()
        self.manual_toggle.toggled.connect(self.manual.setVisible)
        self.python = QLineEdit()
        self.model_dir = QLineEdit()
        self.source_model = QLineEdit()
        for key, title in (('python', 'Python 环境'), ('model_dir', 'Base 原始模型目录'), ('source_model', '关联的 GGUF 模型')):
            field = getattr(self, key)
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(button('选择', lambda _, k=key: self.browse(k), 'folder', 'small'))
            form.addRow(title, row)
            field.textChanged.connect(self.changed)
        self.device = QComboBox()
        self.device.addItem('NVIDIA CUDA', 'cuda'); self.device.addItem('CPU', 'cpu')
        self.dtype = QComboBox()
        for title, key in (('自动', 'auto'), ('FP16', 'float16'), ('BF16', 'bfloat16'), ('FP32', 'float32')):
            self.dtype.addItem(title, key)
        self.size = QSpinBox()
        self.size.setRange(2, 8); self.size.setValue(2); self.size.setSuffix(' 条')
        form.addRow('计算设备', self.device)
        form.addRow('计算精度', self.dtype)
        form.addRow('每批最多', self.size)
        layout.addWidget(self.manual)
        row = QHBoxLayout()
        self.apply = button('检查并应用', self.save, 'check')
        row.addWidget(self.apply)
        row.addWidget(button('安装说明', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(project_root() / 'docs/BATCH_INFERENCE.md'))), 'info', 'ghost'))
        row.addStretch()
        layout.addLayout(row)
        self.status = label('尚未配置独立批量后端。', 'hint')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        from .benchmark import BenchmarkControls
        self.benchmark = BenchmarkControls(self)
        layout.addWidget(self.benchmark)
        self.enabled.toggled.connect(self.changed)
        self.device.currentIndexChanged.connect(self.changed)
        self.dtype.currentIndexChanged.connect(self.changed)
        self.size.valueChanged.connect(self.changed)

    def changed(self, *_):
        self.dirty = True

    def browse(self, key):
        if key == 'model_dir':
            path = QFileDialog.getExistingDirectory(self, '选择 Qwen3-TTS Base 原始模型目录')
        else:
            path, _ = QFileDialog.getOpenFileName(self, '选择 Python 环境' if key == 'python' else '选择关联 GGUF', '',
                'Python (python.exe python3.exe)' if key == 'python' else 'GGUF (*.gguf)')
        if path:
            getattr(self, key).setText(path)

    def bind(self, result):
        self.loading = False
        self.settings = result['settings']
        if not self.dirty:
            controls = [(self.enabled, lambda: self.enabled.setChecked(self.settings['enabled'])),
                        (self.size, lambda: self.size.setValue(self.settings['max_batch_size']))]
            controls += [(getattr(self, key), lambda k=key: getattr(self, k).setText(self.settings[k]))
                         for key in ('python', 'model_dir', 'source_model')]
            controls += [(getattr(self, key), lambda k=key: getattr(self, k).setCurrentIndex(
                getattr(self, k).findData(self.settings[k]))) for key in ('device', 'dtype')]
            for control, bind in controls:
                control.blockSignals(True); bind(); control.blockSignals(False)
        text = ('批量后端已就绪 · ' + ('模型已驻留' if result['loaded'] else '首次批量任务时加载模型')) if result['ready'] else result['reason']
        recent = result.get('last_result')
        if recent:
            text += f"\n上批 {recent['batch_size']} 条 · {recent['batch_seconds']:.2f} 秒"
            if 'prompt_cache_hit' in recent:
                text += ' · 音色缓存命中' if recent['prompt_cache_hit'] else ' · 新建音色缓存'
        if result.get('fallbacks'):
            text += '\n最近回退：' + result['fallbacks'][-1]['reason']
        self.status.setText(text)
        if 'setup' in result:
            self.automatic.bind(result['setup'], self.settings)
        if 'benchmark' in result:
            self.benchmark.bind(result['benchmark'])

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.sequence += 1
        sequence = self.sequence
        self.owner.call(lambda c: checked(c.get('/settings/batch-inference')),
                        lambda r: self.bind(r) if sequence == self.sequence else None,
                        lambda r: self.failed(r) if sequence == self.sequence else None)

    def failed(self, message):
        self.loading = False
        self.setEnabled(True)
        self.apply.setEnabled(True)
        self.status.setText(message)

    def save(self):
        value = {**self.settings, 'enabled': self.enabled.isChecked(),
                 'python': self.python.text().strip(), 'model_dir': self.model_dir.text().strip(),
                 'source_model': self.source_model.text().strip(), 'device': self.device.currentData(),
                 'dtype': self.dtype.currentData(), 'max_batch_size': self.size.value()}
        self.setEnabled(False)
        self.loading = True
        self.sequence += 1
        def saved(result):
            self.dirty = False
            self.setEnabled(True)
            self.apply.setEnabled(True)
            self.bind(result)
            self.refresh()
        self.owner.call(lambda c: checked(c.put('/settings/batch-inference', json=value)), saved, self.failed)
