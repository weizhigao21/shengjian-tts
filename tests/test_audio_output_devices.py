"""Output changes must affect all previews without changing transport state."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import wave

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QAudioDevice, QMediaDevices, QMediaPlayer

from voice_workbench.desktop.audio_output import SystemAudioOutput


@pytest.fixture
def endpoints(monkeypatch, qapp):
    devices = QMediaDevices.audioOutputs()
    if len(devices) < 2:
        pytest.skip('This native routing regression requires two available audio endpoints.')
    selected = {'device': devices[0]}
    monkeypatch.setattr(QMediaDevices, 'defaultAudioOutput', staticmethod(lambda: selected['device']))
    return selected, devices[:2]


def test_every_workbench_player_follows_switch_and_switch_back(endpoints, qtbot, tmp_path):
    from voice_workbench.gui import Window
    selected, (speaker, headset) = endpoints
    window = Window(18888, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    controller = window.audio_editor.controller
    outputs = [window.audio_output, window.reference_output, window.settings.output, controller.output]
    players = [window.player, window.reference_player, window.settings.player, controller.player]
    try:
        for index, (output, player) in enumerate(zip(outputs, players)):
            assert isinstance(output, SystemAudioOutput) and player.audioOutput() is output
            output.setVolume(.2 + index * .1)
            output.setMuted(True)
            assert output.device().id() == speaker.id()
        for device in (headset, speaker):
            selected['device'] = device
            for index, (output, player) in enumerate(zip(outputs, players)):
                output.media_devices.audioOutputsChanged.emit()
                assert output.device().id() == device.id()
                assert player.audioOutput() is output and output.isMuted()
                assert output.volume() == pytest.approx(.2 + index * .1)
                assert player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    finally:
        window.close()


def test_unrelated_device_notification_does_not_reset_output(endpoints, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    output = SystemAudioOutput(parent)
    changes = []
    monkeypatch.setattr(output, 'setDevice', changes.append)
    output.media_devices.audioOutputsChanged.emit()
    assert not changes


def test_temporarily_missing_default_uses_null_device_then_recovers(endpoints, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    selected, (_, headset) = endpoints
    parent = QWidget()
    qtbot.addWidget(parent)
    output = SystemAudioOutput(parent)
    changes = []
    original = output.setDevice
    def set_device(device):
        changes.append(device)
        original(device)
    monkeypatch.setattr(output, 'setDevice', set_device)
    selected['device'] = QAudioDevice()
    output.media_devices.audioOutputsChanged.emit()
    assert changes[-1].isNull()
    selected['device'] = headset
    output.media_devices.audioOutputsChanged.emit()
    assert output.device().id() == headset.id()


def test_switch_during_playback_keeps_position_source_and_paused_state(endpoints, qtbot, tmp_path):
    from PyQt6.QtWidgets import QWidget
    selected, (speaker, headset) = endpoints
    parent = QWidget()
    qtbot.addWidget(parent)
    output = SystemAudioOutput(parent)
    output.setMuted(True)
    player = QMediaPlayer(parent)
    player.setAudioOutput(output)
    audio = tmp_path / 'silent-route-check.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\0\0' * 24000 * 10)
    source = QUrl.fromLocalFile(str(audio))
    player.setSource(source)
    try:
        player.play()
        qtbot.waitUntil(lambda: player.position() > 200, timeout=8000)
        position = player.position()
        selected['device'] = headset
        output.media_devices.audioOutputsChanged.emit()
        assert output.device().id() == headset.id()
        assert player.source() == source
        assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        assert player.position() >= position - 100
        qtbot.waitUntil(lambda: player.position() > position + 200, timeout=5000)
        player.pause()
        position = player.position()
        selected['device'] = speaker
        output.media_devices.audioOutputsChanged.emit()
        qtbot.wait(100)
        assert output.device().id() == speaker.id()
        assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        assert player.position() == position
        assert output.isMuted()
    finally:
        player.stop()
        player.setSource(QUrl())
