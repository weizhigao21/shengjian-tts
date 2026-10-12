"""Verify the frozen GUI/service and real speech using isolated test data."""
from __future__ import annotations

import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import wave

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def unused_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_ready(client, data, process):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"打包界面退出：{process.returncode}；请查看测试目录 logs/startup.log。")
        try:
            health = client.get("/health").json()
            if health.get("service") == "local-voice-workbench" and Path(health["data_dir"]).resolve() == data.resolve():
                return health
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(.15)
    raise RuntimeError("打包界面未能自动启动对应服务；请查看测试目录 logs/service.log。")


def generate(client, voice, label, artifacts):
    submitted = client.post("/jobs", json={"voice_id": voice["id"], "title": label,
                            "segments": [{"text": "你好，这是声笺打包程序的语音验证。", "timestamp": "00:00:00.00"}]} )
    submitted.raise_for_status()
    job_id = submitted.json()["job_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = client.get("/jobs/" + job_id).json()
        if job["status"] in ("done", "failed", "cancelled"):
            if job["status"] != "done":
                raise RuntimeError(label + "生成失败：" + json.dumps(job["segments"], ensure_ascii=False))
            segment = job["segments"][0]
            response = client.get("/audio/" + segment["audio"])
            response.raise_for_status()
            with wave.open(io.BytesIO(response.content), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                sample_rate = audio.getframerate()
                assert duration > 0 and audio.readframes(1)
            output = artifacts / (label + ".wav")
            output.write_bytes(response.content)
            print(f"{label}：{duration:.3f} 秒，{sample_rate} Hz", flush=True)
            return {"file": str(output), "duration": duration, "sample_rate": sample_rate}
        time.sleep(.2)
    raise RuntimeError(label + "生成超时。")


def check_reference_retention(client, sample, artifacts, data, voices):
    source = artifacts / "reference-import-source.wav"
    original = Path(sample["file"]).read_bytes()
    source.write_bytes(original)
    imported = client.post("/references", json={"name": "打包参考音频", "reference_audio": str(source)})
    imported.raise_for_status()
    reference = imported.json()
    retained = Path(reference["reference_audio"])
    assert retained.parent == data / "voices" and retained.read_bytes() == original
    clone = next((v for v in voices if not v.get("error") and
                  set(v.get("model_ui", {}).get("modes", [])) & {"clone", "continuation"}), None)
    if clone:
        response = client.put("/voices/package-reference-copy", json={**clone, "id": "package-reference-copy",
            "name": "打包音频副本验证", "reference_audio": str(source)})
        response.raise_for_status()
        assert response.json()["reference_audio"] == str(retained)
    source.unlink()
    saved_reference = next(r for r in client.get("/references").json() if r["id"] == reference["id"])
    assert saved_reference["error"] is None and retained.read_bytes() == original
    if clone:
        saved_voice = next(v for v in client.get("/voices").json() if v["id"] == "package-reference-copy")
        assert saved_voice["error"] is None
    print("参考音频已复制到 EXE 数据目录，删除原文件后仍可读取；预设复用同一份副本。", flush=True)
    return {"reference_import": True, "preset_copy": bool(clone), "original_removed": True,
            "retained": str(retained)}


def check_engine_install(client, artifacts, data, kokoro):
    response = client.post("/models/runtime/download", json={"backend": "cpu"})
    response.raise_for_status()
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        engine = client.get("/models").json()["engine"]
        option = next(e for e in engine["options"] if e["backend"] == "cpu")
        if option["status"] in ("done", "failed", "paused"):
            break
        time.sleep(.3)
    assert option["status"] == "done", option
    assert Path(engine["path"]).is_relative_to(data / "runtime") and engine["ready"]
    report = {"backend": "cpu", "version": option["version"], "path": engine["path"], "configured": True}
    if kokoro:
        voice = next(v for v in client.get("/voices").json() if v["id"] == kokoro["id"])
        assert voice["device"] == "cpu" and voice["engine_executable"] == engine["path"]
        report["native_cpu"] = generate(client, voice, "新下载CPU引擎", artifacts)
    print("EXE 引擎下载、校验、安装和自动配置通过。", flush=True)
    return report


def check_lifecycle(executable, data):
    # Exercise the normal close handler with real frozen child processes.
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from voice_workbench.gui import Window
    previous = getattr(sys, "frozen", None), sys.executable
    sys.frozen, sys.executable = True, str(executable)
    app = QApplication([])
    window = Window(unused_port(), data)
    errors = []
    window.show_error = errors.append
    window.show()
    timer = QTimer()
    deadline = time.monotonic() + 35
    success = [False]

    def check():
        if window.ready or time.monotonic() > deadline or errors:
            success[0] = window.ready and window.owns_service
            timer.stop()
            window.close()
            app.quit()

    try:
        timer.timeout.connect(check)
        timer.start(150)
        app.exec()
        assert success[0], f"打包服务生命周期检查失败：{errors}"
        assert window.process.poll() is not None, "关闭界面后服务没有停止"
        print("界面正常关闭：后台服务已退出。", flush=True)
    finally:
        window.close()
        if previous[0] is None:
            del sys.frozen
        else:
            sys.frozen = previous[0]
        sys.executable = previous[1]


def check_workflow_batching(client, voice):
    client.put("/settings/performance", json={"workflow_workers": 2, "cpu_threads": 4}).raise_for_status()
    submissions = [client.post("/jobs", json={"workflow_key": "packaged-workflow-check", "title": "打包批量验证",
                   "voice_id": voice["model_name"], "segments": [{"text": f"第{batch}批的第{index}段测试字幕。"} for index in (1, 2)]}).json()
                   for batch in (1, 2)]
    assert submissions[0]["job_id"] == submissions[1]["job_id"]
    assert submissions[0]["segment_indices"] == [1, 2] and submissions[1]["segment_indices"] == [3, 4]
    deadline = time.monotonic() + 120
    maximum = 0
    while time.monotonic() < deadline:
        maximum = max(maximum, len(client.get("/health").json()["active"]))
        job = client.get("/jobs/" + submissions[0]["job_id"]).json()
        if job["status"] in ("done", "failed", "cancelled"):
            assert job["status"] == "done" and job["completed"] == 4, job
            for item in job["segments"]:
                response = client.get(item["audio_url"])
                response.raise_for_status()
                with wave.open(io.BytesIO(response.content), "rb") as audio:
                    assert audio.getframerate() == 24000 and audio.getnframes() > 0
            assert not any(j.get("workflow_key") == "packaged-workflow-check" for j in client.get("/jobs").json())
            assert all(s["audio"] is None for s in client.get("/jobs/" + submissions[0]["job_id"]).json()["segments"])
            print("打包工作流验证：2 批 / 4 条字幕只保留数据库记录，音频传输后清理。", flush=True)
            return {"batches": 2, "segments": 4, "jobs": 0, "workers": 2, "maximum_active": maximum}
        time.sleep(.05)
    raise RuntimeError("打包批量推理超时")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, default=ROOT / "dist" / "声笺" / "声笺.exe")
    args = parser.parse_args()
    executable = args.executable.resolve()
    assert executable.is_file(), "请先运行打包.bat"
    artifacts = ROOT / "artifacts" / "package-check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts.mkdir(parents=True, exist_ok=False)
    data = artifacts / "gui-data"
    port = unused_port()
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PATH=os.pathsep.join(str(p) for p in (windows, windows / "System32", windows / "System32/WindowsPowerShell/v1.0")))
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.Popen([str(executable), "gui", "--port", str(port), "--data-dir", str(data)],
                               cwd=artifacts, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    (artifacts / "process.json").write_text(json.dumps({"pid": process.pid, "executable": str(executable), "data_dir": str(data)}), encoding="utf-8")
    report = {}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=4) as client:
            health = wait_ready(client, data, process)
            report["version"] = health["version"]
            manifest = client.get("/workflow").json()
            assert manifest["schema_version"] == 1 and manifest["voices"]
            models = client.get("/models").json()
            assert models["catalog"], "模型目录资源缺失"
            report.update(frozen_gui_started=True, no_python_in_path=True, voice_discovery=True,
                          catalog_count=len(models["catalog"]))
            print("独立 EXE 已自动启动服务，音色发现与模型目录可用；PATH 不包含 Python。", flush=True)
            voices = client.get("/voices").json()
            report["windows_sapi"] = generate(client, next(v for v in voices if v["id"] == "default"), "Windows系统声音", artifacts)
            report["reference_retention"] = check_reference_retention(client, report["windows_sapi"], artifacts, data, voices)
            kokoro = next((v for v in voices if v.get("backend") == "audio_cpp" and v.get("family") == "kokoro_tts" and not v.get("error")), None)
            if kokoro:
                report["kokoro"] = generate(client, kokoro, "Kokoro统一引擎", artifacts)
                report["workflow_batching"] = check_workflow_batching(client, kokoro)
            report["engine_install"] = check_engine_install(client, artifacts, data, kokoro)
            client.post("/shutdown").raise_for_status()
    finally:
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as cleanup:
                health = cleanup.get("/health").json()
                if Path(health.get("data_dir", ".")).resolve() == data.resolve():
                    cleanup.post("/shutdown")
        except (httpx.HTTPError, ValueError):
            pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
    check_lifecycle(executable, artifacts / "lifecycle-data")
    report["normal_close_stops_service"] = True
    (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"打包验证通过：{artifacts / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
