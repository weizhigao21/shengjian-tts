"""Discover immutable GGUF revisions without downloading model weights."""
import re

from .errors import VoiceError


def fetch_model_updates(catalog, client_factory, source="mirror"):
    host = "https://hf-mirror.com" if source == "mirror" else "https://huggingface.co"
    entries = {}
    try:
        repos = {e.get("repo") or "audio-cpp/audio.cpp-gguf" for e in catalog.values() if e.get("remotePath")}
        with client_factory() as client:
            for repo in sorted(repos):
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                    raise ValueError("模型仓库地址无效")
                response = client.get(f"{host}/api/models/{repo}/revision/main", params={"blobs": "true"})
                response.raise_for_status()
                value = response.json()
                revision = value.get("sha", "")
                if not re.fullmatch(r"[a-f0-9]{40}", revision):
                    raise ValueError("模型仓库没有返回固定版本")
                files = {item.get("rfilename"): item for item in value.get("siblings", [])}
                for identity, entry in catalog.items():
                    if (entry.get("repo") or "audio-cpp/audio.cpp-gguf") != repo:
                        continue
                    file = files.get(entry.get("remotePath"), {})
                    metadata = file.get("lfs") or {}
                    digest = metadata.get("sha256", "")
                    size = metadata.get("size", file.get("size", 0))
                    if re.fullmatch(r"[a-f0-9]{64}", digest) and isinstance(size, int) and 0 < size < 32 * 1024**3:
                        entries[identity] = {**entry, "revision": revision, "size": size, "sha256": digest}
        if not entries:
            raise ValueError("未返回受支持 GGUF 文件的大小与 SHA-256")
        return entries
    except Exception as exc:
        raise VoiceError("MODEL_UPDATE_CHECK_FAILED", f"无法检查模型更新：{exc}",
                         "原模型和下载目录保持可用，请切换下载源或稍后重试。", "模型更新") from exc
