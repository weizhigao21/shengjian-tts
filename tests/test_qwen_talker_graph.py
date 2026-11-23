"""CPU-only simulation of the whole-step graph's control flow; no CUDA claims here."""
from contextlib import contextmanager
from types import SimpleNamespace
import sys
import pytest

from voice_workbench.batch_inference import qwen_talker_graph as tg
from voice_workbench.batch_inference.schemas import BatchSettings


class Tensor:
    def __init__(self, shape, dtype=None, device='cuda'):
        self.shape, self.dtype = shape, dtype or FLOAT16
        self.device = SimpleNamespace(type=device)
        self.ndim = len(shape)


FLOAT16 = object()


class Stream:
    def wait_stream(self, other):
        pass


class Cuda:
    def __init__(self):
        self.rng = 5
        self.recordings = self.restores = 0

    def get_rng_state(self, device):
        return self.rng

    def set_rng_state(self, value, device):
        self.restores += 1
        self.rng = value

    def current_stream(self, device=None):
        return Stream()

    def Stream(self, device=None):
        return Stream()

    def CUDAGraph(self):
        return SimpleNamespace(replay=lambda: None)

    @contextmanager
    def stream(self, stream):
        yield

    @contextmanager
    def graph(self, graph, stream):
        self.recordings += 1
        yield

    def synchronize(self, device=None):
        pass


@pytest.fixture
def torch_stub(monkeypatch):
    cuda = Cuda()
    stub = SimpleNamespace(__version__='2.9.0+cu128', float16=FLOAT16, cuda=cuda)
    monkeypatch.setitem(sys.modules, 'torch', stub)
    monkeypatch.setitem(sys.modules, 'transformers.cache_utils', SimpleNamespace(StaticCache=lambda **kw: None))
    monkeypatch.setattr(tg, 'version', lambda name: tg.SUPPORTED[name])
    return cuda


def talker(original=None):
    kind = type('Qwen3TTSTalkerForConditionalGeneration', (), {})
    kind.__module__ = 'qwen_tts.core.models.modeling_qwen3_tts'
    config = SimpleNamespace(num_code_groups=16, num_hidden_layers=28, num_key_value_heads=8,
                             head_dim=128, max_position_embeddings=32768)
    instance = kind()
    instance.training = False
    instance.config = config
    instance.generate = original
    return instance


def model_with(talker_instance):
    return SimpleNamespace(model=SimpleNamespace(talker=talker_instance))


def good_kwargs(batch=2, length=10, new=40):
    return {'inputs_embeds': Tensor((batch, length, 2048)), 'attention_mask': Tensor((batch, length), device='cuda'),
            'trailing_text_hidden': Tensor((batch, 12, 2048)), 'tts_pad_embed': Tensor((1, 1, 2048)),
            'max_new_tokens': new, 'min_new_tokens': 2, 'do_sample': True, 'top_k': 50, 'top_p': 1.0,
            'temperature': 0.9, 'subtalker_dosample': True, 'subtalker_top_k': 50, 'subtalker_top_p': 1.0,
            'subtalker_temperature': 0.9, 'eos_token_id': 2150, 'repetition_penalty': 1.05,
            'suppress_tokens': [2048], 'output_hidden_states': True, 'return_dict_in_generate': True}


def test_off_by_default_leaves_the_sdk_loop_untouched(torch_stub):
    original = lambda **kw: 'sdk'
    instance = talker(original)
    assert BatchSettings().talker_graph is False
    path = tg.install(model_with(instance), False)
    assert path.graphs is None and instance.generate is original and '关闭' in path.reason


@pytest.mark.parametrize('device,graph', [('cpu', True), ('cuda', False)])
def test_device_and_switch_gating(torch_stub, device, graph):
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device=device, cuda_graph=graph)
    assert instance.generate is not path.generate and path.graphs is None


def test_install_patches_and_reports_revision(torch_stub):
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device='cuda')
    assert instance.generate == path.generate and path.graphs is not None
    status = path.status()
    assert status['talker_mode'] == 'standard' and status['talker_revision'] == tg.REVISION
    assert status['talker_reason'] == '' and status['talker_graph_reason'] == ''


