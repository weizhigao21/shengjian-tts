"""Recognition selection without the right mouse button starting playback."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QListWidget


class RecognitionLines(QListWidget):
    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None and not item.isSelected():
                self.clearSelection()
                self.setCurrentItem(item)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)
