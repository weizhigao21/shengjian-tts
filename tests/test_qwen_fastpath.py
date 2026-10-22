"""Conservative entry guards and integration; real GPU parity is measured separately."""
from copy import deepcopy
from types import SimpleNamespace
import sys
import pytest

from voice_workbench.batch_inference import qwen_fastpath as fast
from voice_workbench.batch_inference.policy import model_signature
from voice_workbench.desktop.components.inference_status import execution_details
from test_batch_inference import batch_manager
from test_service import wait_job


class GenerationConfig:
    def __init__(self):
        self.do_sample, self.temperature, self.top_k, self.top_p = False, 1., 50, 1.
        self.eos_token_id = self.pad_token_id = None
        self.num_beams, self.repetition_penalty, self.use_cache = 1, 1., True
    def to_dict(self): return deepcopy(vars(self))


@pytest.fixture
def predictor(monkeypatch):
    calls = []
    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return 'original-result'
    cls = type('Qwen3TTSTalkerCodePredictorModelForConditionalGeneration', (),
               {'__module__': 'qwen_tts.core.models.modeling_qwen3_tts'})
    p = cls()
    p.config, p.generation_config, p.training, p.generate = SimpleNamespace(num_code_groups=16), GenerationConfig(), False, original
    model = SimpleNamespace(model=SimpleNamespace(talker=SimpleNamespace(code_predictor=p)))
    monkeypatch.setattr(fast, 'version', lambda name: fast.SUPPORTED[name])
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(GenerationConfig=GenerationConfig))
    return model, p, original, calls


def test_disabled_and_unverified_sdk_never_patch_original(predictor, monkeypatch):
    model, p, original, _ = predictor
    assert fast.install(model, False).predictor is None and p.generate is original
    assert fast.install(model, device='cpu').predictor is None and p.generate is original
    monkeypatch.setattr(fast, 'version', lambda _: 'future-version')
    path = fast.install(model)
    assert path.predictor is None and p.generate is original
    assert '版本' in path.reason


@pytest.mark.parametrize('change', [lambda p: setattr(p, 'training', True),
    lambda p: setattr(p.config, 'num_code_groups', 17),
    lambda p: setattr(p.generation_config, 'eos_token_id', 1),
    lambda p: setattr(p.generation_config, 'num_beams', 2),
    lambda p: setattr(p.generation_config, 'repetition_penalty', 1.1),
    lambda p: setattr(p.generation_config, 'use_cache', False)])
def test_other_structures_stopping_rules_and_logits_constraints_keep_sdk(predictor, change):
    model, p, original, _ = predictor
    change(p)
    path = fast.install(model)
    assert path.predictor is None and p.generate is original and path.reason


@pytest.mark.parametrize('change', [lambda kw, p: kw.update(attention_mask='custom'),
    lambda kw, p: kw.update(max_new_tokens=14),
    lambda kw, p: kw.update(return_dict_in_generate=False),
    lambda kw, p: kw.update(inputs_embeds=SimpleNamespace(ndim=3,shape=(1,3,8))),
    lambda kw, p: setattr(p.generation_config,'num_beams',2),
    lambda kw, p: kw.update(top_p=0.),
    lambda kw, p: kw.update(top_k=-1)])
def test_unsupported_runtime_calls_are_forwarded_without_partial_sampling(predictor, change):
    model, p, original, calls = predictor
    path = fast.install(model)
    assert path.predictor is p
    kw = dict(inputs_embeds=SimpleNamespace(ndim=3,shape=(1,2,8)), max_new_tokens=15,
              output_hidden_states=True, return_dict_in_generate=True)
    change(kw,p)
    assert p.generate(**kw) == 'original-result'
    assert calls == [((),kw)] and path.calls == 0 and path.fallbacks == 1
    assert path.status()['predictor_mode'] == 'standard'


def test_every_rendered_frame_is_reported_so_the_parent_can_judge_pace(predictor, monkeypatch):
    """The stall guard watches frames now, so one frame must mean one report."""
    model, p, _original, _calls = predictor
    path = fast.install(model)
    assert path.predictor is p and path.progress is None
    reported = []
    path.warper_key = (False, 1., 50, 1.)   # already built: no transformers import
    monkeypatch.setattr(path, 'loop', lambda embeds, sample: 'codes')
    kw = dict(inputs_embeds=SimpleNamespace(ndim=3, shape=(2, 2, 8)), max_new_tokens=15,
              output_hidden_states=True, return_dict_in_generate=True)
    path.progress = lambda: reported.append(len(reported) + 1)
    assert path.generate(**kw).sequences == 'codes' and reported == [1]
    path.progress = None
    assert path.generate(**kw).sequences == 'codes' and reported == [1]


