"""Reference previews belong to the clicked entry, even when files are shared."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QAudioBufferOutput, QMediaPlayer

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from test_inline_generation import Runner, index_voice, make_audio


@pytest.fixture
def previews(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    data = tmp_path / "data"
    first = index_voice(tmp_path)
    first.update(id="first", name="御姐")
    second = {**first, "id": "second", "name": "另一个御姐"}
    other = tmp_path / "other.wav"
    make_audio(other)
    other.write_bytes(other.read_bytes()[:-2] + b"\x20\x00")
    third = {**first, "id": "third", "name": "另一段声音", "reference_audio": str(other)}
    config = ConfigStore(data)
    for voice in (first, second, third):
        config.save_voice(voice)
    with TestClient(create_app(data, runner=Runner())) as client:
        window = Window(8789, data, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.reference_output.setMuted(True)
        window.audio_output.setMuted(True)
        # Exercise real decoding without depending on the machine's audio device.
        window.reference_player.setAudioOutput(None)
        window.reference_player.setAudioBufferOutput(QAudioBufferOutput(window))
        window.player.setAudioOutput(None)
        errors = []
        monkeypatch.setattr(window, "show_error", errors.append)

        def call(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)

        monkeypatch.setattr(window, "call", call)
        window.poll()
        window.set_page(1)
        yield window, errors
        window.close()


def ticket(window, item_id):
    return next(item for item in window.library.tickets if item.item["id"] == item_id)


def playing(qtbot, window):
    qtbot.waitUntil(lambda: window.reference_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)


def test_shared_file_marks_only_clicked_preset_and_switches_immediately(previews, qtbot):
    window, errors = previews
    first, second = ticket(window, "first"), ticket(window, "second")
    assert first.item["reference_audio"] == second.item["reference_audio"]
    first.cover.click()
    playing(qtbot, window)
    assert first.cover.playing and first.cover.timer.isActive()
    assert not second.cover.playing and not second.cover.timer.isActive()
    assert window.model_controls.reference_play.toolTip() == "试听参考声音"
    qtbot.waitUntil(lambda: window.reference_player.position() > 100)
    second.cover.click()
    playing(qtbot, window)
    assert second.cover.playing and not first.cover.playing
    assert window.reference_player.position() < 100
    assert second.cover.toolTip() == "停止试听"
    second.cover.click()
    assert window.reference_player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert not first.cover.playing and not second.cover.playing
    assert not errors


def test_clicking_different_audio_switches_the_player_source(previews, qtbot):
    window, errors = previews
    first, third = ticket(window, "first"), ticket(window, "third")
    assert first.item["reference_audio"] != third.item["reference_audio"]
    first.cover.click()
    playing(qtbot, window)
    third.cover.click()
    playing(qtbot, window)
    assert third.cover.playing and not first.cover.playing
    assert Path(window.reference_player.source().toLocalFile()) == Path(third.item["reference_audio"]).resolve()
    assert not errors


def test_refresh_and_search_restore_the_active_entry(previews, qtbot):
    window, _ = previews
    ticket(window, "first").cover.click()
    playing(qtbot, window)
    window.voices = [{**voice, "name": "御姐改名"} if voice["id"] == "first" else voice for voice in window.voices]
    window.library.bind()
    assert ticket(window, "first").cover.playing
    assert not ticket(window, "second").cover.playing
    window.library.search.setText("没有这个声音")
    assert not window.library.tickets
    window.library.search.clear()
    assert ticket(window, "first").cover.playing
    assert not ticket(window, "second").cover.playing


def test_reference_and_preset_with_same_id_and_file_have_separate_states(previews, qtbot):
    window, _ = previews
    first = ticket(window, "first")
    window.references = [{"id": "first", "name": "共用参考", "reference_audio": first.item["reference_audio"], "duration": 2}]
    first.cover.click()
    playing(qtbot, window)
    window.library.set_page("references")
    reference = ticket(window, "first")
    assert not reference.cover.playing
    reference.cover.click()
    playing(qtbot, window)
    assert reference.cover.playing
    window.library.set_page("presets")
    assert not ticket(window, "first").cover.playing


@pytest.mark.parametrize("dialog_type", ["preset", "reference", "picker"])
def test_controls_and_dialog_preview_do_not_mark_shared_library_cards(previews, qtbot, dialog_type):
    from voice_workbench.library import PresetDialog, ReferenceDialog, ReferencePicker
    window, errors = previews
    first = ticket(window, "first")
    window.use_library_voice(first.item)
    window.model_controls.reference_play.click()
    playing(qtbot, window)
    assert window.model_controls.reference_play.toolTip() == "停止试听"
    assert not any(item.cover.playing for item in window.library.tickets)
    reference = {"id": "reference", "name": "参考", "reference_audio": first.item["reference_audio"], "duration": 2}
    if dialog_type == "preset":
        dialog = PresetDialog(window, first.item)
    elif dialog_type == "reference":
        dialog = ReferenceDialog(window, reference)
    else:
        window.references = [reference]
        dialog = ReferencePicker(window, window.model_controls)
        dialog.choose(reference)
    window.open_dialog(dialog)
    dialog.preview_button.click()
    playing(qtbot, window)
    assert dialog.preview_button.toolTip() == "停止试听"
    assert window.model_controls.reference_play.toolTip() == "试听参考声音"
    assert not any(item.cover.playing for item in window.library.tickets)
    dialog.reject()
    assert window.reference_player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert not errors


def test_emotion_preview_is_distinct_from_speaker_preview_with_same_file(previews, qtbot):
    window, errors = previews
    first = ticket(window, "first")
    window.use_library_voice(first.item)
    expression = window.model_controls.index_emotion
    expression.select_reference({"name":"演绎", "reference_audio":first.item['reference_audio']})
    expression.set_mode('reference')
    expression.reference_play.click()
    playing(qtbot, window)
    assert window.reference_preview_origin is expression.reference_play
    assert expression.reference_play.toolTip() == '停止试听'
    assert window.model_controls.reference_play.toolTip() == '试听参考声音'
    window.model_controls.reference_play.click()
    playing(qtbot, window)
    assert window.reference_preview_origin is window.model_controls.reference_play
    assert expression.reference_play.toolTip() == '试听参考声音'
    assert not any(item.cover.playing for item in window.library.tickets)
    assert not errors


def test_natural_completion_resets_playback_indicators(previews, qtbot):
    window, errors = previews
    first = ticket(window, "first")
    first.cover.click()
    playing(qtbot, window)
    qtbot.waitUntil(lambda: window.reference_player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia, timeout=5000)
    assert not first.cover.playing and not first.cover.timer.isActive()
    assert first.cover.toolTip() == "试听参考声音"
    assert window.reference_preview_origin is None
    assert not errors


@pytest.mark.parametrize("ending", ["error", "delete", "direct_stop"])
def test_stop_error_and_deletion_clear_the_active_entry(previews, qtbot, ending):
    window, errors = previews
    first = ticket(window, "first")
    first.cover.click()
    playing(qtbot, window)
    if ending == "error":
        window.reference_player.errorOccurred.emit(QMediaPlayer.Error.ResourceError, "测试播放失败")
        assert errors == ["参考音频播放失败：测试播放失败"]
    elif ending == "delete":
        window.delete_library_voice(first.item)
        assert all(item.item["id"] != "first" for item in window.library.tickets)
    else:
        window.reference_player.stop()
    assert window.reference_player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert window.reference_preview_origin is None
    assert not any(item.cover.playing for item in window.library.tickets)


def test_preview_stops_generated_audio_player(previews, qtbot):
    window, _ = previews
    first = ticket(window, "first")
    window.player.setSource(QUrl.fromLocalFile(str(Path(first.item["reference_audio"]).resolve())))
    window.player.play()
    qtbot.waitUntil(lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
    first.cover.click()
    playing(qtbot, window)
    assert window.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState


def test_reference_dialog_waveform_follows_only_its_own_preview(previews, qtbot):
    window, errors = previews
    source = {'reference_audio': ticket(window, 'first').item['reference_audio'], 'name': '待入库声音'}
    first = window.import_reference(source=source)
    second = window.import_reference(source=source)
    idle = first.waveform.grab().toImage()
    first.preview_button.click()
    playing(qtbot, window)
    qtbot.waitUntil(lambda: first.waveform.progress > .1)
    assert first.waveform.grab().toImage() != idle
    assert first.preview_button.text() == '停止试听'
    assert second.waveform.progress == 0
    window.reference_player.pause()
    progress = first.waveform.progress
    qtbot.wait(120)
    assert first.waveform.progress == progress
    second.preview_button.click()
    playing(qtbot, window)
    qtbot.waitUntil(lambda: second.waveform.progress > .1)
    assert first.waveform.progress == 0 and first.preview_button.text() == '试听参考声音'
    second.preview_button.click()
    assert second.waveform.progress == 0 and second.preview_button.text() == '试听参考声音'
    first.reject(); second.reject()
    assert not errors


@pytest.mark.parametrize('ending', ['finished', 'error', 'close'])
def test_reference_dialog_waveform_resets_on_completion_error_and_close(previews, qtbot, ending):
    window, errors = previews
    dialog = window.import_reference(source={'reference_audio': ticket(window, 'first').item['reference_audio']})
    dialog.preview_button.click()
    playing(qtbot, window)
    qtbot.waitUntil(lambda: dialog.waveform.progress > .05)
    if ending == 'finished':
        qtbot.waitUntil(lambda: window.reference_player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia, timeout=5000)
    elif ending == 'error':
        window.reference_player.errorOccurred.emit(QMediaPlayer.Error.ResourceError, '测试播放失败')
    else:
        dialog.reject()
    assert dialog.waveform.progress == 0
    assert dialog.preview_button.text() == '试听参考声音'
    assert window.reference_preview_origin is None
    if ending == 'error':
        assert errors == ['参考音频播放失败：测试播放失败']
    else:
        assert not errors
    if ending != 'close': dialog.reject()
    # Deleted dialogs must no longer receive the shared player's signals.
    qtbot.waitUntil(lambda: not window.dialogs)
    ticket(window, 'first').cover.click()
    playing(qtbot, window)


def test_browsing_reference_audio_stops_own_preview_and_resets_waveform(previews, qtbot, monkeypatch):
    window, errors = previews
    first, third = ticket(window, 'first'), ticket(window, 'third')
    dialog = window.import_reference(source={'reference_audio': first.item['reference_audio']})
    dialog.preview_button.click()
    playing(qtbot, window)
    qtbot.waitUntil(lambda: dialog.waveform.progress > .05)
    monkeypatch.setattr('voice_workbench.library.QFileDialog.getOpenFileName',
                        lambda *args: (third.item['reference_audio'], ''))
    dialog.browse()
    assert window.reference_player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert dialog.waveform.progress == 0 and dialog.preview_button.text() == '试听参考声音'
    assert dialog.waveform.path == Path(third.item['reference_audio'])
    # Changing the dialog's file must not stop a different preview entry.
    first.cover.click()
    playing(qtbot, window)
    dialog.browse()
    assert window.reference_preview_origin == first.preview_origin
    assert window.reference_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    dialog.reject()
    assert not errors
