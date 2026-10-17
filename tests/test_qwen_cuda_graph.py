"""CPU-only graph lifecycle simulation; it does not claim CUDA correctness/speed."""
from contextlib import contextmanager
from types import SimpleNamespace
import sys
import pytest

from voice_workbench.batch_inference.qwen_cuda_graph import GraphStateError, PredictorGraphs
from voice_workbench.batch_inference.schemas import BatchSettings
from voice_workbench.batch_inference import qwen_fastpath
from voice_workbench.desktop.components.inference_status import execution_details
from test_qwen_fastpath import predictor
from test_batch_inference import batch_manager
from test_service import wait_job


class Tensor:
    def __init__(self, value, shape=(8, 2, 4), dtype='float16', device=None):
        self.value, self.shape, self.dtype = value, shape, dtype
        self.device = device or SimpleNamespace(type='cuda', index=0)
    def stride(self): return (8, 4, 1)
    def clone(self): return Tensor(self.value, self.shape, self.dtype, self.device)
    def copy_(self, other): self.value = other.value


class Stream:
    def wait_stream(self, other): pass


class Graph:
    def __init__(self, cuda): self.cuda, self.callback = cuda, None
    def replay(self): self.callback()


class Cuda:
    def __init__(self):
        self.rng, self.free, self.active = 11, 4 * 1024**3, None
        self.mismatch = self.broken_rng = False
        self.recordings = self.syncs = 0
    def get_rng_state(self, device): return self.rng
    def set_rng_state(self, value, device):
        if self.broken_rng: raise RuntimeError('rng restore failed')
        self.rng = value
    def mem_get_info(self, device): return self.free, 12 * 1024**3
    def synchronize(self, device): self.syncs += 1
    def current_stream(self, device): return Stream()
    def Stream(self, device): return Stream()
    def CUDAGraph(self): return Graph(self)
    @contextmanager
    def stream(self, stream): yield
    @contextmanager
    def graph(self, graph, stream):
        self.active = graph
        self.recordings += 1
        try: yield
        finally: self.active = None
    def forward(self, inputs):
        output = Tensor(inputs.value + self.rng)
        self.rng += 15
        if self.active:
            def replay():
                output.value = inputs.value + self.rng + int(self.mismatch)
                self.rng += 15
            self.active.callback = replay
        return output


@pytest.fixture
def cuda(monkeypatch):
    value = Cuda()
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(__version__='2.9.0+fixture', float16='float16',
        cuda=value, equal=lambda a,b: (a.value if isinstance(a, Tensor) else a) == (b.value if isinstance(b, Tensor) else b)))
    return value


def test_default_off_does_not_create_a_graph_cache(predictor):
    model, _, _, _ = predictor
    assert BatchSettings().predictor_cuda_graph is False
    assert qwen_fastpath.install(model).graphs is None
    assert isinstance(qwen_fastpath.install(model, cuda_graph=True).graphs, PredictorGraphs)


def test_capture_and_validation_do_not_consume_request_rng_or_alias_results(cuda):
    cache = PredictorGraphs()
    a = cache.run(Tensor(3), (True,.9,50,1.), cuda.forward)
    assert a.value == 14 and cuda.rng == 26
    assert cache.captures == cache.replays == 1
    b = cache.run(Tensor(8), (True,.9,50,1.), cuda.forward)
    assert a.value == 14 and b.value == 34 and cuda.rng == 41
    assert cache.captures == 1 and cache.replays == 2


def test_mismatch_disables_key_and_restores_rng_before_fixed_fallback(cuda):
    cuda.mismatch = True
    cache = PredictorGraphs()
    result = cache.run(Tensor(3), (True,.9,50,1.), cuda.forward)
    assert result.value == 14 and cuda.rng == 26
    assert cache.captures == cache.replays == 0 and cache.fallbacks == 1
    assert '不一致' in cache.reason
    recordings = cuda.recordings
    cache.run(Tensor(3), (True,.9,50,1.), cuda.forward)
    assert cuda.recordings == recordings and cache.fallbacks == 2


def test_rng_restoration_failure_escapes_to_worker_fallback(cuda):
    cuda.broken_rng = True
    cache = PredictorGraphs()
    with pytest.raises(GraphStateError, match='随机数状态'):
        cache.run(Tensor(3), (True,.9,50,1.), cuda.forward)
    assert not cache.entries and not cache.replays


def test_low_memory_and_recording_failure_keep_fixed_path(cuda, monkeypatch):
    cache = PredictorGraphs()
    cuda.free = 0
    assert cache.run(Tensor(3), (True,.9,50,1.), cuda.forward).value == 14
    assert not cuda.recordings and '512' in cache.reason
    cuda.free = 4 * 1024**3
    cache = PredictorGraphs()
    def fail(*args): raise RuntimeError('capture unsupported')
    monkeypatch.setattr(cache, 'capture', fail)
    assert cache.run(Tensor(3), (True,.9,50,1.), cuda.forward).value == 29
    assert cache.fallbacks == 1 and 'capture unsupported' in cache.reason


def test_graph_storage_and_failed_shapes_are_bounded(cuda):
    cache = PredictorGraphs()
    for batch in (1, 2, 4, 8):
        cache.run(Tensor(3, shape=(batch,2,4)), (True,.9,50,1.), cuda.forward)
        assert len(cache.entries) <= 2
    assert cache.captures == 4
    for i in range(20): cache.reject(i, 'test')
    assert len(cache.failed) == 8


@pytest.mark.parametrize('device,dtype,version', [('cpu','float16','2.9.0'),('cuda','float32','2.9.0'),('cuda','float16','future')])
def test_unverified_device_precision_or_runtime_never_captures(cuda, device, dtype, version):
    sys.modules['torch'].__version__ = version
    cache = PredictorGraphs()
    result = cache.run(Tensor(3, dtype=dtype, device=SimpleNamespace(type=device)), (False,1.,0,1.), cuda.forward)
    assert result.value == 14 and not cuda.recordings and cache.fallbacks == 1


def test_status_records_graph_use_and_fallback_without_warmup_inflation(predictor):
    model, _, _, _ = predictor
    path = qwen_fastpath.install(model, cuda_graph=True)
    before = path.snapshot()
    path.calls = 7
    path.graphs.captures, path.graphs.replays = 1, 7
    status = path.status(before)
    assert status['predictor_mode'] == 'graph'
    assert status['predictor_graph_captures'] == 1 and status['predictor_graph_replays'] == 7
    assert 'GPU 图加速' in execution_details(status)
    path.calls += 1
    path.graphs.fallbacks += 1
    assert path.status(before)['predictor_mode'] == 'mixed'


def test_graph_metadata_survives_queue_history_and_health(batch_manager, monkeypatch):
    manager, _, probe = batch_manager
    original = probe.generate
    monkeypatch.setattr(probe, 'generate', lambda *a, **kw: dict(original(*a, **kw),
        predictor_mode='graph', predictor_graph_captures=1, predictor_graph_replays=40,
        predictor_graph_fallbacks=0, predictor_graph_reason=''))
    identity = manager.submit('图状态元数据测试', 'qwen', [{'text':'一'}, {'text':'二'}])
    result = wait_job(manager.store.get, identity, ('done',))
    assert all(s['inference']['predictor_graph_replays']==40 for s in result['segments'])
    assert manager.status()['last_execution']['predictor_mode']=='graph'
    assert '40' in execution_details(result['segments'][0]['inference'])
