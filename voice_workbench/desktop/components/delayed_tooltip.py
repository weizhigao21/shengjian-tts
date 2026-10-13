"""A delayed detail tooltip that leaves action buttons' own hints available."""
from html import escape

from PyQt6.QtCore import QObject, QEvent, QTimer
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QToolTip


class DelayedToolTip(QObject):
    def __init__(self, parent, text, delay=1200):
        super().__init__(parent)
        self.text, self.targets, self.actions = text, set(), set()
        self.target, self.position, self.shown = None, None, False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay)
        self.timer.timeout.connect(self.show)

    def watch(self, widget, details=True):
        if details:
            self.targets.add(widget)
        else:
            self.actions.add(widget)
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def stop(self):
        self.timer.stop()
        self.target = None
        if self.shown:
            QToolTip.hideText()
            self.shown = False

    def eventFilter(self, watched, event):
        type_ = event.type()
        if type_ in (QEvent.Type.Enter, QEvent.Type.MouseMove):
            position = QCursor.pos()
            # Mouse moves can propagate from a button to its containing row.
            if watched not in self.targets or self.over_action(position):
                self.stop()
            else:
                if watched != self.target or self.position is None or (position - self.position).manhattanLength() > 4:
                    self.stop()
                    self.target, self.position = watched, position
                    self.timer.start()
        elif type_ in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
            if watched == self.target:
                self.stop()
        elif type_ == QEvent.Type.ToolTip and watched in self.targets:
            return True
        return super().eventFilter(watched, event)

    def show(self):
        target = self.target
        if (target is not None and target.isVisible() and not self.over_action(QCursor.pos())
                and target.rect().contains(target.mapFromGlobal(QCursor.pos()))):
            text = '<qt>' + escape(self.text()).replace('\n', '<br>') + '</qt>'
            QToolTip.showText(QCursor.pos(), text, target, target.rect(), 10000)
            self.shown = True

    def over_action(self, position):
        return any(widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(position)) for widget in self.actions)
