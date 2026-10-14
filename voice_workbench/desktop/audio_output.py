"""Keep every preview on the current system audio output, including hot switches."""
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices


class SystemAudioOutput(QAudioOutput):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Keep the monitor alive for as long as its output. Qt emits this signal
        # for both endpoint changes and changes to the system default.
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(self.follow_default_device)
        self.follow_default_device()

    def follow_default_device(self):
        device = self.media_devices.defaultAudioOutput()
        # Unrelated device notifications should not reset an active audio sink.
        if self.device().id() != device.id():
            self.setDevice(device)
