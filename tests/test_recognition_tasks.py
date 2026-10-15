"""Model acquisition, immediate import feedback and four-mode recent works."""
from copy import deepcopy
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.recent_works import external_work, read_recent, task_type, WORK_ICONS
from voice_workbench.store import Store
from test_audio_editing import wav
from test_model_capabilities import gguf
from test_model_scan import setup_scan, finish, spec
from test_recognition_regressions import page


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


def deferred(window):
    calls = []
    window.call = lambda action, success=None, failure=None: calls.append((action, success, failure))
    return calls


def saved_work(record):
    state = record['state']
    return external_work('recognition', record['id'], state['name'], record['created'], record['updated'], state['status'])


def test_dropped_audio_name_and_recent_task_appear_before_the_request_finishes(page, tmp_path):
    window, controller, client = page
    source = wav(tmp_path/'新拖入的录音.wav')
    calls = deferred(window)
    controller.import_audio(str(source))
    assert controller.submitting and len(calls) == 1
    assert controller.panel.title.text() == source.stem
    assert source.name in controller.panel.audio_name.text()
    assert controller.panel.audio_name.toolTip() == str(source)
    assert '导入中' in controller.panel.history.currentText()
    assert controller.panel.title.isReadOnly()
    assert not controller.panel.lines.isVisible()
    assert window.jobs_table.rowCount() == 1
    cell = window.jobs_table.item(0, 0)
    assert cell.text() == source.stem and '导入中' in cell.toolTip()
    assert cell.data(257)['task_type'] == 'recognition'
    assert cell.data(257)['pending_import']
    assert window.jobs_table.currentRow() == 0
    # A concurrent poll of the old session must not replace the pending name.
    controller.render_sessions(); controller.render_session(); window.render_jobs([])
    assert controller.panel.title.text() == source.stem
    record = deepcopy(client.record)
    record['id'] = 'new-session'
    record['created'] = record['updated'] = 123
    record['state'].update(name=source.stem, source_path=str(source), status='queued', lines=[])
    calls[0][1](record)
    assert controller.importing is None and not controller.submitting
    assert not controller.panel.title.isReadOnly()
    assert window.jobs_table.rowCount() == 1
    assert window.jobs_table.item(0, 0).data(256) == 'recognition:new-session'
    assert 'pending_import' not in window.jobs_table.item(0, 0).data(257)
    assert source.name in controller.panel.audio_name.text()


def test_failed_import_removes_the_pending_row_and_restores_the_previous_task(page, tmp_path):
    window, controller, _ = page
    before = deepcopy(controller.session)
    window.render_jobs([saved_work(before)])
    source = wav(tmp_path/'失败的导入.wav')
    calls = deferred(window)
    controller.import_audio(str(source))
    assert window.jobs_table.rowCount() == 2
    calls[0][2]('fixture: decoder failed')
    assert not controller.submitting and controller.importing is None
    assert controller.session == before
    assert controller.panel.title.text() == before['state']['name']
    assert 'decoder failed' in controller.panel.status.text()
    assert window.jobs_table.rowCount() == 1
    assert window.jobs_table.item(0, 0).data(256) == 'recognition:' + before['id']
    assert controller.panel.import_button.isEnabled()


@pytest.mark.parametrize('task,search', [('asr', 'qwen3_asr'), ('align', 'qwen3_forced_aligner')])
def test_model_download_buttons_stay_available_and_open_the_matching_catalog(page, monkeypatch, task, search):
    window, controller, _ = page
    monkeypatch.setattr(window.settings, 'refresh', lambda *a: None)
    controller.options['align'] = []
    controller.update_actions()
    button = controller.panel.aligner_download if task == 'align' else controller.panel.asr_download
    assert button.isVisible() and button.isEnabled()
    if task == 'align':
        assert button.text() == '下载'
    window.settings.set_section(3)
    button.click()
    assert window.tabs.currentIndex() == 3
    assert window.settings.pages.currentIndex() == 0
    assert window.settings.model_filter.currentData() == 'stt'
    assert window.settings.model_search.text() == search


