"""Read stable, checksum-pinned Windows packages from audio.cpp releases."""
import re

from .errors import VoiceError

RELEASE_API = "https://api.github.com/repos/0xShug0/audio.cpp/releases/latest"


def version_key(value):
    if not isinstance(value, str):
        return (0, 0, 0)
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def release_catalog(value):
    version = value.get("tag_name", "")
    if not isinstance(version, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", version) or value.get("draft") or value.get("prerelease"):
        raise ValueError("只接受正式的 audio.cpp 发布版本")
    assets = {a["name"]: a for a in value.get("assets", [])}
    backends = []
    for backend, suffixes in (("cuda", ("bin-windows-x64-cuda12.4.zip", "cudart-windows-x64-cuda12.4.zip")),
                              ("cpu", ("bin-windows-x64-cpu-portable.zip",))):
        archives = []
        for suffix in suffixes:
            name = f"audio-{version}-{suffix}"
            asset = assets.get(name, {})
            digest = asset.get("digest", "") or ""
            expected = f"https://github.com/0xShug0/audio.cpp/releases/download/{version}/{name}"
            size = asset.get("size", 0)
            if (asset.get("browser_download_url") != expected or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                    or not isinstance(size, int) or not 0 < size < 8 * 1024**3):
                raise ValueError(f"正式发布缺少可校验的 Windows 包：{name}")
            archives.append({"name": name, "size": size, "sha256": digest[7:]})
        backends.append({"id": backend, "name": "NVIDIA CUDA 12.4" if backend == "cuda" else "CPU 便携版",
                         "description": "官方 Windows x64 发布；下载后校验 SHA-256，保留旧版本。", "archives": archives})
    return {"version": version, "release": f"https://github.com/0xShug0/audio.cpp/releases/tag/{version}", "backends": backends}


def fetch_release(client_factory):
    try:
        with client_factory() as client:
            response = client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "shengjian-tts"})
            response.raise_for_status()
            return release_catalog(response.json())
    except Exception as exc:
        raise VoiceError("ENGINE_UPDATE_CHECK_FAILED", f"无法检查引擎更新：{exc}",
                         "保留当前引擎；网络恢复后重试，或使用已内置的正式版本。", "引擎更新") from exc
