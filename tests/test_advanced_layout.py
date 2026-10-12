import pytest
from PyQt6.QtCore import QPoint

from voice_workbench.gui import Window, configure_font
from test_model_capabilities import gguf


@pytest.mark.parametrize("height", [720, 1000])
def test_advanced_toggle_keeps_inspector_controls_in_place(tmp_path, qtbot, qapp, height):
    qapp.setStyle("Fusion")
    configure_font(qapp)
    path = tmp_path / "voice.gguf"
    gguf(path, {"family": "voxcpm2", "schema_version": 1, "capabilities": {"tts": ["voice_design", "voice_clone"]}})
    window = Window(8789, tmp_path / "data", autostart=False)
    qtbot.addWidget(window)
    window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
    window.resize(1280, height)
    window.model_controls.bind({"id": "vox", "model_name": "vox", "backend": "audio_cpp", "family": "voxcpm2",
        "model_dir": str(path), "voice_mode": "design", "voice_description": "温暖女声"})
    window.show()
    qtbot.wait(20)
    def positions():
        return tuple((w.mapTo(window, QPoint()).y(), w.height()) for w in (
            window.model_combo, window.voice_combo, window.model_controls.description, window.model_controls.advanced_toggle))
    expected = positions()
    for _ in range(8):
        window.model_controls.advanced_toggle.click()
        qtbot.wait(20)
        assert positions() == expected
        if window.model_controls.advanced_toggle.isChecked():
            controls = window.model_controls.generation_controls
            assert controls.fields
            assert all(field.isVisible() and field.height() > 0 for field in controls.fields.values())
            assert controls.height() >= controls.minimumSizeHint().height()
    for _ in range(5):
        window.model_controls.generation_controls.reset()
        qtbot.wait(20)
        assert positions() == expected
    window.document_dirty = False
    window.close()


def test_already_queued_api_result_is_ignored_after_window_close(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(Window, "load_saved_jobs", lambda self: None)
    window = Window(8789, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
    received = []
    window.call(lambda client: "late result", received.append)
    worker = next(iter(window.workers))
    assert worker.wait(5000)
    assert not received
    window.close()
    qtbot.wait(20)
    assert not received
