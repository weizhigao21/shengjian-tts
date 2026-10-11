"""Read a link in the staged workflow UI, select a real preset and generate a batch.

Uses an isolated data directory and owned service; does not modify workflow files or user tasks.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
import argparse
from copy import deepcopy
import importlib
import json
import logging
from pathlib import Path
import sys
import time
import types
import httpx

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.config import atomic_json
from voice_workbench.gui import Window, configure_font
from voice_workbench.runner import validate_wave
from scripts.install_workflow_adapter import prepare


def load_panel(workflow):
    prefix = "discovery_preview"
    for name, paths in ((prefix, [workflow / "src"]),
                        (prefix + ".widgets", [ROOT / "integration" / "prepared", ROOT / "integration", workflow / "src" / "widgets"]),
                        (prefix + ".steps", [ROOT / "integration" / "prepared", ROOT / "integration", workflow / "src" / "steps"])):
        module = types.ModuleType(name)
        module.__path__ = [str(p) for p in paths]
        sys.modules[name] = module
    # Cache display does not need the user's workflow cache database.
    cache = types.ModuleType(prefix + ".steps.tts_cache")
    class EmptyCache:
        def get_cache_stats(self):
            return {"total_count": 0, "total_size": 0, "total_reuse": 0}
        def close(self):
            pass
        def get_cached_audio(self, *args, **kwargs):
            return None
        def save_audio_cache(self, *args, **kwargs):
            return True
    cache.AudioCache = EmptyCache
    sys.modules[cache.__name__] = cache
    logger = types.ModuleType(prefix + ".steps.tts_logger")
    logger.logger = logging.getLogger("voice-discovery-preview")
    logger.logger.addHandler(logging.NullHandler())
    sys.modules[logger.__name__] = logger
    panel = importlib.import_module(prefix + ".widgets.tts_panel")
    defaults = importlib.import_module(prefix + ".config").DEFAULT_CONFIG["tts"]
    return panel.TTSConfigPanel, deepcopy(defaults)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=Path(r"G:\code\双语音声工作流"))
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    for path, source in prepare(args.workflow):
        (ROOT / "integration" / "prepared" / path.name).write_text(source, encoding="utf-8")
    output = ROOT / "artifacts" / "voice-discovery-check" / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    configure_font(app)
    window = Window(args.port, output / "data")
    panel = None
    def wait_until(predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.02)
        assert predicate(), "Timed out waiting for the discovery UI/service"
    try:
        wait_until(lambda: window.ready)
        window.timer.stop()
        window.settings.timer.stop()
        Panel, defaults = load_panel(args.workflow)
        class Config:
            tts_cfg = defaults
            def save(self):
                atomic_json(output / "workflow-config.json", self.tts_cfg)
        config = Config()
        panel = Panel(config)
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("api"))
        panel.resize(960, 560)
        panel.show()
        picker = panel.local_voice_picker
        picker.link.setText(window.workflow_link.text())
        QTest.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: not picker.tasks)
        assert picker.catalog, picker.hint.text()
        voice = next(v for v in picker.catalog["voices"] if v["available"] and "kokoro" in v["model"].casefold())
        picker.voices.setCurrentIndex(picker.voices.findData(voice["id"]))
        assert config.tts_cfg["api_configs"][config.tts_cfg["current_api_index"]]["voice_id"] == voice["id"]
        app.processEvents()
        ui = ROOT / "artifacts" / "ui"
        ui.mkdir(parents=True, exist_ok=True)
        panel.grab().save(str(ui / "workflow-voice-picker.png"))
        window.tabs.setCurrentIndex(2)
        window.show()
        app.processEvents()
        window.grab().save(str(ui / "workflow-link.png"))
        bridge_module = importlib.import_module("discovery_preview.steps.step2_tts")
        task_module = importlib.import_module("discovery_preview.task_manager")
        utils = importlib.import_module("discovery_preview.steps.tts_utils")
        original_session = utils._get_session
        def local_session():
            session = original_session()
            session.trust_env = False
            return session
        utils._get_session = local_session
        subtitle = output / "discovery.lrc"
        subtitle.write_text("[00:00.00]你好，这是链接读取音色的测试。\n[00:04.00]选好音色后可以批量生成。\n[00:08.00]第二批字幕也归入同一个任务。\n[00:12.00]原来的文本顺序会保持不变。\n[00:16.00]这里继续生成第三批声音。\n[00:20.00]全部生成后就可以进行混音。", encoding="utf-8")
        config.tts_cfg["bulk_batch_size"] = 2
        with httpx.Client(base_url=window.url, trust_env=False) as client:
            response = client.put("/settings/performance", json={"workflow_workers": 2, "cpu_threads": 4})
            response.raise_for_status()
        task = task_module.TaskInfo("voice-discovery", str(output / "source.wav"), "discovery", str(output / "workspace"))
        task.step1_output = str(subtitle)
        bridge = bridge_module.TTSBridgeWorker(task, config)
        bridge.start()
        try:
            wait_until(lambda: bridge.isFinished(), timeout=180)
        finally:
            if bridge.isRunning():
                bridge.stop()
                assert bridge.wait(20000), "Workflow bridge did not finish after cancellation"
        assert bridge.result[0], bridge.result[1]
        from_folder = Path(bridge.result[1])
        profile = importlib.import_module("discovery_preview.steps.tts_profile")
        assert profile.profile_matches(str(from_folder), config.tts_cfg)
        paths = sorted(from_folder.glob("*.wav"))
        assert len(paths) == 6
        with httpx.Client(base_url=window.url, trust_env=False) as client:
            jobs = client.get("/jobs").json()
            assert len(jobs) == 1 and jobs[0]["total"] == 6 and jobs[0]["completed"] == 6, jobs
            job = client.get("/jobs/" + jobs[0]["id"]).json()
            assert [s["generation"] for s in job["segments"]] == [1, 1, 2, 2, 3, 3]
        audio = [{"file": str(path), **validate_wave(path)} for path in paths]
        report = {"link": window.workflow_link.text(), "voice": voice, "audio": audio,
                  "workflow_ui_selection": True, "real_batch_generation": True,
                  "micro_batches": 3, "service_jobs": 1, "workflow_workers": 2}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output / "report.json"), "voice_id": voice["id"], "audio": audio}, ensure_ascii=True), flush=True)
    finally:
        if panel is not None:
            panel.close()
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
