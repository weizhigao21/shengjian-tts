from copy import deepcopy

from PyQt6.QtCore import Qt
from PyQt6.QtMultimedia import QMediaPlayer

from voice_workbench.generation_controls import SeedControl
from voice_workbench.audio_cpp import build_native_request
from voice_workbench.model_controls import ModelControls
from voice_workbench.ui import sorted_audio
from test_generation_experience import player_window, segment
from test_generation_options import vox_voice
from test_service_startup import window_without_service


def test_task_sort_preserves_selection_source_and_new_records_use_the_selected_order(tmp_path, qtbot, monkeypatch):
    window, job, played = player_window(tmp_path, qtbot, monkeypatch)
    job['segments'] = [dict(segment(i, i, audio='old.wav'), created=100 + i) for i in (1, 2, 3)]
    window.render_job(deepcopy(job))
    assert [s['idx'] for s in window.visible_segments] == [3, 2, 1]
    window.segments_table.selectRow(1)
    source = window.player.source()
    window.player.setPosition(500)
    position = window.player.position()
    window.clip_sort.setCurrentIndex(1)
    assert [s['idx'] for s in window.visible_segments] == [1, 2, 3]
    assert window.current_segment()['idx'] == 2
    assert window.player.source() == source and window.player.position() == position
    job['segments'].append(dict(segment(4, 4, audio='old.wav'), created=104))
    window.render_job(deepcopy(job))
    assert [s['idx'] for s in window.visible_segments] == [1, 2, 3, 4]
    assert window.current_segment()['idx'] == 2 and played == []
    window.clip_sort.setCurrentIndex(0)
    assert [s['idx'] for s in window.visible_segments] == [4, 3, 2, 1]
    assert window.current_segment()['idx'] == 2
    # The original data remains in its export/subtitle order.
    assert [s['idx'] for s in window.job_data['segments']] == [1, 2, 3, 4]
    window.close()


def test_manual_play_and_clip_selection_do_not_continue_on_end_of_media(tmp_path, qtbot, monkeypatch):
    window, job, played = player_window(tmp_path, qtbot, monkeypatch)
    job['segments'].append(segment(2, 2, audio='old.wav'))
    window.render_job(deepcopy(job))
    window.autoplay_indices = {1, 2}
    window.play_button.click()
    assert played == [1] and window.autoplay_indices is None
    window.playback_finished(QMediaPlayer.MediaStatus.EndOfMedia)
    assert played == [1] and window.current_segment()['idx'] == 1
    window.segments_table.selectRow(0)
    window.choose_clip()
    window.playback_finished(QMediaPlayer.MediaStatus.EndOfMedia)
    assert played == [1, 2] and window.current_segment()['idx'] == 2
    window.close()


def test_unsigned_seed_can_be_entered_and_randomized_without_overflow(qtbot, monkeypatch):
    control = SeedControl({'min': -1, 'max': 4294967295}, -1)
    qtbot.addWidget(control)
    control.show()
    qtbot.mouseClick(control.random, Qt.MouseButton.LeftButton)
    for value in (2147483648, 4294967295, 0):
        control.number.lineEdit().selectAll()
        qtbot.keyClicks(control.number.lineEdit(), str(value))
        qtbot.keyClick(control.number.lineEdit(), Qt.Key.Key_Enter)
        assert control.value() == value and isinstance(control.value(), int)
    monkeypatch.setattr('voice_workbench.generation_controls.secrets.randbelow', lambda bound: bound - 1)
    control.new_seed.click()
    assert control.value() == 4294967295
    control.random.click()
    assert control.value() == -1
    control.random.click()
    assert control.value() == 4294967295


def test_audio_sort_handles_legacy_dates_and_same_batch_without_modifying_input():
    records = [{'idx': 1, 'created': None}, {'idx': 2, 'created': 10}, {'idx': 3, 'created': 10}]
    assert [s['idx'] for s in sorted_audio(records)] == [3, 2, 1]
    assert [s['idx'] for s in sorted_audio(records, 'oldest')] == [1, 2, 3]
    assert [s['idx'] for s in records] == [1, 2, 3]


def test_unsigned_seed_builds_the_whole_advanced_form_and_reaches_native_request(tmp_path, qtbot):
    voice = vox_voice(tmp_path, [
        {'name': 'seed', 'type': 'int', 'default': -1, 'min': -1, 'max': 4294967295},
        {'name': 'guidance_scale', 'type': 'float', 'default': 2, 'min': .5, 'max': 5},
    ])
    controls = ModelControls()
    qtbot.addWidget(controls)
    controls.bind(voice)
    controls.show()
    controls.advanced_toggle.click()
    qtbot.wait(10)
    fields = controls.generation_controls.fields
    assert set(fields) == {'seed', 'guidance_scale'} and all(field.isVisible() for field in fields.values())
    fields['seed'].setValue(4294967295)
    options = controls.overrides()['request_options']
    assert options['seed'] == 4294967295 and isinstance(options['seed'], int)
    request = build_native_request({**voice, **controls.overrides()}, '范围验证')['request']
    assert request['options']['seed'] == 4294967295
    controls.generation_controls.reset()
    qtbot.wait(10)
    assert set(controls.generation_controls.fields) == {'seed', 'guidance_scale'}
    assert controls.generation_controls.fields['seed'].value() == -1


def test_settings_sort_keeps_checked_ids_and_search_uses_the_chosen_order(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    settings = window.settings
    settings.audio_data = [
        {'id': str(i), 'idx': i, 'work_title': '作品', 'generation': 1, 'text': '测试',
         'audio': '', 'model': 'VoxCPM2', 'mode': '声音设计', 'created': 100+i,
         'duration': 1, 'available': False, 'provenance': {'preset_used': False}}
        for i in (1, 2, 3)
    ]
    settings.render_audio()
    assert [s['id'] for s in settings.visible_audio] == ['3', '2', '1']
    settings.audio_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert settings.selected_ids == {'3'}
    settings.audio_sort.setCurrentIndex(1)
    assert [s['id'] for s in settings.visible_audio] == ['1', '2', '3']
    assert settings.selected_ids == {'3'}
    assert settings.audio_table.item(2, 0).checkState() == Qt.CheckState.Checked
    settings.audio_search.setText('不存在')
    assert not settings.visible_audio and settings.selected_ids == {'3'}
    settings.audio_search.clear()
    assert [s['id'] for s in settings.visible_audio] == ['1', '2', '3']
    window.close()


def test_engine_row_shows_installed_version_and_explicit_unknown(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    settings = window.settings
    settings.model_data = {'runtime': 'audiocpp_server.exe', 'engine': {
        'ready': True, 'path': 'audiocpp_server.exe', 'version': 'v0.9.1',
        'options': [{'backend': 'cuda', 'path': 'audiocpp_server.exe', 'installed': True,
                     'installed_version': 'v0.9.1', 'status': 'done'}]}}
    settings.render_engine()
    assert 'audio.cpp v0.9.1' in settings.engine_description.text()
    settings.model_data['engine']['version'] = None
    settings.model_data['engine']['options'][0]['installed_version'] = None
    settings.render_engine()
    assert '版本未知' in settings.engine_description.text()
    assert 'v0.7.4' not in settings.engine_description.text()
    window.close()
