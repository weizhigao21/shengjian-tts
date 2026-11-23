"""局域网分发服务：主电脑把清单里的文件按需交给另一台电脑。

只服务启动时那份清单里的文件，用序号而不是路径寻址，因此既不会泄露
清单之外的目录，也不存在路径拼接穿越的可能。配对码放在 URL 前缀里，
同一网段里随手扫到端口的人拿不到内容。

实现只用标准库，不引入额外依赖；服务跑在后台线程里，随时可以停。
"""
from __future__ import annotations

import gzip
import io
import json
import mimetypes
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from .discovery import PairRegistry, Responder
from .manifest import SCOPES, Builder, summarize

# 去掉形状相近的字符，避免对着屏幕手抄配对码时出错。
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CHUNK = 1024 * 1024
PACKAGE_NAME = "声笺"


def pairing_code(length=8):
    return "".join(ALPHABET[b % len(ALPHABET)] for b in os.urandom(length))


def _address_score(address: str) -> int:
    """Prefer a real LAN address over tunnels, APIPA and emulator adapters.

    198.18.0.0/15 is what a proxy TUN (Clash and friends) hands out, and it is
    reachable from exactly one machine; announcing it makes the other side time
    out with no hint about why.
    """
    if address.startswith(("127.", "0.", "169.254.", "198.18.", "198.19.")):
        return -1
    if address.startswith("192.168."):
        return 5
    if address.startswith("10."):
        return 4
    if address.startswith("172."):
        try:
            if 16 <= int(address.split(".")[1]) <= 31:
                return 4
        except (ValueError, IndexError):
            pass
    return 1


