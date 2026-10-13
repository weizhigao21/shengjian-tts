"""Mouse-drag selection over the existing PCM waveform preview."""
from PyQt6.QtCore import Qt, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen
from ...ui import Waveform


class AudioSelection(Waveform):
    selection_changed = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0
        self.begin, self.end = 0, 1
        self.anchor = None
        self.setMinimumHeight(132)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("拖动选择音频范围；也可以在下面输入精确的起止秒数。")

    def select(self, start, end):
        if self.duration:
            self.begin, self.end = start / self.duration, end / self.duration
        else:
            self.begin, self.end = 0, 1
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration:
            self.anchor = max(0, min(1, event.position().x() / self.width()))
            self.begin = self.end = self.anchor
            self.update()

    def mouseMoveEvent(self, event):
        if self.anchor is not None:
            point = max(0, min(1, event.position().x() / self.width()))
            self.begin, self.end = sorted((self.anchor, point))
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is not None:
            self.mouseMoveEvent(event)
            self.anchor = None
            if self.end - self.begin > 0.00001:
                self.selection_changed.emit(self.begin * self.duration, self.end * self.duration)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.duration:
            painter = QPainter(self)
            left, right = self.begin * self.width(), self.end * self.width()
            painter.fillRect(QRectF(left, 0, right - left, self.height()), QColor(237, 130, 56, 28))
            painter.setPen(QPen(QColor("#db7432"), 1))
            painter.drawLine(int(left), 0, int(left), self.height())
            painter.drawLine(int(right), 0, int(right), self.height())
