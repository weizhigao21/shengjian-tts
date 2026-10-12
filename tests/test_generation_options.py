"""Model contracts, advanced values, inference forwarding and cache isolation."""
import time

import pytest
from fastapi.testclient import TestClient
from voice_workbench.api import create_app
from voice_workbench.audio_cpp import build_native_request
from voice_workbench.config import ConfigStore
from voice_workbench.model_info import model_ui
from voice_workbench.model_controls import ModelControls
from test_model_capabilities import gguf, kokoro
from test_inline_generation import index_voice, make_audio


def vox_voice(tmp_path, options=None):
    voice = index_voice(tmp_path)
    path = tmp_path / "vox.gguf"
    spec = {"schema_version": 1, "family": "voxcpm2", "capabilities": {"design": ["voice_design"]}}
    if options is not None:
        spec["options"] = {"request": options}
    gguf(path, spec, config={"max_length": 4096})
    return {**voice, "family": "voxcpm2", "model_dir": str(path), "voice_mode": "design", "reference_audio": "",
            "voice_description": "自然、温柔的女声"}


def test_vox_advanced_controls_reach_native_request_and_survive_refresh(tmp_path, qapp):
    voice = vox_voice(tmp_path)
    controls = ModelControls()
    controls.bind(voice)
    fields = controls.generation_controls.fields
    fields["guidance_scale"].setValue(2.8)
    fields["num_inference_steps"].setValue(14)
    fields["text_chunk_size"].setValue(64)
    fields["voxcpm2.chunk_strategy"].setCurrentIndex(fields["voxcpm2.chunk_strategy"].findData("stateless"))
    fields["seed"].setValue(45)
    fields["retry_badcase"].setChecked(False)
    controls.bind(dict(voice))
    values = controls.overrides()["request_options"]
    assert values["guidance_scale"] == 2.8 and values["num_inference_steps"] == 14
    assert values["text_chunk_size"] == 64 and values["voxcpm2.chunk_strategy"] == "stateless"
    request = build_native_request({**voice, **controls.overrides()}, "Hello")["request"]
    assert request["options"] == values
    assert request["options"]["seed"] == 45 and request["options"]["retry_badcase"] is False
    controls.close()


def test_declared_option_contract_controls_ranges_and_available_fields(tmp_path, qapp):
    voice = vox_voice(tmp_path, [{"name": "guidance_scale", "type": "float", "default": 1.6, "min": 1, "max": 3}])
    controls = ModelControls()
    controls.bind(voice)
    assert set(controls.generation_controls.fields) == {"guidance_scale"}
    field = controls.generation_controls.fields["guidance_scale"]
    assert (field.minimum(), field.maximum(), field.value()) == (1, 3, 1.6)
    controls.generation_controls.reset()
    assert controls.overrides()["request_options"] == {"guidance_scale": 1.6}
    controls.close()


def test_seed_can_be_typed_randomized_and_keeps_its_value_on_refresh(tmp_path, qtbot, monkeypatch):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QSignalSpy

    voice = vox_voice(tmp_path)
    controls = ModelControls()
    qtbot.addWidget(controls)
    controls.bind(voice)
    controls.advanced_toggle.setChecked(True)
    controls.show()
    seed = controls.generation_controls.fields["seed"]
    assert controls.generation_controls.form.itemAt(0).widget().text() == "随机种子"
    assert seed.random.isChecked() and not seed.number.isEnabled() and seed.value() == -1
    qtbot.mouseClick(seed.random, Qt.MouseButton.LeftButton)
    seed.number.lineEdit().selectAll()
    qtbot.keyClicks(seed.number.lineEdit(), "2147483647")
    qtbot.keyClick(seed.number.lineEdit(), Qt.Key.Key_Enter)
    assert seed.value() == 2147483647
    controls.bind(dict(voice))
    assert controls.overrides()["request_options"]["seed"] == 2147483647
    qtbot.mouseClick(seed.random, Qt.MouseButton.LeftButton)
    assert controls.overrides()["request_options"]["seed"] == -1
    qtbot.mouseClick(seed.random, Qt.MouseButton.LeftButton)
    assert seed.value() == 2147483647
    monkeypatch.setattr("voice_workbench.generation_controls.secrets.randbelow", lambda maximum: 123)
    qtbot.mouseClick(seed.random, Qt.MouseButton.LeftButton)
    changed = QSignalSpy(controls.changed)
    qtbot.mouseClick(seed.new_seed, Qt.MouseButton.LeftButton)
    assert not seed.random.isChecked() and seed.number.isEnabled() and seed.value() == 123
    assert len(changed) == 1
    seed.number.lineEdit().selectAll()
    qtbot.keyClicks(seed.number.lineEdit(), "0")
    qtbot.keyClick(seed.number.lineEdit(), Qt.Key.Key_Enter)
    assert controls.overrides()["request_options"]["seed"] == 0
    controls.generation_controls.reset()
    assert controls.overrides()["request_options"]["seed"] == -1


