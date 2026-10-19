"""Mirror routing keeps official checksums, shared partials and explicit user choices."""
import httpx
import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.config import atomic_json
from voice_workbench.engine_management import EngineManager
from voice_workbench.manager import Manager
from pe_fixture import engine_files
from test_engine_download import installer
from test_inline_generation import Runner
from test_updates import update_engine


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


def large_engine():
    files = engine_files(False)
    name = next(name for name in files if name.endswith('audiocpp_server.exe'))
    files[name] += b'x' * 600000
    return files


@pytest.mark.parametrize('source,host', [('ghproxy', 'gh-proxy.org'), ('ghfast', 'ghfast.top'), ('github', 'github.com')])
def test_selected_source_installs_the_same_official_asset_and_is_remembered(tmp_path, source, host):
    manager = Manager(tmp_path, Runner())
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=payloads['engine.zip'])

    try:
        engines, payloads = installer(manager, handle)
        engines.start('cpu', source=source)
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'done', engines.states['cpu']
        assert len(requests) == 1 and requests[0].url.host == host
        assert str(requests[0].url).endswith('github.com/0xShug0/audio.cpp/releases/download/v0.7.4/engine.zip')
        assert engines.listing()['download_source'] == source
        assert engines.states['cpu']['active_source'] == source
        restarted = EngineManager(manager.models, engines.catalog, engines.client_factory)
        try:
            assert restarted.download_source == source
        finally:
            restarted.close()
    finally:
        manager.close()


def test_auto_fallback_continues_partial_transfer_from_next_mirror(tmp_path):
    manager = Manager(tmp_path, Runner())
    requests = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield payloads['engine.zip'][:262144]
            raise httpx.ReadError('connection lost')

    def handle(request):
        requests.append(request)
        content = payloads['engine.zip']
        if request.url.host == 'ghfast.top':
            return httpx.Response(200, stream=BrokenStream())
        assert request.url.host == 'gh-proxy.org'
        assert request.headers['range'] == 'bytes=262144-'
        return httpx.Response(206, content=content[262144:],
            headers={'Content-Range': f'bytes 262144-{len(content)-1}/{len(content)}'})

    try:
        engines, payloads = installer(manager, handle, large_engine())
        engines.start('cpu', source='auto')
        engines.threads['cpu'].join(3)
        state = engines.states['cpu']
        assert state['status'] == 'done', state
        assert state['active_source'] == 'ghproxy' and state['resumed_from'] == 262144
        assert len(requests) == 2 and state['source_errors'][0]['source'] == 'ghfast'
    finally:
        manager.close()


def test_auto_rejects_bad_mirror_bytes_and_can_fall_back_to_official(tmp_path):
    manager = Manager(tmp_path, Runner())
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        content = payloads['engine.zip']
        if request.url.host == 'ghfast.top':
            return httpx.Response(200, content=content[:-1] + b'?')
        if request.url.host == 'gh-proxy.org':
            return httpx.Response(200, content=b'<html>error</html>')
        assert 'range' not in request.headers
        return httpx.Response(200, content=content)

    try:
        engines, payloads = installer(manager, handle)
        engines.start('cpu')
        engines.threads['cpu'].join(3)
        state = engines.states['cpu']
        assert state['status'] == 'done', state
        assert hosts == ['ghfast.top', 'gh-proxy.org', 'github.com']
        assert 'SHA-256' in state['source_errors'][0]['message']
        assert state['active_source'] == 'github'
    finally:
        manager.close()


@pytest.mark.parametrize('source', ['ghproxy', 'ghfast', 'github'])
def test_manual_source_failure_never_switches_without_user_selection(tmp_path, source):
    manager = Manager(tmp_path, Runner())
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ConnectError('unavailable', request=request)

    try:
        engines, _ = installer(manager, handle)
        engines.start('cpu', source=source)
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'failed'
        assert len(requests) == 1 and not manager.models.runtime()
    finally:
        manager.close()


def test_pause_does_not_fall_through_to_another_source(tmp_path):
    manager = Manager(tmp_path, Runner())
    requests = []

    def handle(request):
        requests.append(request)
        engines.cancel['cpu'].set()
        return httpx.Response(200, content=payloads['engine.zip'])

    try:
        engines, payloads = installer(manager, handle)
        engines.start('cpu')
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'paused' and len(requests) == 1
    finally:
        manager.close()


