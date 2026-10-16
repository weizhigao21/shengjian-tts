"""Live cold/warm backend comparisons; report copying without creating works."""
import json
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QProgressBar, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from ...ui import button, label
from ..api_client import checked


class BenchmarkControls(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.busy = self.loading = False
        self.result = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(label('速度对比测试', 'section'))
        hint = label('用同一音色和 4 条短句对比原生单实例逐条生成与批量生成。首组包含进程启动和模型加载，后续测试热模型速度；不读取成品缓存，也不加入作品。测试会占用推理引擎，结束后释放。', 'muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.voice = QComboBox()
        self.voice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.workload = QComboBox()
        for title, key in (('中英双语 · 4 条', 'bilingual'), ('中文 · 4 条', 'zh'), ('英文 · 4 条', 'en')):
            self.workload.addItem(title, key)
        self.rounds = QSpinBox()
        self.rounds.setRange(1, 3); self.rounds.setValue(1); self.rounds.setSuffix(' 组')
        form.addRow('测试音色', self.voice)
        form.addRow('测试文本', self.workload)
        form.addRow('热模型重复', self.rounds)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.start_button = button('开始测速', self.start, 'play')
        self.cancel_button = button('取消测试', self.cancel, 'close', 'ghost')
        self.cancel_button.hide()
        self.copy_button = button('复制报告', self.copy, 'copy', 'ghost')
        self.copy_button.setEnabled(False)
        row.addWidget(self.start_button); row.addWidget(self.cancel_button); row.addWidget(self.copy_button); row.addStretch()
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = label('完成批量环境配置后即可测试。', 'hint')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['后端', '首组 / 秒', '热模型 / 秒', '音频 / 秒', 'RTF', '状态'])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(112)
        self.table.hide()
        layout.addWidget(self.table)
        self.summary = label('', 'hint')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = label('热模型用时、音频时长均按每组统计。RTF 越小越快；两种模型的量化与采样实现不同，音频时长和质量可能不同。固定种子 1234，每条最多 256 tokens；“冷启动”指新进程，不清空系统文件缓存。', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(panel.refresh)

    def bind(self, data):
        previous = self.voice.currentData()
        self.voice.blockSignals(True)
        self.voice.clear()
        for voice in data['voices']:
            self.voice.addItem(voice['name'], voice['id'])
        if self.voice.count(): self.voice.setCurrentIndex(max(0, self.voice.findData(previous)))
        else: self.voice.addItem('请先保存关联模型的声音预设', '')
        self.voice.blockSignals(False)
        self.busy = data['busy']
        self.result = data.get('result')
        self.start_button.setEnabled(bool(data['ready'] and self.voice.currentData() and not self.busy and not self.loading))
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(data['status'] != 'cancelling')
        self.copy_button.setEnabled(bool(self.result))
        for control in (self.voice, self.workload, self.rounds): control.setEnabled(not self.busy)
        self.progress.setVisible(self.busy)
        self.progress.setRange(0, max(1, data.get('total', 0)))
        self.progress.setValue(data.get('completed', 0))
        self.status.setText(data.get('message') or data.get('reason') or '已就绪，可以开始速度测试。')
        self.render_result()
        self.panel.enabled.setEnabled(not self.busy)
        if self.busy:
            for control in (self.panel.manual, self.panel.apply, self.panel.automatic): control.setEnabled(False)
            self.timer.start()
        else:
            self.panel.automatic.setEnabled(True)
            self.timer.stop()

    def render_result(self):
        rows = (self.result or {}).get('results', [])
        self.table.setVisible(bool(rows))
        self.table.setRowCount(len(rows))
        for index, result in enumerate(rows):
            cold, warm = result.get('cold', {}), result.get('warm', {})
            number = lambda v: f'{v:.2f}' if v is not None else '—'
            state = {'done': '完成', 'failed': '失败', 'cancelled': '取消', 'running': '测试中'}.get(result['status'], result['status'])
            values = ['原生 GGUF' if result['backend'] == 'native' else 'Qwen 批量', number(cold.get('seconds')),
                      number(warm.get('seconds')), number(warm.get('audio_seconds')), number(warm.get('rtf')), state]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(result.get('error', {}).get('message', text))
                self.table.setItem(index, column, item)
        speed = (self.result or {}).get('speedup')
        total = (self.result or {}).get('total_seconds')
        text = f'测试总耗时 {total:.2f} 秒' if total is not None else ''
        if speed:
            text += f" · 首组倍率 {speed['cold']:.2f}× · 热模型倍率 {speed['warm']:.2f}×（大于 1 表示批量更快）"
        self.summary.setText(text.lstrip(' ·'))

    def start(self):
        if self.loading or self.busy or not self.voice.currentData(): return
        self.loading = True
        self.start_button.setEnabled(False)
        payload = {'voice_id': self.voice.currentData(), 'workload_name': self.workload.currentData(),
                   'warm_rounds': self.rounds.value()}
        def done(_):
            self.loading = False
            self.panel.refresh()
        def failed(message):
            self.loading = False
            self.start_button.setEnabled(True)
            self.status.setText(message)
        self.panel.owner.call(lambda c: checked(c.post('/inference/benchmark', json=payload)), done, failed)

    def cancel(self):
        self.cancel_button.setEnabled(False)
        self.panel.owner.call(lambda c: checked(c.post('/inference/benchmark/cancel')),
                              lambda _: self.panel.refresh(), lambda text: self.status.setText(text))

    def copy(self):
        if self.result:
            QApplication.clipboard().setText(json.dumps(self.result, ensure_ascii=False, indent=2))
