"""Verified, resumable transfers shared by model and native-engine installers."""
import hashlib
import os
import re


class DownloadPaused(Exception):
    pass


def verify_file(path, entry, event):
    if path.stat().st_size != entry["size"]:
        raise ValueError(f"下载文件大小不符：应为 {entry['size']} 字节，实际 {path.stat().st_size} 字节")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if event.is_set():
                raise DownloadPaused()
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"]:
        if path.suffix == ".part":
            path.unlink(missing_ok=True)
        raise ValueError("SHA-256 校验失败，请重新下载或移除不匹配的本地文件")


def download_file(target, entry, url, event, client_factory, progress):
    part = target.with_name(target.name + ".part")
    if event.is_set():
        raise DownloadPaused()
    if target.is_file():
        progress(status="verifying", received=entry["size"])
        verify_file(target, entry, event)
        return
    offset = part.stat().st_size if part.exists() else 0
    if offset > entry["size"]:
        part.unlink()
        offset = 0
    progress(status="downloading", received=offset)
    if offset < entry["size"]:
        with client_factory() as client:
            with client.stream("GET", url, headers={"Range": f"bytes={offset}-"} if offset else {}, follow_redirects=True) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                    if not match or int(match[1]) != offset or int(match[3]) != entry["size"]:
                        raise ValueError("下载服务器返回的续传范围不正确")
                elif response.status_code == 200:
                    offset = 0
                else:
                    raise ValueError(f"下载服务器返回 HTTP {response.status_code}")
                if response.headers.get("content-length") and int(response.headers["content-length"]) != entry["size"] - offset:
                    raise ValueError("下载服务器返回的文件大小与下载目录不一致")
                progress(resumed_from=offset)
                with part.open("ab" if offset else "wb") as stream:
                    for chunk in response.iter_bytes(chunk_size=262144):
                        if event.is_set():
                            raise DownloadPaused()
                        if offset + len(chunk) > entry["size"]:
                            raise ValueError("下载数据超过文件大小")
                        stream.write(chunk)
                        offset += len(chunk)
                        progress(received=offset)
    progress(status="verifying")
    verify_file(part, entry, event)
    if event.is_set():
        raise DownloadPaused()
    os.replace(part, target)
