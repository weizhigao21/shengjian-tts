from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import pytest

from fastapi.testclient import TestClient

from integration.local_voice_client import generate_local_tasks, run_local_job
from voice_workbench.api import create_app
from test_service import ControlledRunner, wait_job
from test_workflow_client import Session
from test_workflow_retention import Cache


class BlockingSecond(ControlledRunner):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def generate(self, request, cancelled):
        if request['text']=='second':
            while not self.release.wait(.01) and not cancelled.is_set():
                pass
        return super().generate(request, cancelled)


def test_download_and_callback_arrive_before_whole_job_finishes(tmp_path):
    runner = BlockingSecond()
    received, ready = [], threading.Event()
    tasks = [(41,'00:01','first',str(tmp_path),0), (42,'00:02','second',str(tmp_path),0)]
    with TestClient(create_app(tmp_path/'data', runner)) as client, ThreadPoolExecutor(1) as pool:
        def receive(index, value):
            received.append((index, value))
            if index==0: ready.set()
        future = pool.submit(generate_local_tasks, Session(client), 'http://testserver', 'default',
            tasks, Cache(), 'voice', lambda i,*_: str(tmp_path/f'{i}.wav'),
            lambda url,path: Path(path).write_bytes(client.get(url).content), on_result=receive)
        try:
            assert ready.wait(5), 'First output waited for the entire job'
            assert not future.done() and (tmp_path/'41.wav').is_file()
            job = client.app.state.manager.store.list(include_workflow=True)[0]
            progress = client.get(f"/jobs/{job['id']}/generations/1/progress").json()
            assert progress['status']=='running' and progress['completed']==1
            assert len(progress['segments'])==1 and 'voice' not in progress['segments'][0]
        finally:
            runner.release.set()
        assert all(r[0] for r in future.result(5))
        assert [i for i,_ in received]==[0,1]


def test_cancel_retains_already_downloaded_audio_and_never_redelivers(tmp_path):
    runner = BlockingSecond()
    stopped, received = threading.Event(), []
    with TestClient(create_app(tmp_path/'data', runner)) as client:
        def receive(index, result):
            received.append(index)
            if index==0: stopped.set()
        result = generate_local_tasks(Session(client), 'http://testserver', 'default',
            [(1,'','first',str(tmp_path),0),(2,'','second',str(tmp_path),0)], Cache(), 'voice',
            lambda i,*_:str(tmp_path/f'{i}.wav'),
            lambda url,path:Path(path).write_bytes(client.get(url).content),
            cancel_check=stopped.is_set, on_result=receive)
        assert [r[0] for r in result]==[True,False]
        assert (tmp_path/'1.wav').is_file() and not (tmp_path/'2.wav').exists()
        assert received==[0,1]
        assert '取消' in result[1][1]


def test_paused_generation_does_not_block_other_work_and_can_resume(tmp_path):
    runner=ControlledRunner()
    runner.block.set()
    with TestClient(create_app(tmp_path, runner)) as client:
        manager = client.app.state.manager
        held = client.post('/jobs', json={'segments':[{'text':'active'},{'text':'held'}], 'record_only':True}).json()
        wait_job(manager.store.get, held['job_id'], ('running',))
        control = f"/jobs/{held['job_id']}/generations/{held['generation']}"
        assert client.post(control+'/pause').json()['paused'] is True
        other = client.post('/jobs', json={'segments':[{'text':'other'}]}).json()
        runner.block.clear()
        assert wait_job(manager.store.get, other['job_id'])['status']=='done'
        assert [s['status'] for s in manager.store.get(held['job_id'])['segments']]==['done','queued']
        assert client.get(control+'/progress').json()['paused'] is True
        assert client.post(control+'/resume').json()['paused'] is False
        assert wait_job(manager.store.get, held['job_id'])['status']=='done'
        missing = client.post(f"/jobs/{held['job_id']}/generations/99/pause")
        assert missing.status_code != 200


class FakeSession:
    def __init__(self, snapshots, incremental=True):
        self.snapshots = iter(snapshots)
        self.incremental = incremental
        self.posts, self.gets = [], []

    def post(self, url, **kwargs):
        self.posts.append(url)
        body = {'job_id':'job','generation':3,'segment_indices':[5,7]} if url.endswith('/jobs') else {}
        return SimpleNamespace(status_code=202 if url.endswith('/jobs') else 200, json=lambda:body)

    def get(self, url, **kwargs):
        self.gets.append(url)
        if url.endswith('/health'):
            value = {'capabilities':['workflow_record_only'],'workflow_incremental':self.incremental}
        else:
            value = next(self.snapshots)
            if isinstance(value, Exception): raise value
        return SimpleNamespace(status_code=200, json=lambda:value)


