"""Small view construction helpers."""
from PyQt6.QtWidgets import QFrame, QVBoxLayout
from ...ui import label


def card(title=None, subtitle=None):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(14)
    if title:
        layout.addWidget(label(title, "section"))
    if subtitle:
        text = label(subtitle, "hint")
        text.setWordWrap(True)
        layout.addWidget(text)
    return frame, layout
