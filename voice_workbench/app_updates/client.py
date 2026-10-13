"""Pinned public releases, exact Windows asset selection and verified downloads."""
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
import httpx
from .._vendor.pyupdater.config import UpdateConfig

SLUG = "weizhigao21/shengjian-tts"
PAGE = f"https://github.com/{SLUG}/releases"
MAX_DOWNLOAD = 1024 * 1024 * 1024
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\Z")


class UpdateError(RuntimeError):
    pass


def configuration():
    return UpdateConfig(owner="weizhigao21", repo="shengjian-tts", exe_name="声笺.exe", app_name="声笺",
                        updater_name="声笺更新器.exe", asset_prefix="shengjian",
                        data_dirs=("data", "Config", "Download", "logs", "models", "voices", "references", "audio", "audio-editor"),
                        exit_wait_seconds=120)


def version_tuple(value):
    match = VERSION.fullmatch(value or "")
    if not match:
        raise UpdateError("发行版本号无效，更新已停止")
    return tuple(int(v) for v in match.groups())


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    page: str
    filename: str
    url: str
    size: int
    sha256: str


def asset_url(asset, tag):
    name = asset.get("name", "")
    url = asset.get("browser_download_url", "")
    if url != f"https://github.com/{SLUG}/releases/download/{tag}/{name}":
        raise UpdateError("更新资源不属于指定的发行仓库")
    return url


def parse_release(data, client):
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise UpdateError("最新发行不是可用的正式版本")
    tag = data.get("tag_name", "")
    version_tuple(tag)
    version = tag.removeprefix("v")
    filename = f"shengjian-v{version}-windows-x64.zip"
    assets = data.get("assets", [])
    matches = [a for a in assets if a.get("name") == filename and a.get("state", "uploaded") == "uploaded"]
    if len(matches) != 1:
        raise UpdateError("正式发行未提供匹配的 Windows x64 完整包")
    asset = matches[0]
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_DOWNLOAD:
        raise UpdateError("更新包大小无效")
    url = asset_url(asset, tag)
    digest = asset.get("digest") or ""
    if digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError("更新包校验信息无效")
    checksum = digest[7:].lower() if digest else ""
    if not checksum:
        manifests = [a for a in assets if a.get("name") == "SHA256SUMS.txt"]
        if len(manifests) != 1:
            raise UpdateError("发行包缺少 SHA-256 校验信息，请到发行页手动下载")
        response = client.get(asset_url(manifests[0], tag))
        response.raise_for_status()
        if len(response.content) > 128 * 1024:
            raise UpdateError("校验清单异常")
        entries = [line.split() for line in response.text.splitlines() if line.strip()]
        hashes = [parts[0].lower() for parts in entries if len(parts) == 2 and parts[1].lstrip('*') == filename]
        if len(hashes) != 1 or not re.fullmatch(r"[0-9a-f]{64}", hashes[0]):
            raise UpdateError("校验清单中没有对应的 Windows 更新包")
        checksum = hashes[0]
    return Release(version, tag, str(data.get("body") or "")[:50000], f"{PAGE}/tag/{tag}", filename, url, size, checksum)


def check_release(client_factory=None, cancelled=None):
    factory = client_factory or httpx.Client
    failures = []
    for proxy in configuration().proxies:
        if cancelled and cancelled.is_set():
            raise UpdateError("已取消检查")
        try:
            with factory(proxy=proxy, timeout=httpx.Timeout(10), follow_redirects=True, trust_env=False,
                         headers={"User-Agent": "Shengjian-Updater", "Accept": "application/vnd.github+json"}) as client:
                response = client.get(f"https://api.github.com/repos/{SLUG}/releases/latest")
                if response.status_code == 404:
                    raise UpdateError("仓库没有可用的公开发行版")
                if response.status_code == 429 or response.status_code == 403 and response.headers.get('x-ratelimit-remaining') == '0':
                    raise UpdateError("GitHub 接口访问频繁，请稍后再检查更新")
                response.raise_for_status()
                return parse_release(response.json(), client)
        except UpdateError:
            raise
        except Exception as exc:
            failures.append(str(exc))
    raise UpdateError("无法检查更新，请稍后重试或打开发行页。" + ("\n" + failures[-1] if failures else ""))


def download(release, directory, *, progress=None, cancelled=None, client_factory=None):
    target = Path(directory) / release.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".zip.part")
    factory = client_factory or httpx.Client
    failures = []
    for proxy in configuration().proxies:
        if cancelled and cancelled.is_set():
            raise UpdateError("已取消下载")
        try:
            received = 0
            digest = hashlib.sha256()
            with factory(proxy=proxy, timeout=httpx.Timeout(10), follow_redirects=True, trust_env=False) as client:
                with client.stream("GET", release.url) as response:
                    response.raise_for_status()
                    with part.open("wb") as stream:
                        for block in response.iter_bytes(1024 * 256):
                            if cancelled and cancelled.is_set():
                                raise UpdateError("已取消下载")
                            received += len(block)
                            if received > release.size:
                                raise UpdateError("下载长度超过发行资源声明")
                            stream.write(block)
                            digest.update(block)
                            if progress:
                                progress(received, release.size)
            if received != release.size or digest.hexdigest() != release.sha256:
                raise UpdateError("更新包大小或 SHA-256 不匹配，未安装")
            part.replace(target)
            return target
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:
            part.unlink(missing_ok=True)
            failures.append(str(exc))
    raise UpdateError("下载失败，请稍后重试。\n" + failures[-1])