def test_out_of_order_partial_results_survive_transport_error_without_resubmission():
    session = FakeSession([{'status':'running','segments':[{'idx':7,'status':'done','audio_url':'/audio/second.wav'}]},
                           OSError('disconnected')])
    received=[]
    result=run_local_job(session,'http://testserver','default',[(1,'','a','',0),(2,'','b','',0)],
        on_result=lambda i,r:received.append(i))
    assert result[0][0] is None and result[1][0].endswith('second.wav')
    assert received==[1,0] and len(session.posts)==1


def test_pause_is_forwarded_once_per_change_and_old_service_can_still_poll(monkeypatch):
    monkeypatch.setattr('integration.local_voice_client.time.sleep', lambda _:None)
    snapshots=[{'status':'queued','segments':[]}, {'status':'done','segments':[
        {'idx':5,'status':'done','audio_url':'/audio/a.wav'}, {'idx':7,'status':'done','audio_url':'/audio/b.wav'}]}]
    paused=iter((True,False))
    session=FakeSession(snapshots)
    result=run_local_job(session,'http://testserver','default',[(1,'','a','',0),(2,'','b','',0)],pause_check=lambda:next(paused))
    assert all(url for url,_ in result)
    assert [s.rsplit('/',1)[-1] for s in session.posts]==['jobs','pause','resume']
    old=FakeSession(snapshots[-1:],incremental=False)
    assert all(url for url,_ in run_local_job(old,'http://testserver','default',[(1,'','a','',0),(2,'','b','',0)]))
    assert not any(s.endswith('/progress') for s in old.gets)


def test_cancel_before_submission_does_not_create_a_job():
    session=FakeSession([])
    result=run_local_job(session,'http://testserver','default',[(1,'','a','',0)],cancel_check=lambda:True)
    assert '未提交' in result[0][1] and not session.posts


def test_cancel_during_one_progress_snapshot_stops_remaining_deliveries():
    session = FakeSession([{'status': 'done', 'segments': [
        {'idx': 5, 'status': 'done', 'audio_url': '/audio/a.wav'},
        {'idx': 7, 'status': 'done', 'audio_url': '/audio/b.wav'}]}])
    stopped, received = threading.Event(), []

    def receive(position, value):
        received.append(position)
        if position == 0:
            stopped.set()

    result = run_local_job(session, 'http://testserver', 'default',
        [(1, '', 'a', '', 0), (2, '', 'b', '', 0)],
        cancel_check=stopped.is_set, on_result=receive)
    assert result[0][0].endswith('a.wav') and result[1][0] is None
    assert '取消' in result[1][1] and received == [0, 1]
    assert session.posts[-1].endswith('/generations/3/cancel')


