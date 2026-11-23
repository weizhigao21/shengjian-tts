"""Experimental whole-step Talker CUDA graph. Opt-in; validated on RTX 2080 Ti.

The released fast path only intercepts the 15-step sub-codebook predictor. The
Talker's own single-token forward still runs eager, and on a 2080 Ti that is
about 37 ms of the 83 ms a frame costs -- more than the predictor it already
graphs. A CUDA graph needs fixed shapes, and the SDK loop grows a DynamicCache
frame by frame, so this module rebuilds the decode loop around one preallocated
StaticCache: prefill is still eager (prompt length varies), every frame replays
a single captured graph, and sampling stays in eager PyTorch because it consumes
the RNG stream the SDK's token sequence depends on.

Nothing here runs unless the caller opts in. Any unsupported call, model, version
or shape falls straight back to the SDK loop; a capture failure falls back for
that shape only, and a lost RNG position releases the whole worker upstream.
"""
from collections import OrderedDict
from importlib.metadata import version
from time import perf_counter
from types import SimpleNamespace
import traceback

REVISION = 'talker-graph-v1'
SUPPORTED = {'qwen-tts': '0.1.1', 'transformers': '4.57.3'}
TORCH = '2.9.0'
MAX_BATCH = 8
# A static cache is allocated in full before the batch starts, while the SDK's
# dynamic cache only reaches that size at the last frame. 640 MiB keeps the
# upfront hold inside what a batch already ends up using on this card; beyond it
# the graph is not worth risking an out-of-memory batch.
CACHE_BUDGET = 640 * 1024**2
# Same reading as the predictor path: a frame is 0.09 s with room on the card and
# 1.3 s without it, and a graph replay loses to eager execution under paging. A
# downgrade has to replace the whole loop, so it is judged per batch and applied
# to the next one.
SLOW_RATIO = 3.0
# The same two-way reasoning as the predictor path: paging is a property of the
# moment, so the loop is retried after a wait that doubles on every failure, and
# only when the card actually has room for a static cache again.
REARM_SECONDS = 60
REARM_CEILING = 600
REARM_HEADROOM = 1536 * 1024**2
# Without bucketing every batch was its own shape: the cache length is the
# prompt plus the token budget, and both follow the line being read, so the step
# was recaptured for every batch of a job. A capture costs about 1.5 s while an
# 8-line batch only decodes for 2-3 s, so that was half of it. The graph itself
# depends only on (batch, cache length, dtype) -- the mask is rewritten per
# batch and the position is a buffer -- so rounding the cache up to a multiple
# of this lets a whole job share one capture, at the price of a few dozen unused
# cache slots (about 0.2 MiB each at this model size).
CACHE_BUCKET = 32


