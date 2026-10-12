import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.engine_validation import CUDA_DLLS, inspect_engine
from voice_workbench.manager import Manager
from pe_fixture import engine_files, pe_binary
from test_engine_download import installer
from test_inline_generation import Runner, index_voice


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])


def local_engine(folder, cuda=True):
    for name, content in engine_files(cuda).items():
        target = folder / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if cuda:
        for name in CUDA_DLLS:
            (folder / name).write_bytes(pe_binary())
    return folder / "audiocpp_server.exe"


def test_external_engine_is_owned_without_install_registration_and_updates_on_dll_change(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    try:
        path = local_engine(tmp_path / "external")
        voice = {**index_voice(tmp_path), "engine_executable": str(path), "device": "cuda"}
        manager.config.save_voice(voice)
        original = manager.config.read()
        engine = manager.models.engines.listing()
        cuda = next(o for o in engine["options"] if o["backend"] == "cuda")
        assert engine["ready"] and cuda["installed"] and cuda["path"] == str(path)
        assert all(a["verified"] and a["status"] == "done" for a in cuda["archives"])
        assert manager.config.read() == original  # Detection never rewrites user presets.
        (path.parent / "cublas64_12.dll").unlink()
        engine = manager.models.engines.listing()
        cuda = next(o for o in engine["options"] if o["backend"] == "cuda")
        assert not engine["ready"] and not cuda["installed"]
        assert [a["verified"] for a in cuda["archives"]] == [True, False]
        assert "缺少 cublas64_12.dll" in cuda["verification"]["issues"]
        (path.parent / "cublas64_12.dll").write_bytes(b"MZ damaged")
        assert not manager.models.engines.verify("cuda")["verification"]["valid"]
        (path.parent / "cublas64_12.dll").write_bytes(pe_binary())
        assert manager.models.engines.verify("cuda")["verification"]["valid"]
    finally:
        manager.close()


def test_recursive_dependency_validation_and_x64_header(tmp_path):
    path = local_engine(tmp_path / "engine", cuda=False)
    (path.parent / "ggml-base.dll").write_bytes(pe_binary(["required-runtime.dll"]))
    system = tmp_path / "system"
    system.mkdir()
    result = inspect_engine(str(path), "cpu", system)
    assert result["issues"] == ["缺少 required-runtime.dll"]
    (system / "required-runtime.dll").write_bytes(pe_binary())
    assert inspect_engine(str(path), "cpu", system)["valid"]
    damaged = bytearray(path.read_bytes())
    damaged[132:134] = b"\x4c\x01"  # x86 executable is incompatible with an x64 engine.
    path.write_bytes(damaged)
    assert not inspect_engine(str(path), "cpu", system)["valid"]


def test_missing_cuda_runtime_download_reuses_external_program(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    requests = []
    import httpx
    def transport(request):
        name = request.url.path.split("/")[-1]
        requests.append(name)
        return httpx.Response(200, content=payloads[name])
    try:
        path = local_engine(tmp_path / "external")
        (path.parent / "cudart64_12.dll").unlink()
        manager.config.save_voice({**index_voice(tmp_path), "engine_executable": str(path), "device": "cuda"})
        engines, payloads = installer(manager, transport, cuda=True)
        engines.start("cuda")
        engines.threads["cuda"].join(4)
        assert engines.states["cuda"]["status"] == "done", engines.states["cuda"]
        assert requests == ["cudart.zip"]
        current = Path(manager.models.runtime())
        assert current.is_relative_to(engines.root) and inspect_engine(str(current), "cuda")["valid"]
        assert not (path.parent / "cudart64_12.dll").exists()  # Original folder is untouched.
    finally:
        manager.close()


def test_missing_program_download_reuses_valid_cuda_runtime(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    requests = []
    import httpx
    def transport(request):
        name = request.url.path.split("/")[-1]
        requests.append(name)
        return httpx.Response(200, content=payloads[name])
    try:
        path = local_engine(tmp_path / "external")
        (path.parent / "ggml-cuda.dll").unlink()
        manager.config.save_voice({**index_voice(tmp_path), "engine_executable": str(path), "device": "cuda"})
        engines, payloads = installer(manager, transport, cuda=True)
        engines.start("cuda")
        engines.threads["cuda"].join(4)
        assert engines.states["cuda"]["status"] == "done", engines.states["cuda"]
        assert requests == ["engine.zip"]
        assert inspect_engine(manager.models.runtime(), "cuda")["valid"]
        assert not (path.parent / "ggml-cuda.dll").exists()
    finally:
        manager.close()


def test_existing_complete_external_engine_never_downloads_again(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    try:
        path = local_engine(tmp_path / "external")
        manager.config.save_voice({**index_voice(tmp_path), "engine_executable": str(path), "device": "cuda"})
        engines, _ = installer(manager, lambda _: pytest.fail("A complete engine must not be downloaded again"), cuda=True)
        engines.start("cuda")
        engines.threads["cuda"].join(4)
        assert engines.states["cuda"]["status"] == "done"
        assert manager.models.runtime() == str(path)
        assert not list(engines.root.glob("v0.7.4-*"))
    finally:
        manager.close()


def test_verify_api_and_ui_show_owned_and_report_missing_component(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    path = local_engine(tmp_path / "external")
    with TestClient(create_app(tmp_path / "data", runner=Runner())) as client:
        manager = client.app.state.manager
        manager.config.save_voice({**index_voice(tmp_path), "engine_executable": str(path), "device": "cuda"})
        assert "engine_verification" in client.get("/health").json()["capabilities"]
        window = Window(8799, manager.root, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        window.ready = True
        panel = window.settings
        panel.model_data = client.get("/models").json()
        panel.render_engine()
        assert panel.engine_download.text() == "已拥有" and not panel.engine_download.isEnabled()
        assert [row[-1].text() for row in panel.engine_asset_rows] == ["已拥有", "已拥有"]
        def immediate(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", immediate)
        (path.parent / "cufft64_11.dll").unlink()
        panel.engine_verify.click()
        assert panel.engine_download.text() == "补全缺失组件" and panel.engine_download.isEnabled()
        assert "cufft64_11.dll" in panel.engine_error.text()
        assert [row[-1].text() for row in panel.engine_asset_rows] == ["已拥有", "待下载"]
        assert client.post("/models/runtime/use", json={"backend": "cuda"}).status_code == 400
        window.close()


def test_new_gui_rejects_old_service_without_verification_and_model_fixes(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    window = Window(8799, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)
    window._existing_service({"service": "local-voice-workbench", "data_dir": str(tmp_path),
        "capabilities": ["reference_library", "generation_options", "work_generations", "asset_management", "voice_discovery"]})
    assert not window.ready and errors and "旧版本" in errors[0]
    window.close()
