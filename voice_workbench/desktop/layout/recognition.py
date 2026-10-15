"""Speech recognition page: drop audio in, read the transcript, play one line at a time."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                            QListWidget, QScrollArea, QVBoxLayout, QWidget)

from ...ui import button, label
from ..controllers.recognition import RecognitionController

DROP_STYLE = ("QWidget#dropZone { background: #fbfbfa; border: 1px dashed #cfcfcc; border-radius: 10px; }"
              "QWidget#dropZone[dragging=\"true\"] { background: #fff6ef; border: 1px dashed #e0651c; }")
LINE_STYLE = ("QListWidget#recognitionLines { background: white; border: 1px solid #e5e5e5; border-radius: 8px;"
              " padding: 4px; }"
              "QListWidget#recognitionLines::item { padding: 7px 8px; border-radius: 6px; }"
              "QListWidget#recognitionLines::item:hover { background: #f4f4f2; }"
              "QListWidget#recognitionLines::item:selected { background: #fdece0; color: #18181a; }")


class RecognitionPanel(QWidget):
    """Owns the widget tree; every decision about audio and playback lives in the controller."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.controller = RecognitionController(owner, self)
        self.setAcceptDrops(True)
        main = QHBoxLayout(self)
        main.setContentsMargins(18, 12, 18, 12)
        main.setSpacing(20)

        center = QWidget()
        center.setObjectName("recognitionContent")
        content = QVBoxLayout(center)
        content.setContentsMargins(0, 0, 8, 0)
        content.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(button("返回", owner.open_creation_home, "back", "ghost"))
        self.title = QLineEdit("未命名音频")
        self.title.setObjectName("documentTitle")
        self.title.setMaxLength(200)
        self.title.textChanged.connect(lambda _: self.controller.name_timer.start(900))
        header.addWidget(self.title, 1)
        self.import_button = button("导入音频", self.controller.import_audio, "import")
        self.recognize_button = button("重新识别", self.controller.recognize, "retry", "ghost")
        header.addWidget(self.import_button)
        header.addWidget(self.recognize_button)
        content.addLayout(header)

        recent = QHBoxLayout()
        recent.addWidget(label("识别记录", "muted"))
        self.history = QComboBox()
        self.history.setMinimumContentsLength(10)
        self.history.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.history.currentIndexChanged.connect(self.controller.open_selected)
        recent.addWidget(self.history, 1)
        self.remove_button = button("移出记录", self.controller.remove, "trash", "ghost")
        recent.addWidget(self.remove_button)
        content.addLayout(recent)

        self.info = label("把音频拖到下方区域，或点击「导入音频」。", "hint")
        self.info.setWordWrap(True)
        content.addWidget(self.info)

        self.drop_zone = QWidget()
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setStyleSheet(DROP_STYLE)
        self.drop_zone.setMinimumHeight(200)
        drop = QVBoxLayout(self.drop_zone)
        drop.setSpacing(6)
        drop.addStretch()
        self.drop_title = label("把音频拖到这里", "section")
        self.drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop.addWidget(self.drop_title)
        self.drop_hint = label("WAV / FLAC / MP3 / M4A / OGG 等；导入后自动识别文字并按标点换行。", "hint")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setWordWrap(True)
        drop.addWidget(self.drop_hint)
        drop.addStretch()
        content.addWidget(self.drop_zone, 1)

        self.lines = QListWidget()
        self.lines.setObjectName("recognitionLines")
        self.lines.setStyleSheet(LINE_STYLE)
        self.lines.setWordWrap(True)
        self.lines.setUniformItemSizes(False)
        self.lines.setAlternatingRowColors(False)
        self.lines.itemClicked.connect(lambda item: self.controller.play_line(self.lines.row(item)))
        self.lines.hide()
        content.addWidget(self.lines, 1)

        transport = QHBoxLayout()
        self.toggle = button("播放第一行", self.controller.toggle, "play")
        self.cancel_button = button("取消识别", self.controller.cancel, "close", "ghost")
        self.copy_button = button("复制文字", self.controller.copy_text, "copy", "ghost")
        self.export_button = button("导出字幕", self.controller.export_subtitles, "export", "ghost")
        for control in (self.toggle, self.cancel_button, self.copy_button, self.export_button):
            transport.addWidget(control)
        self.continuous = QCheckBox("播完自动下一行")
        self.continuous.setToolTip("连续播放时不打断；取消后每行播放完自动停止。")
        self.continuous.toggled.connect(self.set_continuous)
        transport.addWidget(self.continuous)
        transport.addStretch()
        self.clock = label("00:00.00", "muted")
        transport.addWidget(self.clock)
        content.addLayout(transport)

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_scroll.setWidget(center)
        main.addWidget(content_scroll, 1)

        scroll = QScrollArea()
        scroll.setFixedWidth(300)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector = QWidget()
        inspector.setObjectName("inspectorBody")
        side = QVBoxLayout(inspector)
        side.setContentsMargins(12, 4, 12, 12)
        side.setSpacing(12)
        side.addWidget(label("识别设置", "section"))
        form = QFormLayout()
        self.asr = QComboBox()
        self.asr.setMinimumContentsLength(10)
        self.asr.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.asr.currentIndexChanged.connect(self.controller.apply_selection)
        self.aligner = QComboBox()
        self.aligner.setMinimumContentsLength(10)
        self.aligner.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.aligner.currentIndexChanged.connect(self.controller.apply_selection)
        self.language = QComboBox()
        self.language.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.language.currentIndexChanged.connect(self.controller.apply_selection)
        form.addRow("识别模型", self.asr)
        form.addRow("时间对齐", self.aligner)
        form.addRow("音频语言", self.language)
        side.addLayout(form)
        self.setup_button = button("下载模型 / 管理引擎", lambda: owner.set_page(3), "import", "ghost")
        side.addWidget(self.setup_button)
        side.addSpacing(10)
        side.addWidget(label("怎么用", "section"))
        guide = label("拖入音频后自动识别，按标点和停顿自动换行。"
                      "点击任意一行播放它对应的音频片段，再点一次停止。", "hint")
        guide.setWordWrap(True)
        side.addWidget(guide)
        note = label("识别用引擎自带的 Qwen3-ASR；每行的时间来自 Qwen3 强制对齐器，"
                     "精确到字。未安装对齐器时，每行时间按标点估算，只能大致定位。", "hint")
        note.setWordWrap(True)
        side.addWidget(note)
        side.addStretch()
        scroll.setWidget(inspector)
        main.addWidget(scroll)

        self.status = label("导入后会自动识别。", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(34)
        content.addWidget(self.status)
        self.controller.connect()

    def set_continuous(self, enabled):
        self.controller.continuous = bool(enabled)

    # ---------------------------------------------------------------------- drops

    def dragEnterEvent(self, event):
        if self.controller.accepts(event.mimeData()):
            event.acceptProposedAction()
            self.highlight(True)

    def dragLeaveEvent(self, event):
        self.highlight(False)

    def dropEvent(self, event):
        self.highlight(False)
        path = self.controller.dropped_path(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.controller.import_audio(path)

    def highlight(self, active):
        self.drop_zone.setProperty("dragging", "true" if active else "false")
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)
