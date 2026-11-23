"""Compact article/dialogue creation entry, shared by navigation and new works."""
from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QGridLayout, QVBoxLayout, QWidget

from ...ui import FAINT, INK, LINE, MUTED, ORANGE, ORANGE_TINT, PAPER, icon, label

CHOICES_PER_ROW = 3
CARD_HEIGHT = 148


class CreationCard(QAbstractButton):
    """A paper card with its own icon tile, title and two-line description."""

    def __init__(self, name, description, symbol, parent=None):
        super().__init__(parent)
        self.name, self.description, self.symbol = name, description, symbol
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(CARD_HEIGHT)
        self.setAccessibleName(name)
        self.setToolTip(description.replace("\n", " "))

    def text(self):
        return self.name + "\n" + self.description

    def setText(self, text):
        # The card owns its words; Qt only needs a stable text() for accessibility.
        pass

    def sizeHint(self):
        return QSize(300, CARD_HEIGHT)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        if not enabled:
            painter.setOpacity(0.5)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        hover = enabled and (self.underMouse() or self.hasFocus())
        painter.setBrush(QColor("#fffdfa" if hover else "#ffffff"))
        painter.setPen(QPen(QColor("#e7b28d" if hover else LINE), 1))
        painter.drawRoundedRect(rect, 12, 12)
        tile = QRectF(20, 20, 40, 40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ORANGE_TINT if hover else PAPER))
        painter.drawRoundedRect(tile, 10, 10)
        icon(self.symbol, ORANGE if hover else INK, 21).paint(
            painter, int(tile.x() + 9), int(tile.y() + 9), 22, 22)
        title_font = QFont(self.font())
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(INK))
        painter.drawText(QRectF(20, 70, self.width() - 44, 22),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.name)
        body_font = QFont(self.font())
        body_font.setPixelSize(12)
        body_font.setWeight(QFont.Weight.Normal)
        painter.setFont(body_font)
        painter.setPen(QColor(MUTED))
        metrics = QFontMetrics(body_font)
        for index, line in enumerate(self.description.split("\n")[:2]):
            width = max(0, self.width() - 48)
            painter.drawText(QRectF(20, 96 + index * 19, width, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(line, Qt.TextElideMode.ElideRight, width))
        chevron = icon("chevron", ORANGE if hover else FAINT, 16)
        chevron.paint(painter, self.width() - 34, self.height() - 30, 16, 16)
        painter.end()


def creation_home(window):
    page = QWidget()
    page.setObjectName("creationHome")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(40, 36, 40, 28)
    layout.setSpacing(0)
    layout.addWidget(label("开始创作", "title"))
    layout.addSpacing(6)
    layout.addWidget(label("选择一种创作方式，声音和作品都留在本机。", "muted"))
    layout.addSpacing(24)
    # Three per row: five cards squeezed into one line cut every description to
    # an ellipsis. The sixth card is the way back into the work already open.
    options = QGridLayout()
    options.setHorizontalSpacing(16)
    options.setVerticalSpacing(16)
    window.creation_choices = {}
    entries = (
        ("article", "文章", "article", "长文、旁白和字幕\n沿用文本编辑与参数设置"),
        ("dialogue", "对话", "dialogue", "多角色对白\n输入 @ 选择音色，每段独立编辑"),
        ("audio", "音频编辑", "audio", "裁剪、拼接与音量调整\nVevo2 换声和台词修改"),
        ("recognition", "语音识别", "recognize", "拖入音频自动识别文字\n点击每行播放对应片段"),
        ("float", "边剪边配", "float", "悬浮小窗常驻\n打字即生成，拖进时间线"),
    )
    for position, (mode, name, symbol, description) in enumerate(entries):
        control = CreationCard(name, description, symbol)
        control.clicked.connect(lambda checked=False, m=mode: window.start_creation(m))
        options.addWidget(control, position // CHOICES_PER_ROW, position % CHOICES_PER_ROW)
        window.creation_choices[mode] = control
    window.resume_creation = CreationCard("继续当前作品", "回到刚才的编辑内容\n保留未保存的修改", "back")
    window.resume_creation.clicked.connect(window.resume_creation_editor)
    options.addWidget(window.resume_creation, len(entries) // CHOICES_PER_ROW,
                      len(entries) % CHOICES_PER_ROW)
    for column in range(CHOICES_PER_ROW):
        options.setColumnStretch(column, 1)
        options.setColumnMinimumWidth(column, 0)
    grid_host = QWidget()
    grid_host.setMaximumWidth(1080)
    grid_host.setLayout(options)
    layout.addWidget(grid_host)
    layout.addSpacing(20)
    layout.addWidget(label("生成、编辑和导出都在本机完成；作品与音频保存在数据目录。", "hint"))
    layout.addStretch()
    return page