def test_advanced_wheel_scrolls_page_without_editing_focused_controls(tmp_path, qtbot):
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtTest import QSignalSpy
    from PyQt6.QtWidgets import QApplication, QSlider
    from voice_workbench.gui import Window

    window = Window(8799, tmp_path / "data", autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    controls = window.model_controls
    controls.bind(vox_voice(tmp_path))
    controls.advanced_toggle.setChecked(True)
    controls.runtime_toggle.setChecked(True)
    window.resize(1080, 720)
    window.show()
    scroll = window.inspector_scroll
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)
    fields = controls.generation_controls.fields
    fields["guidance_scale"].setValue(2.8)
    fields["num_inference_steps"].setValue(18)
    fields["seed"].setValue(45)
    combo = fields["voxcpm2.chunk_strategy"]
    combo.setCurrentIndex(1)
    controls.device.setCurrentIndex(1)
    slider = controls.generation_controls.findChild(QSlider)
    targets = (fields["guidance_scale"], fields["guidance_scale"].lineEdit(),
               fields["num_inference_steps"], fields["seed"].number,
               fields["seed"].number.lineEdit(), slider, combo,
               controls.device, controls.timeout, controls.timeout.lineEdit())
    expected = controls.overrides()
    changed = QSignalSpy(controls.changed)
    for target in targets:
        scroll.ensureWidgetVisible(target, 0, 0)
        target.setFocus()
        QApplication.processEvents()
        assert target.hasFocus()
        before = scroll.verticalScrollBar().value()
        delta = -120 if before < scroll.verticalScrollBar().maximum() else 120
        pos = QPointF(target.rect().center())
        event = QWheelEvent(pos, QPointF(target.mapToGlobal(pos.toPoint())), QPoint(),
                            QPoint(0, delta), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(target, event)
        assert controls.overrides() == expected
        assert scroll.verticalScrollBar().value() != before, (type(target).__name__, target.objectName())
    assert len(changed) == 0

    # Explicit editing and mouse interactions still change the chosen parameter.
    field = fields["num_inference_steps"]
    scroll.ensureWidgetVisible(field, 0, 0)
    qtbot.mouseClick(field.lineEdit(), Qt.MouseButton.LeftButton)
    field.lineEdit().selectAll()
    qtbot.keyClicks(field.lineEdit(), "20")
    qtbot.keyClick(field.lineEdit(), Qt.Key.Key_Enter)
    assert field.value() == 20
    scroll.ensureWidgetVisible(slider, 0, 0)
    previous = slider.value()
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton,
                    pos=QPoint(slider.width() - 8, slider.height() // 2))
    assert slider.value() != previous
    scroll.ensureWidgetVisible(combo, 0, 0)
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: combo.view().isVisible())
    index = combo.model().index(0, 0)
    combo.view().scrollTo(index)
    QApplication.processEvents()
    qtbot.mouseMove(combo.view().viewport(), pos=combo.view().visualRect(index).center())
    qtbot.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                    pos=combo.view().visualRect(index).center())
    assert combo.currentIndex() == 0
    assert len(changed) > 0
    window.close()


class CaptureRunner:
    def __init__(self):
        self.requests = []

    def generate(self, request, cancelled):
        self.requests.append(build_native_request(request["voice"], request["text"]))
        make_audio(request["output"])

    def interrupt(self):
        pass