def test_pooled_blocks_are_reclaimed_while_a_batch_is_still_rendering(predictor, monkeypatch):
    """A batch fills the card as it renders: 0.090 s/frame, then six batches later
    0.342 s/frame, with free VRAM down from 254 MiB to 120 MiB. Reclaiming only
    between batches never saw it, so it has to happen mid-batch too -- often enough
    to matter, rarely enough not to show up in the frame time itself.
    """
    model, p, _original, _calls = predictor
    path = fast.install(model, cuda_graph=True)
    path.device = 'cuda'
    lights = []

    class Graphs:
        captures = replays = fallbacks = reliefs = lights = 0
        reason = ''
        def run(self, embeds, key, forward): return forward(embeds)
        def light(self, device): lights.append(path.calls)

    path.graphs = Graphs()
    monkeypatch.setattr(path, 'loop', lambda embeds, sample: 'codes')
    path.warper_key = (False, 1., 50, 1.)   # already built: no transformers import
    kw = dict(inputs_embeds=SimpleNamespace(ndim=3, shape=(2, 2, 8)), max_new_tokens=15,
              output_hidden_states=True, return_dict_in_generate=True)
    for _ in range(fast.FRAMES_PER_RELIEF * 2 + 3):
        path.generate(**kw)
    assert lights == [fast.FRAMES_PER_RELIEF, fast.FRAMES_PER_RELIEF * 2]
    assert 8 <= fast.FRAMES_PER_RELIEF <= 16   # about once a second at 0.09 s/frame


def test_sustained_slow_frames_keep_the_plain_loop_for_this_worker(predictor, monkeypatch):
    """Graph replay wins 0.09 s to 0.15 s with room and loses 1.3 s to 0.43 s
    without. Trading 1.7x for never losing 14x must not wait for the batch to end.
    """
    model, p, _original, _calls = predictor
    path = fast.install(model, cuda_graph=True)
    path.device = 'cuda'
    runs = []

    class Graphs:
        captures = replays = fallbacks = reliefs = lights = 0
        reason = ''
        def run(self, embeds, key, forward):
            runs.append(1)
            return forward(embeds)
        def light(self, device): pass

    path.graphs = Graphs()
    monkeypatch.setattr(path, 'loop', lambda embeds, sample: 'codes')
    path.warper_key = (False, 1., 50, 1.)   # already built: no transformers import
    kw = dict(inputs_embeds=SimpleNamespace(ndim=3, shape=(2, 2, 8)), max_new_tokens=15,
              output_hidden_states=True, return_dict_in_generate=True)
    for _ in range(4):
        path.generate(**kw)
    assert not path.downgraded and path.baseline is not None
    path.baseline = 1e-9   # every real frame now looks three times slower
    path.slow = 0
    for _ in range(fast.SLOW_FRAMES):
        path.generate(**kw)
    assert path.downgraded and '专用循环' in path.reason
    assert path.status()['predictor_graph_downgraded'] is True
    replaying = len(runs)
    path.generate(**kw)
    assert len(runs) == replaying   # the graph is not driven any more this worker
    assert path.tidy() is False     # and no pooling work is spent on it either


def test_acceleration_option_and_revision_partition_audio_cache(batch_manager, monkeypatch):
    manager, _, _ = batch_manager
    settings = manager.batch.settings
    old = model_signature(settings)
    assert model_signature(settings.model_copy(update={'predictor_fastpath':False})) != old
    assert model_signature(settings.model_copy(update={'predictor_cuda_graph':True})) != old
    monkeypatch.setattr(fast, 'REVISION', 'future-predictor')
    assert model_signature(settings) != old


def test_actual_predictor_metadata_is_saved_in_history_and_status(batch_manager, monkeypatch):
    manager, _, probe = batch_manager
    original = probe.generate
    def generate(*args, **kwargs):
        return dict(original(*args, **kwargs), predictor_mode='fixed', predictor_revision=fast.REVISION,
                    predictor_calls=60, predictor_fallback_calls=0, predictor_reason='')
    monkeypatch.setattr(probe,'generate',generate)
    identity = manager.submit('实际加速状态','qwen',[{'text':'一'},{'text':'二'}])
    result = wait_job(manager.store.get,identity,('done',))
    assert all(s['inference']['predictor_mode']=='fixed' for s in result['segments'])
    with manager.lock:
        assert manager.last_execution['predictor_calls']==60
    assert '专用加速' in execution_details(result['segments'][0]['inference'])
