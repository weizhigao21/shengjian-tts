"""Generation completion and preset selection stay aligned with the actual request."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import time

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtMultimedia import QMediaPlayer

from voice_workbench.api import create_app
from voice_workbench.audio_cpp import build_native_request
from voice_workbench.config import ConfigStore
from voice_workbench.gui import Window
from test_inline_generation import Runner, index_voice, make_audio
from test_library_and_deletion import wait_job
from test_model_capabilities import gguf


def segment(idx, generation, status="done", audio=""):
    return {"idx": idx, "generation": generation, "status": status, "audio": audio,
            "text": f"片段 {idx}", "timestamp": "", "created": time.time(), "cached": 0,
            "error": {"message": "失败原因"} if status == "failed" else None}


def player_window(tmp_path, qtbot, monkeypatch):
    (tmp_path / "audio").mkdir()
    make_audio(tmp_path / "audio/old.wav")
    window = Window(8799, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    window.selected_job = "work"
    monkeypatch.setattr(window, "poll", lambda: None)
    played = []
    monkeypatch.setattr(window.player, "play", lambda: played.append(window.current_segment()["idx"]))
    job = {"id": "work", "title": "作品", "status": "done", "created": time.time(),
           "segments": [segment(1, 1, audio="old.wav")]}
    window.render_job(job)
    assert played == []  # Opening completed history does not start playback.
    return window, job, played


def test_latest_submission_plays_once_in_order_skips_failure_and_stays_in_its_batch(tmp_path, qtbot, monkeypatch):
    window, job, played = player_window(tmp_path, qtbot, monkeypatch)
    for name in ("second.wav", "third.wav", "external.wav"):
        make_audio(tmp_path / "audio" / name)
    window.submitted({"job_id": "work", "generation": 2, "segment_indices": [2, 3, 4]})
    job["status"] = "running"
    job["segments"] += [segment(2, 2, "failed"), segment(3, 2, audio="second.wav"), segment(4, 2, "running")]
    window.render_job(deepcopy(job))
    assert played == [] and window.current_segment()["idx"] == 1
    job["segments"][-1].update(status="done", audio="third.wav")
    job["status"] = "failed"
    window.render_job(deepcopy(job))
    assert played == [3] and window.current_segment()["idx"] == 3
    assert window.pending_autoplay is None
    for _ in range(3):
        window.render_job(deepcopy(job))
    assert played == [3]
    window.playback_finished(QMediaPlayer.MediaStatus.EndOfMedia)
    assert played == [3, 4]
    job["segments"].append(segment(5, 3, audio="external.wav"))
    window.render_job(deepcopy(job))
    window.playback_finished(QMediaPlayer.MediaStatus.EndOfMedia)
    assert played == [3, 4]  # Automatic continuation never enters a different generation.
    window.play_adjacent(1)
    assert played == [3, 4, 5]
    job["segments"].append(dict(segment(6, 4, audio="external.wav"), cached=1))
    window.submitted({"job_id": "work", "generation": 4, "segment_indices": [6]})
    window.render_job(deepcopy(job))
    window.render_job(deepcopy(job))
    assert played == [3, 4, 5, 6]  # Cached audio also starts exactly once.
    window.close()


@pytest.mark.parametrize("status,audio", [("failed", ""), ("cancelled", ""), ("done", "missing.wav")])
def test_unsuccessful_or_missing_new_audio_never_replays_old_history(tmp_path, qtbot, monkeypatch, status, audio):
    window, job, played = player_window(tmp_path, qtbot, monkeypatch)
    window.submitted({"job_id": "work", "generation": 2, "segment_indices": [2]})
    job["segments"].append(segment(2, 2, status, audio))
    job["status"] = status
    window.render_job(deepcopy(job))
    assert played == [] and window.pending_autoplay is None
    window.close()


def test_leaving_a_work_clears_pending_playback_and_completed_history_stays_silent(tmp_path, qtbot, monkeypatch):
    window, job, played = player_window(tmp_path, qtbot, monkeypatch)
    window.submitted({"job_id": "work", "generation": 2, "segment_indices": [2]})
    window.new_document()
    assert window.pending_autoplay is None and window.autoplay_indices is None
    window.selected_job = "work"
    job["segments"].append(segment(2, 2, audio="old.wav"))
    window.render_job(deepcopy(job))
    assert played == []
    window.close()


def clone_and_design_voice(tmp_path):
    voice = index_voice(tmp_path)
    path = tmp_path / "vox.gguf"
    gguf(path, {"schema_version": 1, "family": "voxcpm2", "capabilities": {
        "clone": ["speaker_reference"], "design": ["voice_design"]}})
    return {**voice, "family": "voxcpm2", "model_dir": str(path), "voice_mode": "clone"}


@pytest.mark.parametrize("height", [720, 900])
def test_mode_buttons_stay_in_place_across_validation_and_scrollbar_changes(tmp_path, qtbot, monkeypatch, height):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtWidgets import QLabel

    voice = clone_and_design_voice(tmp_path)
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    with TestClient(create_app(root, runner=Runner())) as client:
        window = Window(8799, root, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        def immediate(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)
        monkeypatch.setattr(window, "call", immediate)
        window.poll()
        window.refresh_generation_models(voice["id"])
        window.text_edit.setPlainText("测试生成方式切换")
        window.resize(1080, height)
        window.show()
        qtbot.wait(30)
        controls = window.model_controls
        bar = window.inspector_scroll.verticalScrollBar()
        anchors = (window.generate_button, window.save_generation_button, window.model_combo,
                   window.voice_combo, controls.mode_strip, window.voice_health)
        def geometry():
            return [(w.mapTo(window, QPoint()), w.size()) for w in anchors]
        def click_mode(mode):
            target = next(b for b in controls.mode_group.buttons() if b.property("mode") == mode)
            qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
            qtbot.wait(20)
            assert controls.mode.currentData() == mode and target.isChecked()
        scroll_ranges = set()
        for expanded in (False, True):
            controls.advanced_toggle.setChecked(expanded)
            controls.runtime_toggle.setChecked(expanded)
            qtbot.wait(20)
            bar.setValue(100 if expanded else 0)
            qtbot.wait(20)
            baseline, offset = geometry(), bar.value()
            for _ in range(2):
                for mode, description, original, enabled in (
                    ("design", "", "", False), ("design", "温柔自然的女声", "", True),
                    ("clone", "温柔自然的女声", "", True), ("continuation", "", "", False),
                    ("continuation", "", "参考音频中的原文", True), ("clone", "", "", True)):
                    click_mode(mode)
                    controls.description.setText(description)
                    controls.reference_text.setPlainText(original)
                    window.poll()
                    qtbot.wait(20)
                    assert geometry() == baseline
                    assert bar.value() == offset
                    assert window.generate_button.isEnabled() is enabled
                    assert window.voice_health.isVisible()
                    scroll_ranges.add(bar.maximum())
            # A long file-path error must keep its full details without wrapping the action area.
            missing = str(tmp_path / ("missing-engine-" * 12 + ".exe"))
            controls.assets["engine_executable"].setText(missing)
            qtbot.wait(20)
            assert not window.generate_button.isEnabled()
            assert missing in window.voice_health.toolTip()
            assert QLabel.text(window.voice_health).endswith("…")
            assert geometry() == baseline and bar.value() == offset
            controls.assets["engine_executable"].setText(voice["engine_executable"])
            qtbot.wait(20)
            assert window.generate_button.isEnabled() and geometry() == baseline
        assert 0 in scroll_ranges and any(value > 0 for value in scroll_ranges)
        window.close()


def test_mode_change_detaches_preset_keeps_edits_and_records_design_without_reference(tmp_path, qtbot, monkeypatch):
    class CaptureRunner(Runner):
        def __init__(self):
            self.requests = []
        def generate(self, request, cancelled):
            self.requests.append(build_native_request(request["voice"], request["text"]))
            super().generate(request, cancelled)

    voice = clone_and_design_voice(tmp_path)
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    runner = CaptureRunner()
    with TestClient(create_app(root, runner=runner)) as client:
        window = Window(8799, root, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        def immediate(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)
        monkeypatch.setattr(window, "call", immediate)
        monkeypatch.setattr(window.player, "play", lambda: None)
        window.poll()
        window.refresh_generation_models(voice["id"])
        controls = window.model_controls
        stored = window.selected_preset()
        controls.generation_controls.fields["guidance_scale"].setValue(2.8)
        assert window.selected_preset()["id"] == stored["id"]
        assert "已调整" in window.voice_combo.currentText()
        controls.mode.setCurrentIndex(controls.mode.findData("design"))
        controls.description.setText("温柔自然的女声")
        assert window.voice_combo.currentData() is None
        assert "已调整" not in window.voice_combo.itemText(window.voice_combo.findData(stored["id"]))
        assert controls.generation_controls.fields["guidance_scale"].value() == 2.8
        assert not window.link_config.toPlainText()
        # A deleted reference belonging to the old clone preset cannot block voice design.
        from pathlib import Path
        Path(stored["reference_audio"]).unlink()
        window.poll()
        assert window.voice_combo.currentData() is None
        assert controls.mode.currentData() == "design" and controls.description.text() == "温柔自然的女声"
        # Refreshing the shared engine applies the new path without restoring the old preset mode.
        fresh_engine = tmp_path / "updated-server.exe"
        fresh_engine.write_bytes(b"updated engine fixture")
        updated = {**stored, "name": "改名后的克隆预设", "engine_executable": str(fresh_engine)}
        assert client.put(f"/voices/{stored['id']}", json=updated).status_code == 200
        window.poll()
        assert window.voice_combo.currentData() is None and controls.mode.currentData() == "design"
        assert controls.description.text() == "温柔自然的女声"
        assert controls.generation_controls.fields["guidance_scale"].value() == 2.8
        assert controls.assets["engine_executable"].text() == str(fresh_engine)
        window.text_edit.setPlainText("新的声音设计")
        assert window.generate_button.isEnabled()
        window.generate()
        job = wait_job(client, window.selected_job)
        item = job["segments"][-1]
        assert item["voice"]["voice_mode"] == "design" and item["voice"]["reference_audio"] == ""
        assert item["provenance"] == {"preset_used": False, "preset_id": None, "preset_name": None,
                                     "preset_modified": False, "mode": "design"}
        native = runner.requests[0]["request"]
        assert native["options"]["guidance_scale"] == 2.8
        assert native["text"] == "(温柔自然的女声)新的声音设计"
        assert "voice_ref" not in native and "reference_text" not in native
        assert ConfigStore(root).read()["voices"][-1]["voice_mode"] == "clone"
        window.open_job_data(job)
        assert window.voice_combo.currentData() is None and controls.mode.currentData() == "design"
        assert controls.description.text() == "温柔自然的女声"
        controls.mode.setCurrentIndex(controls.mode.findData("clone"))
        assert window.voice_combo.currentData() is None  # Returning to a mode never silently reattaches a preset.
        window.voice_combo.setCurrentIndex(window.voice_combo.findData(stored["id"]))
        assert controls.mode.currentData() == "clone" and controls.description.text() == ""
        assert controls.generation_controls.fields["guidance_scale"].value() == 2
        window.voice_combo.setCurrentIndex(0)
        assert controls.mode.currentData() == "clone"
        window.close()


def test_api_explicit_none_and_changed_mode_have_correct_preset_provenance(tmp_path):
    voice = clone_and_design_voice(tmp_path)
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    with TestClient(create_app(root, runner=Runner())) as client:
        for overrides, use_preset in (({}, False), ({"voice_mode": "design", "reference_audio": "", "reference_text": "",
                "voice_description": "自然的女声"}, True)):
            result = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "你好"}],
                "overrides": overrides, "use_preset": use_preset})
            assert result.status_code == 202, result.text
            job = wait_job(client, result.json()["job_id"])
            source = job["segments"][0]["provenance"]
            assert source["preset_used"] is False and source["preset_id"] is None
