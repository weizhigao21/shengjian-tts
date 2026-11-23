"""局域网发现：广播询问、主电脑应答、授权之后单向发一次码。

这几条是安全边界，不是便利功能：同网段任何人都能广播，所以应答里只要混进
配对码，等于把 9.7 GB 模型和整个音色库对全楼开放。
"""
from __future__ import annotations

import socket

import pytest

from voice_workbench.handoff import discovery


def test_registry_hands_the_code_out_only_once():
    registry = discovery.PairRegistry()
    registry.register("tok-1", "192.168.1.9")
    assert registry.state("tok-1") == "pending"
    assert registry.take("tok-1", "CODE1234") == "", "没批准之前不能发码"

    assert registry.approve("tok-1")
    assert registry.take("tok-1", "CODE1234") == "CODE1234"
    assert registry.take("tok-1", "CODE1234") == "", "配对码只能交出去一次"


def test_registry_reports_and_drops_pending_requests():
    registry = discovery.PairRegistry()
    registry.register("tok-1", "192.168.1.9")
    registry.register("tok-2", "192.168.1.10")
    assert {item["address"] for item in registry.pending()} == {"192.168.1.9", "192.168.1.10"}

    registry.approve("tok-1")
    assert [item["token"] for item in registry.pending()] == ["tok-2"], "批准后不该再出现在待办里"

    assert registry.deny("tok-2")
    assert registry.pending() == []
    assert registry.state("tok-2") == ""


def test_foreign_udp_messages_are_ignored():
    assert discovery._parse(b"not json") is None
    assert discovery._parse(b'{"kind":"who"}') is None, "别的程序发的包不该被当成询问"
    assert discovery._parse(discovery._payload("who", token="t"))["token"] == "t"


def test_responder_answers_without_leaking_the_code():
    registry = discovery.PairRegistry()
    responder = discovery.Responder(
        lambda: {"port": 8765, "name": "主电脑", "files": 3, "bytes": 100},
        registry, lambda: "CODE1234")
    if not responder.start():
        pytest.skip(f"发现端口被占用：{responder.error}")

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(3)
    try:
        probe.sendto(discovery._payload("who", token="tok-1"), ("127.0.0.1", discovery.PORT))
        answer = discovery._parse(probe.recvfrom(4096)[0])
        assert answer["kind"] == "here" and answer["port"] == 8765
        assert "code" not in answer, "广播应答里绝不能带配对码"
        assert registry.state("tok-1") == "pending"

        probe.sendto(discovery._payload("pair", token="tok-1"), ("127.0.0.1", discovery.PORT))
        assert discovery._parse(probe.recvfrom(4096)[0])["code"] == ""

        registry.approve("tok-1")
        probe.sendto(discovery._payload("pair", token="tok-1"), ("127.0.0.1", discovery.PORT))
        assert discovery._parse(probe.recvfrom(4096)[0])["code"] == "CODE1234"

        probe.sendto(discovery._payload("pair", token="tok-1"), ("127.0.0.1", discovery.PORT))
        assert discovery._parse(probe.recvfrom(4096)[0])["code"] == "", "再次询问不该重复发码"
    finally:
        probe.close()
        responder.stop()


def test_responder_stays_silent_until_the_handoff_is_ready():
    """beacon 返回 None 表示还没准备好：这时连登记都不该发生。"""
    registry = discovery.PairRegistry()
    responder = discovery.Responder(lambda: None, registry, lambda: "CODE1234")
    if not responder.start():
        pytest.skip(f"发现端口被占用：{responder.error}")

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(1.0)
    try:
        probe.sendto(discovery._payload("who", token="tok-1"), ("127.0.0.1", discovery.PORT))
        with pytest.raises(socket.timeout):
            probe.recvfrom(4096)
        assert registry.pending() == []
    finally:
        probe.close()
        responder.stop()
