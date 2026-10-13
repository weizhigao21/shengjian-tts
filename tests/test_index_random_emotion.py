"""Saved random-emotion options must survive dialogue regeneration."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.audio_cpp import build_native_request
from voice_workbench.config import ConfigStore, voice_fingerprint
from voice_workbench.errors import VoiceError
from voice_workbench.generation_options import FALLBACK
from voice_workbench.model_controls import ModelControls
from voice_workbench.runner import check_voice
from test_generation_options import CaptureRunner, done
from test_inline_generation import index_voice
from test_model_capabilities import gguf, kokoro


def configured_index(tmp_path, random_emotion, declared=True):
    voice = index_voice(tmp_path)
    if declared:
        specs = deepcopy(FALLBACK['index_tts2'])
        specs = [s for s in specs if s['name'] != 'use_random_emotion']
        specs.append({'name':'use_random_emotion','type':'bool','default':False})
        gguf(tmp_path/'declared-index.gguf', {'schema_version':1,'family':'index_tts2',
            'capabilities':{'clone':['speaker_reference','emotion_control']},
            'languages':['zh','en'], 'options':{'request':specs}})
        voice['model_dir'] = str(tmp_path/'declared-index.gguf')
    return {**voice, 'emotion_mode':'vector', 'emotion_strength':.6,
        'emotion_vector':[.4,0,0,0,0,0,.35,0], 'speed':.7,
        'request_options':{'seed':42, 'use_random_emotion':random_emotion}}


@pytest.mark.parametrize('random_emotion', [False, True])
@pytest.mark.parametrize('declared', [False, True])
def test_old_option_is_valid_visible_and_forwarded_without_changing_emotion(tmp_path, qtbot, random_emotion, declared):
    voice = configured_index(tmp_path, random_emotion, declared)
    check_voice(voice)
    controls = ModelControls()
    qtbot.addWidget(controls)
    controls.bind(voice)
    field = controls.generation_controls.fields['use_random_emotion']
    assert field.isChecked() is random_emotion
    spec = next(s for s in controls.generation_controls.specs if s['name']=='use_random_emotion')
    assert spec['label'] == '随机情绪采样'
    assert 'use_random_emotion' not in controls.generation_controls.extra
    values = controls.overrides()
    request = build_native_request({**voice, **values}, '你吃了没，小妹妹？')['request']
    assert request['options']['use_random_emotion'] is random_emotion
    assert values['emotion_vector'] == voice['emotion_vector'] and values['speed'] == .7
    field.setChecked(not random_emotion)
    controls.bind(dict(voice))
    assert controls.overrides()['request_options']['use_random_emotion'] is not random_emotion


@pytest.mark.parametrize('random_emotion', [False, True])
def test_saved_dialogue_can_regenerate_with_existing_option(tmp_path, random_emotion):
    config = ConfigStore(tmp_path/'data')
    saved = config.save_voice(configured_index(tmp_path, random_emotion))
    original = config.path.read_bytes()
    fingerprint = voice_fingerprint(saved)
    runner = CaptureRunner()
    entry = {'id':'existing-box','voice_id':saved['id'],'text':'你吃了没，小妹妹？',
        'overrides':{'emotion_mode':'vector','emotion_vector':saved['emotion_vector'],
            'speed':.7,'request_options':saved['request_options']}}
    with TestClient(create_app(config.data_dir, runner=runner)) as client:
        draft = client.post('/jobs/draft', json={'title':'已有对话','script':entry['text'],
            'editor_state':{'mode':'dialogue','dialogue_entries':[entry]}}).json()
        payload = {'voice_id':saved['id'], 'segments':[{'text':entry['text'],'voice_id':saved['id'],
            'source_id':entry['id'],'overrides':entry['overrides'],'force_generate':True}]}
        for take in range(2):
            response = client.post('/jobs/'+draft['id']+'/generate', json=payload)
            assert response.status_code == 202, response.text
            job = done(client, draft['id'])
            assert len(job['segments']) == take + 1
            assert job['segments'][-1]['voice']['request_options']['use_random_emotion'] is random_emotion
            assert job['segments'][-1]['voice']['emotion_vector'] == saved['emotion_vector']
            assert job['segments'][-1]['source_id'] == entry['id'] and not job['segments'][-1]['cached']
            assert job['editor_state']['dialogue_entries'][0]['overrides']['speed'] == .7
        assert job['segments'][0]['audio'] != job['segments'][1]['audio']
        assert all(r['request']['options']['use_random_emotion'] is random_emotion for r in runner.requests)
        assert next(v for v in client.get('/voices').json() if v['id']==saved['id'])['error'] is None
    assert config.path.read_bytes() == original
    assert voice_fingerprint(config.resolve(saved['id'])[0]) == fingerprint


@pytest.mark.parametrize('value', ['false', 0, None])
def test_invalid_random_emotion_value_is_rejected(tmp_path, value):
    voice = configured_index(tmp_path, value)
    with pytest.raises(VoiceError) as error:
        check_voice(voice)
    assert error.value.code == 'INVALID_GENERATION_OPTION'


def test_random_emotion_is_not_silently_accepted_by_other_models(tmp_path):
    voice = {**index_voice(tmp_path), **kokoro(tmp_path), 'request_options':{'use_random_emotion':False}}
    with pytest.raises(VoiceError) as error:
        check_voice(voice)
    assert error.value.code == 'UNSUPPORTED_GENERATION_OPTION'