def done(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job
            return job
        time.sleep(.01)
    raise AssertionError("job timed out")


@pytest.mark.parametrize("seed", [0, 2147483647])
def test_saved_fixed_seed_is_used_by_every_workflow_batch(tmp_path, seed):
    voice = vox_voice(tmp_path)
    voice["request_options"] = {"seed": seed}
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    runner = CaptureRunner()
    with TestClient(create_app(root, runner=runner)) as client:
        ids = []
        for text in ("第一批字幕", "第二批字幕"):
            response = client.post("/jobs", json={"voice_id": voice["id"],
                "segments": [{"text": text}], "workflow_key": "fixed-seed-test", "delivery_ack": True})
            assert response.status_code == 202, response.text
            ids.append(response.json()["job_id"])
            job = done(client, ids[-1])
            assert job["voice"]["request_options"]["seed"] == seed
            assert all(s["voice"]["request_options"]["seed"] == seed for s in job["segments"])
        assert len(set(ids)) == 1 and client.get("/jobs").json() == []
        assert [r["request"]["options"]["seed"] for r in runner.requests] == [seed, seed]
        assert next(v for v in ConfigStore(root).voices() if v["id"] == voice["id"])["request_options"]["seed"] == seed


def test_advanced_options_save_merge_in_tasks_and_change_cached_audio(tmp_path):
    voice = vox_voice(tmp_path)
    voice["request_options"] = {"seed": 81, "guidance_scale": 2.4, "num_inference_steps": 12}
    root = tmp_path / "data"
    runner = CaptureRunner()
    with TestClient(create_app(root, runner=runner)) as client:
        assert client.put(f"/voices/{voice['id']}", json=voice).status_code == 200
        loaded = next(v for v in client.get("/voices").json() if v["id"] == voice["id"])
        assert loaded["request_options"] == voice["request_options"]
        jobs = []
        for scale in (2.4, 2.4, 3.1):
            response = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "Hello"}],
                "overrides": {"request_options": {"guidance_scale": scale}}})
            assert response.status_code == 202, response.text
            jobs.append(done(client, response.json()["job_id"]))
        assert len(runner.requests) == 2
        assert jobs[1]["segments"][0]["cached"]
        assert jobs[0]["fingerprint"] != jobs[2]["fingerprint"]
        assert jobs[2]["voice"]["request_options"] == {"seed": 81, "guidance_scale": 3.1, "num_inference_steps": 12}
        assert runner.requests[-1]["request"]["options"]["guidance_scale"] == 3.1
        for values in ({"guidance_scale": 99}, {"num_inference_steps": 0}, {"min_tokens": 50, "max_tokens": 20}, {"made_up": 5}):
            invalid = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "Hello"}], "overrides": {"request_options": values}})
            assert invalid.status_code == 400 and invalid.json()["error"]["stage"] == "参数检查"
        assert len(runner.requests) == 2


def test_random_seed_creates_new_take_but_saved_preset_keeps_random_setting(tmp_path):
    voice = vox_voice(tmp_path)
    voice["request_options"] = {"seed": -1}
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    runner = CaptureRunner()
    with TestClient(create_app(root, runner=runner)) as client:
        jobs = []
        for _ in range(2):
            response = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "Hello"}]})
            assert response.status_code == 202
            jobs.append(done(client, response.json()["job_id"]))
        assert len(runner.requests) == 2
        assert jobs[0]["voice"]["request_options"]["seed"] != jobs[1]["voice"]["request_options"]["seed"]
        assert all(j["voice"]["request_options"]["seed"] >= 0 for j in jobs)
        assert next(v for v in ConfigStore(root).voices() if v["id"] == voice["id"])["request_options"]["seed"] == -1


def test_switching_package_removes_previous_models_advanced_options(tmp_path, qapp):
    voice = vox_voice(tmp_path)
    voice["request_options"] = {"guidance_scale": 3, "num_inference_steps": 14}
    controls = ModelControls()
    controls.bind(voice)
    target = kokoro(tmp_path)
    controls.assets["model_dir"].setText(target["model_dir"])
    controls.refresh_capabilities()
    assert "guidance_scale" not in controls.overrides()["request_options"]
    assert "num_inference_steps" not in controls.generation_controls.fields
    assert "seed" not in controls.generation_controls.fields  # This fixture's contract only exposes speed.
    controls.close()
    root = tmp_path / "data"
    ConfigStore(root).save_voice(voice)
    target["request_options"] = {}
    with TestClient(create_app(root, runner=CaptureRunner())) as client:
        response = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "Hello"}],
            "overrides": {"model_dir": target["model_dir"], "family": "kokoro_tts", "voice_mode": "builtin",
                "native_voice": "zf_xiaobei", "voice_description": "", "request_options": {}}})
        assert response.status_code == 202, response.text
        job = done(client, response.json()["job_id"])
        assert job["voice"]["request_options"] == {}


def test_default_chunk_and_random_seed_sentinels_are_not_sent(tmp_path):
    voice = index_voice(tmp_path)
    request = build_native_request({**voice, "request_options": {"text_chunk_size": 0, "seed": -1}}, "Hello")
    assert "text_chunk_size" not in request["request"]["options"]
    assert "seed" not in request["request"]["options"]
