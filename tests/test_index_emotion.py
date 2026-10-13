"""Expression modes must reach both engines, survive storage and stay scoped."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voice_workbench.audio_cpp import build_native_request
from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore, voice_fingerprint
from voice_workbench.errors import VoiceError
from voice_workbench.index_emotion import EMOTION_FIELDS, EMOTION_PRESETS, emotion_mode, native_emotion, python_emotion
from voice_workbench.runner import check_voice
from voice_workbench.schemas import GenerationOverrides, VoiceConfig
from voice_workbench.services.generation import prepare_generation
from test_inline_generation import index_voice, make_audio, Runner
from test_library_and_deletion import wait_job


@pytest.mark.parametrize('mode', ['speaker', 'reference', 'vector', 'text', 'inferred'])
def test_native_modes_send_only_their_own_expression_parameters(tmp_path, mode):
    voice = index_voice(tmp_path)
    voice.update(emotion_mode='text' if mode == 'inferred' else mode, emotion='温柔',
                 emotion_strength=.75, emotion_reference_audio=voice['reference_audio'],
                 emotion_vector=EMOTION_PRESETS['温柔'], emotion_infer_from_text=mode == 'inferred')
    request = build_native_request(voice, '今天真开心')['request']
    options = request['options']
    assert request['voice_ref'] == voice['reference_audio']
    assert ('audio' in request) == (mode == 'reference')
    assert ('emotion_alpha' in options) == (mode != 'speaker')
    assert ('emotion_vector' in options) == (mode == 'vector')
    assert ('use_emotion_text' in options) == (mode in ('text', 'inferred'))
    assert ('emotion_text' in options) == (mode == 'text')
    if mode == 'vector':
        assert json.loads(options['emotion_vector']) == EMOTION_PRESETS['温柔']
    if mode != 'speaker':
        assert options['emotion_alpha'] == .75


@pytest.mark.parametrize('version', ['2', '2.5'])
@pytest.mark.parametrize('mode', ['speaker', 'reference', 'vector', 'text', 'inferred'])
def test_official_python_adapter_receives_expression_settings(tmp_path, monkeypatch, version, mode):
    from voice_workbench import engine_worker
    calls, created = [], []
    class FakeIndex:
        def __init__(self, **kwargs):
            created.append(kwargs)
        def infer(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, 'indextts.infer_v2', SimpleNamespace(IndexTTS2=FakeIndex))
    monkeypatch.setitem(sys.modules, 'indextts.infer_v2_5', SimpleNamespace(IndexTTS2=FakeIndex))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(engine_worker, '_model', None)
    monkeypatch.setattr(engine_worker, '_signature', None)
    voice = VoiceConfig(**{**index_voice(tmp_path), 'backend':'index_tts', 'device':'cpu',
        'index_version':version, 'emotion_mode':'text' if mode == 'inferred' else mode,
        'emotion_infer_from_text':mode == 'inferred', 'emotion':'喜悦', 'emotion_strength':.8,
        'emotion_vector':EMOTION_PRESETS['愉快'], 'emotion_reference_audio':'acting.wav'}).model_dump()
    engine_worker.index_speech({'voice':voice, 'text':'台词', 'output':'output.wav', 'fingerprint':'fake'})
    assert calls[0] == {'spk_audio_prompt':voice['reference_audio'], 'text':'台词', 'output_path':'output.wav',
        'verbose':False, **({'lang':'ZH','duration_factor':1.} if version == '2.5' else {}), **python_emotion(voice)}
    if version == '2.5':
        assert created[0]['use_qwen_emo'] == (mode in ('text', 'inferred'))


@pytest.mark.parametrize('fields', [
    {'emotion_strength':float('nan')}, {'emotion_strength':1.1},
    {'emotion_vector':[0] * 7}, {'emotion_vector':[0] * 7 + [float('inf')]},
    {'emotion_vector':[0] * 7 + [-.1]}, {'emotion_mode':'unknown'}])
def test_api_rejects_invalid_expression_values(fields):
    with pytest.raises(ValidationError):
        GenerationOverrides(**fields)


@pytest.mark.parametrize('fields', [
    {'emotion_mode':'reference'}, {'emotion_mode':'text'},
    {'emotion_mode':'vector','emotion_vector':[0] * 7}])
def test_direct_generation_rejects_incomplete_expression(tmp_path, fields):
    with pytest.raises(VoiceError) as error:
        check_voice({**index_voice(tmp_path), **fields})
    assert error.value.code == 'INVALID_EMOTION'


def test_legacy_emotion_and_default_fields_keep_old_preset_version(tmp_path):
    voice = VoiceConfig(**{**index_voice(tmp_path), 'emotion':'平静'}).model_dump()
    old = {k:v for k,v in voice.items() if k not in EMOTION_FIELDS}
    assert native_emotion(old) == ({'emotion_alpha':.6, 'use_emotion_text':True, 'emotion_text':'平静'}, None)
    assert emotion_mode(old) == 'text'
    assert voice_fingerprint(old) == voice_fingerprint(voice)
    voice['emotion_mode'] = 'speaker'
    assert voice_fingerprint(voice) != voice_fingerprint(old)


def test_reference_snapshot_survives_original_removal_and_affects_cache(tmp_path):
    config = ConfigStore(tmp_path / 'data')
    voice = config.save_voice(index_voice(tmp_path))
    acting = tmp_path / '演绎.wav'
    make_audio(acting)
    original = config.path.read_bytes()
    prepared = prepare_generation(config, voice['id'], overrides={
        'emotion_mode':'reference','emotion_strength':.8,'emotion_reference_audio':str(acting)})
    retained = Path(prepared.voice['emotion_reference_audio'])
    assert retained.parent == config.data_dir / 'voices'
    acting.unlink()
    assert retained.is_file() and config.path.read_bytes() == original
    assert prepared.provenance['preset_modified']
    first = voice_fingerprint(prepared.voice)
    # The filename alone cannot determine whether the reference content changed.
    content = bytearray(retained.read_bytes())
    content[-2:] = b'\x11\x00'
    retained.write_bytes(content)
    assert voice_fingerprint(prepared.voice) != first


def test_saved_preset_retains_separate_reference_and_resets_model_template(tmp_path):
    config = ConfigStore(tmp_path / 'data')
    voice = index_voice(tmp_path)
    acting = tmp_path / 'acting.wav'
    make_audio(acting)
    voice.update(emotion_mode='reference', emotion_reference_audio=str(acting), emotion_strength=.9)
    saved = config.save_voice(voice)
    acting.unlink()
    assert Path(saved['emotion_reference_audio']).is_file()
    assert config.resolve(voice['id'])[0]['emotion_strength'] == .9
    config.delete_voice(voice['id'])
    template = next(v for v in config.voices() if v.get('is_template') and v.get('family') == 'index_tts2')
    assert template['emotion_mode'] == 'auto' and template['emotion_reference_audio'] == ''


def test_dialogue_rows_preserve_independent_expression_snapshots(tmp_path):
    config = ConfigStore(tmp_path / 'data')
    voice = config.save_voice(index_voice(tmp_path))
    rows = [{'id':'box-a','voice_id':voice['id'],'text':'你好', 'overrides':{
        'emotion_mode':'vector','emotion_vector':EMOTION_PRESETS['愉快'],'emotion_strength':.7}},
        {'id':'box-b','voice_id':voice['id'],'text':'再见', 'overrides':{
        'emotion_mode':'text','emotion':'温柔','emotion_strength':.4}}]
    with TestClient(create_app(config.data_dir, runner=Runner())) as client:
        response = client.post('/jobs', json={'voice_id':voice['id'], 'title':'独立情绪',
            'editor_state':{'mode':'dialogue','dialogue_entries':rows},
            'segments':[{'text':r['text'],'voice_id':r['voice_id'],'source_id':r['id'],'overrides':r['overrides']} for r in rows]})
        assert response.status_code == 202, response.text
        job = wait_job(client, response.json()['job_id'])
        assert job['status'] == 'done'
        assert job['editor_state']['dialogue_entries'][0]['overrides']['emotion_vector'] == EMOTION_PRESETS['愉快']
        assert job['segments'][0]['voice']['emotion_mode'] == 'vector'
        assert job['segments'][1]['voice']['emotion_mode'] == 'text'
        assert job['segments'][1]['voice']['emotion_strength'] == .4
        assert config.resolve(voice['id'])[0]['emotion_mode'] == 'auto'


def test_expression_controls_restore_modes_and_keep_inspector_width(tmp_path, qtbot):
    from PyQt6.QtWidgets import QScrollArea
    from voice_workbench.model_controls import ModelControls
    from test_model_capabilities import kokoro
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.resize(352, 620)
    controls = ModelControls()
    scroll.setWidget(controls)
    qtbot.addWidget(scroll)
    controls.bind(index_voice(tmp_path))
    scroll.show()
    panel = controls.index_emotion
    for mode in ('speaker','reference','vector','text'):
        panel.mode_buttons[mode].click()
        qtbot.wait(10)
        assert controls.overrides()['emotion_mode'] == mode
        assert scroll.horizontalScrollBar().maximum() == 0
    panel.set_mode('vector')
    panel.preset_buttons['温柔'].click()
    assert panel.values()['emotion_vector'] == EMOTION_PRESETS['温柔']
    assert panel.preset_buttons['温柔'].isChecked()
    panel.tune_toggle.click()
    panel.sliders[0].setValue(25)
    assert not any(b.isChecked() for b in panel.preset_buttons.values())
    qtbot.wait(10)
    assert scroll.horizontalScrollBar().maximum() == 0
    snapshot = {**index_voice(tmp_path), **controls.overrides()}
    panel.set_mode('speaker')
    controls.bind(snapshot, preserve_edits=False)
    assert panel.mode == 'vector' and panel.sliders[0].value() == 25
    controls.bind({**kokoro(tmp_path),'id':'kokoro'}, preserve_edits=False)
    assert panel.isHidden() and controls.overrides()['emotion_mode'] == 'auto'
    assert controls.overrides()['emotion_vector'] == [0] * 8


def test_legacy_description_and_inference_checkbox_round_trip(tmp_path, qtbot):
    from voice_workbench.model_controls import ModelControls
    controls = ModelControls()
    qtbot.addWidget(controls)
    controls.bind({**index_voice(tmp_path),'emotion':'平静'})
    assert controls.index_emotion.mode == 'text'
    controls.index_emotion.infer_text.setChecked(True)
    values = controls.overrides()
    assert values['emotion_infer_from_text'] and not controls.emotion.isEnabled()
    assert 'emotion_text' not in native_emotion(values)[0]


def test_unchanged_legacy_preset_is_not_marked_modified(tmp_path):
    config = ConfigStore(tmp_path / 'data')
    voice = config.save_voice({**index_voice(tmp_path),'emotion':'平静'})
    prepared = prepare_generation(config, voice['id'], overrides={'emotion_mode':'text','emotion_strength':.6})
    assert not prepared.provenance['preset_modified']
    assert prepared.fingerprint == voice_fingerprint(voice)


def test_new_reference_is_protected_during_workflow_cleanup(tmp_path):
    from voice_workbench.workflow_audio import WorkflowAudio
    from voice_workbench.store import Store
    config = ConfigStore(tmp_path)
    acting = tmp_path / 'audio/acting.wav'
    acting.parent.mkdir()
    make_audio(acting)
    config_data = {'emotion_reference_audio':str(acting)}
    store = Store(tmp_path)
    try:
        manager = SimpleNamespace(root=tmp_path, config=SimpleNamespace(read=lambda:config_data), store=store)
        assert acting.resolve() in WorkflowAudio(manager)._reference_paths()
    finally:
        store.close()
