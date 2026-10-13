"""Compact article/dialogue creation entry, shared by navigation and new works."""
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from ...ui import button, label


def creation_home(window):
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(40, 42, 40, 30)
    layout.setSpacing(14)
    title = label("语音创作", "brand")
    layout.addWidget(title)
    layout.addWidget(label("选择创作方式，开始新的声音作品。", "muted"))
    layout.addSpacing(12)
    options = QHBoxLayout()
    options.setSpacing(16)
    window.creation_choices = {}
    for mode, name, symbol, description in (
            ("article", "文章", "pen", "长文、旁白和字幕\n沿用文本编辑与参数设置"),
            ("dialogue", "对话", "voices", "多角色对白\n输入 @ 选择音色，每段独立编辑")):
        control = button(name + "\n" + description, lambda checked=False, m=mode: window.choose_creation_mode(m), symbol)
        control.setObjectName("creationChoice")
        control.setIconSize(QSize(25, 25))
        control.setMinimumHeight(128)
        control.setMaximumWidth(460)
        control.setStyleSheet("QPushButton { background: white; border: 1px solid #e5e5e5; border-radius: 8px;"
                              " text-align: left; padding: 18px; font-size: 14px; }"
                              "QPushButton:hover { background: #f8f8f7; border-color: #c8c8c6; }")
        options.addWidget(control, 1)
        window.creation_choices[mode] = control
    options.addStretch()
    layout.addLayout(options)
    window.resume_creation = button("继续当前作品", window.resume_creation_editor, "chevron", "ghost")
    layout.addWidget(window.resume_creation, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addStretch()
    return page
