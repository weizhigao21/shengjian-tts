"""Presentation of observed execution, independent of routing preferences."""
from PyQt6.QtCore import Qt
from ...ui import ElidedLabel


def backend_name(item):
    backend = item.get('backend')
    if backend == 'qwen3_batch':
        return f"Qwen 批量 · {item.get('batch_size', 1)} 条"
    return '原生引擎' if backend == 'native' else '引擎未记录'


def execution_details(item):
    lines = [backend_name(item)]
    if item.get('job_id'):
        lines.append('任务 ID：' + item['job_id'])
    if item.get('indices'):
        lines.append('片段：' + '、'.join(map(str, item['indices'])))
    if item.get('cached'):
        lines.append('本次复用成品音频缓存，未执行推理；上方引擎为缓存来源。')
    for key, title in (('fallback_reason', '回退原因'), ('route_reason', '调度原因')):
        if item.get(key):
            lines.append(title + '：' + str(item[key]))
    return '\n'.join(lines)


def record_engine(item):
    metadata = item.get('inference') or {}
    text = ('缓存' if item.get('cached') else
            f"批量 ×{metadata.get('batch_size', 1)}" if metadata.get('backend') == 'qwen3_batch' else
            '原生 · 回退' if metadata.get('fallback_reason') else
            '原生' if metadata.get('backend') == 'native' else
            '—' if item.get('status') in ('queued', 'running', 'draft', 'cancelled') else '未记录')
    return text, execution_details(dict(metadata, cached=item.get('cached', False)))


def status_presentation(health):
    if health is None:
        return '推理状态 · 未连接', '连接本地服务后显示实际使用的引擎。', 'idle'
    batch = health.get('inference') or {}
    benchmark = health.get('inference_benchmark') or {}
    active = health.get('active') or []
    if benchmark.get('busy'):
        text = '速度测试中' + (' · ' + backend_name(benchmark) if benchmark.get('backend') else '')
        return text, benchmark.get('message', '') + f"\n进度：{benchmark.get('completed', 0)}/{benchmark.get('total', 0)}", 'running'
    if health.get('recognition') or health.get('audio_editing'):
        name = '语音识别' if health.get('recognition') else '音频编辑'
        return name + '处理中', '当前引擎用于' + name + '，语音生成等待处理。', 'running'
    if active:
        first = next((a for a in active if a.get('phase') == 'generating'), active[0])
        name = backend_name(first)
        if first.get('backend') == 'qwen3_batch':
            name = f"Qwen 批量 · 本批 {first.get('batch_size', len(first.get('indices', [])) or 1)} 条"
        elif len(active) > 1:
            name += f' · {len(active)} 实例'
        phase = '取消中' if any(a.get('cancelling') for a in active) else '生成中' if first.get('phase') == 'generating' else '准备中'
        fallback = any(a.get('fallback_reason') for a in active)
        tip = '\n\n'.join(execution_details(a) for a in active)
        tip += '\n\n生成中包含本次调用所需的模型加载和音色准备；准备中可能直接命中成品缓存。'
        if first.get('backend') == 'qwen3_batch':
            tip += '\n批量模型：' + ('已确认驻留。' if batch.get('loaded') else '本次调用完成后确认驻留状态。')
        return name + (' · 已回退' if fallback else '') + ' · ' + phase, tip, 'fallback' if fallback else 'running'
    last = health.get('last_execution')
    text = '空闲 · 批量模型驻留' if batch.get('loaded') else '空闲 · 批量已就绪' if batch.get('ready') else '空闲 · 原生引擎'
    tip = '当前没有语音生成正在执行。\n' + ('批量模型已加载，可复用。' if batch.get('loaded') else
        '批量环境已就绪；达到合批条件才会启用。' if batch.get('ready') else '批量不可用：' + batch.get('reason', '未配置'))
    color = 'idle'
    if last:
        result = '缓存命中' if last.get('cached') else {'done': '已完成', 'failed': '失败', 'cancelled': '已取消', 'fallback': '待原生重试'}.get(last.get('status'), '结束')
        text += ' · 上次' + (result if last.get('cached') else '批量' if last.get('backend') == 'qwen3_batch' else '原生')
        if last.get('fallback_reason'):
            text += '回退'
            color = 'fallback'
        tip += '\n\n本次服务最近一次处理（' + result + '）：\n' + execution_details(last)
    return text, tip, color


class InferenceStatus(ElidedLabel):
    def __init__(self):
        super().__init__()
        self.setObjectName('inferenceStatus')
        self.setMinimumWidth(200)
        self.setMaximumWidth(540)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bind(None)

    def bind(self, health):
        text, detail, state = status_presentation(health)
        color = {'idle': '#77777c', 'running': '#3c7a51', 'fallback': '#ad5427'}[state]
        self.setText(text)
        self.setToolTip(detail)
        self.setAccessibleDescription(detail)
        if self.property('state') != state:
            self.setProperty('state', state)
            self.setStyleSheet(f'color: {color}; padding: 3px 0;')
