"""Role names are display metadata; audio ownership stays with the stable row ID."""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QToolTip

from voice_workbench.desktop.components.dialogue import DialogueEditor
from voice_workbench.services.dialogue import dialogue_id, entry_revision
from test_dialogue_scope import environment
from test_library_and_deletion import wait_job


def test_rename_updates_display_and_survives_reopen_without_invalidating_audio(environment):
    window, client, runner, first, second = environment
    window.generate()
    wait_job(client, window.selected_job)
    window.poll()
    window.dialogue_editor.select(first.identity)
    identity = first.identity
    old_revision = entry_revision(first.entry(), window.voices, window.generation_voice())
    original = client.get('/jobs/'+window.selected_job).json()['segments'][0]
    first.name_edit.setText('  姐姐（主角）  ')
    first.finish_name()
    assert first.name_edit.text() == '姐姐（主角）' and first.role_name == '姐姐（主角）'
    assert first.identity == identity and second.role_name == ''
    assert entry_revision(first.entry(), window.voices, window.generation_voice()) == old_revision
    assert first.status.text() == '已生成'
    assert window.clip_list_title.text() == '姐姐（主角） · 生成记录'
    assert window.segments_table.item(0, 2).text() == '姐姐（主角） · 御姐'
    assert identity in window.segments_table.item(0, 2).toolTip()
    assert '姐姐（主角）' in window.clip_title.text() and identity in window.clip_title.toolTip()
    window.save_document()
    job = client.get('/jobs/'+window.selected_job).json()
    assert job['editor_state']['dialogue_entries'][0]['role_name'] == '姐姐（主角）'
    assert {k:v for k,v in job['segments'][0].items() if k != 'source_name'} == original
    window.open_job_data(job)
    first = window.dialogue_editor.rows[0]
    assert first.identity == identity and first.role_name == '姐姐（主角）'
    assert '已生成' in first.status.text() and first.play_button.isEnabled()
    assert runner.speeds == [.8, 1.2]
    records = client.get('/generated-audio').json()
    record = next(r for r in records if r['source_id'] == identity)
    assert record['source_name'] == '姐姐（主角）' and record['audio_label'] == '姐姐（主角） · 御姐'
    assert record['source_label'] == dialogue_id(identity)
    settings = window.settings
    settings.audio_data = records
    for query in ('姐姐（主角）', identity, dialogue_id(identity)):
        settings.audio_search.setText(query)
        settings.render_audio()
        assert [r['source_id'] for r in settings.visible_audio] == [identity]
    first.name_edit.clear()
    assert first.name_edit.placeholderText() == dialogue_id(identity)
    assert window.clip_list_title.text() == dialogue_id(identity) + ' · 生成记录'
    window.save_document()
    record = next(r for r in client.get('/generated-audio').json() if r['source_id'] == identity)
    assert 'source_name' not in record and record['audio_label'].startswith(dialogue_id(identity))


def test_duplicate_role_names_do_not_merge_histories_or_generation_scopes(environment):
    window, client, runner, first, second = environment
    first.name_edit.setText('旁白')
    second.name_edit.setText('旁白')
    window.generate()
    wait_job(client, window.selected_job)
    window.poll()
    for row in (first, second):
        window.dialogue_editor.select(row.identity)
        assert [s['source_id'] for s in window.visible_segments] == [row.identity]
    first.generate_button.click()
    job = wait_job(client, window.selected_job)
    window.poll()
    assert len(job['segments']) == 3 and job['segments'][-1]['source_id'] == first.identity
    assert runner.speeds == [.8, 1.2, .8]
    assert len({s['source_id'] for s in job['segments']}) == 2


def test_generation_actions_are_icons_with_correct_hints(environment):
    window, client, runner, first, second = environment
    assert first.generate_button.text() == '' and first.generate_button.width() == 32
    assert first.generate_button.toolTip() == first.generate_button.accessibleName() == '生成这条对白'
    first.generate_button.click()
    wait_job(client, window.selected_job)
    window.poll()
    assert first.generate_button.text() == '' and not first.generate_button.icon().isNull()
    assert first.generate_button.toolTip() == first.generate_button.accessibleName() == '重新生成这条对白'
    assert second.generate_button.toolTip() == '生成这条对白'


def test_hover_details_wait_and_show_full_name_id_and_escape_user_text(qtbot, monkeypatch):
    editor = DialogueEditor()
    qtbot.addWidget(editor)
    editor.resize(720, 320)
    editor.load([{'id':'a'*32,'role_name':'姐姐（主角） <人物>','text':'一段对白'}])
    editor.show()
    row = editor.rows[0]
    shown = []
    monkeypatch.setattr(QToolTip, 'showText', lambda *args: shown.append(args[1]))
    qtbot.mouseMove(editor.add_button)
    qtbot.mouseMove(row.name_edit)
    qtbot.waitUntil(row.detail_tip.timer.isActive)
    qtbot.wait(600)
    assert shown == []
    qtbot.waitUntil(lambda: bool(shown), timeout=1800)
    assert '姐姐（主角） &lt;人物&gt;' in shown[-1] and 'a'*32 in shown[-1]
    assert '生成记录：0 条' in shown[-1] and '未选择' in shown[-1]
    row.update_status([{'source_id':row.identity,'status':'failed',
        'source_revision':entry_revision(row.entry(), []),'error':{'message':'生成失败原因示例'}}], None, True)
    assert '生成失败原因示例' in row.detail_text()
    shown.clear()
    qtbot.mouseMove(row.text.viewport())
    qtbot.waitUntil(row.detail_tip.timer.isActive)
    qtbot.mouseMove(row.generate_button)
    assert not row.detail_tip.timer.isActive()
    qtbot.wait(1300)
    assert shown == []


@pytest.mark.parametrize('name', ['旁白'*51, 123])
def test_invalid_role_names_are_rejected_before_document_save(environment, name):
    window, client, runner, first, second = environment
    original = client.get('/jobs/'+window.selected_job).json()
    body = window.document_payload()
    body['editor_state']['dialogue_entries'][0]['role_name'] = name
    response = client.patch('/jobs/'+window.selected_job+'/document', json=body)
    assert response.status_code == 422
    assert client.get('/jobs/'+window.selected_job).json() == original
