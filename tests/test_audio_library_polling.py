"""The audio library answers "is it on disk" once per folder, and not at all when nothing changed."""
import os
import wave
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.audio_files import present_audio, present_files
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from test_service import ControlledRunner, wait_job

CLIPS = 60


def tone(path, seconds=0.2):
    """A real WAV, so a backfilled duration can actually be read."""
    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b"\x00\x00" * int(16000 * seconds))


def library(tmp_path, *, files=True, duration=3.0, distinct=CLIPS):
    """A saved library of done clips, without running inference."""
    store = Store(tmp_path)
    voice = {"backend": "windows_sapi", "family": "", "id": "default", "name": "系统声音"}
    segments = [{"text": "第 %d 句" % i} for i in range(CLIPS)]
    job = store.create("压力测试", voice, "fp", segments)
    folder = tmp_path / "audio"
    folder.mkdir(exist_ok=True)
    names = ["%064x.wav" % (i % distinct) for i in range(CLIPS)]
    if files:
        for name in set(names):
            tone(folder / name)
    indices = [row[0] for row in store.db.execute("SELECT idx FROM segments WHERE job_id=? ORDER BY idx", (job,))]
    assert len(indices) == CLIPS
    with store.db:
        store.db.executemany("UPDATE segments SET status='done',audio=?,duration=? WHERE job_id=? AND idx=?",
                             [(name, duration, job, index) for index, name in zip(indices, names)])
    store.close()
    return Manager(tmp_path)


def test_present_files_matches_what_is_actually_there(tmp_path):
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / "one.wav").write_bytes(b"RIFF")
    (folder / "note.txt").write_text("x", encoding="utf-8")
    (folder / "nested").mkdir()
    assert present_files(folder) == {"one.wav", "note.txt"}
    assert present_audio(tmp_path) == {"one.wav", "note.txt"}
    assert present_files(tmp_path / "missing") == set()


def test_audio_list_reads_the_folder_once_instead_of_statting_each_clip(tmp_path, monkeypatch):
    manager = library(tmp_path)
    scans = []
    original = os.scandir

    def counted(path):
        scans.append(Path(path))
        return original(path)

    statted = []
    original_is_file = Path.is_file

    def watched(self, *args, **kwargs):
        statted.append(self)
        return original_is_file(self, *args, **kwargs)

    monkeypatch.setattr("voice_workbench.audio_files.os.scandir", counted)
    monkeypatch.setattr(Path, "is_file", watched)
    try:
        rows = manager.generated_audio()
    finally:
        manager.close()
    assert len(rows) == CLIPS and all(row["available"] for row in rows)
    # One pass over the audio folder, and no per-record existence check on the clips.
    assert [path.name for path in scans if path.name == "audio"] == ["audio"]
    assert not [path for path in statted if path.parent.name == "audio"]


def test_missing_clips_are_marked_unavailable_without_opening_them(tmp_path):
    manager = library(tmp_path, files=False)
    try:
        rows = manager.generated_audio()
    finally:
        manager.close()
    assert len(rows) == CLIPS and not any(row["available"] for row in rows)


def test_a_shared_take_is_opened_once_for_its_duration(tmp_path, monkeypatch):
    # Three clips serve all sixty records, the way a retried take or a shared
    # line does: the header must be read once per file, not once per record.
    manager = library(tmp_path, duration=None, distinct=3)
    opened = []
    from voice_workbench import manager as manager_module
    original = manager_module.validate_wave

    def counted(path):
        opened.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(manager_module, "validate_wave", counted)
    try:
        rows = manager.generated_audio()
    finally:
        manager.close()
    assert all(row["duration"] is not None for row in rows)
    assert len(opened) == 3 and len(set(opened)) == 3


def test_unchanged_library_answers_304_and_changes_after_a_new_take(tmp_path):
    with TestClient(create_app(tmp_path, runner=ControlledRunner())) as client:
        job = client.post("/jobs", json={"segments": [{"text": "第一句"}]}).json()["job_id"]
        wait_job(lambda key: client.get(f"/jobs/{key}").json(), job)
        first = client.get("/generated-audio")
        tag = first.headers["etag"]
        assert first.status_code == 200 and len(first.json()) == 1

        unchanged = client.get("/generated-audio", headers={"If-None-Match": tag})
        assert unchanged.status_code == 304 and unchanged.content == b""
        assert unchanged.headers["etag"] == tag

        client.post(f"/jobs/{job}/generate", json={"segments": [{"text": "第二句"}]})
        wait_job(lambda key: client.get(f"/jobs/{key}").json(), job)
        grown = client.get("/generated-audio", headers={"If-None-Match": tag})
        assert grown.status_code == 200 and len(grown.json()) == 2
        assert grown.headers["etag"] != tag

        removed = client.post("/generated-audio/delete", json={"ids": [grown.json()[0]["id"]]}).json()
        assert removed["deleted"]
        after_delete = client.get("/generated-audio", headers={"If-None-Match": grown.headers["etag"]})
        assert after_delete.status_code == 200 and len(after_delete.json()) == 1


def test_desktop_keeps_the_table_when_the_list_did_not_change(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    window = Window(8794, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    window.ready = True
    window.set_page(3)
    window.show()
    panel = window.settings
    rendered, headers = [], []

    class Client:
        def get(self, path, **kwargs):
            if path == "/generated-audio":
                headers.append(kwargs.get("headers"))
                if len(headers) == 1:
                    return httpx.Response(200, json=[{"id": "a:1"}], headers={"ETag": '"v1"'})
                return httpx.Response(304, headers={"ETag": '"v1"'})
            return httpx.Response(200, json={})

    def call(action, success=None, failure=None):
        value = action(Client())
        if success:
            success(value)

    monkeypatch.setattr(window, "call", call)
    monkeypatch.setattr(panel, "render_models", lambda: None)
    monkeypatch.setattr(panel, "render_audio", lambda: rendered.append(True))
    try:
        panel.set_section(1)
        assert panel.audio_data == [{"id": "a:1"}] and len(rendered) == 1
        assert panel.audio_revision == '"v1"'
        assert headers[0] == {}  # nothing cached yet: a full request
        panel.refresh()
        assert headers[1] == {"If-None-Match": '"v1"'}
        assert len(rendered) == 1  # unchanged list is not rebuilt
        assert panel.audio_data == [{"id": "a:1"}]
        panel.reload()
        assert headers[2] == {}  # the refresh button always asks in full
    finally:
        window.close()