def cache_bucket(length):
    """Round a static cache length up so neighbouring batches share one graph."""
    return -(-int(length) // CACHE_BUCKET) * CACHE_BUCKET


KEYS = {'inputs_embeds', 'attention_mask', 'trailing_text_hidden', 'tts_pad_embed',
        'max_new_tokens', 'min_new_tokens', 'do_sample', 'top_k', 'top_p', 'temperature',
        'subtalker_dosample', 'subtalker_top_k', 'subtalker_top_p', 'subtalker_temperature',
        'eos_token_id', 'repetition_penalty', 'suppress_tokens',
        'output_hidden_states', 'return_dict_in_generate'}


class GraphUnavailable(RuntimeError):
    """The step could not be captured: this batch must rerun on the SDK loop."""


class GraphStateError(RuntimeError):
    """RNG restoration or CUDA state failed: the worker must exit, not retry."""


class StepGraphs:
    """One captured decode step, kept in a private pool.

    The pool only dies with the graph, so a second shape would hold a second set
    of blocks. One entry is the limit: a new shape evicts the old one, exactly
    like the predictor cache.
    """

    max_entries = 1

    def __init__(self, capture=True):
        self.entries = OrderedDict()
        self.failed = OrderedDict()
        self.captures = self.replays = self.fallbacks = 0
        self.reason = ''
        # Off only for the consistency probe: same buffers, same call, no graph,
        # so a divergence can be blamed on the loop instead of the capture.
        self.capture_enabled = capture

    def reject(self, key, reason):
        self.reason = str(reason)[:500]
        self.failed[key] = self.reason
        while len(self.failed) > 8:
            self.failed.popitem(last=False)

    def acquire(self, key, build, safe_position):
        """Return a replayable step for this shape, or None to stay eager.

        The graph is bound to the buffers and the cache it was captured with,
        so an entry owns both: a later batch of the same shape prefills into
        the same cache and rewrites the mask, and never needs a new capture.
        """
        import torch
        if not self.capture_enabled:
            return EagerReplay(build())
        entry = self.entries.get(key)
        if entry is not None:
            self.entries.move_to_end(key)
            return entry
        if key in self.failed:
            return None
        while len(self.entries) >= self.max_entries:
            self.entries.popitem(last=False)
            empty = getattr(torch.cuda, 'empty_cache', None)
            if empty:
                empty()
        step = build()
        try:
            entry = self.capture(step, safe_position)
            self.captures += 1
        except RuntimeError as exc:
            self.reject(key, '整步图录制失败，保留专用循环：' + str(exc))
            self.fallbacks += 1
            # A poisoned context must go through the worker's exit and native
            # fallback, never be retried with uncertain GPU state.
            torch.cuda.synchronize()
            return None
        self.entries[key] = entry
        self.reason = ''
        return entry

    def capture(self, step, safe_position):
        import torch
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            step.fill(safe_position)
            for _ in range(3):
                step.call()
        torch.cuda.current_stream().wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph, stream=stream):
            hidden, logits = step.call()
        torch.cuda.synchronize()
        return Replay(graph, hidden, logits, step)


class Replay:
    """A captured step plus the buffers it reads and the storage it writes."""

    def __init__(self, graph, hidden, logits, step):
        self.graph, self.hidden, self.logits, self.step = graph, hidden, logits, step

    def __call__(self, embed, position):
        import torch
        self.step.fill(position, embed)
        self.graph.replay()
        return self.hidden.clone(), self.logits.clone()


class EagerReplay:
    """The same step without a graph: only the consistency probe asks for this."""

    def __init__(self, step):
        self.step = step

    def __call__(self, embed, position):
        self.step.fill(position, embed)
        return self.step.call()


class FrameStep:
    """The static buffers one decoded frame reads, and the eager call using them."""

    def __init__(self, talker, cache, buffers):
        self.talker, self.cache, self.buffers = talker, cache, buffers

    def prepare(self, mask, length):
        """Rewrite the whole mask for this batch: padding differs batch to batch."""
        import torch
        buffers = self.buffers
        counts = mask.sum(-1)
        lattice = torch.arange(buffers['mask'].shape[-1], device=mask.device)
        visible = (lattice[None, :] >= (length - counts).unsqueeze(1)) & (lattice[None, :] < length)
        floor = torch.finfo(buffers['mask'].dtype).min
        flat = torch.where(visible,
                           torch.zeros((mask.shape[0], lattice.numel()), dtype=torch.float32, device=mask.device),
                           torch.full((mask.shape[0], lattice.numel()), float(floor), dtype=torch.float32,
                                      device=mask.device))
        buffers['mask'][:, 0, 0, :] = flat.to(buffers['mask'].dtype)

    def fill(self, position, embed=None):
        import torch
        buffers = self.buffers
        if embed is not None:
            buffers['embed'].copy_(embed)
        buffers['position'].fill_(int(position))
        buffers['ids'].copy_((torch.arange(1, device='cuda') + (int(position) + self.talker.rope_deltas))
                             .unsqueeze(0).expand(3, -1, -1))
        buffers['mask'][..., int(position)].fill_(0)

    def call(self):
        state = self.talker.model(inputs_embeds=self.buffers['embed'],
                                  attention_mask=self.buffers['mask'],
                                  position_ids=self.buffers['ids'],
                                  past_key_values=self.cache, use_cache=True,
                                  cache_position=self.buffers['position'],
                                  output_hidden_states=False)
        return state.last_hidden_state, self.talker.codec_head(state.last_hidden_state)


