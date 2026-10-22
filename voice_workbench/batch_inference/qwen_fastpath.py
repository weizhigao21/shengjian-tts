"""Optional fixed-count residual-code generation for a verified Qwen SDK contract.

Only the private two-embedding call inside Qwen's Talker is intercepted. The
model forward, KV cache, logits warpers and RNG sampling remain unchanged. This
module is also shipped as source beside the standalone worker; no SDK is edited.
"""
from importlib.metadata import version
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace

REVISION = 'fixed-code-v2-graph-experimental'
SUPPORTED = {'qwen-tts': '0.1.1', 'transformers': '4.57.3'}
CALL_KEYS = {'inputs_embeds', 'max_new_tokens', 'do_sample', 'top_p', 'top_k',
             'temperature', 'output_hidden_states', 'return_dict_in_generate'}
CONFIG_KEYS = {'max_length', 'max_new_tokens', 'do_sample', 'temperature', 'top_k', 'top_p',
               'output_hidden_states', 'return_dict_in_generate', '_from_model_config', 'transformers_version'}
# One frame is about 0.09 s when the card has room and 1.3 s when it does not, so
# this is roughly once a second: often enough to stop the drift, rare enough that
# the check itself never shows up in the frame time.
FRAMES_PER_RELIEF = 12
# Same graph, same batch size, one job: 0.118 s/frame, then four batches at
# 1.24-1.56 s/frame. Nothing was captured or evicted in between -- the card had
# drifted to its VRAM limit (other programs on it move gigabytes in and out), so
# every frame round-tripped through WDDM paging. Evicting the pools helps only
# when there is something to evict; when the paging is real, replaying a graph is
# strictly worse than the plain loop. So watch the pace and give the graph up.
SLOW_RATIO = 3.0
SLOW_FRAMES = 6


class PredictorPath:
    def __init__(self, reason=''):
        self.reason = reason
        self.calls = self.fallbacks = 0
        self.predictor = self.original = None
        self.graphs = None
        self.device = None
        self.baseline = None
        self.slow = 0
        self.last = None
        self.downgraded = False
        # One call renders one frame. The worker reports each one so the parent can
        # tell a slow batch from a wedged one instead of timing the whole batch.
        self.progress = None

    def snapshot(self):
        return (self.calls, self.fallbacks, self.graphs.captures if self.graphs else 0,
                self.graphs.replays if self.graphs else 0, self.graphs.fallbacks if self.graphs else 0,
                self.graphs.reliefs if self.graphs else 0, self.graphs.lights if self.graphs else 0)

    def status(self, before=(0, 0, 0, 0, 0, 0, 0)):
        calls, fallbacks = self.calls-before[0], self.fallbacks-before[1]
        graph_calls = self.graphs.replays-before[3] if self.graphs else 0
        mode = ('mixed' if calls and fallbacks else 'graph' if calls and graph_calls == calls else
                'mixed' if graph_calls else 'fixed' if calls else 'standard')
        return {'predictor_mode': mode,
                'predictor_revision': REVISION, 'predictor_calls': calls,
                'predictor_fallback_calls': fallbacks, 'predictor_reason': self.reason,
                'predictor_graph_captures': self.graphs.captures-before[2] if self.graphs else 0,
                'predictor_graph_replays': graph_calls,
                'predictor_graph_fallbacks': self.graphs.fallbacks-before[4] if self.graphs else 0,
                'predictor_graph_reliefs': self.graphs.reliefs-before[5] if self.graphs else 0,
                'predictor_graph_lights': self.graphs.lights-before[6] if self.graphs else 0,
                'predictor_graph_downgraded': self.downgraded,
                'predictor_graph_reason': self.graphs.reason if self.graphs else ''}

    def tidy(self):
        """Hand pooled VRAM back before a batch starts. Never called per frame."""
        if not self.graphs or self.downgraded or self.device is None:
            return False
        return self.graphs.relief(self.device)

    def generate(self, *args, **kwargs):
        p = self.predictor
        embeds = kwargs.get('inputs_embeds')
        if (args or set(kwargs)-CALL_KEYS or kwargs.get('max_new_tokens') != 15
                or not kwargs.get('return_dict_in_generate') or not kwargs.get('output_hidden_states')
                or embeds is None or embeds.ndim != 3 or embeds.shape[1] != 2
                or p.training or p.generation_config is not self.config_object
                or vars(p.generation_config) != self.config_snapshot):
            self.fallbacks += 1
            self.reason = '当前调用或生成配置超出已验证范围，使用 SDK 原循环。'
            return self.original(*args, **kwargs)
        sample, temperature, top_k, top_p = [kwargs.get(k) if kwargs.get(k) is not None else getattr(p.generation_config, k)
                                          for k in ('do_sample', 'temperature', 'top_k', 'top_p')]
        if (not isinstance(sample, bool) or not isinstance(top_k, int) or top_k < 0
                or not isinstance(temperature, (float, int)) or temperature <= 0
                or not isinstance(top_p, (float, int)) or not 0 < top_p <= 1):
            self.fallbacks += 1
            self.reason = '采样参数超出专用循环范围，使用 SDK 原循环。'
            return self.original(*args, **kwargs)
        # Warpers have no token-history dependence; reuse their setup across frames.
        key = sample, temperature, top_k, top_p
        if self.warper_key != key:
            from transformers.generation.logits_process import (LogitsProcessorList, TemperatureLogitsWarper,
                                                                TopKLogitsWarper, TopPLogitsWarper)
            processors = LogitsProcessorList()
            if sample:
                if temperature != 1: processors.append(TemperatureLogitsWarper(temperature))
                if top_k: processors.append(TopKLogitsWarper(top_k))
                if top_p < 1: processors.append(TopPLogitsWarper(top_p))
            self.warper_key, self.processors = key, processors
        self.reason = ''
        self.calls += 1
        self.watch()
        forward = lambda value: self.loop(value, sample)
        use_graph = self.graphs is not None and not self.downgraded
        output = self.graphs.run(embeds, key, forward) if use_graph else forward(embeds)
        if use_graph and self.calls % FRAMES_PER_RELIEF == 0:
            # A batch fills the card as it renders, so reclaiming only between
            # batches left the last batches of a run three times slower.
            self.graphs.light(self.device)
        self.last = perf_counter()
        if self.progress:
            self.progress()
        return SimpleNamespace(sequences=output)

    def watch(self):
        """Drop the graph for the rest of this worker when frames crawl.

        The graph wins 0.09 s over 0.15 s per frame while the card has room and
        loses badly once it does not: the same replay costs 1.3 s/frame while the
        plain loop stays near 0.43 s under the same pressure. Trading 1.7x for
        never losing 14x is the right way round, so a sustained slowdown keeps the
        plain loop for this worker instead of re-judging every frame.
        """
        if self.last is None or self.graphs is None or self.downgraded:
            return
        gap = perf_counter() - self.last
        if not gap:
            return
        if self.baseline is None or gap < self.baseline:
            self.baseline, self.slow = gap, 0
            return
        if gap <= self.baseline * SLOW_RATIO:
            self.slow = 0
            return
        self.slow += 1
        if self.slow >= SLOW_FRAMES:
            self.downgraded = True
            self.reason = '逐帧耗时超过最快帧 3 倍，本次运行改用专用循环以避开显存分页。'

    def loop(self, embeds, sample):
        import torch
        p = self.predictor
        tokens, state = [], None
        for step in range(15):
            state = p(inputs_embeds=embeds if step == 0 else None,
                      input_ids=None if step == 0 else tokens[-1],
                      past_key_values=None if step == 0 else state.past_key_values,
                      generation_steps=step, use_cache=True, output_hidden_states=False)
            scores = state.logits[:, -1, :].to(copy=True, dtype=torch.float32, device=embeds.device)
            scores = self.processors(None, scores)
            token = (torch.multinomial(torch.nn.functional.softmax(scores, dim=-1), num_samples=1)
                     if sample else torch.argmax(scores, dim=-1, keepdim=True))
            tokens.append(token)
        return torch.cat(tokens, dim=-1)


