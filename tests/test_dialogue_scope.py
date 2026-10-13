"""Dialogue selection scopes parameters, history, playback and audio management."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.gui import Window
from voice_workbench.naming import display_title
from voice_workbench.services.dialogue import dialogue_id
from voice_workbench.store import Store
from test_inline_generation import Runner
from test_library_and_deletion import wait_job


class RecordingRunner(Runner):
    def __init__(self):
        self.speeds = []
    def generate(self, request, cancelled):
        self.speeds.append(request["voice"]["speed"])
        super().generate(request, cancelled)


@pytest.fixture
def environment(tmp_path, qtbot, monkeypatch):
    config = ConfigStore(tmp_path)
    for identity, name, speed in (("older", "御姐", .8), ("young", "萝莉", 1.2)):
        config.save_voice({"id": identity, "name": name, "backend": "windows_sapi", "python": sys.executable, "speed": speed})
    runner = RecordingRunner()
    with TestClient(create_app(tmp_path, runner=runner)) as client:
        window = Window(8791, tmp_path, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        def immediate(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)
        monkeypatch.setattr(window, "call", immediate)
        window.poll()
        window.show()
        window.new_document()
        window.choose_creation_mode("dialogue")
        first = window.dialogue_editor.rows[0]
        first.set_voice(next(v for v in window.voices if v["id"] == "older"))
        first.text.setPlainText("姐姐你在干什么呀？")
        second = window.dialogue_editor.add_after(first)
        second.set_voice(next(v for v in window.voices if v["id"] == "young"))
        second.text.setPlainText("我在听雪落下的声音。")
        yield window, client, runner, first, second
        window.document_dirty = False
        window.close()


def test_clicked_frame_scopes_history_player_and_keeps_selection_on_poll(environment, qtbot, monkeypatch):
    window, client, runner, first, second = environment
    window.generate()
    job = wait_job(client, window.selected_job)
    window.poll()
    qtbot.waitUntil(lambda: first.status.geometry().right() < first.play_button.geometry().left())
    assert first.play_button.geometry().right() < first.generate_button.geometry().left()
    qtbot.mouseClick(first.text.viewport(), Qt.MouseButton.LeftButton)
    assert first.property("selected") is True and second.property("selected") is False
    assert len(window.visible_segments) == 1 and window.current_segment()["source_id"] == first.identity
    assert dialogue_id(first.identity) in window.clip_title.toolTip()
    assert "御姐" in window.segments_table.item(0, 2).text()
    assert not window.next_button.isEnabled() and not window.previous_button.isEnabled()
    window.generate_dialogue_entry(first.identity)
    job = wait_job(client, window.selected_job)
    window.poll()
    assert len(window.visible_segments) == 2
    assert all(s["source_id"] == first.identity for s in window.visible_segments)
    window.clip_sort.setCurrentIndex(1)
    window.render_job(job)
    assert all(s["source_id"] == first.identity for s in window.visible_segments)
    window.dialogue_editor.select(second.identity)
    assert window.current_segment()["source_id"] == second.identity
    assert len(window.visible_segments) == 1
    assert "萝莉" in window.clip_title.toolTip()
    assert not window.autoplay_indices and not window.pending_autoplay
    blank = window.dialogue_editor.add_after(second)
    assert not window.visible_segments and not window.play_button.isEnabled()
    assert not window.clip_title.isEnabled() and window.player.source().isEmpty()
    window.dialogue_editor.remove_row(blank)
    assert window.dialogue_editor.selected_id == second.identity


def test_inspector_changes_only_selected_frame_and_snapshots_reopen(environment):
    window, client, runner, first, second = environment
    window.dialogue_editor.select(first.identity)
    assert window.model_controls.speed.value() == .8
    window.model_controls.speed.setValue(.9)
    assert first.overrides["speed"] == .9
    window.dialogue_editor.select(second.identity)
    assert window.model_controls.speed.value() == 1.2
    window.model_controls.speed.setValue(1.3)
    assert second.overrides["speed"] == 1.3 and first.overrides["speed"] == .9
    window.dialogue_editor.select(first.identity)
    assert window.model_controls.speed.value() == .9
    window.generate()
    job = wait_job(client, window.selected_job)
    assert runner.speeds == [.9, 1.3]
    assert [s["provenance"]["preset_id"] for s in job["segments"]] == ["older", "young"]
    window.open_job_data(job)
    first, second = window.dialogue_editor.rows
    window.dialogue_editor.select(second.identity)
    assert second.overrides["speed"] == 1.3
    assert window.model_controls.speed.value() == 1.3
    window.update_dialogue_records()
    assert "已生成" in first.status.text() and "已生成" in second.status.text()


def test_audio_categories_and_delete_all_respect_current_list(environment, monkeypatch):
    window, client, runner, first, second = environment
    window.generate()
    wait_job(client, window.selected_job)
    window.choose_creation_mode("article")
    window.text_edit.setPlainText("一段文章")
    window.generate()
    job = wait_job(client, window.selected_job)
    window.poll()
    assert len(window.visible_segments) == 1 and not window.current_segment()["source_id"]
    settings = window.settings
    settings.audio_data = client.get('/generated-audio').json()
    settings.render_audio()
    assert len(settings.visible_audio) == 3
    settings.set_audio_category("dialogue")
    assert len(settings.visible_audio) == 2
    assert {s['source_id'] for s in settings.visible_audio} == {first.identity, second.identity}
    settings.audio_search.setText(dialogue_id(first.identity))
    assert len(settings.visible_audio) == 1
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
    settings.delete_audio(delete_all=True)
    remaining = client.get('/generated-audio').json()
    assert len(remaining) == 2 and {a['content_type'] for a in remaining} == {'article', 'dialogue'}
    assert all(a.get('source_id') != first.identity for a in remaining)


def test_default_voice_keeps_local_parameters_without_reselecting_a_preset(environment):
    window, client, runner, first, second = environment
    other_entry = second.entry()
    window.dialogue_editor.select(first.identity)
    first.use_default()
    assert window.selected_preset() is None
    window.model_controls.speed.setValue(.95)
    assert first.voice_id is None and first.overrides['speed'] == .95
    assert second.entry() == other_entry
    window.generate()
    job = wait_job(client, window.selected_job)
    assert runner.speeds == [.95, 1.2]
    assert job['segments'][0]['provenance']['preset_used'] is False
    window.open_job_data(job)
    first = window.dialogue_editor.rows[0]
    window.dialogue_editor.select(first.identity)
    assert first.voice_id is None and window.selected_preset() is None
    assert window.model_controls.speed.value() == .95


def test_type_views_backfill_old_sources_without_changing_old_fields(tmp_path):
    store = Store(tmp_path)
    target = store.create("文章", {"id": "default"}, "key", [{'text':'文章'}])
    store.append(target, "对话", {"id": "default"}, "key", [{'text':'对白','source_id':'stable-box','source_revision':'a'*64}],
                 editor_state={'mode':'dialogue','dialogue_entries':[{'id':'stable-box','text':'对白'}]})
    store.db.execute('DROP VIEW article_audio_records')
    store.db.execute('DROP VIEW dialogue_audio_records')
    store.db.execute('ALTER TABLE segments DROP COLUMN content_type')
    store.db.commit()
    before = [dict(r) for r in store.db.execute('SELECT * FROM segments ORDER BY idx')]
    store.close()
    migrated = Store(tmp_path)
    assert migrated.db.execute('SELECT count(*) FROM article_audio_records').fetchone()[0] == 1
    assert migrated.db.execute('SELECT source_id FROM dialogue_audio_records').fetchone()[0] == 'stable-box'
    after = [dict(r) for r in migrated.db.execute('SELECT * FROM segments ORDER BY idx')]
    for old, new in zip(before, after):
        assert all(old[k] == new[k] for k in old)
    backups = list((tmp_path/'backups').glob('before-dialogue-scope-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert 'content_type' not in {r[1] for r in db.execute('PRAGMA table_info(segments)')}
    migrated.close()


def test_sidebar_shows_type_name_and_keeps_time_in_tooltip(environment):
    window, client, runner, first, second = environment
    window.generate()
    job = wait_job(client, window.selected_job)
    window.poll()
    row = next(i for i in range(window.jobs_table.rowCount()) if window.jobs_table.item(i,0).data(256)==job['id'])
    cell = window.jobs_table.item(row, 0)
    assert cell.text() == '姐姐你在干'
    assert '创建时间：' in cell.toolTip() and '对话' in cell.toolTip()
    assert display_title({'title':'2026-10-13 15:00:00 · 保留手动日期','auto_named':False}) == '2026-10-13 15:00:00 · 保留手动日期'
