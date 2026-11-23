"""生成池：多份字幕合成一个任务，让批量合批凑得满 6 条冷启动门槛。"""
from __future__ import annotations

import os

import pytest

from voice_workbench.subtitles import POOL_LIMIT, build_pool, parse_text, read_text

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:03,500 --> 00:00:05,000
第二句
"""

TXT = "第三句\n第四句\n"


def test_pool_merges_several_files_into_one_task():
    merged, segments = build_pool([SRT, TXT])
    assert segments == 4, "两个字幕文件的段数应该相加"
    assert "第一句" in merged and "第四句" in merged
    assert len(parse_text(merged)) == segments


def test_pool_appends_instead_of_overwriting():
    merged, segments = build_pool([TXT], existing="已有的正文")
    assert merged.startswith("已有的正文")
    assert "第三句" in merged
    assert segments == 3


def test_pool_rejects_empty_input():
    with pytest.raises(ValueError):
        build_pool(["   ", ""])


def test_pool_keeps_webvtt_header_out_of_the_pool():
    """VTT 文件头不是台词；混进池里会被当成一句话念出来。"""
    _, segments = build_pool(["WEBVTT\n\n00:01.000 --> 00:02.000\nhello", "第二句"])
    texts = [item["text"] for item in parse_text("WEBVTT\n\n00:01.000 --> 00:02.000\nhello\n\n第二句")]
    assert texts == ["hello", "第二句"], texts
    assert segments == 2


def test_pool_limit_matches_the_api_cap():
    """池的上限必须与接口的段数上限一致，否则报错会拖到提交那一步。"""
    from voice_workbench.schemas import JobInput

    field = JobInput.model_fields["segments"]
    caps = [meta.max_length for meta in field.metadata if hasattr(meta, "max_length")]
    assert POOL_LIMIT in caps


def test_pool_rejects_more_than_the_api_can_take():
    with pytest.raises(ValueError, match="超过单次可提交"):
        build_pool(["\n".join(f"第{i}句" for i in range(POOL_LIMIT + 5))])


def test_import_dialog_accumulates_a_pool(monkeypatch, tmp_path):
    """导入按钮读到的多份文件要合成一个池，而不是后者覆盖前者。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QFileDialog, QLineEdit, QPlainTextEdit

    from voice_workbench.desktop.controllers.generation import GenerationController

    app = QApplication.instance() or QApplication([])
    first, second = tmp_path / "第一集.srt", tmp_path / "第二集.txt"
    first.write_text(SRT, encoding="utf-8")
    second.write_text(TXT, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *args, **kwargs: ([str(first), str(second)], "")))

    messages = []

    class Stub(GenerationController):
        def __init__(self):
            self.text_edit = QPlainTextEdit()
            self.title_edit = QLineEdit()

        def statusBar(self):
            return type("Bar", (), {"showMessage": lambda _self, text, ms=0: messages.append(text)})()

        def show_error(self, message):
            raise AssertionError(message)

    stub = Stub()
    stub.import_text()
    body = stub.text_edit.toPlainText()
    assert "第一句" in body and "第三句" in body
    assert stub.title_edit.text() == "第一集 等 2 个字幕"
    assert "生成池共 4 段" in messages[-1]
    app.processEvents()


def test_read_text_falls_back_to_gb18030(tmp_path):
    path = tmp_path / "字幕.srt"
    path.write_bytes(SRT.encode("gb18030"))
    assert "第一句" in read_text(path)

    utf8 = tmp_path / "utf8.txt"
    utf8.write_text("带 BOM 的文本", encoding="utf-8-sig")
    assert read_text(utf8) == "带 BOM 的文本"


def test_pool_preflight_sends_the_texts_and_explains_the_route():
    """池导入后要问服务端一次路由；答案必须分成「能合批」和「会走原生」两句话。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from voice_workbench.desktop.controllers.generation import GenerationController

    app = QApplication.instance() or QApplication([])
    messages, sent = [], []

    class Stub(GenerationController):
        def current_voice(self):
            return {"id": "qwen", "model_name": "qwen@abc"}

        def selected_preset(self):
            return {"name": "旁白"}

        def call(self, action, success=None, failure=None):
            sent.append(action)
            self.success = success

        def statusBar(self):
            return type("Bar", (), {"showMessage": lambda _self, text, ms=0: messages.append(text)})()

    stub = Stub()
    stub.pool_preflight(["一", "二", "三"])
    assert sent, "预检应该发一次请求"

    stub.pool_planned({"backend": "qwen3_batch", "batch_sizes": [8, 8, 3]})
    assert "19 段" in messages[-1] and "3 批" in messages[-1] and "最多 8" in messages[-1]

    stub.pool_planned({"backend": "native", "reason": "当前语言或语速不支持批量后端。"})
    assert "原生引擎" in messages[-1] and "语速" in messages[-1]

    stub.pool_preflight(["只有一句"])
    assert len(sent) == 1, "单段不值得预检"
    app.processEvents()