def install(model, enabled=True, *, device='cuda', cuda_graph=False):
    path = PredictorPath()
    if not enabled:
        path.reason = '编码预测加速已关闭。'
        return path
    if device != 'cuda':
        path.reason = '编码预测加速仅对 CUDA 启用，CPU 使用 SDK 原循环。'
        return path
    try:
        if any(version(name) != expected for name, expected in SUPPORTED.items()):
            path.reason = 'SDK 版本未经过编码一致性验证，使用原循环。'
            return path
        p = model.model.talker.code_predictor
        if (type(p).__module__ != 'qwen_tts.core.models.modeling_qwen3_tts'
                or type(p).__name__ != 'Qwen3TTSTalkerCodePredictorModelForConditionalGeneration'
                or p.config.num_code_groups != 16 or p.training):
            path.reason = '模型结构不在编码预测加速适配范围，使用原循环。'
            return path
        from transformers import GenerationConfig
        snapshot, defaults = p.generation_config.to_dict(), GenerationConfig().to_dict()
        if any(value != defaults.get(key) for key, value in snapshot.items() if key not in CONFIG_KEYS):
            path.reason = '模型含其他生成约束，使用 SDK 原循环。'
            return path
        path.predictor, path.original = p, p.generate
        path.device = device
        path.config_object, path.config_snapshot = p.generation_config, deepcopy(vars(p.generation_config))
        path.warper_key, path.processors = None, None
        if cuda_graph:
            try:
                from .qwen_cuda_graph import PredictorGraphs
            except ImportError:
                from qwen_cuda_graph import PredictorGraphs
            path.graphs = PredictorGraphs()
        p.generate = path.generate
    except (AttributeError, ImportError, ValueError, TypeError) as exc:
        path.reason = '编码预测加速不可用，使用原循环：' + str(exc)
    return path
