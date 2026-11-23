"""Speech recognition page: drop audio in, read the transcript, play one line at a time."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                            QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ...ui import ORANGE, ElidedLabel, button, icon, label
from ..controllers.recognition import RecognitionController
from ..components.recognition_lines import RecognitionLines

DROP_STYLE = ("QWidget#dropZone { background: #fffdfa; border: 1px dashed #d8d1c6; border-radius: 12px; }"
              "QWidget#dropZone:hover { background: #fffaf5; border-color: #e7b28d; }"
              "QWidget#dropZone[dragging=\"true\"] { background: #fbeee4; border: 1px dashed #d9601f; }")
LINE_STYLE = ("QListWidget#recognitionLines { background: white; border: 1px solid #e5e0d8; border-radius: 10px;"
              " padding: 4px; }"
              "QListWidget#recognitionLines::item { padding: 7px 8px; border-radius: 6px; }"
              "QListWidget#recognitionLines::item:hover { background: #faf7f2; }"
              "QListWidget#recognitionLines::item:selected { background: #fbeee4; color: #1c1b19; }")


class RecognitionPanel(QWidget):
    """Owns the widget tree; every decision about audio and playback lives in the controller."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.controller = RecognitionController(owner, self)
        self.setAcceptDrops(True)
        main = QHBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        center = QWidget()
        center.setObjectName("recognitionContent")
        content = QVBoxLayout(center)
        content.setContentsMargins(24, 16, 24, 14)
        content.setSpacing(0)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(button("返回", owner.open_creation_home, "back", "ghost"))
        self.title = QLineEdit("未命名音频")
        self.title.setObjectName("documentTitle")
        self.title.setMaxLength(200)
        self.title.textChanged.connect(lambda _: self.controller.name_timer.start(900))
        header.addWidget(self.title, 1)
        self.import_button = button("导入音视频", self.controller.import_audio, "import")
        header.addWidget(self.import_button)
        self.recognize_button = button("重新识别", self.controller.recognize, "retry", "ghost")
        header.addWidget(self.recognize_button)
        content.addLayout(header)
        content.addSpacing(14)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.audio_name = ElidedLabel("尚未导入音频")
        self.audio_name.setProperty("role", "muted")
        self.audio_name.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.audio_name.customContextMenuRequested.connect(
            lambda position: self.controller.reference_context_menu(self.audio_name, position, whole=True))
        self.audio_name.setToolTip("右键可将整段音频保存到声音库 → 参考声音。")
        meta.addWidget(self.audio_name, 1)
        meta.addWidget(label("识别记录", "muted"))
        self.history = QComboBox()
        self.history.setMinimumContentsLength(10)
        self.history.setMinimumWidth(180)
        self.history.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.history.setPlaceholderText("暂无识别记录")
        self.history.currentIndexChanged.connect(self.controller.open_selected)
        meta.addWidget(self.history)
        self.remove_button = button("", self.controller.remove, "trash", "icon")
        self.remove_button.setToolTip("把当前记录移出识别历史")
        self.remove_button.setFixedSize(28, 28)
        meta.addWidget(self.remove_button)
        content.addLayout(meta)
        content.addSpacing(8)

        self.info = label("把音频或视频拖到下方区域，或点击「导入音视频」。", "hint")
        self.info.setWordWrap(True)
        content.addWidget(self.info)
        content.addSpacing(10)

        self.drop_zone = QWidget()
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setStyleSheet(DROP_STYLE)
        self.drop_zone.setMinimumHeight(240)
        drop = QVBoxLayout(self.drop_zone)
        drop.setContentsMargins(28, 24, 28, 24)
        drop.setSpacing(8)
        drop.addStretch()
        mark = QLabel()
        mark.setPixmap(icon("recognize", ORANGE, 36).pixmap(36, 36))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop.addWidget(mark)
        drop.addSpacing(4)
        self.drop_title = label("把音频或视频拖到这里", "section")
        self.drop_title.setWordWrap(True)
        self.drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop.addWidget(self.drop_title)
        self.drop_hint = label("WAV / FLAC / MP3 / M4A / OGG 等音频，MP4 / MKV / MOV / AVI / WEBM 等视频自动提取音轨。",
                               "hint")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setWordWrap(True)
        drop.addWidget(self.drop_hint)
        drop.addSpacing(12)
        self.drop_button = button("选择音频或视频", self.controller.import_audio, "import", "primary")
        self.drop_button.setFixedHeight(34)
        drop.addWidget(self.drop_button, alignment=Qt.AlignmentFlag.AlignCenter)
        drop.addStretch()
        content.addWidget(self.drop_zone, 1)

        self.lines = RecognitionLines()
        self.lines.setObjectName("recognitionLines")
        self.lines.setStyleSheet(LINE_STYLE)
        self.lines.setWordWrap(True)
        self.lines.setUniformItemSizes(False)
        self.lines.setAlternatingRowColors(False)
        self.lines.itemClicked.connect(lambda item: self.controller.play_line(self.lines.row(item)))
        self.lines.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lines.customContextMenuRequested.connect(
            lambda position: self.controller.reference_context_menu(self.lines, position))
        self.lines.setToolTip("点击播放；右键保存片段到声音库。可按住 Shift 选择连续多行，参考音频需要 1–60 秒。")
        self.lines.hide()
        content.addWidget(self.lines, 1)

        self.status = label("导入后会自动识别。", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(24)
        content.addSpacing(6)
        content.addWidget(self.status)
        content.addSpacing(8)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self.toggle = button("播放第一行", self.controller.toggle, "play", "primary")
        self.toggle.setFixedHeight(32)
        self.cancel_button = button("取消识别", self.controller.cancel, "close", "ghost")
        self.copy_button = button("复制文字", self.controller.copy_text, "copy", "ghost")
        self.export_button = button("导出字幕", self.controller.export_subtitles, "export", "ghost")
        transport.addWidget(self.toggle)
        transport.addWidget(self.cancel_button)
        transport.addWidget(self.copy_button)
        transport.addWidget(self.export_button)
        transport.addStretch()
        content.addLayout(transport)
        playback = QHBoxLayout()
        playback.setSpacing(8)
        self.clock = label("00:00.00", "mono")
        playback.addWidget(self.clock)
        playback.addStretch()
        self.continuous = QCheckBox("播完自动下一行")
        self.continuous.setToolTip("连续播放时不打断；取消后每行播放完自动停止。")
        self.continuous.toggled.connect(self.set_continuous)
        playback.addWidget(self.continuous)
        content.addLayout(playback)

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_scroll.setWidget(center)
        main.addWidget(content_scroll, 1)

        scroll = QScrollArea()
        scroll.setObjectName("inspector")
        scroll.setFixedWidth(352)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inspector = QWidget()
        inspector.setObjectName("inspectorBody")
        side = QVBoxLayout(inspector)
        side.setContentsMargins(18, 16, 18, 16)
        side.setSpacing(8)
        side.addWidget(label("识别设置", "groupTitle"))
        side.addSpacing(2)
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
        self.asr_download = button("下载", lambda: self.controller.open_model_download("asr"), "import", "ghost")
        self.aligner_download = button("下载", lambda: self.controller.open_model_download("align"), "import", "ghost")
        for title, combo, download in (("识别模型", self.asr, self.asr_download),
                                       ("时间对齐", self.aligner, self.aligner_download)):
            side.addWidget(label(title, "muted"))
            row = QWidget()
            controls = QHBoxLayout(row)
            controls.setContentsMargins(0, 0, 0, 0)
            controls.setSpacing(6)
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(6)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            download.setFixedSize(58, 32)
            controls.addWidget(combo, 1)
            controls.addWidget(download)
            side.addWidget(row)
            side.addSpacing(4)
        side.addWidget(label("音频语言", "muted"))
        side.addWidget(self.language)
        side.addSpacing(8)
        self.setup_button = button("模型 / 引擎管理", lambda: self.controller.open_model_download(), "settings")
        side.addWidget(self.setup_button)
        side.addSpacing(14)
        side.addWidget(label("怎么用", "groupTitle"))
        side.addSpacing(2)
        guide = label("拖入音频或视频后自动识别，按标点和停顿自动换行；视频只读取音轨，原文件不改动。"
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
