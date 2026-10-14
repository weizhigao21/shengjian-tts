"""Parameter persistence, model compatibility and explicit GUI application."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PyQt6 import sip
from PyQt6.QtWidgets import QMessageBox

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.data_paths import migrate_legacy_data
from voice_workbench.errors import VoiceError
from voice_workbench.model_controls import ModelControls
from voice_workbench.model_info import model_ui
from voice_workbench.parameter_presets import ParameterPresets, checked_options, model_descriptor, model_key
from test_generation_options import CaptureRunner, done, vox_voice
from test_option_compatibility import current_qwen


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


def test_crud_persists_without_saving_voice_or_reference_configuration(tmp_path):
    config = ConfigStore(tmp_path/'data')
    original = config.read()
    voice = current_qwen(tmp_path)
    presets = ParameterPresets(config)
    before = config.path.read_bytes()
    assert presets.listing() == [] and config.path.read_bytes() == before
    saved = presets.save(' 配音参数 ', voice, {'seed': -1, 'temperature': .75, 'do_sample': False})
    assert saved['name'] == '配音参数'
    assert set(config.read()['parameter_presets'][0]) == {'id', 'name', 'model', 'request_options', 'created', 'updated'}
    assert set(saved['model']) == {'backend', 'family', 'model_dir', 'index_version', 'variant'}
    assert ParameterPresets(ConfigStore(config.data_dir)).listing() == [saved]
    renamed = presets.rename(saved['id'], '朗读')
    assert renamed['request_options'] == saved['request_options']
    updated = presets.save('朗读', voice, {'seed': 4294967295, 'temperature': .9}, saved['id'])
    assert updated['created'] == saved['created'] and updated['request_options']['seed'] == 4294967295
    assert {k: v for k, v in config.read().items() if k != 'parameter_presets'} == original
    presets.delete(saved['id'])
    assert presets.listing() == []


def test_model_scope_ignores_voice_and_engine_but_distinguishes_models(tmp_path):
    voice = current_qwen(tmp_path)
    model = model_descriptor(voice, model_ui(voice))
    other_voice = {**voice, 'id': 'other', 'reference_audio': 'other.wav', 'engine_executable': 'new.exe', 'device': 'cpu'}
    assert model_key(model) == model_key(model_descriptor(other_voice, model_ui(other_voice)))
    assert model_key(model) != model_key({**model, 'model_dir': str(tmp_path/'other.gguf')})
    assert model_key(model) != model_key({**model, 'variant': 'custom_voice'})
    native = {**voice, 'backend': 'index_tts', 'index_version': '2'}
    assert model_key(model_descriptor(native, {'family': 'index_tts2'})) != model_key(
        model_descriptor({**native, 'index_version': '2.5'}, {'family': 'index_tts2'}))
    presets = ParameterPresets(ConfigStore(tmp_path/'data'))
    saved = presets.save('Reading', voice, {'seed': 1})
    with pytest.raises(VoiceError, match='同名'):
        presets.save('reading', other_voice, {'seed': 2})
    different = vox_voice(tmp_path)
    presets.save('Reading', different, {'seed': 2})
    with pytest.raises(VoiceError, match='其他模型'):
        presets.save('Reading', different, {'seed': 2}, saved['id'])


@pytest.mark.parametrize('values', [
    {'temperature': 99}, {'temperature': float('nan')}, {'temperature': float('inf')},
    {'temperature': 10**400}, {'seed': True}, {'seed': 1.5}, {'seed': 4294967296},
    {'do_sample': 1}, {'top_p': 0}, {'made_up': 1}, {'speed': 1.2}, {},
])
def test_invalid_parameters_do_not_write_configuration(tmp_path, values):
    config = ConfigStore(tmp_path/'data')
    before = config.path.read_bytes()
    with pytest.raises(VoiceError):
        ParameterPresets(config).save('invalid', current_qwen(tmp_path), values)
    assert config.path.read_bytes() == before


@pytest.mark.parametrize('name', [' ', 'x'*101])
def test_invalid_names_do_not_write(tmp_path, name):
    config = ConfigStore(tmp_path/'data')
    before = config.path.read_bytes()
    with pytest.raises(VoiceError):
        ParameterPresets(config).save(name, vox_voice(tmp_path), {'seed': 1})
    assert config.path.read_bytes() == before


def test_legacy_options_translate_and_enums_validate(tmp_path):
    voice = current_qwen(tmp_path)
    options, notes = checked_options(voice, model_ui(voice), {'text_chunk_mode': 'default', 'seed': 3})
    assert options == {'seed': 3} and notes
    voice = vox_voice(tmp_path)
    with pytest.raises(VoiceError):
        checked_options(voice, model_ui(voice), {'voxcpm2.chunk_strategy': 'invalid'})
    with pytest.raises(VoiceError):
        checked_options(voice, model_ui(voice), {'min_tokens': 30, 'max_tokens': 20})


def test_model_paths_are_rebased_with_data_migration(tmp_path):
    source = tmp_path/'old'
    source.mkdir()
    voice = current_qwen(source)
    config = ConfigStore(source)
    saved = ParameterPresets(config).save('迁移', voice, {'seed': 4})
    target = tmp_path/'new'
    assert migrate_legacy_data(source, target)
    loaded = ParameterPresets(ConfigStore(target)).listing()[0]
    assert loaded['model']['model_dir'] == str(target/'qwen.gguf')
    assert loaded['model_key'] != saved['model_key']
    assert loaded['model_key'] == model_key(model_descriptor({**voice, 'model_dir': str(target/'qwen.gguf')}, model_ui(voice)))


def test_parallel_config_writes_keep_all_presets_and_other_settings(tmp_path):
    config = ConfigStore(tmp_path/'data')
    presets = ParameterPresets(config)
    voice = current_qwen(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(presets.save, f'参数{i}', voice, {'seed': i}) for i in range(5)]
        futures.append(executor.submit(config.save_performance, {'workflow_parallelism': 2}))
        for future in futures:
            future.result()
    assert len(presets.listing()) == 5
    assert config.read()['performance'] == {'workflow_parallelism': 2}


def test_http_management_and_applied_parameters_reach_generation(tmp_path):
    root = tmp_path/'data'
    voice = current_qwen(tmp_path)
    ConfigStore(root).save_voice(voice)
    runner = CaptureRunner()
    with TestClient(create_app(root, runner=runner)) as client:
        original = client.get('/voices').json()
        body = {'name': '配音', 'model': voice, 'request_options': {'seed': 42, 'temperature': .65, 'do_sample': False}}
        response = client.post('/parameter-presets', json=body)
        assert response.status_code == 201, response.text
        saved = response.json()
        path = '/parameter-presets/' + saved['id']
        assert client.get('/parameter-presets').json() == [saved]
        assert client.post('/parameter-presets', json=body).status_code == 400
        assert client.patch(path, json={'name': '测试'}).json()['request_options'] == saved['request_options']
        updated = client.put(path, json={**body, 'name': '测试', 'request_options': {'seed': 99, 'temperature': .65}}).json()
        result = client.post('/jobs', json={'voice_id': voice['id'], 'segments': [{'text': 'Hello'}],
            'overrides': {'request_options': updated['request_options']}})
        assert result.status_code == 202, result.text
        done(client, result.json()['job_id'])
        assert runner.requests[0]['request']['options']['seed'] == 99
        assert runner.requests[0]['request']['options']['temperature'] == .65
        assert client.get('/voices').json() == original
        assert client.delete(path).json() == {'deleted': saved['id']}
        for method, content in [('delete', None), ('patch', {'name': 'x'}), ('put', body)]:
            kwargs = {'json': content} if content else {}
            assert getattr(client, method)(path, **kwargs).status_code == 404
        assert client.post('/parameter-presets', json={**body, 'unexpected': True}).status_code == 422


class Owner:
    ready, closing = True, False

    def __init__(self, client, deferred=False):
        self.client, self.deferred = client, deferred
        self.calls, self.errors = [], []

    def call(self, action, success, error=None):
        if self.deferred:
            self.calls.append((action, success, error))
        else:
            try:
                result = action(self.client)
            except Exception as exc:
                error(str(exc))
            else:
                success(result)

    def show_error(self, message):
        self.errors.append(message)


def controls_for(voice, owner, qtbot):
    controls = ModelControls()
    if qtbot:
        qtbot.addWidget(controls)
    controls.parameter_presets.set_owner(owner)
    controls.bind(voice)
    return controls


def test_gui_save_select_update_rename_delete_and_defaults(tmp_path, qtbot, monkeypatch):
    voice = current_qwen(tmp_path)
    with TestClient(create_app(tmp_path/'data')) as client:
        owner = Owner(client)
        controls = controls_for(voice, owner, qtbot)
        presets = controls.parameter_presets
        fields = controls.generation_controls.fields
        fields['seed'].setValue(4294967295)
        fields['do_sample'].setChecked(False)
        fields['subtalker_temperature'].setValue(.7)
        unchanged = {k: v for k, v in controls.overrides().items() if k != 'request_options'}
        monkeypatch.setattr(presets, 'ask_name', lambda *_: '我的配音')
        presets.save_button.click()
        saved = client.get('/parameter-presets').json()[0]
        assert saved['request_options']['seed'] == 4294967295
        assert saved['request_options']['do_sample'] is False
        fields['seed'].setValue(2)
        assert '已调整' in presets.combo.currentText()
        presets.combo.activated.emit(presets.combo.findData(saved['id']))
        assert controls.overrides()['request_options'] == saved['request_options']
        assert {k: v for k, v in controls.overrides().items() if k != 'request_options'} == unchanged
        assert '已调整' not in presets.combo.currentText()
        controls.generation_controls.fields['seed'].setValue(-1)
        presets.save(saved['name'], saved['id'])
        assert presets.current()['request_options']['seed'] == -1
        presets.rename(presets.current(), '朗读参数')
        assert presets.combo.currentText() == '朗读参数'
        controls.generation_controls.reset()
        assert '已调整' in presets.combo.currentText()
        current_values = controls.generation_controls.values()
        monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Cancel)
        presets.delete(presets.current())
        assert len(client.get('/parameter-presets').json()) == 1
        monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
        presets.delete(presets.current())
        assert presets.combo.count() == 1 and controls.generation_controls.values() == current_values
        assert not owner.errors


def test_model_switch_filters_without_automatic_application(tmp_path, qtbot):
    voice = current_qwen(tmp_path)
    with TestClient(create_app(tmp_path/'data')) as client:
        records = ParameterPresets(client.app.state.manager.config)
        qwen = records.save('Qwen', voice, {'seed': 13})
        vox = vox_voice(tmp_path)
        records.save('Vox', vox, {'seed': 19})
        controls = controls_for(voice, Owner(client), qtbot)
        presets = controls.parameter_presets
        assert presets.combo.count() == 2 and presets.combo.itemText(1) == 'Qwen'
        assert controls.generation_controls.values()['seed'] == voice['request_options']['seed']
        presets.combo.setCurrentIndex(1)
        for _ in range(5):
            controls.bind(dict(voice))
        assert presets.combo.currentIndex() == 1  # Polling must not rebuild an open selection.
        assert controls.generation_controls.values()['seed'] == voice['request_options']['seed']
        presets.combo.activated.emit(presets.combo.findData(qwen['id']))
        assert '已调整' not in presets.combo.currentText()  # Omitted fields use model defaults.
        controls.bind(vox, preserve_edits=False)
        assert presets.combo.count() == 2 and presets.combo.itemText(1) == 'Vox' and presets.selected is None
        assert controls.generation_controls.values()['seed'] == -1
        controls.assets['model_dir'].setText(voice['model_dir'])
        controls.refresh_capabilities()
        assert presets.combo.itemText(1) == 'Qwen' and presets.selected is None


def test_incompatible_preset_leaves_parameters_intact(tmp_path, qtbot):
    voice = current_qwen(tmp_path)
    with TestClient(create_app(tmp_path/'data')) as client:
        record = ParameterPresets(client.app.state.manager.config).save('test', voice, {'seed': 13})
        owner = Owner(client)
        controls = controls_for(voice, owner, qtbot)
        presets = controls.parameter_presets
        presets.entries[0]['request_options']['unsupported'] = 1
        before = controls.overrides()
        presets.combo.activated.emit(presets.combo.findData(record['id']))
        assert owner.errors and controls.overrides() == before and presets.selected is None


def test_voice_preset_dialog_can_apply_parameters_without_saving_the_voice(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    from voice_workbench.library import PresetDialog
    voice = current_qwen(tmp_path)
    monkeypatch.setattr(Window, 'load_saved_jobs', lambda self: None)
    with TestClient(create_app(tmp_path/'data')) as client:
        record = ParameterPresets(client.app.state.manager.config).save('通用参数', voice, {'seed': 77})
        window = Window(8799, tmp_path/'data', autostart=False)
        qtbot.addWidget(window)
        window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
        window.ready = True
        window.voices = [voice]
        owner = Owner(client)
        window.call = owner.call
        dialog = PresetDialog(window, voice)
        qtbot.addWidget(dialog)
        before = client.get('/voices').json()
        controls = dialog.controls
        controls.parameter_presets.combo.activated.emit(controls.parameter_presets.combo.findData(record['id']))
        assert controls.overrides()['request_options']['seed'] == 77
        assert controls.reference_path == voice['reference_audio']
        assert client.get('/voices').json() == before
        dialog.reject()
        window.close()


def test_failed_save_keeps_unsaved_parameters_and_can_be_retried(tmp_path, qtbot):
    voice = current_qwen(tmp_path)
    with TestClient(create_app(tmp_path/'data')) as client:
        owner = Owner(client)
        controls = controls_for(voice, owner, qtbot)
        presets = controls.parameter_presets
        presets.save('相同名称')
        controls.generation_controls.fields['seed'].setValue(37)
        before = presets.values()
        presets.save('相同名称')
        assert owner.errors and not presets.busy and presets.save_button.isEnabled()
        assert presets.values() == before and '已调整' in presets.combo.currentText()
        presets.save('新的名称')
        assert len(presets.entries) == 2 and presets.current()['request_options']['seed'] == 37


def test_async_save_uses_captured_model_and_ignores_stale_list(tmp_path, qtbot):
    voice = current_qwen(tmp_path)
    with TestClient(create_app(tmp_path/'data')) as client:
        owner = Owner(client)
        controls = controls_for(voice, owner, None)
        presets = controls.parameter_presets
        owner.deferred = True
        presets.refresh()
        fetch, fetched, _ = owner.calls.pop()
        stale = fetch(client)
        presets.save('Qwen')
        action, success, _ = owner.calls.pop()
        captured = deepcopy(presets.values())
        controls.bind(vox_voice(tmp_path), preserve_edits=False)
        result = action(client)
        success(result)
        fetched(stale)
        assert presets.selected is None and presets.combo.count() == 1
        assert presets.entries[0]['model']['family'] == 'qwen3_tts'
        assert presets.entries[0]['request_options'] == captured
        presets.refresh()
        _, late, _ = owner.calls.pop()
        sip.delete(controls)
        late([])  # Dialog closure must not mutate deleted Qt controls.