def test_unverified_sdk_or_runtime_never_patches(torch_stub, monkeypatch):
    original = lambda **kw: 'sdk'
    instance = talker(original)
    monkeypatch.setattr(tg, 'version', lambda name: '0.1.2')
    path = tg.install(model_with(instance), True, device='cuda')
    assert instance.generate is original and '一致性验证' in path.reason
    monkeypatch.setattr(tg, 'version', lambda name: tg.SUPPORTED[name])
    sys.modules['torch'].__version__ = '2.10.0+cu128'
    path = tg.install(model_with(instance), True, device='cuda')
    assert '2.9.0' in path.reason and instance.generate is original


@pytest.mark.parametrize('mutate,needle', [
    (lambda kw: kw.update(extra=1), '超出整步图范围'),
    (lambda kw: kw.update(output_hidden_states=False), '超出整步图范围'),
    (lambda kw: kw.update(inputs_embeds=Tensor((9, 10, 2048))), '形状或精度'),
    (lambda kw: kw.update(inputs_embeds=Tensor((2, 10, 2048), device='cpu')), '形状或精度'),
    (lambda kw: kw.update(inputs_embeds=Tensor((2, 10, 4096), dtype=object())), '形状或精度'),
    (lambda kw: kw.update(top_p=1.2), '采样参数'),
    (lambda kw: kw.update(max_new_tokens=0), '采样参数'),
    (lambda kw: kw.update(max_new_tokens=32760), '显存预算'),
    (lambda kw: kw.update(inputs_embeds=Tensor((8, 10, 2048)), attention_mask=Tensor((8, 10)),
                          max_new_tokens=2000), '显存预算'),
])
def test_unsupported_calls_fall_back_before_spending_a_token(torch_stub, mutate, needle):
    original = lambda **kw: 'sdk'
    instance = talker(original)
    path = tg.install(model_with(instance), True, device='cuda')
    kwargs = good_kwargs()
    mutate(kwargs)
    assert path.generate(**kwargs) == 'sdk'
    assert path.fallbacks == 1 and needle in path.reason
    assert path.graphs.captures == 0


def test_unavailable_graph_restores_rng_and_reruns_the_sdk_loop(torch_stub, monkeypatch):
    calls = []

    def original(**kw):
        calls.append(kw)
        return 'sdk'

    instance = talker(original)
    path = tg.install(model_with(instance), True, device='cuda')
    path.graphs.capture_enabled = False

    def unavailable(kwargs):
        raise tg.GraphUnavailable('本形状未能录制整步图')

    monkeypatch.setattr(path, 'decode', unavailable)
    torch_stub.rng = 9
    assert path.generate(**good_kwargs()) == 'sdk'
    assert calls and torch_stub.restores == 1 and path.fallbacks == 1
    assert '本批改用 SDK 原循环' in path.reason


def test_a_lost_rng_position_escapes_instead_of_retrying(torch_stub, monkeypatch):
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device='cuda')
    monkeypatch.setattr(path, 'decode', lambda kw: (_ for _ in ()).throw(tg.GraphStateError('boom')))
    with pytest.raises(tg.GraphStateError, match='boom'):
        path.generate(**good_kwargs())


def test_a_slow_batch_downgrades_the_following_ones(torch_stub):
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device='cuda')
    path.tally(1.0, 10)
    path.tally(1.0, 10)
    assert not path.downgraded
    path.tally(9.0, 10)
    assert path.downgraded and '3 倍' in path.reason
    assert path.graphs.captures == 0


def test_graph_entries_own_the_cache_and_are_reused_for_the_same_shape(torch_stub, monkeypatch):
    graphs = tg.StepGraphs()
    builds = []

    def build():
        builds.append(1)
        return SimpleNamespace(cache='cache-%d' % len(builds))

    def capture(step, position):
        return tg.Replay(None, None, None, step)

    monkeypatch.setattr(graphs, 'capture', capture)
    key = (8, 12, 60, 'torch.float16')
    first = graphs.acquire(key, build, 12)
    assert first.step.cache == 'cache-1' and graphs.captures == 1
    # A later batch of the same shape must reuse the captured buffers and their
    # cache: a fresh cache here would leave the replay reading the previous batch.
    again = graphs.acquire(key, build, 12)
    assert again is first and len(builds) == 1 and graphs.captures == 1
    other = graphs.acquire((4, 12, 60, 'torch.float16'), build, 12)
    assert other.step.cache == 'cache-2' and len(builds) == 2 and graphs.captures == 2
    assert len(graphs.entries) == 1


