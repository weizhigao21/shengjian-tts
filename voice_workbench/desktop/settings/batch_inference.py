"""Batch inference settings: model choice, batch limits and accelerators.

The page is grouped into four cards — model, batch parameters, accelerators and
runtime — so the common path (pick a model, set a batch size, turn everything on)
reads top to bottom without wading through installer options.
"""
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QSpinBox,
    QVBoxLayout, QWidget,
)

from ...runtime import resource_root
from ...ui import button, label
from ..api_client import checked
from .common import settings_group, settings_row

CONTROL_WIDTH = 176


def sized(control, width=CONTROL_WIDTH):
    control.setFixedWidth(width)
    return control


class BatchInferenceControls(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.dirty = self.loading = False
        self.sequence = 0
        self.settings = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # ── 批量模型 ────────────────────────────────────────────────────────
        self.enabled = QCheckBox()
        self.enabled.setChecked(True)
        self.enabled.setToolTip('总开关。关闭后所有文本都走原生引擎。')
        layout.addWidget(label('批量模型', 'groupTitle'))
        self.model_picker = sized(QComboBox())
        self.model_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_picker.setMinimumContentsLength(16)
        self.model_picker.setToolTip('批量推理使用的 Qwen3-TTS Base 模型。切换后下一次批量任务会重新加载权重，'
                                     '已保存的声音预设和作品不受影响。')
        self.get_model = button('下载 0.6B', self.download_smaller, 'import', 'ghost')
        self.get_model.setToolTip('下载 Qwen3-TTS 0.6B Base（约 2.34 GB）。参数量约为 1.7B 的三分之一，'
                                  '生成更快、占用显存更少，适合长稿批量配音；音色细腻度略低于 1.7B。')
        self.status_badge = label('正在检测…', 'hint')
        layout.addWidget(settings_group([
            settings_row('启用批量推理', '关闭后所有文本都走原生引擎，忽略下面的批量参数。', (self.enabled,)),
            settings_row('生成模型', '同一时间只用一套权重；可在 1.7B 与 0.6B 之间随时切换。',
                         (self.model_picker, self.get_model)),
            settings_row('运行状态', None, (self.status_badge,)),
        ]))
        self.status = label('尚未配置独立批量后端。', 'hint')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.migration = QWidget()
        migration_row = QHBoxLayout(self.migration)
        migration_row.setContentsMargins(0, 0, 0, 0)
        migration_row.setSpacing(8)
        self.migrate_notice = label('', 'muted')
        self.migrate_notice.setWordWrap(True)
        migration_row.addWidget(self.migrate_notice, 1)
        self.migrate_button = button('一并切换', self.migrate_presets, 'check', 'ghost')
        self.migrate_button.setToolTip('把这些预设的模型文件改成当前批量模型，参考音频和生成参数保持不变。')
        migration_row.addWidget(self.migrate_button)
        self.migration.hide()
        layout.addWidget(self.migration)

        # ── 批处理参数 ──────────────────────────────────────────────────────
        layout.addWidget(label('批处理参数', 'groupTitle'))
        self.size = sized(QSpinBox())
        self.size.setRange(2, 8)
        self.size.setValue(2)
        self.size.setSuffix(' 条')
        self.size.setToolTip('每批同时生成的条数。实测批 8 的吞吐最高，是这张显卡的运行上限。')
        self.minimum_size = sized(QSpinBox())
        self.minimum_size.setRange(2, 2)
        self.minimum_size.setSuffix(' 条')
        self.minimum_size.setToolTip('积压的兼容文本达到此数量才启动批量引擎；不足时使用原生引擎。'
                                     '已驻留的批量模型会继续处理尾段，不会为凑数而回退。')
        self.dtype = sized(QComboBox())
        for title, key in (('自动', 'auto'), ('FP16', 'float16'), ('BF16', 'bfloat16'), ('FP32', 'float32')):
            self.dtype.addItem(title, key)
        self.dtype.setToolTip('自动会按设备选择精度。FP16 最快，但个别模型（如 0.6B）的归一化权重'
                              '超出 FP16 范围，会自动改用 BF16 并变慢——这是数值稳定性要求，不是配置错误。')
        self.device = sized(QComboBox())
        self.device.addItem('NVIDIA CUDA', 'cuda')
        self.device.addItem('CPU', 'cpu')
        self.device.setToolTip('运行与安装批量环境使用的设备。CPU 可以跑通，但速度远低于显卡。')
        self.idle_seconds = sized(QSpinBox())
        self.idle_seconds.setRange(30, 3600)
        self.idle_seconds.setSingleStep(30)
        self.idle_seconds.setSuffix(' 秒')
        self.idle_seconds.setToolTip('空闲多久后释放常驻模型。调大可以避免每次批量都重新加载（约 20–30 秒），'
                                     '代价是常驻约 3.4 GB 显存。')
        layout.addWidget(settings_group([
            settings_row('每批最多', '越大越省固定开销；显存装得下就用最大值。', (self.size,)),
            settings_row('启动至少', '积压到这个条数才值得加载模型合批，只影响冷启动。', (self.minimum_size,)),
            settings_row('计算精度', None, (self.dtype,)),
            settings_row('计算设备', None, (self.device,)),
            settings_row('空闲卸载', '写一段配一段时调大，省掉反复加载。', (self.idle_seconds,)),
        ]))

        # ── 加速 ────────────────────────────────────────────────────────────
        layout.addWidget(label('加速', 'groupTitle'))
        self.predictor_fastpath = QCheckBox()
        self.predictor_fastpath.setChecked(True)
        self.predictor_fastpath.setToolTip('减少固定 15 步编码预测的通用生成开销，不修改模型或采样参数；'
                                           'SDK 或调用不兼容时自动使用原循环。')
        self.predictor_cuda_graph = QCheckBox()
        self.predictor_cuda_graph.setToolTip('把编码预测录进 CUDA 图。需要先启用专用循环；'
                                             '首次准备有额外耗时并增加显存占用。')
        self.talker_graph = QCheckBox()
        self.talker_graph.setToolTip('把主模型每帧的整步前向录进一张 CUDA 图，实测批 8 约快 1.8 倍、批 2 约 2.1 倍。'
                                     '只在 RTX 2080 Ti / FP16 / qwen-tts 0.1.1 验证，其它设备仍需实测；'
                                     '形状不支持或显存不足时自动回到原循环。')
        self.predictor_fastpath.toggled.connect(self.predictor_cuda_graph.setEnabled)
        layout.addWidget(settings_group([
            settings_row('编码预测专用循环', '15 步固定内循环的专用实现，风险最低。', (self.predictor_fastpath,)),
            settings_row('编码预测 GPU 图', '在专用循环之上再省掉内核调度开销。', (self.predictor_cuda_graph,)),
            settings_row('主模型整步图', '收益最大的一项；首帧多花零点几秒录制。', (self.talker_graph,)),
        ]))

        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        self.apply = button('应用更改', self.save, 'check')
        row.addWidget(self.apply)
        row.addWidget(button('安装说明', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(resource_root() / 'docs/QWEN_BATCH_SETUP.md'))), 'info', 'ghost'))
        row.addStretch()
        layout.addLayout(row)

        # ── 运行环境 ────────────────────────────────────────────────────────
        layout.addWidget(label('运行环境', 'groupTitle'))
        from .batch_setup import BatchSetupControls
        self.automatic = BatchSetupControls(self)
        layout.addWidget(self.automatic)

        # ── 高级手动配置 ────────────────────────────────────────────────────
        self.manual_toggle = QCheckBox('显示高级手动配置')
        self.manual_toggle.setToolTip('直接指定 Python 解释器、Base 原始权重目录和关联的 GGUF 路径。'
                                      '日常使用不需要改动。')
        layout.addWidget(self.manual_toggle)
        self.manual = QWidget()
        form = QFormLayout(self.manual)
        form.setContentsMargins(18, 10, 18, 12)
        form.setVerticalSpacing(10)
        self.manual.hide()
        self.manual_toggle.toggled.connect(self.manual.setVisible)
        self.python = QLineEdit()
        self.model_dir = QLineEdit()
        self.source_model = QLineEdit()
        for key, title in (('python', 'Python 环境'), ('model_dir', 'Base 原始模型目录'), ('source_model', '关联的 GGUF')):
            field = getattr(self, key)
            field.setPlaceholderText('留空表示自动检测')
            holder = QWidget()
            line = QHBoxLayout(holder)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)
            line.addWidget(field, 1)
            line.addWidget(button('选择', lambda _, k=key: self.browse(k), 'folder', 'small'))
            form.addRow(title, holder)
            field.textChanged.connect(self.changed)
        layout.addWidget(self.manual)

        # ── 速度测试 ────────────────────────────────────────────────────────
        from .benchmark import BenchmarkControls
        self.benchmark = BenchmarkControls(self)
        layout.addWidget(self.benchmark)

        self.model_picker.currentIndexChanged.connect(self.model_chosen)
        self.enabled.toggled.connect(self.changed)
        self.device.currentIndexChanged.connect(self.changed)
        self.dtype.currentIndexChanged.connect(self.changed)
        self.size.valueChanged.connect(self.changed)
        self.size.valueChanged.connect(self.minimum_size.setMaximum)
        self.minimum_size.valueChanged.connect(self.changed)
        self.idle_seconds.valueChanged.connect(self.changed)
        self.predictor_fastpath.toggled.connect(self.changed)
        self.predictor_cuda_graph.toggled.connect(self.changed)
        self.talker_graph.toggled.connect(self.changed)

    # ── 状态与绑定 ──────────────────────────────────────────────────────────
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
            def bind_minimum():
                self.minimum_size.setMaximum(self.settings['max_batch_size'])
                self.minimum_size.setValue(self.settings.get('min_batch_size', 2))
            controls = [(self.enabled, lambda: self.enabled.setChecked(self.settings['enabled'])),
                        (self.size, lambda: self.size.setValue(self.settings['max_batch_size'])),
                        (self.minimum_size, bind_minimum),
                        (self.idle_seconds, lambda: self.idle_seconds.setValue(self.settings.get('idle_seconds', 120)))]
            controls.append((self.predictor_fastpath, lambda: self.predictor_fastpath.setChecked(self.settings.get('predictor_fastpath', True))))
            controls.append((self.predictor_cuda_graph, lambda: self.predictor_cuda_graph.setChecked(self.settings.get('predictor_cuda_graph', False))))
            controls.append((self.talker_graph, lambda: self.talker_graph.setChecked(self.settings.get('talker_graph', False))))
            controls += [(getattr(self, key), lambda k=key: getattr(self, k).setText(self.settings[k]))
                         for key in ('python', 'model_dir', 'source_model')]
            controls += [(getattr(self, key), lambda k=key: getattr(self, k).setCurrentIndex(
                getattr(self, k).findData(self.settings[k]))) for key in ('device', 'dtype')]
            for control, bind in controls:
                control.blockSignals(True); bind(); control.blockSignals(False)
            self.predictor_cuda_graph.setEnabled(self.predictor_fastpath.isChecked())
        ready = result['ready']
        self.status_badge.setText('已关闭' if not self.settings.get('enabled', True) else
                                  ('已就绪 · 模型已驻留' if ready and result.get('loaded') else
                                   # Weights handed back, interpreter still here: the
                                   # next batch costs seconds instead of a minute.
                                   '已就绪 · 环境驻留' if ready and result.get('resident') else
                                   '已就绪' if ready else '待配置'))
        text = ('批量后端已就绪。' if ready else result['reason']) if self.settings.get('enabled', True) \
            else '自动批量推理已关闭，所有文本使用原生引擎。'
        if ready and self.settings.get('enabled', True):
            text += f" 每批最多 {self.settings['max_batch_size']} 条，积压 {self.settings.get('min_batch_size', 2)} 条时启动。"
        recent = result.get('last_result')
        if recent:
            text += f"\n上批 {recent['batch_size']} 条 · {recent['batch_seconds']:.2f} 秒"
            if 'prompt_cache_hit' in recent:
                text += ' · 音色缓存命中' if recent['prompt_cache_hit'] else ' · 新建音色缓存'
            if recent.get('predictor_mode'):
                text += ' · 编码预测：' + {'graph': 'GPU 图加速', 'fixed': '专用加速', 'standard': 'SDK 原循环', 'mixed': '部分加速'}.get(recent['predictor_mode'], recent['predictor_mode'])
            if recent.get('talker_mode'):
                text += ' · 主模型：' + {'graph': '整步图加速', 'standard': '原循环'}.get(
                    recent['talker_mode'], recent['talker_mode'])
            # Everything the batch spent outside the model. A cold worker's
            # imports never showed up anywhere before, and a second hiding
            # between the model and the wall clock could not be seen at all.
            if 'import_seconds' in recent:
                outside = []
                startup = (recent.get('spawn_seconds') or 0) + (recent.get('import_seconds') or 0)
                if startup >= 0.3:
                    outside.append(f'冷启动 {startup:.1f} 秒')
                if (recent.get('load_seconds') or 0) >= 0.3:
                    outside.append(f'加载权重 {recent["load_seconds"]:.1f} 秒')
                if (recent.get('prompt_seconds') or 0) >= 0.3:
                    outside.append(f'音色编码 {recent["prompt_seconds"]:.1f} 秒')
                if (recent.get('write_seconds') or 0) >= 0.3:
                    outside.append(f'写盘 {recent["write_seconds"]:.1f} 秒')
                if (recent.get('ipc_seconds') or 0) >= 0.3:
                    outside.append(f'其它 {recent["ipc_seconds"]:.1f} 秒')
                if outside:
                    text += '\n模型外 ' + ' · '.join(outside)
            for key in ('precision_reason', 'predictor_reason', 'predictor_graph_reason', 'talker_reason'):
                if recent.get(key):
                    text += '\n' + recent[key]
        if result.get('fallbacks'):
            text += '\n最近回退：' + result['fallbacks'][-1]['reason']
        self.status.setText(text)
        if 'setup' in result:
            self.bind_models(result['setup'])
            self.automatic.bind(result['setup'], self.settings)
        if 'benchmark' in result:
            self.benchmark.bind(result['benchmark'])
        self.check_presets()

    def check_presets(self):
        """A batch run only serves presets that point at the configured GGUF."""
        source = self.settings.get('source_model')
        if not source or getattr(self, 'presets_for', None) == source:
            return
        self.presets_for = source
        self.owner.call(lambda c: checked(c.get('/voices')), self.bind_presets, lambda _: None)

    def bind_presets(self, voices):
        from ...batch_inference.policy import selected_mode
        source = self.settings.get('source_model')
        if not source:
            return
        target = Path(source).resolve()
        # Only clone presets on a Base model can ever batch, so a CustomVoice or
        # VoiceDesign preset is not "stale" — it is simply a different mode. A
        # preset with no reference audio is a model-settings stub, not a voice.
        stale = [voice for voice in voices
                 if voice.get('backend') == 'audio_cpp' and voice.get('family') == 'qwen3_tts'
                 and voice.get('model_dir') and voice.get('reference_audio')
                 and Path(voice['model_dir']).resolve() != target
                 and selected_mode(voice, voice.get('model_ui') or {}) == 'clone']
        self.stale_presets = stale
        if not stale:
            self.migration.hide()
            return
        names = '、'.join(voice['name'] for voice in stale[:3])
        if len(stale) > 3:
            names += f' 等 {len(stale)} 个'
        self.migrate_notice.setText(f'{len(stale)} 个声音预设绑定的是其他模型（{names}），'
                                    f'批量推理不会用到它们。')
        self.migration.show()

    def migrate_presets(self):
        stale = getattr(self, 'stale_presets', [])
        source = self.settings.get('source_model')
        if not stale or not source:
            return
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle('切换预设模型')
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f'把 {len(stale)} 个声音预设切换到 {Path(source).name}？')
        box.setInformativeText('只改模型文件，参考音频、语言和生成参数保持不变，随时可以切回。')
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText('切换')
        box.button(QMessageBox.StandardButton.Cancel).setText('取消')
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        def apply(client):
            for voice in stale:
                body = {key: value for key, value in voice.items()
                        if key not in ('model_name', 'model_tag', 'error', 'model_ui')}
                body['model_dir'] = source
                response = client.put(f"/voices/{voice['id']}", json=body)
                if response.is_error:
                    raise RuntimeError(f"{voice['name']}：{response.text[:120]}")
            return len(stale)

        self.migrate_button.setEnabled(False)
        def done(count):
            self.migrate_button.setEnabled(True)
            self.owner.statusBar().showMessage(f'{count} 个预设已切换到当前批量模型。', 6000)
            self.presets_for = None
            self.check_presets()
        def failed(message):
            self.migrate_button.setEnabled(True)
            self.owner.show_error('切换预设失败：' + str(message))
        self.owner.call(apply, done, failed)

    def bind_models(self, setup):
        """List every installed Qwen3-TTS Base GGUF as a selectable batch model."""
        sources = setup.get('sources') or []
        current = self.settings.get('source_model', '')
        self.model_picker.blockSignals(True)
        self.model_picker.clear()
        for source in sources:
            self.model_picker.addItem(f"Qwen3-TTS {source['size']} Base · " + Path(source['path']).name, source['path'])
        if not sources:
            self.model_picker.addItem('没有可用的 Qwen3-TTS Base 模型', '')
        index = self.model_picker.findData(current)
        self.model_picker.setCurrentIndex(index if index >= 0 else 0)
        self.model_picker.setToolTip(current or '还没有可用的 Base 模型；先在「模型」页下载或导入。')
        self.model_picker.blockSignals(False)
        installed = {source['size'] for source in sources}
        self.get_model.setVisible('0.6B' not in installed)
        pending = getattr(self, 'pending_model', None)
        if pending:
            self.pending_model = None
            self.select_model(pending)

    def select_model(self, path):
        """Point the batch backend at a specific GGUF (used from the model page)."""
        index = self.model_picker.findData(path)
        if index < 0:
            self.model_picker.addItem('Qwen3-TTS Base · ' + Path(path).name, path)
            index = self.model_picker.count() - 1
        self.model_picker.blockSignals(True)
        self.model_picker.setCurrentIndex(index)
        self.model_picker.blockSignals(False)
        self.model_chosen()

    def model_chosen(self, *_):
        """Switching the model is an explicit act: save it immediately."""
        path = self.model_picker.currentData()
        if not path or path == self.settings.get('source_model') or self.loading:
            return
        self.dirty = False
        # An empty directory lets the backend rediscover the Base weights that
        # belong to this GGUF instead of keeping the previous model's folder.
        self.save({'source_model': path, 'model_dir': ''})

    def download_smaller(self):
        self.get_model.setEnabled(False)
        self.get_model.setText('查询中…')

        def found(models):
            entry = next((m for m in models['catalog'] if m.get('family') == 'qwen3_tts'
                          and '0.6B' in m['name'] and m.get('adapted', True) and m['downloadable']), None)
            if entry is None:
                self.reset_download_button()
                self.owner.statusBar().showMessage('模型目录里没有可下载的 0.6B Base，可到「模型」页手动查找。', 6000)
                return
            def started(_):
                self.reset_download_button()
                self.owner.statusBar().showMessage('0.6B 模型开始下载，完成后可在「生成模型」里切换。', 6000)
            def failed(message):
                self.reset_download_button()
                self.owner.statusBar().showMessage('下载启动失败：' + str(message), 6000)
            self.owner.call(lambda c: checked(c.post(f"/models/{entry['id']}/download", json={'source': 'modelscope'})),
                            started, failed)

        self.owner.call(lambda c: checked(c.get('/models')), found,
                        lambda message: (self.reset_download_button(),
                                         self.owner.statusBar().showMessage('读取模型目录失败：' + str(message), 6000)))

    def reset_download_button(self):
        self.get_model.setText('下载 0.6B')
        self.get_model.setEnabled(True)

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

    def save(self, overrides=None):
        value = {**self.settings, 'enabled': self.enabled.isChecked(),
                 'python': self.python.text().strip(), 'model_dir': self.model_dir.text().strip(),
                 'source_model': self.source_model.text().strip(), 'device': self.device.currentData(),
                 'dtype': self.dtype.currentData(), 'max_batch_size': self.size.value(),
                 'min_batch_size': self.minimum_size.value(),
                 'idle_seconds': self.idle_seconds.value(),
                 'predictor_fastpath': self.predictor_fastpath.isChecked(),
                 'predictor_cuda_graph': self.predictor_cuda_graph.isChecked(),
                 'talker_graph': self.talker_graph.isChecked()}
        value.update(overrides or {})
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
