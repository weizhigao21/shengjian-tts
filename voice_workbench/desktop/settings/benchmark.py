"""Live cold/warm backend comparisons; report copying without creating works."""
import json
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QHeaderView, QProgressBar, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from ...ui import button, label
from ..api_client import checked
from .common import settings_group, settings_row

NOTE = ('热模型用时、音频时长按每组统计，每条平均 = 热模型整组耗时 ÷ 每组条数，不代表单条延迟。'
        'RTF 越小越快；两种模型的量化与采样实现不同，音频时长和质量可能不同。'
        '固定种子 1234，每条最多 256 tokens；"冷启动"指新进程，不清空系统文件缓存。')


class BenchmarkControls(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.busy = self.loading = False
        self.result = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(label('速度对比测试', 'section'))
        self.voice = QComboBox()
        self.voice.setFixedWidth(260)
        self.voice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.workload = QComboBox()
        self.workload.setFixedWidth(220)
        for title, key in (('中英双语 · 20 条', 'bilingual'), ('中文 · 20 条', 'zh'), ('英文 · 20 条', 'en')):
            self.workload.addItem(title, key)
        self.rounds = QSpinBox()
        self.rounds.setFixedWidth(220)
        self.rounds.setRange(1, 3)
        self.rounds.setValue(1)
        self.rounds.setSuffix(' 组')
        self.rounds.setToolTip('热模型重复的轮数；多跑一轮会摊薄首组的冷启动开销。')
        layout.addWidget(settings_group([
            settings_row('测试音色', '同一音色、同一文本，只换后端。', (self.voice,)),
            settings_row('测试文本', '每组 20 条短句，共生成 80 段测试音频。', (self.workload,)),
            settings_row('热模型重复', '重复轮数越多，热模型均值越稳。', (self.rounds,)),
        ]))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.start_button = button('开始测速', self.start, 'play')
        self.cancel_button = button('取消测试', self.cancel, 'close', 'ghost')
        self.cancel_button.hide()
        self.copy_button = button('复制报告', self.copy, 'copy', 'ghost')
        self.copy_button.setEnabled(False)
        row.addWidget(self.start_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.copy_button)
        row.addStretch()
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setObjectName('benchmarkProgress')
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setToolTip('整体进度：已完成 / 计划生成的测试音频条数，含原生与批量两个后端')
        self.progress.hide()
        self.progress_text = label('', 'hint')
        self.progress_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.progress_text.setMinimumWidth(self.progress_text.fontMetrics().horizontalAdvance('100 / 100 条 · 100%') + 6)
        self.progress_text.setToolTip('整体进度：已完成 / 计划生成的测试音频条数')
        self.progress_text.hide()
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_text)
        layout.addLayout(progress_row)
        self.status = label('完成批量环境配置后即可测试。', 'hint')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['后端', '首组 / 秒', '热模型 / 秒', '每条 / 秒', '音频 / 秒', 'RTF', '状态'])
        self.table.horizontalHeaderItem(3).setToolTip('热模型每条平均耗时 = 总耗时 / 总条数')
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
        note = label('RTF = 生成耗时 ÷ 音频时长，越小越快；「首组」含进程启动和模型加载。', 'muted')
        note.setToolTip(NOTE)
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
        total, completed = max(0, data.get('total', 0)), max(0, data.get('completed', 0))
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(completed, total) if total else 0)
        self.progress.setVisible(self.busy)
        self.progress_text.setVisible(self.busy)
        self.progress_text.setText(f'{completed} / {total} 条 · {round(completed * 100 / total) if total else 0}%' if total else '')
        self.status.setText(data.get('message') or data.get('reason') or '已就绪，可以开始速度测试。')
        self.render_result()
        self.panel.enabled.setEnabled(not self.busy)
        # A finished run must restore manual configuration as well; previously only
        # the automatic section was re-enabled, so batch limits stayed read-only.
        # Installing a runtime locks the same fields, so both states share one lock.
        locked = self.busy or getattr(self.panel.automatic, 'busy', False)
        for control in (self.panel.manual, self.panel.apply, self.panel.automatic):
            control.setEnabled(not locked)
        if self.busy:
            self.timer.start()
        else:
            self.timer.stop()

    def render_result(self):
        rows = (self.result or {}).get('results', [])
        count = (self.result or {}).get('sample_count') or len((self.result or {}).get('workload', []))
        self.table.setVisible(bool(rows))
        self.table.setRowCount(len(rows))
        for index, result in enumerate(rows):
            cold, warm = result.get('cold', {}), result.get('warm', {})
            # Reports saved before v0.7.47 have four rows and no per-item metric.
            mean = warm.get('seconds_per_item')
            if mean is None and count and warm.get('seconds') is not None:
                mean = warm['seconds']/count
            number = lambda v: f'{v:.2f}' if v is not None else '—'
            state = {'done': '完成', 'failed': '失败', 'cancelled': '取消', 'running': '测试中'}.get(result['status'], result['status'])
            values = ['原生 GGUF' if result['backend'] == 'native' else 'Qwen 批量', number(cold.get('seconds')),
                      number(warm.get('seconds')), number(mean), number(warm.get('audio_seconds')), number(warm.get('rtf')), state]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(result.get('error', {}).get('message', text))
                self.table.setItem(index, column, item)
        speed = (self.result or {}).get('speedup')
        total = (self.result or {}).get('total_seconds')
        languages = {row['language'] for row in (self.result or {}).get('workload', [])}
        kind = {frozenset({'zh'}): '中文', frozenset({'en'}): '英文',
                frozenset({'zh', 'en'}): '中英双语'}.get(frozenset(languages), '自定义')
        text = f'报告：{kind} · 每组 {count} 条' if count else ''
        size = (self.result or {}).get('max_batch_size')
        if size: text += f' · 每批最多 {size} 条'
        if total is not None: text += f' · 测试总耗时 {total:.2f} 秒'
        if speed:
            text += f" · 首组倍率 {speed['cold']:.2f}× · 热模型倍率 {speed['warm']:.2f}×（大于 1 表示批量更快）"
        ignored = (self.result or {}).get('ignored_options') or []
        self.summary.setToolTip('不参与对比的参数：' + '、'.join(ignored) if ignored else '')
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
