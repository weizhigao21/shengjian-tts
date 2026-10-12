import json
import os
from pathlib import Path
import sqlite3
import sys
import wave

import pytest

from voice_workbench import data_paths
from voice_workbench.config import ConfigStore, atomic_json
from voice_workbench.store import Store
from voice_workbench.manager import ServiceLock


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


def legacy(root):
    config = ConfigStore(root)
    ref = root/'voices'/'reference.wav'
    ref.parent.mkdir(exist_ok=True)
    with wave.open(str(ref), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', ''))
        audio.writeframes(b'\0\0'*16000)
    voice = config.save_voice({'id':'speaker', 'name':'我的音色', 'backend':'windows_sapi', 'python':sys.executable, 'reference_audio':str(ref)})
    hidden_id = ConfigStore.reference_id(ref)
    config.delete_reference(hidden_id)
    another = root/'voices/another.wav'
    another.write_bytes(ref.read_bytes())
    config.import_reference({'name':'另一个参考', 'reference_audio':str(another), 'reference_text':'参考原文'})
    (root/'audio').mkdir()
    (root/'audio/generated.wav').write_bytes(ref.read_bytes())
    (root/'models').mkdir()
    (root/'models/model.gguf.part').write_bytes(b'partial download')
    (root/'models/upstream.json').write_text(json.dumps({'path': str(root/'models/model.gguf')}))
    (root/'runtime').mkdir()
    (root/'runtime/engine.exe').write_bytes(b'engine')
    atomic_json(config.path, {**config.read(), 'native_engine':str(root/'runtime/engine.exe'),
                             'external_model':'X:/outside/model.gguf',
                             'removed_models':[os.path.normcase(str(root/'models/removed.gguf'))]})
    store = Store(root)
    script = '字幕原文\n'+str(ref)+'\n保留原文'
    job = store.create('原作品', voice, 'version', [{'text':'声音内容'}], script=script)
    store.finish(job, 1, audio='generated.wav', duration=1)
    store.cache_put('cache-key', root/'audio/generated.wav', 'checksum')
    # Hold the source connection open so newly committed data is still in WAL.
    return config, store, job, hidden_id


def test_migrate_keeps_wal_history_audio_presets_cache_and_partial_downloads(tmp_path):
    source, target = tmp_path/'portable/data', tmp_path/'profile/声笺/data'
    config, store, job, hidden = legacy(source)
    try:
        original = config.path.read_bytes()
        assert (source/'tasks.sqlite3-wal').is_file()
        assert data_paths.migrate_legacy_data(source, target)
        assert config.path.read_bytes() == original
        migrated = ConfigStore(target)
        preset = next(v for v in migrated.voices() if v['id']=='speaker')
        assert preset['reference_audio'] == str(target/'voices/reference.wav')
        assert migrated.read()['native_engine'] == str(target/'runtime/engine.exe')
        assert migrated.read()['external_model'] == 'X:/outside/model.gguf'
        assert migrated.read()['removed_models'] == [os.path.normcase(str(target/'models/removed.gguf'))]
        assert len(migrated.read()['references']) == 1  # Hidden voice reference stays hidden, without duplicates.
        new_hidden = ConfigStore.reference_id(target/'voices/reference.wav')
        assert migrated.read()['hidden_references'] == [new_hidden]
        assert new_hidden in migrated.read()['deleted_references']
        assert (target/'models/model.gguf.part').read_bytes() == b'partial download'
        assert (target/'models/upstream.json').read_bytes() == (source/'models/upstream.json').read_bytes()
        assert (target/'audio/generated.wav').read_bytes() == (source/'audio/generated.wav').read_bytes()
        restored = Store(target)
        try:
            assert restored.get(job)['segments'][0]['voice']['reference_audio'] == preset['reference_audio']
            assert restored.get(job)['script'] == store.get(job)['script']
            assert restored.cache_get('cache-key')['path'] == str(target/'audio/generated.wav')
        finally:
            restored.db.close()
        migrated.save_performance({'workflow_parallelism':1})
        saved = migrated.path.read_bytes()
        assert not data_paths.migrate_legacy_data(source, target)
        assert migrated.path.read_bytes() == saved
    finally:
        store.db.close()


@pytest.mark.parametrize('existing', ['config.json', 'tasks.sqlite3'])
def test_migration_never_replaces_existing_user_data(tmp_path, existing):
    source, target = tmp_path/'legacy', tmp_path/'current'
    source.mkdir()
    (source/'config.json').write_text('{}')
    target.mkdir()
    (target/existing).write_bytes(b'keep user data')
    assert not data_paths.migrate_legacy_data(source,target)
    assert (target/existing).read_bytes() == b'keep user data'


def test_invalid_database_aborts_without_publishing_or_modifying_source(tmp_path):
    source, target = tmp_path/'legacy', tmp_path/'profile/data'
    source.mkdir()
    (source/'config.json').write_text('{"voices":[]}')
    (source/'tasks.sqlite3').write_bytes(b'broken database')
    with pytest.raises(sqlite3.DatabaseError):
        data_paths.migrate_legacy_data(source,target)
    assert not target.exists()
    assert (source/'tasks.sqlite3').read_bytes() == b'broken database'
    assert list(target.parent.glob('.data-migration-*')) == []


def test_installed_app_defaults_to_user_profile_and_defers_while_legacy_service_runs(tmp_path, monkeypatch):
    install, local = tmp_path/'installation', tmp_path/'user'
    monkeypatch.setattr(data_paths,'frozen',lambda:True)
    monkeypatch.setattr(data_paths,'project_root',lambda:install)
    monkeypatch.setenv('LOCALAPPDATA',str(local))
    source = install/'data'
    source.mkdir(parents=True)
    (source/'config.json').write_text('{"voices":[]}')
    target = local/'声笺/data'
    assert data_paths.default_data_dir() == target
    monkeypatch.setattr(data_paths,'_legacy_in_use', lambda *_:True)
    assert data_paths.prepare_data_dir(8765) == source
    assert not target.exists()
    monkeypatch.setattr(data_paths,'_legacy_in_use', lambda *_:False)
    assert data_paths.prepare_data_dir(8765) == target
    assert (target/'config.json').exists()
    # Moving/replacing the program folder still selects the same user data.
    monkeypatch.setattr(data_paths,'project_root',lambda:tmp_path/'new-installation')
    assert data_paths.prepare_data_dir() == target
    monkeypatch.setattr(data_paths,'frozen',lambda:False)
    assert data_paths.default_data_dir() == tmp_path/'new-installation/data'


def test_migration_refuses_a_locked_legacy_directory_on_another_port(tmp_path):
    source, target = tmp_path/'legacy', tmp_path/'profile/data'
    source.mkdir()
    (source/'config.json').write_text('{"voices":[]}')
    lock = ServiceLock(source)
    try:
        with pytest.raises(RuntimeError, match='已有语音服务'):
            data_paths.migrate_legacy_data(source,target)
        assert not target.exists()
    finally:
        lock.close()


def test_migration_relinks_a_matching_reference_copy_from_an_old_build_folder(tmp_path):
    source, target = tmp_path/'portable/data', tmp_path/'profile/data'
    config, store, job, hidden = legacy(source)
    try:
        old_build = tmp_path/'old-build/data/voices/reference.wav'
        old_build.parent.mkdir(parents=True)
        old_build.write_bytes((source/'voices/reference.wav').read_bytes())
        value = config.read()
        voice = next(v for v in value['voices'] if v['id']=='speaker')
        voice['reference_audio'] = str(old_build)
        atomic_json(config.path,value)
        assert data_paths.migrate_legacy_data(source,target)
        migrated = ConfigStore(target)
        preset = next(v for v in migrated.voices() if v['id']=='speaker')
        assert preset['reference_audio'] == str(target/'voices/reference.wav')
        assert Path(preset['reference_audio']).read_bytes() == old_build.read_bytes()
    finally:
        store.db.close()


def test_explicit_data_dir_skips_automatic_migration(tmp_path, monkeypatch):
    from voice_workbench import __main__, gui
    launched = []
    monkeypatch.setattr(sys,'argv',['声笺','gui','--data-dir',str(tmp_path),'--port','8899'])
    monkeypatch.setattr(__main__,'prepare_data_dir',lambda *_: pytest.fail('Explicit directory must bypass migration'))
    monkeypatch.setattr(gui,'launch',lambda port,root: launched.append((port,root)))
    __main__.main()
    assert launched == [(8899,tmp_path)]