class TalkerPath:
    def __init__(self):
        self.reason = ''
        self.calls = self.fallbacks = 0
        self.frames = 0
        # One fastest pace per batch size. Comparing a fourteen-frame batch with
        # a forty-frame one judged the short line slow, gave up the graph for the
        # rest of the worker, and every later batch then ran at four times the
        # cost -- the exact opposite of what the measurement was meant to protect.
        self.best = {}
        self.step_seconds = 0.0
        self.step_frames = 0
        self.downgraded = False
        self.downgrades = self.streak = self.rearms = 0
        self.retry_at = 0.0
        self.talker = self.original = None
        self.device = None
        self.graphs = None

    def snapshot(self):
        g = self.graphs
        return (self.calls, self.fallbacks, g.captures if g else 0,
                g.replays if g else 0, g.fallbacks if g else 0)

    def status(self, before=(0, 0, 0, 0, 0)):
        calls, fallbacks = self.calls - before[0], self.fallbacks - before[1]
        g = self.graphs
        replays = g.replays - before[3] if g else 0
        mode = 'graph' if replays else 'standard'
        return {'talker_mode': mode, 'talker_revision': REVISION,
                'talker_calls': calls, 'talker_fallback_calls': fallbacks,
                'talker_reason': self.reason,
                'talker_graph_captures': g.captures - before[2] if g else 0,
                'talker_graph_replays': replays,
                'talker_graph_fallbacks': g.fallbacks - before[4] if g else 0,
                'talker_graph_reason': g.reason if g else '',
                'talker_graph_downgraded': self.downgraded,
                'talker_downgrades': self.downgrades,
                'talker_graph_rearms': self.rearms}

    def rearm(self, device=None):
        """Try the whole-step graph again once the wait has passed and the card has room."""
        if not self.downgraded or perf_counter() < self.retry_at or self.graphs is None:
            return False
        try:
            import torch
            free = torch.cuda.mem_get_info(device or self.device)[0]
        except (AttributeError, RuntimeError, TypeError):
            free = None
        if free is None or free < REARM_HEADROOM:
            self.retry_at = perf_counter() + REARM_SECONDS
            return False
        self.downgraded = False
        self.rearms += 1
        self.reason = ''
        return True

    def settle(self):
        """A batch that finished on the graph clears the doubling backoff."""
        if not self.downgraded and self.streak:
            self.streak = 0

    def check(self, kwargs):
        """Everything the loop assumes, checked before a single token is spent."""
        import torch
        p = self.talker
        if self.downgraded or self.graphs is None:
            return '整步图已降级，使用 SDK 原循环。'
        if set(kwargs) - KEYS or not kwargs.get('output_hidden_states') \
                or not kwargs.get('return_dict_in_generate'):
            return '调用参数超出整步图范围，使用 SDK 原循环。'
        embeds, mask = kwargs.get('inputs_embeds'), kwargs.get('attention_mask')
        trailing, pad = kwargs.get('trailing_text_hidden'), kwargs.get('tts_pad_embed')
        if (embeds is None or embeds.ndim != 3 or embeds.device.type != 'cuda'
                or embeds.dtype != torch.float16 or not 1 <= embeds.shape[0] <= MAX_BATCH
                or mask is None or mask.ndim != 2 or mask.shape[0] != embeds.shape[0]
                or mask.shape[1] != embeds.shape[1]
                or trailing is None or trailing.ndim != 3 or pad is None or pad.ndim != 3):
            return '张量形状或精度超出整步图范围，使用 SDK 原循环。'
        if p.training or p.config.num_code_groups != 16:
            return '模型结构不在整步图适配范围，使用 SDK 原循环。'
        sample, temperature, top_k, top_p = (kwargs.get(k) for k in ('do_sample', 'temperature', 'top_k', 'top_p'))
        if (not isinstance(sample, bool) or not isinstance(top_k, int) or top_k < 0
                or not isinstance(temperature, (float, int)) or temperature <= 0
                or not 0 < top_p <= 1 or not isinstance(kwargs.get('max_new_tokens'), int)
                or kwargs['max_new_tokens'] <= 0 or not isinstance(kwargs.get('eos_token_id'), int)):
            return '采样参数超出整步图范围，使用 SDK 原循环。'
        cache_len = embeds.shape[1] + kwargs['max_new_tokens']
        # Judge the hold by what is actually allocated, not by what this batch
        # needs: the cache is rounded up to the bucket the graph is keyed on.
        hold_len = cache_bucket(cache_len)
        per_token = 2 * p.config.num_hidden_layers * p.config.num_key_value_heads * p.config.head_dim * 2
        hold = hold_len * embeds.shape[0] * per_token
        if cache_len > p.config.max_position_embeddings or hold > CACHE_BUDGET:
            return ('静态缓存超出显存预算或位置上限（%d 帧 × %d 条 ≈ %d MiB，预算 %d MiB，'
                    '位置上限 %d），使用 SDK 原循环。'
                    % (cache_len, embeds.shape[0], hold // 1024 ** 2,
                       CACHE_BUDGET // 1024 ** 2, p.config.max_position_embeddings))
        return ''

    def generate(self, **kwargs):
        problem = self.check(kwargs)
        if problem:
            self.fallbacks += 1
            self.reason = problem
            return self.original(**kwargs)
        import torch
        state = torch.cuda.get_rng_state(self.device)
        started = perf_counter()
        try:
            result = self.decode(kwargs)
        except (GraphUnavailable, GraphStateError) as exc:
            if isinstance(exc, GraphStateError):
                raise
            self.fallbacks += 1
            self.reason = '整步图不可用，本批改用 SDK 原循环。'
            self.restore(state)
            return self.original(**kwargs)
        except Exception as exc:
            self.fallbacks += 1
            self.reason = '整步图执行失败，使用 SDK 原循环：' + str(exc)
            # The reason string is what the UI shows; the stack is what turns the
            # next report into a fix, and it lands in the worker log.
            traceback.print_exc()
            if self.graphs:
                self.graphs.reject(self.key_of(kwargs), self.reason)
            self.restore(state)
            return self.original(**kwargs)
        self.calls += 1
        if not self.downgraded:
            self.reason = ''
        frames = self.step_frames or len(codes_of(result))
        seconds = self.step_seconds or (perf_counter() - started)
        self.tally(seconds, frames, kwargs['inputs_embeds'].shape[0])
        return result

    def restore(self, state):
        """A fallback must replay the SDK loop from the RNG position it would have had."""
        import torch
        try:
            torch.cuda.set_rng_state(state, self.device)
        except RuntimeError as exc:
            raise GraphStateError('整步图失败后无法恢复随机数状态') from exc

    def key_of(self, kwargs):
        embeds = kwargs['inputs_embeds']
        # The prompt length is deliberately absent: a shorter line prefills into
        # the same buffers and only the mask differs, and that is rewritten for
        # every batch in `prepare`.
        cache_len = int(embeds.shape[1]) + int(kwargs['max_new_tokens'])
        return (int(embeds.shape[0]), cache_bucket(cache_len), str(embeds.dtype))

    def tally(self, seconds, frames, batch=1):
        """Judge the batch by its decode pace, against batches of its own size.

        ``seconds`` is the frame loop only: the prefill is a fixed cost that a
        short line amortises over very few frames, so including it made a
        fourteen-frame batch look three times slower than a forty-frame one and
        cost the worker its graph.
        """
        if not frames:
            return
        self.frames += frames
        pace = seconds / frames
        size = max(1, min(int(batch or 1), MAX_BATCH))
        best = self.best.get(size)
        if best is None or pace < best:
            self.best[size] = pace
            return
        if pace > best * SLOW_RATIO:
            self.downgrade()

    def downgrade(self):
        self.downgraded = True
        self.downgrades += 1
        self.streak += 1
        wait = min(REARM_SECONDS * 2 ** (self.streak - 1), REARM_CEILING)
        self.retry_at = perf_counter() + wait
        self.reason = (f'整批每帧耗时超过同批量最快批 3 倍，改用 SDK 原循环；'
                       f'{int(wait)} 秒后若显存有余量再试图加速。')

    # -- the loop -----------------------------------------------------------

    def decode(self, kwargs):
        import torch
        from transformers.cache_utils import StaticCache
        talker = self.talker
        embeds, mask = kwargs['inputs_embeds'], kwargs['attention_mask']
        trailing, pad = kwargs['trailing_text_hidden'], kwargs['tts_pad_embed']
        max_new, eos = kwargs['max_new_tokens'], kwargs['eos_token_id']
        batch, length = embeds.shape[0], embeds.shape[1]
        # Allocated at the bucket, so a batch of any length below it replays the
        # same capture; the real length still drives the prefill and its mask.
        cache_len = cache_bucket(length + max_new)
        chain = self.processors(kwargs, embeds.device)
        # The SDK recomputes this on every prefill and keeps it for the decodes;
        # left padding is why a frame's position is not just its index. It has to
        # be in place before the capture, whose warm-up frames already read it.
        delta0 = (1 - mask).sum(dim=-1, keepdim=True)
        position_ids, rope_deltas = talker.get_rope_index(mask)
        talker.rope_deltas = rope_deltas - delta0

        def build():
            cache = StaticCache(config=talker.config, max_cache_len=cache_len)
            return self.frame_step(talker, cache, batch, cache_len, length, mask, embeds.dtype)

        replay = self.graphs.acquire(self.key_of(kwargs), build, length)
        if replay is None:
            # Without the graph this loop is strictly worse than the SDK's, so the
            # batch is rerun on the SDK path rather than finished here.
            raise GraphUnavailable('本形状未能录制整步图')
        step = replay.step
        step.prepare(mask, length)
        cache = step.cache

        cache_position = torch.arange(length, device=embeds.device)
        outputs = talker.model(inputs_embeds=embeds, attention_mask=mask, position_ids=position_ids,
                               past_key_values=cache, use_cache=True, cache_position=cache_position,
                               output_hidden_states=False)
        hidden = outputs.last_hidden_state[:, -1:, :]
        logits = talker.codec_head(hidden)[:, -1, :]
        hiddens, codes = [hidden], []
        generated = torch.zeros((batch, 0), dtype=torch.long, device=embeds.device)
        finished = torch.zeros((batch,), dtype=torch.bool, device=embeds.device)
        # The pace that decides whether this loop keeps its graph is the frame
        # loop's own, never the prefill's: prompt length varies per line and has
        # nothing to do with how the decode performs.
        loop_started = perf_counter()
        for index in range(max_new):
            token = self.sample(logits, generated, chain)
            finished |= token.squeeze(1) == eos
            generated = torch.cat((generated, token), dim=-1)
            if index + 1 == max_new or bool(finished.all()):
                break
            last = talker.get_input_embeddings()(token)
            predicted = talker.code_predictor.generate(
                inputs_embeds=torch.cat((hidden, last), dim=1), max_new_tokens=15,
                do_sample=kwargs['subtalker_dosample'], top_p=kwargs['subtalker_top_p'],
                top_k=kwargs['subtalker_top_k'], temperature=kwargs['subtalker_temperature'],
                output_hidden_states=True, return_dict_in_generate=True)
            codes.append(torch.cat((token, predicted.sequences), dim=-1))
            codec = torch.cat(
                [last] + [talker.code_predictor.get_input_embeddings()[i](predicted.sequences[..., i:i + 1])
                          for i in range(talker.config.num_code_groups - 1)], dim=1)
            frame_embed = codec.sum(1, keepdim=True)
            frame_embed = frame_embed + (trailing[:, index].unsqueeze(1) if index < trailing.shape[1] else pad)
            hidden, logits = replay(frame_embed, length + index)
            logits = logits[:, -1, :]
            self.graphs.replays += 1
            hiddens.append(hidden)
        self.step_seconds = perf_counter() - loop_started
        self.step_frames = len(hiddens) - 1
        return SimpleNamespace(hidden_states=tuple(
            [((hiddens[0],), None)] + [((frame,), code) for frame, code in zip(hiddens[1:], codes)]),
            sequences=generated)

    def frame_step(self, talker, cache, batch, cache_len, length, mask, dtype):
        import torch
        hidden = talker.config.hidden_size
        buffers = {
            'embed': torch.zeros((batch, 1, hidden), dtype=dtype, device='cuda'),
            'position': torch.zeros((1,), dtype=torch.long, device='cuda'),
            'ids': torch.zeros((3, batch, 1), dtype=torch.long, device='cuda'),
            'mask': torch.empty((batch, 1, 1, cache_len), dtype=dtype, device='cuda'),
        }
        step = FrameStep(talker, cache, buffers)
        step.prepare(mask, length)
        return step

    @staticmethod
    def processors(kwargs, device):
        """The SDK's sampling order: processors first, warpers last."""
        import torch
        from transformers.generation.logits_process import (LogitsProcessorList, TopKLogitsWarper,
                                                            TopPLogitsWarper, TemperatureLogitsWarper,
                                                            MinNewTokensLengthLogitsProcessor,
                                                            RepetitionPenaltyLogitsProcessor,
                                                            SuppressTokensLogitsProcessor)
        chain = LogitsProcessorList()
        penalty = kwargs.get('repetition_penalty')
        if penalty and penalty != 1:
            chain.append(RepetitionPenaltyLogitsProcessor(float(penalty)))
        if kwargs.get('min_new_tokens'):
            # The SDK's generate() hands this processor a device tensor; a plain
            # int makes torch.isin reject the comparison on CUDA.
            eos = torch.tensor([kwargs['eos_token_id']], dtype=torch.long, device=device)
            chain.append(MinNewTokensLengthLogitsProcessor(0, int(kwargs['min_new_tokens']), eos))
        if kwargs.get('suppress_tokens'):
            chain.append(SuppressTokensLogitsProcessor([int(i) for i in kwargs['suppress_tokens']],
                                                       device=str(device)))
        if kwargs['do_sample']:
            if kwargs['temperature'] != 1:
                chain.append(TemperatureLogitsWarper(float(kwargs['temperature'])))
            if kwargs['top_k']:
                chain.append(TopKLogitsWarper(int(kwargs['top_k'])))
            if kwargs['top_p'] < 1:
                chain.append(TopPLogitsWarper(float(kwargs['top_p'])))
        return chain

    @staticmethod
    def sample(logits, generated, chain):
        import torch
        scores = chain(generated, logits.to(copy=True, dtype=torch.float32, device=logits.device))
        return torch.multinomial(torch.nn.functional.softmax(scores, dim=-1), num_samples=1)


def codes_of(result):
    return [entry for entry in result.hidden_states if entry[-1] is not None]


def install(model, enabled=True, *, device='cuda', cuda_graph=True, capture=True):
    path = TalkerPath()
    if not enabled:
        path.reason = '整步图加速已关闭。'
        return path
    if device != 'cuda':
        path.reason = '整步图加速仅对 CUDA 启用，CPU 使用 SDK 原循环。'
        return path
    if not cuda_graph:
        path.reason = '整步图加速需要开启 CUDA Graph。'
        return path
    try:
        import torch
        if any(version(name) != expected for name, expected in SUPPORTED.items()):
            path.reason = 'SDK 版本未经过整步图一致性验证，使用原循环。'
            return path
        if torch.__version__.split('+')[0] != TORCH:
            path.reason = f'整步图仅适配 PyTorch {TORCH}，当前 {torch.__version__} 使用原循环。'
            return path
        from transformers.cache_utils import StaticCache  # noqa: F401
        talker = getattr(getattr(model, 'model', None), 'talker', None)
        if (type(talker).__module__ != 'qwen_tts.core.models.modeling_qwen3_tts'
                or type(talker).__name__ != 'Qwen3TTSTalkerForConditionalGeneration'
                or talker.config.num_code_groups != 16 or talker.training):
            path.reason = '模型结构不在整步图适配范围，使用原循环。'
            return path
        path.talker, path.original, path.device = talker, talker.generate, device
        path.graphs = StepGraphs(capture)
        talker.generate = path.generate
    except (AttributeError, ImportError, ValueError, TypeError) as exc:
        path.reason = '整步图加速不可用，使用原循环：' + str(exc)
    return path
