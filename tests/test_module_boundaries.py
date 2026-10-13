"""Layer boundaries, stable external contracts and reusable generation preparation."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore, voice_fingerprint
from voice_workbench.services.generation import prepare_generation
from test_generation_options import vox_voice


def without_unstable_metadata(schema):
    schema = json.loads(json.dumps(schema))
    schema['info'].pop('version', None)
    # The existing GET/HEAD streaming route shares a generated operation ID;
    # FastAPI derives its suffix from a set whose iteration order varies.
    for method in ('get', 'head'):
        schema['paths']['/workflow/audio/{filename}'][method].pop('operationId', None)
    return schema


def test_modular_routes_preserve_the_previous_external_http_contract(tmp_path):
    fixture = Path(__file__).parent/'fixtures/http_contract.json'
    expected = json.loads(fixture.read_text(encoding='utf-8'))
    data = tmp_path/'unused-data'
    actual = create_app(data).openapi()
    # New editors add optional metadata and per-segment presets. Keep the
    # v0.7.20 fixture frozen to protect every previously published field/route.
    schemas = actual['components']['schemas']
    for name in ('ScriptMention', 'DialogueEntry', 'EditorState'):
        schemas.pop(name)
    for name, field in [('SegmentInput', 'voice_id'), ('SegmentInput', 'source_id'),
                        ('SegmentInput', 'source_revision'), ('SegmentInput', 'force_generate'),
                        ('SegmentInput', 'use_preset'), ('SegmentInput', 'overrides'),
                        ('JobInput', 'script_mentions'), ('DocumentInput', 'script_mentions'),
                        ('JobInput', 'editor_state'), ('DocumentInput', 'editor_state')]:
        schemas[name]['properties'].pop(field)
    assert without_unstable_metadata(actual) == without_unstable_metadata(expected)
    assert not data.exists()  # Creating an app does not start inference or create user data.


def test_backend_imports_do_not_load_qt_or_create_a_service(tmp_path):
    code = "from voice_workbench.api import create_app; from voice_workbench.manager import Manager; import sys; assert not any(n == 'PyQt6' or n.startswith('PyQt6.') for n in sys.modules); print('backend imports without Qt')"
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'backend imports without Qt' in result.stdout


def test_legacy_desktop_imports_share_the_modular_classes(qapp):
    from voice_workbench.gui import Window, ApiWorker, checked
    from voice_workbench.settings import SettingsPanel, ModelTable
    from voice_workbench.desktop.window import Window as ModularWindow
    from voice_workbench.desktop.api_client import ApiWorker as ModularWorker, checked as decode
    from voice_workbench.desktop.settings import SettingsPanel as ModularSettings
    from voice_workbench.desktop.settings.common import ModelTable as ModularTable
    assert Window is ModularWindow and ApiWorker is ModularWorker and checked is decode
    assert SettingsPanel is ModularSettings and ModelTable is ModularTable
    from voice_workbench.manager import ServiceLock
    from voice_workbench.services.service_lock import ServiceLock as ModularLock
    assert ServiceLock is ModularLock


def test_generation_preparation_is_reusable_without_creating_a_job(tmp_path):
    store = ConfigStore(tmp_path/'data')
    saved = store.save_voice({**vox_voice(tmp_path), 'request_options': {'seed': 17, 'guidance_scale': 2.4}})
    original = store.path.read_bytes()
    overrides = {'request_options': {'guidance_scale': 2.8}}
    prepared = prepare_generation(store, saved['id'], overrides=overrides)
    assert prepared.voice['request_options'] == {'seed': 17, 'guidance_scale': 2.8}
    assert prepared.fingerprint == voice_fingerprint(prepared.voice)
    assert prepared.provenance['preset_used'] and prepared.provenance['preset_modified']
    assert prepared.provenance['preset_id'] == saved['id']
    assert overrides == {'request_options': {'guidance_scale': 2.8}}
    assert store.path.read_bytes() == original
    assert not (store.data_dir/'tasks.sqlite3').exists()


def test_random_seed_is_materialized_only_in_the_prepared_snapshot(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path/'data')
    saved = store.save_voice({**vox_voice(tmp_path), 'request_options': {'seed': -1}})
    original = store.path.read_bytes()
    monkeypatch.setattr('voice_workbench.services.generation.secrets.randbelow', lambda _: 12345)
    prepared = prepare_generation(store, saved['id'])
    assert prepared.voice['request_options']['seed'] == 12345
    assert saved['request_options']['seed'] == -1
    assert not prepared.provenance['preset_modified']
    assert store.path.read_bytes() == original


@pytest.mark.parametrize('callback,expected', [('self.local_voice_picker.sync_from_config()', True),
                                              ('pass', False), ('# self.local_voice_picker.sync_from_config()\n        pass', False)])
def test_installed_picker_recognition_requires_real_complete_wiring(callback, expected):
    from scripts.install_workflow_adapter import voice_picker_installed
    source = '''from .local_voice_picker import VoicePicker
class Panel:
    def _build_ui(self):
        self.local_voice_picker = VoicePicker(self)
        self.new_group.add_widget_row(self.local_voice_picker)
    def _refresh_api_combo(self):
        self.local_voice_picker.sync_from_config()
    def _on_mode_changed(self, mode):
        pass
    def _on_api_changed(self):
        CALLBACK
'''.replace('CALLBACK', callback)
    assert voice_picker_installed(source) is expected