def lan_addresses():
    """Every IPv4 worth showing, best first."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    ranked = sorted({address for address in found if _address_score(address) >= 0},
                    key=lambda address: (-_address_score(address), address))
    return ranked or ["127.0.0.1"]


def lan_address():
    return lan_addresses()[0]


def snapshot_database(data_dir: Path) -> Path | None:
    """Copy the library database through SQLite's backup API.

    Copying the .sqlite3 file while the app is running would drop whatever is
    still only in the WAL, and copying the WAL as well would hand over a page
    cache that describes the *old* file. A backup is the only copy that is
    consistent while the source keeps writing.
    """
    source = Path(data_dir) / "tasks.sqlite3"
    if not source.is_file():
        return None
    folder = Path(data_dir) / "handoff" / "snapshot"
    folder.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix="tasks-", suffix=".sqlite3", dir=folder)
    os.close(handle)
    staged = Path(raw)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as old:
            with sqlite3.connect(staged) as new, new:
                old.backup(new)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def build_program_zip(install_dir, destination: Path, version: str = "") -> Path:
    """Pack the program itself so a machine with nothing on it can bootstrap.

    The other computer has no Python and no 声笺 yet, so this archive is the only
    thing that can be fetched by a bare .bat before any of our code exists.
    """
    install_dir = Path(install_dir)
    if not (install_dir / "声笺.exe").is_file():
        raise RuntimeError(f"安装目录里没有发现声笺.exe：{install_dir}")
    root = install_dir.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix="program-", suffix=".zip.part", dir=destination.parent)
    os.close(handle)
    staged = Path(raw)
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for directory, folders, files in os.walk(root):
                base = Path(directory)
                folders[:] = sorted(f for f in folders if f != "data" and not (base / f).is_symlink())
                for name in sorted(files):
                    path = base / name
                    if path.is_symlink():
                        continue
                    archive.write(path, (Path(PACKAGE_NAME) / path.relative_to(root)).as_posix())
        os.replace(staged, destination)
        return destination
    finally:
        if staged.exists():
            staged.unlink(missing_ok=True)


def build_bootstrap_zip(host: str, port: int, code: str, scope: str, manifest: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("handoff.json", json.dumps({
            "schema": 1, "host": host, "port": port, "code": code, "scope": scope,
            "version": manifest.get("version", ""), "data_dir": manifest.get("data_dir", ""),
            "install_dir": manifest.get("install_dir", ""),
            "total_bytes": manifest.get("bytes", 0), "files": manifest.get("files", 0),
        }, ensure_ascii=False, indent=2))
        archive.writestr("开始接收.bat", _launcher_batch(host, port, code))
        archive.writestr("使用说明.txt", _readme(host, port, code, manifest))
    return buffer.getvalue()


def _launcher_batch(host, port, code):
    """cmd + PowerShell only: the target machine has neither Python nor 声笺 yet."""
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title 声笺 · 局域网接收\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"set \"HOST={host}\"\r\n"
        f"set \"PORT={port}\"\r\n"
        f"set \"CODE={code}\"\r\n"
        "set \"ROOT=%~dp0\"\r\n"
        "set \"SVC=%ROOT%声笺\\声笺服务.exe\"\r\n"
        "set \"APP=%ROOT%声笺\\声笺.exe\"\r\n"
        "if exist \"%SVC%\" goto receive\r\n"
        "echo 正在获取声笺程序（约 70 MB），请稍等...\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -Command \"try { "
        "Invoke-WebRequest -Uri ('http://%HOST%:%PORT%/%CODE%/program.zip') "
        "-OutFile ($env:ROOT + 'program.zip') -UseBasicParsing; "
        "Expand-Archive -Path ($env:ROOT + 'program.zip') -DestinationPath $env:ROOT -Force; "
        "Remove-Item ($env:ROOT + 'program.zip') -Force } "
        "catch { Write-Host ('获取程序失败：' + $_.Exception.Message); exit 1 }\"\r\n"
        "if not exist \"%SVC%\" goto fail\r\n"
        ":receive\r\n"
        "echo.\r\n"
        "echo 正在同网段寻找主电脑（主电脑需要停在「局域网分发」页面）...\r\n"
        "\"%SVC%\" handoff-receive --wait 30\r\n"
        "if not errorlevel 1 goto done\r\n"
        "echo.\r\n"
        "echo 自动发现没有成功，改用引导包里的地址继续...\r\n"
        "\"%SVC%\" handoff-receive --host %HOST% --port %PORT% --code %CODE%\r\n"
        "if errorlevel 1 goto fail\r\n"
        ":done\r\n"
        "echo.\r\n"
        "echo 接收完成，正在启动声笺...\r\n"
        "start \"\" \"%APP%\"\r\n"
        "timeout /t 3 >nul\r\n"
        "exit /b 0\r\n"
        ":fail\r\n"
        "echo.\r\n"
        "echo 接收没有完成。请确认主电脑上的声笺仍停留在「局域网分发」，再重新双击本文件。\r\n"
        "pause\r\n"
        "exit /b 1\r\n"
    )


def _readme(host, port, code, manifest):
    gb = manifest.get("bytes", 0) / 1024 ** 3
    return (
        "声笺 · 局域网交接\n\n"
        "1. 双击「开始接收.bat」，不要把它从文件夹里单独拖出来。\n"
        "2. 主电脑上的声笺需要保持打开，且停留在「局域网分发」页面。\n"
        "3. 程序会先在同网段自动寻找主电脑。找到后如果窗口提示等待确认，\n"
        "   请在那台电脑的「局域网分发」里点「允许接入」；半分钟没有确认会自动\n"
        f"   改用本文件里记下的地址 {host}:{port} 继续。\n"
        f"4. 会接收约 {gb:.1f} GB 内容，完成后自动启动声笺。\n"
        "5. 中途断开不影响：已收下的文件会保留，重新双击就从断点继续。\n\n"
        f"配对码：{code}（自动发现失败时，可手动带到接收命令里）\n"
        f"清单版本：{manifest.get('version') or '未知'}，共 {manifest.get('files', 0)} 个文件\n"
    )


class HandoffState:
    """Mutable state shared by the HTTP thread and the API/UI."""

    def __init__(self, data_dir, install_dir=None, version=""):
        self.builder = Builder(data_dir, install_dir, version)
        self.data_dir = Path(data_dir)
        self.install_dir = Path(install_dir) if install_dir else None
        self.version = version
        self.lock = threading.RLock()
        self.phase = "idle"          # idle | preparing | ready | error
        self.error = ""
        self.warnings = []
        self.scope = "batch"
        self.code = ""
        self.port = 0
        self.manifest = None
        self.entries = []
        self.sources = []
        self.snapshot = None
        self.package = None
        self.started = 0.0
        self.sent = {}               # entry id -> bytes served
        self.package_sent = 0
        self.clients = set()
        self.server = None
        self.thread = None
        # 同网段的另一台声笺可以广播找到这里；应答不含配对码，码只在
        # 主电脑点了「允许」之后按令牌单发一次。
        self.pairs = PairRegistry()
        self.responder = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, scope="batch", port=0):
        with self.lock:
            if self.phase in ("preparing", "ready"):
                return self.status()
            if scope not in SCOPES:
                raise ValueError(f"未知的交接范围：{scope}")
            self.phase, self.error, self.scope = "preparing", "", scope
            self.warnings = []
            self.sent, self.clients, self.package_sent = {}, set(), 0
            self.started = time.time()
        threading.Thread(target=self._prepare, args=(scope, port), name="handoff-start", daemon=True).start()
        return self.status()

    def _prepare(self, scope, port):
        try:
            snapshot = snapshot_database(self.data_dir)
            manifest = self.builder.build(scope, snapshot=snapshot)
            # 工作流音频这类易变内容可能在哈希期间就被清理掉；少数消失只剔除
            # 对应条目，不能让整次交接失败。大面积消失才是磁盘或路径问题。
            entries, sources = [], []
            for entry in manifest["entries"]:
                path = Path(entry["source"])
                if path.is_file():
                    sources.append(path)
                    # 绝对路径只服务端自己要用（读文件），不下发：48366 条各带一段
                    # 长路径会让清单凭空胖出几 MB，而目标机一个字节都用不上。
                    entries.append({key: value for key, value in entry.items() if key != "source"})
            vanished = len(manifest["entries"]) - len(entries)
            if vanished and len(entries) < len(manifest["entries"]) * 0.95:
                raise RuntimeError(f"清单里有 {vanished} 个文件在整理期间消失，请重新开始交接。")
            if vanished:
                manifest = {**manifest, **summarize(entries), "entries": entries,
                            "vanished": vanished}
                with self.lock:
                    self.warnings = [f"{vanished} 个临时文件已过期，未加入清单"]
            package = self._package()
            server = _make_server(self, port)
            with self.lock:
                self.manifest, self.entries, self.sources = manifest, entries, sources
                self.snapshot, self.package = snapshot, package
                self.code = pairing_code()
                self.port = server.server_address[1]
                self.server = server
            thread = threading.Thread(target=server.serve_forever, name="handoff-http", daemon=True)
            thread.start()
            with self.lock:
                self.thread, self.phase = thread, "ready"
            # 发现服务只是方便，端口被占用不该让整次交接失败。
            responder = Responder(self._beacon, self.pairs, lambda: self.code)
            responder.start()
            with self.lock:
                self.responder = responder
        except Exception as exc:                      # noqa: BLE001 - reported to the UI
            with self.lock:
                self.phase, self.error = "error", str(exc)
            self._discard_snapshot()

    def _package(self):
        """Reuse a packed program when nothing in it changed."""
        if not self.install_dir:
            return None
        folder = self.data_dir / "handoff"
        cached = folder / f"program-{self.version or 'local'}.zip"
        marker = self.install_dir / "program-files.json"
        try:
            stamp = marker.stat().st_mtime
        except OSError:
            stamp = 0
        if cached.is_file() and cached.stat().st_mtime >= stamp:
            return cached
        return build_program_zip(self.install_dir, cached, self.version)

    def _discard_snapshot(self):
        if self.snapshot and self.snapshot.exists():
            self.snapshot.unlink(missing_ok=True)
        self.snapshot = None

    def _beacon(self):
        """发现应答的内容：够让对端认出是哪台机器，但不含任何凭据。"""
        with self.lock:
            if self.phase != "ready" or not self.port:
                return None
            manifest = self.manifest or {}
            return {"port": self.port, "name": socket.gethostname(), "version": self.version,
                    "scope": self.scope, "files": manifest.get("files", 0),
                    "bytes": manifest.get("bytes", 0)}

    def stop(self):
        with self.lock:
            server, thread = self.server, self.thread
            responder = self.responder
            self.server, self.thread, self.responder = None, None, None
            self.phase = "idle"
        if responder is not None:
            responder.stop()
        self.pairs.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._discard_snapshot()

    # -- reporting ---------------------------------------------------------
    def status(self):
        with self.lock:
            manifest = self.manifest or {}
            sizes = {entry["id"]: entry["size"] for entry in self.entries}
            # Bytes on the wire can exceed file size when a client restarts a
            # file, so clamp per entry rather than reporting inflated progress.
            sent = sum(min(value, sizes.get(key, value)) for key, value in self.sent.items())
            sent += min(self.package_sent, self.package.stat().st_size if self.package else 0)
            return {
                "phase": self.phase, "error": self.error, "scope": self.scope,
                "warnings": list(self.warnings),
                "scope_label": SCOPES.get(self.scope, {}).get("label", ""),
                "code": self.code, "port": self.port,
                "address": lan_address() if self.phase == "ready" else "",
                "addresses": lan_addresses() if self.phase == "ready" else [],
                "url": f"http://{lan_address()}:{self.port}/{self.code}/" if self.phase == "ready" else "",
                "files": manifest.get("files", 0), "bytes": manifest.get("bytes", 0),
                "groups": manifest.get("groups", {}),
                "package": self.package.name if self.package else "",
                "sent_bytes": sent, "clients": len(self.clients),
                "discovery": bool(self.responder and self.responder.running),
                "pending_pairs": self.pairs.pending() if self.phase == "ready" else [],
                "elapsed": round(time.time() - self.started, 1) if self.started else 0,
            }

    def entry_bytes(self, index):
        with self.lock:
            if 0 <= index < len(self.entries):
                return self.entries[index], self.sources[index]
        return None, None


class _Handler(BaseHTTPRequestHandler):
    server_version = "ShengjianHandoff/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # the UI shows progress; console spam helps nobody
        pass

    def _state(self) -> HandoffState:
        return self.server.state

    def _authorized(self, parts):
        state = self._state()
        with state.lock:
            return bool(parts) and parts[0] == state.code and state.phase == "ready"

    def _deny(self, status=403, text="配对码不正确，或主电脑已结束交接。"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        parsed = urlparse(self.path)
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if not parts:
            return self._landing()
        if not self._authorized(parts):
            return self._deny()
        tail = parts[1:]
        state = self._state()
        with state.lock:
            state.clients.add(self.client_address[0])
        if tail == ["manifest.json"]:
            return self._json(head)
        if len(tail) == 2 and tail[0] == "file":
            return self._file(tail[1], head)
        if tail == ["program.zip"]:
            return self._program(head)
        if tail == ["bootstrap.zip"]:
            return self._bootstrap(head)
        return self._deny(404, "没有这个地址。")

    # -- endpoints ---------------------------------------------------------
    def _landing(self):
        """A plain page so a browser can confirm the address before copying anything."""
        state = self._state()
        if state.phase != "ready":
            return self._deny(503, "主电脑正在准备清单，请稍等几秒再刷新。")
        body = (f"<meta charset=utf-8><title>声笺 · 局域网分发</title>"
                f"<body style='font:15px/1.7 system-ui;padding:40px;max-width:640px'>"
                f"<h2>声笺 · 局域网分发</h2><p>配对码：<b style='font-size:22px'>{state.code}</b></p>"
                f"<p>清单：{state.manifest['files']} 个文件 / {state.manifest['bytes']/1024**3:.1f} GB</p>"
                f"<p><a href='/{state.code}/bootstrap.zip'>下载引导包</a>，"
                f"拷到另一台电脑解压后双击「开始接收.bat」。</p></body>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, head):
        state = self._state()
        with state.lock:
            raw = json.dumps({**state.manifest, "code": state.code}, ensure_ascii=False).encode("utf-8")
        body, encoding = raw, ""
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body, encoding = gzip.compress(raw, 6), "gzip"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _bootstrap(self, head):
        state = self._state()
        with state.lock:
            manifest, code, port = state.manifest, state.code, state.port
        payload = build_bootstrap_zip(lan_address(), port, code, state.scope, manifest)
        self._send_bytes(payload, "application/zip", head,
                         attachment="%s-引导包.zip" % PACKAGE_NAME)

    def _program(self, head):
        state = self._state()
        with state.lock:
            package = state.package
        if package is None or not package.is_file():
            return self._deny(404, "主电脑没有可下发的程序包。")
        return self._send_path(package, "application/zip", head, counter="package")

    def _file(self, raw_index, head):
        state = self._state()
        try:
            index = int(raw_index)
        except ValueError:
            return self._deny(404, "没有这个文件。")
        entry, source = state.entry_bytes(index)
        if entry is None or not source.is_file():
            return self._deny(404, "没有这个文件。")
        return self._send_path(source, mimetypes.guess_type(entry["rel"])[0] or "application/octet-stream",
                               head, counter=entry["id"], related=entry["rel"])

    # -- sending -----------------------------------------------------------
    def _send_bytes(self, payload, content_type, head, attachment=""):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(attachment))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _send_path(self, source: Path, content_type, head, counter, related=""):
        state = self._state()
        total = source.stat().st_size
        start, end = self._range(total)
        if start is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        partial = bool(start) or end != total - 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head:
            return
        remaining = end - start + 1
        try:
            with source.open("rb") as stream:
                stream.seek(start)
                while remaining > 0:
                    block = stream.read(min(CHUNK, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
                    with state.lock:
                        if counter == "package":
                            state.package_sent += len(block)
                        else:
                            state.sent[counter] = state.sent.get(counter, 0) + len(block)
        except (BrokenPipeError, ConnectionResetError):
            # The other side closed the window; the .part file it keeps lets the
            # next attempt continue from exactly here.
            pass

    def _range(self, total):
        header = self.headers.get("Range", "")
        if not header.startswith("bytes="):
            return 0, max(total - 1, 0)
        spec = header[6:].split(",")[0].strip()
        first, _, last = spec.partition("-")
        try:
            if not first:
                length = int(last)
                return max(total - length, 0), total - 1
            start = int(first)
            end = int(last) if last else total - 1
        except ValueError:
            return 0, max(total - 1, 0)
        if start > end or start >= total:
            return None, None
        return start, min(end, total - 1)


def _make_server(state: HandoffState, port: int):
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("0.0.0.0", port), _Handler)
    server.state = state
    return server


__all__ = ["HandoffState", "lan_address", "pairing_code", "snapshot_database",
           "build_bootstrap_zip", "build_program_zip"]