def test_batches_of_different_line_length_share_one_capture(torch_stub):
    """A job's batches differ in prompt length; that alone must not recapture.

    Recapturing per batch cost about 1.5 s on an 8-line batch that only decoded
    for 2-3 s. The graph depends on (batch, cache length, dtype); the prompt goes
    into the same buffers and the mask is rewritten for every batch.
    """
    path = tg.install(model_with(talker(lambda **kw: 'sdk')), True, device='cuda')
    short = path.key_of(good_kwargs(batch=8, length=10, new=40))
    longer = path.key_of(good_kwargs(batch=8, length=18, new=40))
    assert short == longer and short[1] == 64
    # Crossing a bucket is a genuinely different cache shape.
    beyond = path.key_of(good_kwargs(batch=8, length=30, new=40))
    assert beyond != short and beyond[1] == 96
    # Batch size still separates shapes: a batch of 3 cannot replay a batch of 8.
    assert path.key_of(good_kwargs(batch=3, length=10, new=40)) != short


def test_the_cache_bucket_never_shrinks_a_batch(torch_stub):
    for length in (1, 31, 32, 33, 63, 64, 65, 200, 257):
        assert tg.cache_bucket(length) >= length
        assert tg.cache_bucket(length) - length < tg.CACHE_BUCKET


def test_failed_shapes_are_bounded_and_never_recaptured(torch_stub, monkeypatch):
    graphs = tg.StepGraphs()
    monkeypatch.setattr(graphs, 'capture', lambda step, pos: (_ for _ in ()).throw(RuntimeError('no capture')))
    assert graphs.acquire((1, 4, 8, 'f16'), lambda: None, 4) is None
    assert graphs.fallbacks == 1 and 'no capture' in graphs.reason
    assert graphs.acquire((1, 4, 8, 'f16'), lambda: None, 4) is None and graphs.fallbacks == 1
    for index in range(20):
        graphs.reject(index, 'test')
    assert len(graphs.failed) == 8


def test_eager_step_takes_the_same_buffers_without_a_graph(torch_stub):
    graphs = tg.StepGraphs(capture=False)
    calls = []

    def call():
        calls.append(1)
        return 'hidden', 'logits'

    step = SimpleNamespace(fill=lambda position, embed: calls.append(position), call=call)
    replay = graphs.acquire((1, 1, 1, 'f16'), lambda: step, 1)
    assert replay(embed=None, position=7) == ('hidden', 'logits')
    assert calls == [7, 1] and graphs.captures == 0


def test_the_batch_pace_ignores_the_prefill(torch_stub, monkeypatch):
    """Only the frame loop may decide whether the graph stays.

    The prefill is a fixed cost: a fourteen-frame batch spreads it over far fewer
    frames than a forty-frame one, so counting it made short lines look slow.
    """
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device='cuda')
    clock = [0.0]

    def decode(kw):
        clock[0] += 0.80                      # long prompt
        path.step_seconds, path.step_frames = 0.20, 10
        return SimpleNamespace()

    monkeypatch.setattr(path, 'decode', decode)
    monkeypatch.setattr(tg, 'perf_counter', lambda: clock[0])
    path.generate(**good_kwargs(batch=1))
    assert path.best[1] == pytest.approx(0.02)      # 0.20/10, not 1.00/10


def test_a_downgrade_is_retried_when_the_card_has_room(torch_stub, monkeypatch):
    free = [1024**3]
    monkeypatch.setattr(torch_stub, 'mem_get_info', lambda device: (free[0], 11 * 1024**3),
                        raising=False)
    instance = talker(lambda **kw: 'sdk')
    path = tg.install(model_with(instance), True, device='cuda')
    clock = [0.0]
    monkeypatch.setattr(tg, 'perf_counter', lambda: clock[0])

    path.downgrade()
    assert path.downgraded and 'SDK 原循环' in path.reason
    assert path.rearm() is False                      # not yet due
    clock[0] += tg.REARM_SECONDS + 1
    assert path.rearm() is False and path.downgraded  # due, card still full
    clock[0] += tg.REARM_SECONDS + 1
    free[0] = 4 * 1024**3
    assert path.rearm() is True and not path.downgraded
    assert path.status()['talker_graph_rearms'] == 1
    assert path.status()['talker_downgrades'] == 1