def test_paused_time_does_not_exhaust_generation_timeout(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr('integration.local_voice_client.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('integration.local_voice_client.time.sleep', lambda _: setattr(clock, 'now', clock.now + 10))
    session = FakeSession([
        {'status': 'queued', 'segments': []}, {'status': 'queued', 'segments': []},
        {'status': 'done', 'segments': [
            {'idx': 5, 'status': 'done', 'audio_url': '/audio/a.wav'},
            {'idx': 7, 'status': 'done', 'audio_url': '/audio/b.wav'}]}])
    states = iter((True, True, False))
    result = run_local_job(session, 'http://testserver', 'default',
        [(1, '', 'a', '', 0), (2, '', 'b', '', 0)], timeout=1,
        pause_check=lambda: next(states))
    assert all(url for url, _ in result)
    assert [url.rsplit('/', 1)[-1] for url in session.posts] == ['jobs', 'pause', 'resume']


@pytest.mark.parametrize('incremental', [True, False])
def test_progress_renews_whole_intake_timeout_but_legacy_keeps_total_limit(monkeypatch, incremental):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr('integration.local_voice_client.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('integration.local_voice_client.time.sleep', lambda _: setattr(clock, 'now', clock.now + .6))
    first = {'idx': 5, 'status': 'done', 'audio_url': '/audio/a.wav'}
    session = FakeSession([
        {'status': 'queued', 'segments': []}, {'status': 'running', 'segments': [first]},
        {'status': 'done', 'segments': [first, {'idx': 7, 'status': 'done', 'audio_url': '/audio/b.wav'}]}],
        incremental=incremental)
    result = run_local_job(session, 'http://testserver', 'default',
        [(1, '', 'a', '', 0), (2, '', 'b', '', 0)], timeout=1)
    assert bool(result[0][0]) and bool(result[1][0]) == incremental
    if not incremental:
        assert '超过' in result[1][1]
    assert len(session.posts) == 1


def test_repeated_progress_does_not_keep_an_idle_intake_alive(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr('integration.local_voice_client.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('integration.local_voice_client.time.sleep', lambda _: setattr(clock, 'now', clock.now + .6))
    first = {'idx': 5, 'status': 'done', 'audio_url': '/audio/a.wav'}
    session = FakeSession([{'status': 'running', 'segments': [first]}] * 3)
    received = []
    result = run_local_job(session, 'http://testserver', 'default',
        [(1, '', 'a', '', 0), (2, '', 'b', '', 0)], timeout=1, on_result=lambda i, _: received.append(i))
    assert result[0][0] and result[1][0] is None and '超过' in result[1][1]
    assert received == [0, 1] and len(session.posts) == 1


@pytest.mark.parametrize('contents', [b'not a WAV', 'truncated', None])
def test_bad_download_preserves_existing_output_and_does_not_ack(tmp_path, contents):
    from test_service import make_wave
    output = tmp_path / 'out.wav'
    make_wave(output)
    original = output.read_bytes()
    data = original[:-4] if contents == 'truncated' else contents
    with TestClient(create_app(tmp_path / 'data', ControlledRunner())) as client:
        posts = []

        class RecordingSession(Session):
            def post(self, url, **kwargs):
                posts.append(url)
                return super().post(url, **kwargs)

        def download(url, target):
            if data is not None:
                Path(target).write_bytes(data)

        cache = Cache()
        result = generate_local_tasks(RecordingSession(client), 'http://testserver', 'default',
            [(1, '', 'hello', str(tmp_path), 0)], cache, 'voice', lambda *_: output, download)
        assert not result[0][0] and output.read_bytes() == original
        assert not cache.saved and not any(url.endswith('/release') for url in posts)
        assert not list(tmp_path.glob('.tts-*'))


def test_empty_intake_does_not_contact_service():
    session = FakeSession([])
    assert run_local_job(session, 'http://testserver', 'default', []) == []
    assert not session.gets and not session.posts


def test_pause_scope_and_cancel_allow_next_generation_without_resuming_old_rows(tmp_path):
    runner = ControlledRunner()
    runner.block.set()
    with TestClient(create_app(tmp_path, runner)) as client:
        manager = client.app.state.manager
        first = client.post('/jobs', json={'workflow_key': 'one-run', 'segments': [
            {'text': 'active'}, {'text': 'paused'}]}).json()
        wait_job(manager.store.get, first['job_id'], ('running',))
        control = f"/jobs/{first['job_id']}/generations/1"
        client.post(control + '/pause').raise_for_status()
        second = client.post('/jobs', json={'workflow_key': 'one-run', 'segments': [{'text': 'next generation'}]}).json()
        runner.block.clear()
        wait_job(lambda key: client.get(f'/jobs/{key}?generation=2').json(), first['job_id'], ('done',))
        assert second['job_id'] == first['job_id'] and runner.calls == ['active', 'next generation']
        assert client.get(control + '/progress').json()['paused']
        client.post(control + '/cancel').raise_for_status()
        progress = client.get(control + '/progress').json()
        assert progress['status'] == 'cancelled' and not progress['paused']
        assert [row['status'] for row in progress['segments']] == ['done', 'cancelled']
        client.post('/jobs/' + first['job_id'] + '/retry').raise_for_status()
        assert wait_job(manager.store.get, first['job_id'], ('done',))['completed'] == 3
        client.delete('/jobs/' + first['job_id']).raise_for_status()
        for operation in ('progress', 'pause', 'resume'):
            response = (client.get if operation == 'progress' else client.post)(control + '/' + operation)
            assert response.status_code == 404


def test_pause_is_recovered_after_restart_and_resume_finishes_queued_audio(tmp_path):
    from voice_workbench.manager import Manager
    runner = ControlledRunner()
    runner.block.set()
    manager = Manager(tmp_path, runner)
    try:
        identity = manager.submit('restart', 'default', [{'text': 'first'}, {'text': 'second'}])
        wait_job(manager.store.get, identity, ('running',))
        with manager.lock:
            manager.store.pause_generation(identity, 1, True)
    finally:
        manager.close()
    runner = ControlledRunner()
    manager = Manager(tmp_path, runner)
    try:
        assert manager.store.generation_progress(identity, 1)['paused']
        assert manager.store.next() is None and not runner.calls
        with manager.lock:
            manager.store.pause_generation(identity, 1, False)
            manager.wake.set()
        assert wait_job(manager.store.get, identity, ('done',))['completed'] == 2
    finally:
        manager.close()
