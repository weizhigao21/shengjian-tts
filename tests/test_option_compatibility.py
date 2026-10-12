"""Saved legacy presets remain usable across native engine contract changes."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.audio_cpp import build_native_request
from voice_workbench.config import ConfigStore, voice_fingerprint
from voice_workbench.errors import VoiceError
from voice_workbench.generation_options import FALLBACK, compatible_options, option_specs, validate_options
from voice_workbench.model_controls import ModelControls
from voice_workbench.model_info import model_ui
from voice_workbench.runner import check_voice
from test_generation_options import CaptureRunner, done, vox_voice
from test_inline_generation import index_voice
from test_model_capabilities import gguf


def current_qwen(tmp_path):
    voice = index_voice(tmp_path)
    specs = [deepcopy(s) for s in FALLBACK['qwen3_tts'] if s['name'] != 'text_chunk_mode']
    next(s for s in specs if s['name'] == 'seed')['max'] = 4294967295
    gguf(tmp_path/'qwen.gguf', {'family': 'qwen3_tts', 'schema_version': 1,
        'options': {'request': specs}}, config={'tts_model_type': 'base', 'max_length': 8192})
    return {**voice, 'family': 'qwen3_tts', 'model_dir': str(tmp_path/'qwen.gguf'),
        'voice_mode': 'clone', 'reference_text': '你好，欢迎来到这里',
        'request_options': {'text_chunk_mode': 'default', 'text_chunk_size': 8192,
                            'max_tokens': 8192, 'temperature': .75, 'seed': 1601315866}}


def test_declared_enum_localizes_known_choices_and_preserves_new_labels():
    specs = option_specs('voxcpm2', [
        {'name': 'chunk_strategy', 'type': 'string', 'enum': {'continuation': 'Continuous', 'new': 'New strategy'}},
        {'name': 'new_style', 'type': 'enum', 'values': {'soft': 'Soft style'}}])
    assert specs[0]['values'] == {'continuation': '连续生成', 'new': 'New strategy'}
    assert specs[1]['values'] == {'soft': 'Soft style'}


def test_strategy_rollback_also_supports_verified_legacy_adapter_contract(tmp_path):
    voice = vox_voice(tmp_path)
    voice['request_options'] = {'chunk_strategy': 'stateless'}
    info = model_ui(voice)
    assert info['generation_options_source'] == 'audio.cpp-v0.7.4-adapter'
    options, _ = compatible_options(voice, info)
    assert options == {'voxcpm2.chunk_strategy': 'stateless'}
    check_voice(voice)
    assert build_native_request(voice, '你好')['request']['options']['voxcpm2.chunk_strategy'] == 'stateless'


def test_old_qwen_preset_remains_available_to_workflow_without_rewriting_config(tmp_path, qapp):
    voice = current_qwen(tmp_path)
    store = ConfigStore(tmp_path/'data')
    stored = store.save_voice(voice)
    original = store.path.read_bytes()
    original_options = deepcopy(stored['request_options'])
    check_voice(stored)
    controls = ModelControls()
    controls.bind(stored)
    values = controls.overrides()['request_options']
    assert 'text_chunk_mode' not in values
    assert values['seed'] == 1601315866 and values['temperature'] == .75
    assert controls.compatibility_hint.text()
    check_voice({**stored, **controls.overrides()})
    controls.close()
    runner = CaptureRunner()
    with TestClient(create_app(store.data_dir, runner=runner)) as client:
        record = next(v for v in client.get('/voices').json() if v['id'] == voice['id'])
        assert record['error'] is None
        assert record['model_tag'] == voice_fingerprint(stored)
        manifest = next(v for v in client.get('/workflow').json()['voices'] if v['id'] == voice['id'])
        assert manifest['available']
        response = client.post('/jobs', json={'voice_id': record['model_name'],
            'title': '双语工作流配音', 'segments': [{'text': '测试旧预设参数兼容。'}]})
        assert response.status_code == 202, response.text
        job = done(client, response.json()['job_id'])
        assert job['voice']['request_options'] == {k: v for k,v in original_options.items() if k != 'text_chunk_mode'}
        assert job['voice']['reference_text'] == stored['reference_text']
        assert job['voice']['reference_audio'] == stored['reference_audio']
        assert not job['segments'][0]['provenance']['preset_modified']
        options = runner.requests[0]['request']['options']
        assert 'text_chunk_mode' not in options
        assert options['seed'] == 1601315866 and options['temperature'] == .75
    assert store.path.read_bytes() == original
    assert stored['request_options'] == original_options


def test_main_editor_can_generate_with_legacy_preset_without_marking_it_modified(tmp_path, qtbot, monkeypatch):
    from PyQt6.QtCore import QPoint
    from voice_workbench.gui import Window
    from voice_workbench.workflow import voice_records
    voice = current_qwen(tmp_path)
    store = ConfigStore(tmp_path/'data')
    store.save_voice({**vox_voice(tmp_path), 'id': 'vox'})
    store.save_voice(voice)
    monkeypatch.setattr(Window, 'load_saved_jobs', lambda self: None)
    window = Window(8789, store.data_dir, autostart=False)
    qtbot.addWidget(window)
    window.call = lambda *args: None
    for timer in (window.timer, window.document_timer, window.settings.timer): timer.stop()
    window.ready = True
    window.voices = voice_records(store)
    window.model_controls.set_references(store.references())
    window.resize(1280, 820); window.show()
    window.refresh_generation_models('vox')
    window.model_controls.advanced_toggle.setChecked(True)
    qtbot.wait(20)
    window.refresh_generation_models(voice['id'])
    window.text_edit.setPlainText('你好，这是参数兼容验证。')
    window.refresh_main_settings()
    assert window.voice_combo.currentData() == voice['id']
    assert '已调整' not in window.voice_combo.currentText()
    assert window.generate_button.isEnabled()
    assert 'text_chunk_mode' not in window.generation_voice()['request_options']
    def anchors():
        return [(w.mapTo(window,QPoint()), w.size()) for w in (
            window.model_combo, window.voice_combo, window.model_controls.reference_text, window.model_controls.advanced_toggle)]
    qtbot.wait(20)
    expected = anchors()
    for _ in range(6):
        window.model_controls.advanced_toggle.click(); qtbot.wait(20)
        assert anchors() == expected
    window.model_controls.generation_controls.fields['temperature'].setValue(.85)
    window.refresh_main_settings()
    assert '已调整' in window.voice_combo.currentText()
    window.document_dirty = False; window.close()


@pytest.mark.parametrize('name,value', [('unknown_option', 1), ('text_chunk_mode', 'tag_aware'), ('seed', 4294967296)])
def test_compatibility_does_not_hide_unknown_or_invalid_options(tmp_path, name, value):
    voice = current_qwen(tmp_path)
    voice['request_options'][name] = value
    with pytest.raises(VoiceError):
        validate_options(voice, model_ui(voice))


@pytest.mark.parametrize('old,current', [('voxcpm2.chunk_strategy', 'chunk_strategy'), ('chunk_strategy', 'voxcpm2.chunk_strategy')])
def test_strategy_rename_supports_upgrade_and_rollback(tmp_path, qapp, old, current):
    voice = index_voice(tmp_path)
    gguf(tmp_path/'vox.gguf', {'family': 'voxcpm2', 'schema_version': 1,
        'options': {'request': [{'name': current, 'type': 'string', 'default': 'continuation',
                                'enum': ['continuation', 'stateless']}]}})
    voice.update(family='voxcpm2', model_dir=str(tmp_path/'vox.gguf'), voice_mode='clone',
                 request_options={old: 'stateless'})
    options, notes = compatible_options(voice, model_ui(voice))
    assert options == {current: 'stateless'} and notes
    assert voice['request_options'] == {old: 'stateless'}
    check_voice(voice)
    controls = ModelControls(); controls.bind(voice)
    assert controls.overrides()['request_options'] == options
    assert controls.generation_controls.specs[0]['label'] == '长文生成策略'
    controls.close()
    assert build_native_request(voice, '你好')['request']['options'][current] == 'stateless'
    voice['request_options'][current] = 'continuation'
    assert compatible_options(voice, model_ui(voice))[0] == {current: 'continuation'}
