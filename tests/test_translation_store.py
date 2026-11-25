"""术语库、翻译记忆与运行记录的最小存储契约。"""
from voice_workbench.errors import VoiceError
from voice_workbench.translation.store import TranslationStore, normalize_language, normalized


def test_language_codes_normalise_to_the_ui_form():
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("zh_CN") == "zh"
    assert normalize_language("zh-Hant") == "zh"
    assert normalize_language("en-US") == "en"
    assert normalize_language("ja-JP") == "ja"
    assert normalize_language("yue-HK") == "yue"
    assert normalize_language("English") == "English"
    assert normalize_language("") == ""


def test_terms_and_memory_match_across_locale_aliases(tmp_path):
    store = TranslationStore(tmp_path)
    try:
        term = store.add_term("MCP", "协议", "en-US", "zh-CN")
        assert term["source_lang"] == "en" and term["target_lang"] == "zh"
        assert store.terms("MCP", "zh-CN")[0]["id"] == term["id"]
        assert store.matching_terms(["MCP server"], "zh-CN")[0]["id"] == term["id"]
        memory = store.remember("MCP server", "协议服务器", "en-US", "zh-CN")
        assert memory["target_lang"] == "zh"
        assert store.find_memory("MCP server", "en-US", "zh-CN")["id"] == memory["id"]
    finally:
        store.close()


def test_terms_are_unique_and_soft_deleted(tmp_path):
    store = TranslationStore(tmp_path)
    try:
        first = store.add_term("声笺", "Shengjian", "zh", "en", "产品名", 30)
        again = store.add_term("声笺", "Sheng Jian", "zh", "en", "更新译法", 40)
        assert again["id"] == first["id"]
        assert again["target"] == "Sheng Jian" and again["priority"] == 40
        assert store.terms("声笺", "en")[0]["target"] == "Sheng Jian"
        assert not store.terms("声笺", "ja")
        updated = store.update_term(first["id"], {"target": "Shengjian", "enabled": False})
        assert updated["target"] == "Shengjian" and updated["enabled"] is False
        assert store.terms("声笺", "en") == []
        assert len(store.terms("声笺", "en", enabled_only=False)) == 1
        preserved = store.add_term("声笺", "Sheng Jian 2", "zh", "en", "", 5, preserve_metadata=True)
        assert preserved["id"] == first["id"] and preserved["target"] == "Sheng Jian 2"
        assert preserved["note"] == "更新译法" and preserved["priority"] == 40
        try:
            store.get_term("missing")
        except VoiceError as exc:
            assert exc.code == "TRANSLATION_TERM_NOT_FOUND"
        else:
            raise AssertionError("missing term should be a 404")
    finally:
        store.close()


def test_memory_exact_match_touch_and_soft_delete(tmp_path):
    store = TranslationStore(tmp_path)
    try:
        source = "下载进度刷新，保留滚动位置。"
        first = store.remember(source, "The download progress refreshes.", "zh", "en", quality=2)
        assert normalized(source) in first["source"]
        hit = store.find_memory(source, "zh", "en")
        assert hit["id"] == first["id"] and hit["uses"] == 0
        store.touch_memory(first["id"])
        assert store.find_memory(source, "zh", "en")["uses"] == 1
        # A manual add with the same key updates the translation without creating a duplicate.
        again = store.remember(source, "Refresh the progress.", "zh", "en", quality=1)
        assert again["id"] == first["id"] and again["target"] == "Refresh the progress."
        tagged = store.remember(source, "Refresh tagged.", "zh", "en", session_id="s2",
                                provider="api", model="m2", quality=2)
        preserved = store.remember(source, "Refresh kept.", "zh", "en", quality=1, preserve_metadata=True)
        assert preserved["id"] == tagged["id"] and preserved["target"] == "Refresh kept."
        assert preserved["session_id"] == "s2" and preserved["provider"] == "api"
        updated = store.update_memory(first["id"], {"target": "Refresh updated.", "quality": 2})
        assert updated["target"] == "Refresh updated." and updated["quality"] == 2
        assert len(store.memory(source, "en")) == 1
        store.disable_memory(first["id"])
        assert store.find_memory(source, "zh", "en") is None
        assert len(store.memory(source, "en")) == 0
    finally:
        store.close()


def test_terms_and_memory_paginate_with_offset(tmp_path):
    store = TranslationStore(tmp_path)
    try:
        for index in range(5):
            store.add_term(f"原文{index}", f"译文{index}", "zh", "en", "", 10 + index)
            store.remember(f"句子{index}", f"Sentence {index}", "zh", "en")
        term_pages = [store.terms(limit=2, offset=0), store.terms(limit=2, offset=2)]
        assert [len(page) for page in term_pages] == [2, 2]
        ids = {term["id"] for page in term_pages for term in page}
        assert len(ids) == 4
        memory_pages = [store.memory(limit=2, offset=0), store.memory(limit=2, offset=2)]
        assert [len(page) for page in memory_pages] == [2, 2]
        ids = {memory["id"] for page in memory_pages for memory in page}
        assert len(ids) == 4
    finally:
        store.close()


def test_runs_keep_only_a_summary_and_can_be_read_back(tmp_path):
    store = TranslationStore(tmp_path)
    try:
        run = store.create_run("s1", "en", "api")
        assert run["status"] == "running" and run["trace"] == []
        done = store.update_run(run["id"], status="done", trace=[{"tool": "review", "status": "ok"}],
                                result={"lines": 3}, error="")
        assert done["status"] == "done" and done["result"]["lines"] == 3
        assert store.runs("s1")[0]["id"] == run["id"]
        try:
            store.get_run("missing")
        except VoiceError as exc:
            assert exc.code == "TRANSLATION_RUN_NOT_FOUND"
        else:
            raise AssertionError("missing run should be a 404")
    finally:
        store.close()