def test_manual_switch_resumes_download_and_upgrade_uses_saved_source(tmp_path):
    manager = Manager(tmp_path, Runner())
    requests = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield payloads['engine.zip'][:262144]
            raise httpx.ReadError('connection lost')

    def handle(request):
        requests.append(request)
        if request.url.host == 'gh-proxy.org':
            return httpx.Response(200, stream=BrokenStream())
        content = payloads[request.url.path.split('/')[-1]]
        if 'range' in request.headers:
            assert request.headers['range'] == 'bytes=262144-'
            return httpx.Response(206, content=content[262144:],
                headers={'Content-Range': f'bytes 262144-{len(content)-1}/{len(content)}'})
        return httpx.Response(200, content=content)

    try:
        engines, payloads = installer(manager, handle, large_engine())
        engines.start('cpu', source='ghproxy')
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'failed'
        engines.start('cpu', source='ghfast')
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'done'
        assert requests[1].headers['range'] == 'bytes=262144-'
        update_engine(engines, payloads)
        engines.start('cpu', upgrade=True)
        engines.threads['cpu'].join(3)
        assert engines.states['cpu']['status'] == 'done'
        assert requests[-1].url.host == 'ghfast.top' and requests[-1].url.path.endswith('new-engine.zip')
    finally:
        manager.close()


def test_api_validates_sources_and_passes_selection_to_install_and_upgrade(tmp_path):
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        manager = client.app.state.manager
        engines, payloads = installer(manager)
        before = manager.config.path.read_bytes()
        for value in ('https://unknown.example/', 'unknown'):
            assert client.post('/models/runtime/download', json={'backend': 'cpu', 'source': value}).status_code == 422
        assert manager.config.path.read_bytes() == before
        assert client.post('/models/runtime/download', json={'backend': 'cpu', 'source': 'ghfast'}).status_code == 202
        engines.threads['cpu'].join(3)
        update_engine(engines, payloads)
        assert client.post('/models/runtime/upgrade', json={'backend': 'cpu', 'source': 'ghproxy'}).status_code == 202
        engines.threads['cpu'].join(3)
        status = client.get('/models').json()['engine']
        assert status['download_source'] == 'ghproxy'
        assert {item['id'] for item in status['download_sources']} == {'auto', 'ghproxy', 'ghfast', 'github'}
        assert engines.states['cpu']['source'] == 'ghproxy'


def test_new_gui_rejects_service_that_would_ignore_mirror_selection(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        health = client.get('/health').json()
        assert 'engine_download_sources' in health['capabilities']
        window = Window(8799, tmp_path, autostart=False)
        qtbot.addWidget(window)
        for timer in (window.timer, window.document_timer, window.settings.timer):
            timer.stop()
        errors, polls = [], []
        monkeypatch.setattr(window, 'show_error', errors.append)
        monkeypatch.setattr(window, 'poll', lambda: polls.append(True))
        old = dict(health, capabilities=[c for c in health['capabilities'] if c != 'engine_download_sources'])
        window._existing_service(old)
        assert not window.ready and errors and '旧版本' in errors[0] and not polls
        window._existing_service(health)
        assert window.ready and polls == [True]
        window.close()


def test_ui_preserves_selected_source_passes_it_to_download_and_disables_while_busy(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    with TestClient(create_app(tmp_path / 'data', runner=Runner())) as client:
        manager = client.app.state.manager
        engines, _ = installer(manager)
        with manager.config.lock:
            atomic_json(manager.config.path, {**manager.config.read(), 'engine_download_source': 'ghproxy'})
        window = Window(8799, manager.root, autostart=False)
        qtbot.addWidget(window)
        for timer in (window.timer, window.document_timer, window.settings.timer):
            timer.stop()
        window.ready = True
        panel = window.settings
        monkeypatch.setattr(window, 'poll', lambda: None)
        monkeypatch.setattr(panel, 'refresh', lambda: None)

        def call(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)

        monkeypatch.setattr(window, 'call', call)
        panel.model_data = {'engine': engines.listing()}
        panel.engine_backend.setCurrentIndex(panel.engine_backend.findData('cpu'))
        panel.render_engine()
        assert panel.engine_source.currentData() == 'ghproxy'
        panel.engine_source.setCurrentIndex(panel.engine_source.findData('ghfast'))
        panel.render_engine()
        assert panel.engine_source.currentData() == 'ghfast'
        manager.current = ('busy', 1)
        try:
            panel.download_engine(backend='cpu')
            qtbot.waitUntil(lambda: engines.states['cpu']['status'] == 'waiting', timeout=3000)
            panel.model_data = {'engine': engines.listing()}
            panel.render_engine()
            assert not panel.engine_source.isEnabled()
            assert engines.states['cpu']['source'] == 'ghfast'
            assert 'ghfast' in panel.engine_status.toolTip()
            panel.pause_engine()
            engines.threads['cpu'].join(3)
            panel.model_data = {'engine': engines.listing()}
            panel.render_engine()
            assert panel.engine_source.isEnabled()
        finally:
            manager.current = None
            window.close()
