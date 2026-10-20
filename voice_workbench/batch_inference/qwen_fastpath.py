"""Optional fixed-count residual-code generation for a verified Qwen SDK contract.

Only the private two-embedding call inside Qwen's Talker is intercepted. The
model forward, KV cache, logits warpers and RNG sampling remain unchanged. This
module is also shipped as source beside the standalone worker; no SDK is edited.
"""
from importlib.metadata import version
from copy import deepcopy
from types import SimpleNamespace

REVISION = 'fixed-code-v2-graph-experimental'
SUPPORTED = {'qwen-tts': '0.1.1', 'transformers': '4.57.3'}
CALL_KEYS = {'inputs_embeds', 'max_new_tokens', 'do_sample', 'top_p', 'top_k',
             'temperature', 'output_hidden_states', 'return_dict_in_generate'}
CONFIG_KEYS = {'max_length', 'max_new_tokens', 'do_sample', 'temperature', 'top_k', 'top_p',
               'output_hidden_states', 'return_dict_in_generate', '_from_model_config', 'transformers_version'}


class PredictorPath:
    def __init__(self, reason=''):
        self.reason = reason
        self.calls = self.fallbacks = 0
        self.predictor = self.original = None
        self.graphs = None
        # One call renders one frame. The worker reports each one so the parent can
        # tell a slow batch from a wedged one instead of timing the whole batch.
        self.progress = None

    def snapshot(self):
        return (self.calls, self.fallbacks, self.graphs.captures if self.graphs else 0,
                self.graphs.replays if self.graphs else 0, self.graphs.fallbacks if self.graphs else 0)

    def status(self, before=(0, 0, 0, 0, 0)):
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
                'predictor_graph_reason': self.graphs.reason if self.graphs else ''}

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
        forward = lambda value: self.loop(value, sample)
        output = self.graphs.run(embeds, key, forward) if self.graphs else forward(embeds)
        if self.progress:
            self.progress()
        return SimpleNamespace(sequences=output)

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
