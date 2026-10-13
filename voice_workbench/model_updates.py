"""Discover immutable GGUF revisions without downloading model weights."""
import re
from pathlib import PurePosixPath

from .errors import VoiceError
from .model_dates import attach_publication_dates, publication_metadata


def safe_remote_path(value):
    return (isinstance(value, str) and 0 < len(value) < 512 and
            not any(c in value for c in "\\:\x00?#%") and
            not any(ord(c) < 32 for c in value) and
            not PurePosixPath(value).is_absolute() and
            all(p not in ("", ".", "..") for p in value.split("/")))


def repository_files(client, repo, source="mirror", requested_revision="main", paths=None, cancel=None):
    """Read bounded immutable file metadata shared by updates and discovery."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("模型仓库地址无效")
    host = "https://hf-mirror.com" if source == "mirror" else "https://huggingface.co"
    response = client.get(f"{host}/api/models/{repo}/revision/{requested_revision}", params={"blobs": "true"})
    response.raise_for_status()
    value = response.json()
    revision = value.get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("模型仓库没有返回固定版本")
    files = {}
    for item in value.get("siblings", []):
        remote = item.get("rfilename")
        metadata = item.get("lfs") or {}
        digest = metadata.get("sha256", "")
        size = metadata.get("size", item.get("size", 0))
        if (safe_remote_path(remote) and remote.lower().endswith(".gguf") and
                re.fullmatch(r"[a-f0-9]{64}", digest) and type(size) is int and 0 < size < 32 * 1024**3):
            files[remote] = {"sha256": digest, "size": size, **publication_metadata({})}
    attach_publication_dates(client, host, repo, revision, files, paths, cancel)
    return revision, files


def fetch_model_updates(catalog, client_factory, source="mirror"):
    entries = {}
    try:
        repos = {e.get("repo") or "audio-cpp/audio.cpp-gguf" for e in catalog.values() if e.get("remotePath")}
        with client_factory() as client:
            for repo in sorted(repos):
                paths = [e["remotePath"] for e in catalog.values() if (e.get("repo") or "audio-cpp/audio.cpp-gguf") == repo and e.get("remotePath")]
                revision, files = repository_files(client, repo, source, paths=paths)
                for identity, entry in catalog.items():
                    if (entry.get("repo") or "audio-cpp/audio.cpp-gguf") != repo:
                        continue
                    file = files.get(entry.get("remotePath"))
                    if file:
                        entries[identity] = {**entry, "revision": revision, **file}
        if not entries:
            raise ValueError("未返回受支持 GGUF 文件的大小与 SHA-256")
        return entries
    except Exception as exc:
        raise VoiceError("MODEL_UPDATE_CHECK_FAILED", f"无法检查模型更新：{exc}",
                         "原模型和下载目录保持可用，请切换下载源或稍后重试。", "模型更新") from exc
