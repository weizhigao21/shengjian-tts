"""局域网里找主电脑：UDP 询问与应答，只用标准库。

第一次接收的电脑上还没有声笺，那一步仍然靠浏览器下载引导包；这里服务的是
「两台都装着声笺、以后只补新增音色」的场景，省掉手抄 IP。

应答里**不带配对码**。同网段任何人都能广播，配对码只在对端于主电脑界面上点了
「允许」之后，通过 HTTP 单发一次，并且用过即焚。换句话说：发现是公开的，
信任仍然要人点一下。
"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time

PORT = 47831
MAGIC = "shengjian-handoff/1"
PAIR_TTL = 180.0


def _payload(kind, **fields):
    return json.dumps({"magic": MAGIC, "kind": kind, **fields}, ensure_ascii=False).encode("utf-8")


def _parse(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("magic") == MAGIC else None


def announce(timeout=1.5):
    """广播询问，返回同网段里应答了的主电脑（含本次请求的配对令牌）。

    令牌由被控端生成：主电脑只登记「谁用哪个令牌问过」，批准之后凭令牌发码。
    这样不需要被控端先猜一个 ID，也不存在枚举他人令牌就把码骗走的路径。
    """
    token = secrets.token_hex(8)
    found, seen = [], set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        probe.settimeout(0.2)
        message = _payload("who", token=token)
        for target in ("255.255.255.255", "<broadcast>"):
            try:
                probe.sendto(message, (target, PORT))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw, address = probe.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            answer = _parse(raw)
            if not answer or answer.get("kind") != "here":
                continue
            host = address[0]
            if host in seen:
                continue
            seen.add(host)
            try:
                port = int(answer.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if port <= 0:
                continue
            found.append({"host": host, "port": port, "name": str(answer.get("name") or ""),
                          "files": int(answer.get("files") or 0), "bytes": int(answer.get("bytes") or 0),
                          "token": token})
    return found


class PairRegistry:
    """主电脑侧：谁在申请接入，以及批准之后把配对码交给谁一次。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def _expire(self):
        cut = time.time() - PAIR_TTL
        for key in [k for k, v in self.items.items() if v["created"] < cut]:
            self.items.pop(key, None)

    def register(self, token, address):
        if not token:
            return
        with self.lock:
            self._expire()
            entry = self.items.setdefault(token, {"created": time.time(), "state": "pending"})
            entry["address"] = address

    def approve(self, token):
        with self.lock:
            entry = self.items.get(token)
            if entry is None:
                return False
            entry["state"] = "approved"
            return True

    def deny(self, token):
        with self.lock:
            entry = self.items.get(token)
            if entry is None:
                return False
            self.items.pop(token, None)
            return True

    def state(self, token):
        with self.lock:
            self._expire()
            entry = self.items.get(token)
            return entry["state"] if entry else ""

    def pending(self):
        with self.lock:
            self._expire()
            return [{"token": key, "address": value.get("address", ""), "created": value["created"]}
                    for key, value in self.items.items() if value["state"] == "pending"]

    def take(self, token, code):
        """批准过就用掉：配对码只交出去一次。"""
        with self.lock:
            entry = self.items.get(token)
            if entry is None or entry["state"] != "approved":
                return ""
            self.items.pop(token, None)
            return code

    def clear(self):
        with self.lock:
            self.items.clear()


class Responder:
    """主电脑侧：只在分发就绪时应答，未就绪就当没听见。"""

    def __init__(self, beacon, registry, code):
        self.beacon, self.registry, self.code = beacon, registry, code
        self.socket = None
        self.thread = None
        self.running = False
        self.error = ""

    def start(self):
        if self.running:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", PORT))
            sock.settimeout(0.4)
        except OSError as exc:      # 端口被别的进程占了：发现不可用，但分发照旧
            self.error = str(exc)
            return False
        self.socket, self.running, self.error = sock, True, ""
        self.thread = threading.Thread(target=self._serve, name="handoff-discovery", daemon=True)
        self.thread.start()
        return True

    def _serve(self):
        while self.running:
            try:
                raw, address = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            ask = _parse(raw)
            if not ask:
                continue
            kind = ask.get("kind")
            if kind == "pair":
                self._answer_pair(str(ask.get("token") or ""), address)
                continue
            if kind != "who":
                continue
            info = self.beacon()
            if not info:
                continue
            self.registry.register(str(ask.get("token") or ""), address[0])
            try:
                self.socket.sendto(_payload("here", **info), address)
            except OSError:
                continue

    def _answer_pair(self, token, address):
        """被控端在拿到配对码之前进不了任何 HTTP 路径，所以授权结果也走 UDP。"""
        state = self.registry.state(token)
        code = self.registry.take(token, self.code()) if state == "approved" else ""
        try:
            self.socket.sendto(_payload("pair", state=state or "unknown", code=code), address)
        except OSError:
            pass

    def stop(self):
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
        self.socket = None


def ask_pair(host, token, timeout=1.0):
    """向指定主电脑询问授权结果；批准之后会带回一次配对码。"""
    message = _payload("pair", token=token)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(timeout)
        try:
            probe.sendto(message, (host, PORT))
            while True:
                raw, _address = probe.recvfrom(4096)
                answer = _parse(raw)
                if answer and answer.get("kind") == "pair":
                    return {"state": str(answer.get("state") or ""), "code": str(answer.get("code") or "")}
        except (socket.timeout, OSError):
            return {"state": "", "code": ""}
