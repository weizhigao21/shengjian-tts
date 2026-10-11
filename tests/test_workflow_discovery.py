from copy import deepcopy
import importlib
from pathlib import Path
import sys
import threading
import types

from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget
import pytest

from integration.local_voice_client import (bind_discovered_voice, discover_voices,
    prepare_workflow_config, resolve_workflow_config, run_local_job, workflow_base_url)
from voice_workbench.api import create_app
from voice_workbench.config import atomic_json
from test_service import ControlledRunner
from test_workflow_client import Session


BASE = "http://127.0.0.1:8765"


def save_voice(client, voice_id, name, **changes):
    default = next(v for v in client.get("/voices").json() if v["id"] == "default")
    value = {**default, "id": voice_id, "name": name, **changes}
    assert client.put("/voices/" + voice_id, json=value).status_code == 200
    return value


def test_discovery_exposes_saved_presets_and_explicit_unavailable_reasons(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        save_voice(client, "narrator", "中文旁白")
        save_voice(client, "broken", "待修复声音", python=str(tmp_path / "missing-python.exe"))
        config = client.app.state.manager.config
        value = config.read()
        default = next(v for v in value["voices"] if v["id"] == "default")
        value["model_templates"] = [{**default, "id": "template", "python": "template-python"}]
        atomic_json(config.path, value)
        catalog = discover_voices(Session(client), BASE + "/workflow")
        assert catalog["base_url"] == BASE and catalog["schema_version"] == 1
        by_id = {v["id"]: v for v in catalog["voices"]}
        assert "template" not in by_id
        assert by_id["narrator"]["name"] == "中文旁白" and by_id["narrator"]["available"]
        assert by_id["narrator"]["mode_name"] == "内置音色"
        assert by_id["broken"]["error"]["code"] == "PYTHON_MISSING"
        assert not by_id["broken"]["available"]
        assert "reference_audio" not in by_id["narrator"] and "python" not in by_id["narrator"]
        assert "voice_discovery" in client.get("/health").json()["capabilities"]


@pytest.mark.parametrize("link", [BASE, BASE + "/", BASE + "/workflow/", BASE + "/voices", "127.0.0.1:8765/workflow"])
def test_link_accepts_service_and_discovery_addresses(link):
    assert workflow_base_url(link) == BASE


@pytest.mark.parametrize("link", ["http://other-service:8765", BASE + "/other", BASE + "/workflow?x=1",
                                  "file:///tmp/catalog", "http://user:pass@localhost:8765", "http://localhost:99999"])
def test_invalid_links_fail_before_network_access(link):
    class NeverRequest:
        def get(self, *args, **kwargs):
            raise AssertionError("invalid link must not issue a request")
    with pytest.raises(ValueError):
        discover_voices(NeverRequest(), link)


def test_discovered_selection_refreshes_cache_identity_before_batch_and_keeps_input_unchanged(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        voice = save_voice(client, "narrator", "旁白")
        session = Session(client)
        catalog = discover_voices(session, BASE)
        legacy = {"name": "其他 API", "url": "http://other-server:8000", "model": "legacy"}
        entry = bind_discovered_voice({"name": "声笺"}, catalog, "narrator")
        config = {"tts_mode": "api", "api_configs": [legacy, entry], "current_api_index": 1}
        changed = {**voice, "speed": 1.2, "name": "新的旁白名称"}
        assert client.put("/voices/narrator", json=changed).status_code == 200
        resolved = resolve_workflow_config(config, session)
        selected = resolved["api_configs"][1]
        assert selected["model_tag"] != entry["model_tag"] and selected["voice_name"] == "新的旁白名称"
        assert selected["voice_id"] == "narrator"
        assert config["api_configs"][1] == entry and resolved["api_configs"][0] == legacy
        tasks = [(1, "00:00:00.00", "第一句", "unused", 0), (2, "00:00:02.00", "第二句", "unused", 0)]
        results = run_local_job(session, selected["url"], selected["model"], tasks)
        assert all(url and error is None for url, error in results)
        stale_results = run_local_job(session, BASE, entry["model"], tasks)
        assert all("声音配置已变化" in error for _, error in stale_results)


def test_deleted_or_broken_selected_voice_does_not_switch_to_another_preset(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        voice = save_voice(client, "narrator", "旁白")
        session = Session(client)
        entry = bind_discovered_voice({}, discover_voices(session, BASE), "narrator")
        config = {"tts_mode": "api", "api_configs": [entry]}
        client.put("/voices/narrator", json={**voice, "python": "missing-python"})
        with pytest.raises(ValueError, match="不可用"):
            resolve_workflow_config(config, session)
        client.delete("/voices/narrator")
        with pytest.raises(ValueError, match="已删除"):
            resolve_workflow_config(config, session)
        assert config["api_configs"][0]["voice_id"] == "narrator"


def test_preflight_preserves_shared_profile_reference_and_saves_refreshed_metadata(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        voice = save_voice(client, "narrator", "旁白")
        session = Session(client)
        entry = bind_discovered_voice({}, discover_voices(session, BASE), "narrator")
        class Config:
            tts_cfg = {"tts_mode": "api", "api_configs": [entry]}
            saves = 0
            def save(self):
                self.saves += 1
        config = Config()
        shared_profile, shared_entry = config.tts_cfg, entry
        client.put("/voices/narrator", json={**voice, "speed": 1.3})
        resolved = prepare_workflow_config(config, session)
        assert config.tts_cfg is shared_profile and config.tts_cfg["api_configs"][0] is shared_entry
        assert shared_entry["model_tag"] == resolved["api_configs"][0]["model_tag"]
        assert config.saves == 1
        prepare_workflow_config(config, session)
        assert config.saves == 1


def test_preflight_does_not_overwrite_a_selection_changed_during_discovery(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        save_voice(client, "other", "另一个声音")
        session = Session(client)
        catalog = discover_voices(session, BASE)
        original = bind_discovered_voice({}, catalog, "default")
        replacement = bind_discovered_voice({}, catalog, "other")
        config = types.SimpleNamespace(tts_cfg={"tts_mode": "api", "api_configs": [original]})
        class SwitchSession(Session):
            def get(self, *args, **kwargs):
                response = super().get(*args, **kwargs)
                config.tts_cfg["api_configs"][0] = replacement
                return response
        resolved = prepare_workflow_config(config, SwitchSession(client))
        assert config.tts_cfg["api_configs"][0]["voice_id"] == "other"
        assert resolved["api_configs"][0]["voice_id"] == "default"


def test_non_discovery_and_edge_configs_do_not_probe_and_multi_api_reuses_catalog(tmp_path):
    class NeverRequest:
        def get(self, *args, **kwargs):
            raise AssertionError("do not probe legacy or Edge providers")
    legacy = {"tts_mode": "api", "api_configs": [{"url": "http://other-service", "model": "legacy"}]}
    assert resolve_workflow_config(legacy, NeverRequest()) == legacy
    assert resolve_workflow_config({**legacy, "tts_mode": "edge"}, NeverRequest())["tts_mode"] == "edge"
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        session = Session(client)
        catalog = discover_voices(session, BASE)
        entry = bind_discovered_voice({}, catalog, "default")
        class CountSession(Session):
            count = 0
            def get(self, *args, **kwargs):
                self.count += 1
                return super().get(*args, **kwargs)
        counting = CountSession(client)
        config = {"tts_mode": "api", "use_multi_api": True, "api_configs": [entry, deepcopy(entry)]}
        resolve_workflow_config(config, counting)
        assert counting.count == 1


def picker_module(monkeypatch):
    # Load the reusable widget under its destination package; no workflow imports or writes are needed.
    prefix = "_discovery_test"
    path = str(Path(__file__).resolve().parents[1] / "integration")
    for name in (prefix, prefix + ".widgets", prefix + ".steps"):
        package = types.ModuleType(name)
        package.__path__ = [path]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, prefix + ".steps.local_voice_client", importlib.import_module("integration.local_voice_client"))
    name = prefix + ".widgets.local_voice_picker"
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


def test_picker_import_select_refresh_and_deleted_voice_state(qtbot, monkeypatch):
    module = picker_module(monkeypatch)
    catalog = {"base_url": BASE, "discovery_url": BASE + "/workflow", "voices": [
        {"id": "first", "name": "声音一", "model_name": "first@aaa", "model_tag": "aaa", "available": True},
        {"id": "second", "name": "声音二", "model_name": "second@bbb", "model_tag": "bbb", "available": True},
        {"id": "bad", "name": "损坏的声音", "model_name": "bad", "model_tag": "", "available": False,
         "error": {"message": "参考文件缺失"}}]}
    monkeypatch.setattr(module, "discover_voices", lambda *args: deepcopy(catalog))
    class Config:
        tts_cfg = {"tts_mode": "api", "api_configs": [{"name": "其他服务", "url": "http://old-api", "model": "old"}], "current_api_index": 0}
        def save(self):
            pass
    class Owner(QWidget):
        config_changed = pyqtSignal()
        log_message = pyqtSignal(str)
        def __init__(self):
            super().__init__()
            self.config = Config()
        def _refresh_api_combo(self):
            self.picker.sync_from_config()
    owner = Owner()
    qtbot.addWidget(owner)
    picker = owner.picker = module.VoicePicker(owner)
    owner.show()
    picker.link.setText(BASE + "/workflow")
    qtbot.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not picker.tasks)
    assert len(owner.config.tts_cfg["api_configs"]) == 2
    assert owner.config.tts_cfg["api_configs"][0]["model"] == "old"
    assert not picker.voices.model().item(picker.voices.findData("bad")).isEnabled()
    picker.voices.setCurrentIndex(picker.voices.findData("second"))
    assert owner.config.tts_cfg["api_configs"][1]["voice_id"] == "second"
    catalog["voices"][1].update(model_name="second@new", model_tag="new")
    qtbot.mouseClick(picker.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not picker.tasks)
    assert owner.config.tts_cfg["api_configs"][1]["model_tag"] == "new"
    catalog["voices"].pop(1)
    qtbot.mouseClick(picker.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not picker.tasks)
    assert picker.voices.currentData() == "second" and "已删除" in picker.voices.currentText()
    assert owner.config.tts_cfg["api_configs"][1]["voice_id"] == "second"
    entered, release = threading.Event(), threading.Event()
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return deepcopy(catalog)
    monkeypatch.setattr(module, "discover_voices", delayed)
    qtbot.mouseClick(picker.refresh_button, Qt.MouseButton.LeftButton)
    assert entered.wait(2)
    owner.config.tts_cfg["current_api_index"] = 0
    picker.sync_from_config()
    release.set()
    qtbot.waitUntil(lambda: not picker.tasks)
    assert owner.config.tts_cfg["current_api_index"] == 0
    assert picker.voices.currentData() is None
    owner.close()
