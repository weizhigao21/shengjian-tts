"""批量识别与翻译对话框的离屏装配检查。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QLabel

from voice_workbench.gui import Window
from voice_workbench.desktop.components.recognition_batch import BatchRecognitionDialog
from voice_workbench.desktop.components.recognition_translation import TranslationDialog
from voice_workbench.desktop.components.translation_knowledge import TranslationKnowledgeDialog


def test_recognition_page_exposes_batch_and_translation_entries(qtbot, tmp_path):
    window = Window(18895, tmp_path, autostart=False)
    qtbot.addWidget(window)
    panel = window.recognition
    assert panel.batch_button.text() == "打开批量识别…"
    assert panel.translate_button.text() == "翻译 / 查看译文…"
    assert not panel.batch_button.isEnabled() and not panel.translate_button.isEnabled()
    controller = window.recognition.controller
    controller.session = {"id": "s1", "created": 0, "updated": 0, "state": {
        "name": "演示录音", "status": "done", "lines": [{"index": 1, "start": 0, "end": 1, "text": "甲"}],
        "source_path": str(tmp_path / "演示.wav")}}
    controller.update_actions()
    assert panel.translate_button.isEnabled()
    controller.session["state"]["translation"] = {
        "target": "en", "provider": "workbuddy",
        "lines": [{"index": 1, "start": 0, "end": 1, "text": "甲", "translated": "A"}]}
    controller.render_session()
    assert panel.result_tabs.isTabVisible(1)
    assert panel.translated_lines.count() == 1
    assert panel.translated_lines.item(0).text().endswith("A")
    assert "已有译文：1 行" in panel.translation_state.text()
    window.close()


def test_batch_dialog_lists_files_and_reports_progress(qtbot, tmp_path):
    window = Window(18896, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = BatchRecognitionDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    first, second = tmp_path / "一.wav", tmp_path / "二.mp4"
    first.write_bytes(b"wav")
    second.write_bytes(b"mp4")
    dialog.pending = [str(first), str(second)]
    dialog.render_table()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 0).text() == "一.wav"
    assert dialog.table.item(0, 1).text() == str(tmp_path)
    assert dialog.table.item(0, 2).text() == "等待"
    assert dialog.dedupe.isChecked() and "连续" in dialog.dedupe.text()
    dialog.table.setCurrentCell(0, 0)
    dialog.update_actions()
    assert dialog.remove_button.isEnabled() and dialog.start_button.isEnabled()
    assert dialog.folder_button.isEnabled()

    state = {"id": "b1", "name": "批量识别 · 2 个文件", "status": "done", "total": 2, "done": 1, "failed": 1,
             "cancelled": 0, "pending": 0, "running": 0, "current_name": None, "format": "srt", "language": "zh",
             "items": [
                 {"path": str(first), "name": "一", "status": "done", "exported": [str(tmp_path / "一.srt")],
                  "export_error": "", "error": "", "session_id": "s1"},
                 {"path": str(second), "name": "二", "status": "failed", "exported": [],
                  "export_error": "", "error": "无法读取", "hint": "换一个文件", "session_id": "s2"}]}
    dialog.show_batch(state)
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 3).text() == "一.srt"
    assert dialog.table.item(1, 2).text() == "失败"
    assert "批量完成：成功 1/2，失败 1" in dialog.status.text()
    assert dialog.progress.value() == 2
    assert dialog.retry_button.isEnabled()
    assert dialog.start_button.isEnabled() is False
    window.close()


def test_batch_preview_exports_only_the_selected_translation(qtbot, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    window = Window(18893, tmp_path, autostart=False)
    qtbot.addWidget(window)
    panel = window.batch_recognition
    source = tmp_path / "访谈.wav"
    source.write_bytes(b"wav")
    item = {"path": str(source), "name": "访谈", "session_id": "s1"}
    state = {"translation": {"target": "zh", "lines": [
        {"index": 1, "start": 0, "end": 1, "text": "Hello", "translated": "你好。"},
        {"index": 2, "start": 1, "end": 2, "text": "Bye", "translated": ""}]}}
    output = tmp_path / "访谈.zh.srt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *args: (str(output), "SRT 字幕 (*.srt)"))
    panel.write_translation(state, item)
    assert output.read_text(encoding="utf-8") == "1\n00:00:00,00 --> 00:00:01,00\n你好。\n"
    assert "中文字幕已导出" in panel.status.text()
    assert hasattr(panel, "export_translation_button")
    window.close()


def test_batch_preview_plays_the_audio_copy_for_a_clicked_line(qtbot, tmp_path):
    window = Window(18898, tmp_path, autostart=False)
    qtbot.addWidget(window)
    panel = window.batch_recognition
    audio = Path(window.data_dir) / "recognition" / "batch.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"wav")
    source = tmp_path / "访谈.wav"
    source.write_bytes(b"wav")
    item = {"path": str(source), "name": "访谈", "session_id": "s1"}

    class FakePlayer:
        def __init__(self):
            self._source = None
            self.played = False
            self.position = None
        def source(self): return self._source
        def setSource(self, value): self._source = value
        def setPosition(self, value): self.position = value
        def play(self): self.played = True
        def stop(self): self.played = False
        def playbackState(self): return 0

    panel.preview_player = FakePlayer()
    panel.session_cache["s1"] = {"audio": "batch.wav"}
    panel.selected_item = lambda: item
    panel.play_preview_line({"index": 1, "start": 0.16, "end": 1.04})
    assert panel.preview_range == (0.16, 1.04)
    assert panel.preview_player._source.isLocalFile()
    assert Path(panel.preview_player._source.toLocalFile()) == audio
    assert panel.preview_player.played is True
    assert "第 1 行" in panel.status.text()
    panel.stop_preview()
    window.close()


def test_batch_subtitle_preview_maps_clicks_to_line_times(qtbot, tmp_path):
    from voice_workbench.desktop.layout.recognition_batch import SubtitlePreview

    view = SubtitlePreview()
    qtbot.addWidget(view)
    text = ("1\n00:00:00,16 --> 00:00:01,04\nRelax, Bambi.\n\n"
            "2\n00:00:01,12 --> 00:00:01,92\nTake a deep breath in.\n")
    view.set_subtitle_text(text)
    first = view.line_at(text.find("Relax"))
    second = view.line_at(text.find("Take a deep"))
    assert first == {"index": 1, "start": 0.16, "end": 1.04}
    assert second == {"index": 2, "start": 1.12, "end": 1.92}
    assert view.line_at(text.find("00:00:02")) is None


def test_batch_table_shortens_long_names_and_keeps_counts_visible(qtbot, tmp_path):
    window = Window(18894, tmp_path, autostart=False)
    qtbot.addWidget(window)
    panel = window.batch_recognition
    long_name = "A Very Long Interview Recording Name That Needs Ellipsis.wav"
    source = tmp_path / long_name
    source.write_bytes(b"wav")
    item = {"path": str(source), "name": "长文件", "status": "done", "session_id": "s1",
            "line_count": 20, "translated_lines": 17, "exported": [], "export_error": ""}
    panel.batch = {"id": "b1", "items": [item]}
    panel.render_table()
    shown = panel.table.item(0, 0).text()
    assert "…" in shown and shown.endswith(".wav")
    assert panel.table.item(0, 0).toolTip() == str(source)
    assert panel.table.item(0, 4).text() == "17/20"
    assert panel.table.columnWidth(4) >= 72
    window.close()


def test_batch_dialog_adds_dropped_files_and_deduplicates(qtbot, tmp_path):
    window = Window(18896, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = BatchRecognitionDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    assert dialog.acceptDrops()
    first, second, bad = tmp_path / "一.mp3", tmp_path / "二.wav", tmp_path / "说明.txt"
    for path in (first, second, bad):
        path.write_bytes(b"x")
    assert dialog.add_paths([str(first), str(second), str(bad)]) == 2
    assert [Path(path).name for path in dialog.pending] == ["一.mp3", "二.wav"]
    assert dialog.table.rowCount() == 2
    assert "已选择 2 个文件" in dialog.status.text()
    assert dialog.add_paths([str(first)]) == 0
    assert "已经在批量列表里" in dialog.status.text()
    window.close()


def test_batch_dialog_accepts_a_real_drop_event(qtbot, tmp_path):
    window = Window(18896, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = BatchRecognitionDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    first, second = tmp_path / "一.wav", tmp_path / "二.mp4"
    for path in (first, second):
        path.write_bytes(b"x")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(first)), QUrl.fromLocalFile(str(second))])
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog.dropEvent(drop)
    assert drop.isAccepted()
    assert [Path(path).name for path in dialog.pending] == ["一.wav", "二.mp4"]
    assert "已选择 2 个文件" in dialog.status.text()
    window.close()


def test_batch_dialog_compacts_the_table_for_the_file_count(qtbot, tmp_path):
    window = Window(18896, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = BatchRecognitionDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    dialog.show()
    files = []
    for index in range(6):
        path = tmp_path / f"第{index + 1}集.wav"
        path.write_bytes(b"x")
        files.append(str(path))
    dialog.pending = files
    dialog.render_table()
    row = dialog.table.verticalHeader().defaultSectionSize()
    header = dialog.table.horizontalHeader().sizeHint().height()
    assert dialog.table.height() == header + 6 * row + 2
    assert dialog.height() < 600
    assert dialog.status.y() - (dialog.table.y() + dialog.table.height()) < 40
    dialog.pending = [str(tmp_path / f"第{index + 1}集.wav") for index in range(20)]
    dialog.render_table()
    assert dialog.height() <= 650
    assert dialog.table.height() == header + 9 * row + 2
    assert dialog.table.verticalScrollBar().maximum() > 0
    window.close()


def test_translation_dialog_loads_settings_and_keeps_local_placeholder_explicit(qtbot, tmp_path):
    window = Window(18897, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = TranslationDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    dialog.session = {"id": "s1", "created": 0, "updated": 0, "state": {
        "name": "演示录音", "lines": [{"index": 1, "start": 0, "end": 1, "text": "甲"}],
        "source_path": str(tmp_path / "演示.wav")}}
    dialog.options_loaded({
        "providers": [{"id": "api", "name": "OpenAI 兼容 API"}, {"id": "local", "name": "本地模型（接口预留）"}],
        "languages": [["英语", "en"], ["中文", "zh"]],
        "settings": {"provider": "api", "base_url": "https://api.example.com/v1", "model": "demo",
                     "target": "en", "has_key": True}})
    assert dialog.provider.currentData() == "api"
    assert dialog.base_url.text() == "https://api.example.com/v1"
    assert "已保存密钥" in dialog.api_key.placeholderText()
    assert dialog.body()["model_name"] == "demo" and "api_key" not in dialog.body()
    assert dialog.translate_button.isEnabled()
    assert dialog.export_button.isEnabled() is False
    assert dialog.use_agent.isChecked() and dialog.knowledge_button.text() == "术语库 / 记忆"

    dialog.provider.blockSignals(True)
    dialog.provider.setCurrentIndex(dialog.provider.findData("local"))
    dialog.provider.blockSignals(False)
    dialog.provider_changed()
    api_visible = [not dialog.api_fields.itemAt(index).widget().isHidden()
                   for index in range(dialog.api_fields.count())
                   if isinstance(dialog.api_fields.itemAt(index).widget(), QLabel)]
    assert not any(api_visible)
    assert "预留" in dialog.provider_hint.text() and "不会偷偷" in dialog.provider_hint.text()
    window.close()


def test_translation_knowledge_dialog_renders_terms_and_memory(qtbot, tmp_path):
    window = Window(18898, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = TranslationKnowledgeDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    assert dialog.tabs.count() == 3
    assert dialog.term_add_button.text() == "新增术语"
    assert dialog.term_disable_button.isHidden() and dialog.term_clear_button.isHidden()
    assert dialog.memory_delete_button.isHidden()
    dialog.render_terms([{"id": "t1", "source": "声笺", "target": "Shengjian", "source_lang": "zh",
                          "target_lang": "en", "note": "产品名", "priority": 30, "enabled": True}])
    assert dialog.term_table.rowCount() == 1
    assert dialog.term_table.item(0, 0).text() == "声笺"
    assert dialog.term_table.item(0, 2).text() == "中文 → 英语"
    dialog.term_table.setCurrentCell(0, 0)
    # Selecting a row only enables the row actions; it must not jump into edit mode,
    # otherwise the next "add" silently overwrites the selected term.
    assert dialog.editing_term is None and dialog.term_source.text() == ""
    assert dialog.term_add_button.text() == "新增术语"
    assert not dialog.term_edit_button.isHidden()
    dialog.load_selected_term()
    assert dialog.editing_term == "t1" and dialog.term_source.text() == "声笺"
    assert dialog.term_target.text() == "Shengjian"
    assert dialog.term_add_button.text() == "保存修改" and not dialog.term_add_button.isHidden()
    assert not dialog.term_disable_button.isHidden() and not dialog.term_clear_button.isHidden()
    assert not dialog.term_source.isEnabled() and not dialog.term_source_lang.isEnabled()
    assert not dialog.term_target_lang.isEnabled()
    assert dialog.status.text().startswith("正在编辑选中术语")
    dialog.clear_term()
    assert dialog.term_add_button.text() == "新增术语"
    assert dialog.term_source.isEnabled() and dialog.term_source_lang.isEnabled()
    assert dialog.term_disable_button.isHidden() and dialog.term_clear_button.isHidden()

    # An inactive tab's data load must not overwrite the current tab's status.
    dialog.render_terms([{"id": "t1", "source": "声笺", "target": "Shengjian", "source_lang": "zh",
                          "target_lang": "en", "note": "产品名", "priority": 30, "enabled": True}])
    assert dialog.status.text().startswith("术语库：1 条")
    dialog.render_memory([{"id": "m1", "source": "下载完成", "target": "Download complete",
                           "uses": 2, "session_id": "s1", "provider": "api"}])
    assert dialog.status.text().startswith("术语库：1 条")
    assert dialog.memory_table.rowCount() == 1
    assert dialog.memory_table.item(0, 1).text() == "Download complete"
    assert dialog.memory_table.item(0, 2).text() == "2"
    assert dialog.memory_table.item(0, 3).text() == "识别记录 s1"
    assert dialog.memory_table.item(0, 3).toolTip() == "s1"
    dialog.tabs.setCurrentIndex(1)
    assert dialog.tabs.currentIndex() == 1
    dialog.render_memory([{"id": "m1", "source": "下载完成", "target": "Download complete",
                           "uses": 2, "session_id": "s1", "provider": "api"}])
    assert dialog.status.text().startswith("翻译记忆：1 条")
    dialog.memory_table.setCurrentCell(0, 0)
    assert not dialog.memory_delete_button.isHidden()
    window.close()


def test_translation_knowledge_terms_continue_after_add_and_edit_explicitly(qtbot, tmp_path, monkeypatch):
    window = Window(18900, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = TranslationKnowledgeDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    controller = window.recognition.controller
    terms = [{"id": "t1", "source": "声笺", "target": "Shengjian", "source_lang": "zh",
              "target_lang": "en", "note": "产品名", "priority": 30, "enabled": True}]
    added = []
    updates = []

    def fake_terms(query, target, success, failure, limit=200, offset=0):
        success(terms[offset:offset + limit])

    def fake_add(body, success, failure):
        added.append(dict(body))
        identity = "t1" if len(added) == 1 else "t2"
        success({"id": identity, "source": body["source"], "target": body["target"]})

    def fake_update(identity, body, success, failure):
        updates.append((identity, dict(body)))
        success({"id": identity})

    monkeypatch.setattr(controller, "translation_terms", fake_terms)
    monkeypatch.setattr(controller, "add_translation_term", fake_add)
    monkeypatch.setattr(controller, "update_translation_term", fake_update)
    dialog.render_terms(terms)

    # First add hits the existing term; it updates instead of creating a duplicate,
    # and the form must return to "add the next term" rather than stay in edit mode.
    dialog.term_source.setText("声笺")
    dialog.term_target.setText("Shengjian 2")
    dialog.term_priority.setValue(40)
    dialog.add_term()
    assert dialog.editing_term is None
    assert dialog.term_add_button.text() == "新增术语"
    assert dialog.term_source.text() == ""
    assert "已更新" in dialog.status.text()

    # A genuinely new term also clears the form for the next entry.
    dialog.term_source.setText("下载")
    dialog.term_target.setText("Download")
    dialog.add_term()
    assert dialog.editing_term is None
    assert dialog.term_source.text() == ""
    assert dialog.term_add_button.text() == "新增术语"
    assert "已新增" in dialog.status.text()
    assert len(added) == 2 and updates == []
    assert added[0]["preserve_metadata"] is True and added[1]["preserve_metadata"] is True

    # Editing is explicit: select, click "编辑选中", then save.
    dialog.render_terms(terms)
    dialog.term_table.setCurrentCell(0, 0)
    assert dialog.editing_term is None
    dialog.load_selected_term()
    dialog.term_target.setText("Shengjian Pro")
    dialog.save_term()
    assert updates and updates[0][0] == "t1" and updates[0][1]["target"] == "Shengjian Pro"
    assert dialog.editing_term is None
    assert dialog.term_source.isEnabled() and dialog.term_target_lang.isEnabled()
    window.close()


def test_translation_knowledge_memory_loads_only_when_tab_is_opened(qtbot, tmp_path, monkeypatch):
    window = Window(18901, tmp_path, autostart=False)
    qtbot.addWidget(window)
    dialog = TranslationKnowledgeDialog(window, window.recognition.controller)
    qtbot.addWidget(dialog)
    controller = window.recognition.controller
    term_calls, memory_calls = [], []
    terms = [{"id": "t1", "source": "声笺", "target": "Shengjian", "source_lang": "zh",
              "target_lang": "en", "note": "", "priority": 20, "enabled": True}]
    memories = [{"id": f"m{index}", "source": f"句{index}", "target": f"Sentence {index}",
                 "uses": 1, "session_id": "abcdef0123456789", "provider": "api"}
                for index in range(51)]

    def fake_terms(query, target, success, failure, limit=200, offset=0):
        term_calls.append((limit, offset))
        success(terms[offset:offset + limit])

    def fake_memory(query, target, success, failure, limit=200, offset=0):
        memory_calls.append((limit, offset))
        success(memories[offset:offset + limit])

    monkeypatch.setattr(controller, "translation_terms", fake_terms)
    monkeypatch.setattr(controller, "translation_memory", fake_memory)
    dialog.open_window("abcdef0123456789", "en")
    assert term_calls == [(50, 0)] and memory_calls == []
    assert dialog.term_table.rowCount() == 1
    assert dialog.memory_table.rowCount() == 0

    dialog.tabs.setCurrentIndex(1)
    assert memory_calls == [(50, 0)]
    assert dialog.memory_table.rowCount() == 50
    assert not dialog.memory_more_button.isHidden()
    assert dialog.memory_table.item(0, 3).text() == "识别记录 abcdef01"
    dialog.load_more_memory()
    assert memory_calls[-1] == (50, 50)
    assert dialog.memory_table.rowCount() == 51
    assert dialog.memory_more_button.isHidden()

    dialog.tabs.setCurrentIndex(0)
    assert term_calls == [(50, 0)]
    assert not hasattr(dialog, "memory_timer")
    window.close()


def test_knowledge_button_carries_the_current_recognition_session(qtbot, tmp_path, monkeypatch):
    window = Window(18902, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.ready = True
    controller = window.recognition.controller
    controller.session = {"id": "s1", "state": {"name": "演示", "status": "done",
                                                 "lines": [{"index": 1, "start": 0, "end": 1, "text": "甲"}]}}

    def fake_terms(query, target, success, failure, limit=200, offset=0):
        success([])

    monkeypatch.setattr(controller, "translation_terms", fake_terms)
    # The toolbar button emits clicked(False); it must not become the session id.
    controller.open_translation_knowledge(False)
    dialog = controller.knowledge_dialog
    assert dialog is not None and dialog.session_id == "s1"
    assert dialog.agent_input.placeholderText().startswith("让 Agent")
    controller.open_translation_knowledge(True)
    assert dialog.session_id == "s1"
    window.close()


def test_batch_mode_is_a_workspace_not_a_dialog(qtbot, tmp_path):
    window = Window(18899, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.open_creation_home()
    assert "recognition_batch" in window.creation_choices
    window.creation_choices["recognition_batch"].click()
    panel = window.batch_recognition
    assert window.creation_pages.currentIndex() == 4
    assert panel.title.text() == "批量识别"
    assert panel.dedupe.isChecked() and "连续" in panel.dedupe.text()
    assert panel.table.rowCount() == 0
    assert panel.queue.currentIndex() == 1 and not panel.drop_panel.isHidden()
    assert panel.preview.isHidden()
    assert panel.batch_label.isHidden() and panel.copy_id_button.isHidden()
    assert panel.progress.isHidden() and panel.new_button.isHidden()
    assert panel.start_button.isHidden()
    assert panel.batch_id is None and not panel.start_button.isEnabled()

    audio = tmp_path / "第一段.wav"
    audio.write_bytes(b"RIFF")
    video = tmp_path / "第二段.mp4"
    video.write_bytes(b"0000")
    duplicate = tmp_path / "第一段.wav"
    assert panel.add_paths([str(audio), str(video), str(duplicate), str(tmp_path / "说明.txt")]) == 2
    assert panel.table.rowCount() == 2
    assert panel.queue.currentIndex() == 0 and panel.drop_panel.isHidden()
    assert panel.preview.isHidden()
    assert panel.table.item(0, 0).text() == "第一段.wav"
    assert "等待" in panel.table.item(0, 2).text()
    assert panel.start_button.isEnabled()
    panel.remove_selected()
    assert panel.table.rowCount() >= 1
    window.open_creation_home()
    assert window.creation_pages.currentIndex() == 0
    window.close()


def test_batch_row_in_recent_works_opens_the_batch_page(qtbot, tmp_path, monkeypatch):
    window = Window(18900, tmp_path, autostart=False)
    qtbot.addWidget(window)
    requested = []
    monkeypatch.setattr(window.batch_recognition.controller, "batch_state",
                        lambda identity, success, failure: requested.append(identity))
    value = {"id": "recognition_batch:b1", "source_id": "b1", "task_type": "recognition_batch",
             "title": "批量识别 · 6 个文件", "display_title": "批量识别 · 6 个文件",
             "status": "done", "total": 6, "completed": 6}
    window.open_other_recent(value)
    assert window.creation_pages.currentIndex() == 4
    assert window.batch_recognition.batch_id == "b1"
    assert requested == ["b1"]
    window.close()


def test_batch_rows_show_subtitle_files_and_translation_progress(qtbot, tmp_path, monkeypatch):
    window = Window(18900, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.open_batch_recognition()
    panel = window.batch_recognition
    subtitle = tmp_path / "第一段.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    state = {"id": "b1", "name": "批量识别 · 2 个文件", "status": "done", "language": "en",
             "format": "srt", "total": 2, "done": 1, "failed": 1, "cancelled": 0,
             "items": [
                 {"path": str(tmp_path / "第一段.wav"), "name": "第一段", "status": "done",
                  "session_id": "s1", "exported": [str(subtitle)], "export_error": "",
                  "line_count": 1, "translated_lines": 1, "translation_target": "zh"},
                 {"path": str(tmp_path / "第二段.wav"), "name": "第二段", "status": "failed",
                  "error": "解码失败", "exported": [], "export_error": ""}]}
    panel.show_batch(state)
    assert panel.batch_id == "b1" and panel.table.rowCount() == 2
    assert panel.queue.currentIndex() == 0 and not panel.preview.isHidden()
    assert not panel.batch_label.isHidden() and not panel.copy_id_button.isHidden()
    assert not panel.progress.isHidden() and not panel.new_button.isHidden()
    assert panel.table.item(0, 3).text() == "第一段.srt"
    assert panel.table.item(0, 4).text() == "1/1"
    assert "失败" in panel.table.item(1, 2).text()
    assert panel.copy_id_button.isEnabled() and panel.mcp_button.isEnabled()
    assert panel.start_button.isEnabled() is False

    def session(identity, success, failure):
        assert identity == "s1"
        success({"id": "s1", "state": {
            "name": "第一段", "status": "done", "source_path": str(tmp_path / "第一段.wav"),
            "lines": [{"index": 1, "start": 0, "end": 1, "text": "Hello"}],
            "translation": {"target": "zh", "provider": "workbuddy",
                            "lines": [{"index": 1, "start": 0, "end": 1, "text": "Hello",
                                       "translated": "你好"}]}}})
    monkeypatch.setattr(panel.controller, "translation_session", session)
    panel.table.setCurrentCell(0, 0)
    panel.refresh_preview()
    assert "第一段.srt" in panel.preview_path.text()
    assert "Hello" in panel.original_view.toPlainText()
    assert "你好" in panel.translation_view.toPlainText()
    window.close()


def test_batch_page_list_fills_the_column_and_keeps_footers_level(qtbot, tmp_path):
    """列表吃满左栏：短批次不再在列表下方留大片空白，两行操作落在同一底线。"""
    window = Window(18911, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.resize(1145, 884)
    window.show()
    window.open_batch_recognition()
    panel = window.batch_recognition
    left = panel.body.widget(0)

    files = []
    for index in range(4):
        path = tmp_path / f"第{index + 1}集.wav"
        path.write_bytes(b"RIFF")
        files.append(str(path))
    assert panel.add_paths(files) == 4
    qtbot.wait(80)
    assert len(panel.items()) == 4
    assert panel.table.height() >= left.height() * 0.8

    state = {"id": "b2", "name": "批量识别 · 4 个文件", "status": "done", "language": "auto",
             "format": "srt", "total": 4, "done": 3, "failed": 1, "cancelled": 0,
             "items": [{"path": path, "name": Path(path).stem,
                        "status": "failed" if index == 3 else "done",
                        "exported": [], "export_error": "",
                        "session_id": None if index == 3 else f"s{index}"}
                       for index, path in enumerate(files)]}
    panel.show_batch(state)
    qtbot.wait(80)
    assert not panel.retry_button.isHidden()
    left_bottom = panel.retry_button.mapTo(panel, QPoint(0, 0)).y() + panel.retry_button.height()
    right_bottom = (panel.open_file_button.mapTo(panel, QPoint(0, 0)).y()
                    + panel.open_file_button.height())
    assert abs(left_bottom - right_bottom) <= 2
    # 底部只剩外框的 16px 与左栏自己的 12px 内边距，而不是悬在半空
    assert 0 <= panel.height() - 16 - left_bottom <= 24

    panel.new_list()
    qtbot.wait(60)
    assert not panel.drop_panel.isHidden()
    assert panel.queue.height() >= left.height() * 0.8
    window.close()


def test_batch_page_language_combo_works_before_recognition_options_load(qtbot, tmp_path, monkeypatch):
    """识别设置还没回来时，音频语言也要有选项，不能是个点了没反应的空下拉框。"""
    window = Window(18912, tmp_path, autostart=False)
    qtbot.addWidget(window)
    panel = window.batch_recognition
    monkeypatch.setattr(window.recognition.controller, "load_options", lambda success=None, failure=None: None)
    window.open_batch_recognition()
    assert panel.language.count() > 1
    assert panel.language.currentData() == "auto"
    assert panel.language.itemText(0) == "自动判断"

    # 服务端语言表回来后才换表，并选中默认/批次语言
    panel.controller.options = {"languages": [["自动判断", "auto"], ["中文", "zh"], ["英语", "en"]],
                                "selection": {"language": "zh"}}
    panel.sync_options()
    assert panel.language.count() == 3
    assert panel.language.currentData() == "zh"

    # 服务端空表也不能把下拉框清空
    panel.controller.options = {"languages": []}
    panel.sync_options()
    assert panel.language.count() == 3

    # 从最近作品重开同一批次：batch_id 已就位，也要把批次自己的语言摆回下拉框
    state = {"id": "b9", "name": "批量识别 · 2 个文件", "status": "done", "language": "en",
             "format": "srt", "total": 2, "done": 2, "failed": 0, "cancelled": 0, "items": []}
    panel.batch_id = "b9"
    panel.show_batch(state)
    assert panel.language.currentData() == "en"

    # 未连接服务时，后台取设置失败不该往批量页写错
    panel.active = False
    panel.options_failed("连接失败")
    assert "连接失败" not in panel.status.text()
    window.close()


def test_batch_page_fetches_recognition_options_itself(qtbot, tmp_path, monkeypatch):
    """识别页从没打开过时，批量页要自己取一次设置，否则模型状态和语言表都是空的。"""
    window = Window(18913, tmp_path, autostart=False)
    qtbot.addWidget(window)
    controller = window.recognition.controller
    controller.options = {}
    calls = []
    monkeypatch.setattr(controller, "load_options",
                        lambda success=None, failure=None: calls.append(success))
    window.open_batch_recognition()
    assert len(calls) == 1
    controller.options = {"languages": [["自动判断", "auto"]], "selection": {"language": "auto"}}
    window.open_batch_recognition()
    assert len(calls) == 1
    window.close()