def populate_modes(root):
    store = Store(root)
    article = store.create('文章任务', {}, '', [], '')
    dialogue = store.create('对话任务', {}, '', [], '', editor_state={'mode': 'dialogue', 'article_script': '', 'dialogue_entries': []})
    store.close()
    from voice_workbench.audio_editing.store import EditStore
    from voice_workbench.speech_recognition.store import RecognitionStore
    (root/'audio-editor').mkdir(exist_ok=True)
    editor = EditStore(root/'audio-editor')
    audio = editor.create({'name': '剪辑任务', 'original_text': '', 'target_text': ''}, 'a'*32+'.wav', {'duration': 1})
    editor.close()
    (root/'recognition').mkdir(exist_ok=True)
    recognition = RecognitionStore(root/'recognition')
    session = recognition.create({'name': '识别任务', 'audio': 'b'*32+'.wav', 'language': 'zh', 'status': 'done', 'text': '', 'lines': []})
    recognition.close()
    return article, dialogue, audio, session


def test_recent_works_reads_all_modes_without_writing_or_creating_databases(tmp_path):
    assert read_recent(tmp_path) == []
    assert list(tmp_path.iterdir()) == []
    ids = populate_modes(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.sqlite3')}
    records = read_recent(tmp_path)
    assert {task_type(r) for r in records} == {'article', 'dialogue', 'audio', 'recognition'}
    assert {r['id'] for r in records} == {ids[0], ids[1], 'audio:'+ids[2], 'recognition:'+ids[3]}
    assert [r['created'] for r in records] == sorted((r['created'] for r in records), reverse=True)
    assert len(read_recent(tmp_path, 2)) == 2
    assert {p: p.read_bytes() for p in before} == before
    assert len(set(WORK_ICONS.values())) == 4


def test_new_recent_endpoint_leaves_jobs_contract_and_workflow_filtering_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    ids = populate_modes(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        store = client.app.state.manager.store
        hidden = store.create('双语工作流配音', {}, '', [], '', record_only=True)
        response = client.get('/recent-works')
        assert response.status_code == 200
        records = response.json()
        assert hidden not in {r['id'] for r in records}
        assert {task_type(r) for r in records} == {'article', 'dialogue', 'audio', 'recognition'}
        assert {r['id'] for r in client.get('/jobs').json()} == set(ids[:2])
        client.delete('/recognition/sessions/'+ids[3])
        assert 'recognition:'+ids[3] not in {r['id'] for r in client.get('/recent-works').json()}


def test_all_saved_modes_are_visible_in_the_sidebar_before_service_connection(tmp_path, qtbot, monkeypatch):
    populate_modes(tmp_path)
    calls = []
    monkeypatch.setattr(Window, 'call', lambda self, *args, **kwargs: calls.append(args))
    window = Window(18999, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
    assert window.jobs_table.rowCount() == 4
    records = [window.jobs_table.item(r, 0).data(257) for r in range(4)]
    assert {task_type(r) for r in records} == {'article', 'dialogue', 'audio', 'recognition'}
    rows = [window.jobs_table.item(r, 0) for r in range(4)]
    changes = QSignalSpy(window.jobs_table.model().dataChanged)
    window.render_jobs(read_recent(tmp_path))
    assert len(changes) == 0
    assert [window.jobs_table.item(r, 0) for r in range(4)] == rows
    window.close()


def test_sidebar_opens_recognition_and_does_not_request_a_tts_job(page):
    window, controller, client = page
    target = saved_work(client.record)
    window.render_jobs([target])
    paths = []
    original = client.get
    def get(path):
        paths.append(path)
        return original(path)
    client.get = get
    controller.panel.title.setText('切换前修改的名称')
    window.jobs_table.selectRow(0)
    window.select_job()
    assert client.record['state']['name'] == '切换前修改的名称'
    assert window.creation_pages.currentIndex() == 3
    assert '/recognition/sessions/'+client.record['id'] in paths
    assert not any(p.startswith('/jobs/') for p in paths)
    assert controller.session['id'] == client.record['id']
    assert window.jobs_table.currentRow() == 0
    menu = window.job_menu(0)
    assert [a.text() for a in menu.actions()] == ['打开任务']


def test_sidebar_opens_an_audio_edit_project_without_losing_its_versions(page, tmp_path):
    window, _, _ = page
    source = wav(tmp_path/'待剪辑.wav')
    with TestClient(create_app(tmp_path/'api')) as client:
        project = client.post('/audio-editor/projects', json={'path': str(source), 'name': '剪辑项目'}).json()
        paths = []
        class Client:
            def get(self, path):
                paths.append(path)
                return client.get(path)
            def patch(self, path, **kwargs):
                return client.patch(path, **kwargs)
        def call(action, success=None, failure=None):
            result = action(Client())
            if success:
                success(result)
        window.call = call
        entry = external_work('audio', project['id'], project['state']['name'], project['created'])
        window.render_jobs([entry])
        window.jobs_table.selectRow(0)
        assert window.creation_pages.currentIndex() == 2
        assert window.audio_editor.controller.project['id'] == project['id']
        assert window.audio_editor.controller.current['id'] == project['versions'][0]['id']
        assert '/audio-editor/projects/'+project['id'] in paths
        assert not any(p.startswith('/jobs/') for p in paths)
        assert window.jobs_table.currentRow() == 0
        window.audio_editor.title.setText('尚未手动保存的剪辑名称')
        second = client.post('/audio-editor/projects', json={'path': str(source), 'name': '另一个剪辑'}).json()
        window.open_other_recent(external_work('audio', second['id'], second['state']['name'], second['created']))
        assert client.get('/audio-editor/projects/'+project['id']).json()['state']['name'] == '尚未手动保存的剪辑名称'
        assert window.audio_editor.controller.project['id'] == second['id']
        window.audio_editor.controller.timer.stop()
        window.close()


def test_completed_import_does_not_replace_a_task_selected_from_the_sidebar(page, tmp_path):
    window, controller, client = page
    previous = deepcopy(controller.session)
    window.render_jobs([saved_work(previous)])
    calls = deferred(window)
    controller.import_audio(str(wav(tmp_path/'后台导入.wav')))
    import_done = calls[0][1]
    window.open_other_recent(saved_work(previous))
    sidebar_done = calls[-1][1]
    sidebar_done(previous)
    assert not controller.importing['preview']
    assert controller.panel.title.text() == previous['state']['name']
    imported = deepcopy(client.record)
    imported['id'] = 'background-import'
    imported['state']['name'] = '后台导入'
    import_done(imported)
    assert controller.session['id'] == previous['id']
    assert not controller.importing and not controller.submitting
    assert any(window.jobs_table.item(i, 0).text() == '后台导入' for i in range(window.jobs_table.rowCount()))


def test_aligner_can_be_downloaded_and_becomes_selectable_without_creating_a_voice(tmp_path, qtbot):
    manager, metadata, _, _, _ = setup_scan(tmp_path, [spec('qwen3_forced_aligner', ['Align/model.gguf'], ['align'])])
    weight = tmp_path/'align.gguf'
    gguf(weight, {'family': 'qwen3_forced_aligner', 'schema_version': 1})
    payload = weight.read_bytes()
    for item in metadata['siblings']:
        if item['rfilename'] == 'Align/model.gguf':
            item['lfs'] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    def handle(request):
        return httpx.Response(200, json=metadata) if '/api/models/' in request.url.path else httpx.Response(200, content=payload)
    manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handle))
    try:
        manager.models.scanner.start(); finish(manager.models)
        entry = next(m for m in manager.models.listing()['catalog'] if m['family'] == 'qwen3_forced_aligner')
        window = Window(18996, tmp_path/'catalog-gui', autostart=False)
        qtbot.addWidget(window)
        window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
        panel = window.settings
        panel.model_data = manager.models.listing()
        panel.model_filter.setCurrentIndex(panel.model_filter.findData('stt'))
        panel.model_search.setText('qwen3_forced_aligner')
        panel.render_models()
        assert panel.catalog_table.rowCount() == 1
        assert '时间对齐' in panel.catalog_table.item(0, 4).text()
        assert '时间对齐' in panel.catalog_table.cellWidget(0, 5).toolTip()
        panel.model_filter.setCurrentIndex(panel.model_filter.findData('adapted'))
        assert any(m['family'] == 'qwen3_forced_aligner' for m in panel.visible_catalog)
        window.close()
        before = manager.config.voices()
        app = create_app(tmp_path/'unused')
        app.state.manager = manager
        client = TestClient(app)
        try:
            response = client.post('/models/'+entry['id']+'/download', json={'source': 'huggingface'})
            assert response.status_code == 202
            manager.models.threads[entry['id']].join(5)
            assert manager.models.downloads[entry['id']]['status'] == 'done'
            options = client.get('/recognition/options').json()
            assert [m['family'] for m in options['align']] == ['qwen3_forced_aligner']
            assert manager.config.voices() == before
        finally:
            client.close()
    finally:
        manager.close()
