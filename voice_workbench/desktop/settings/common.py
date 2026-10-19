"""Shared settings widgets, table helpers and display labels."""
from datetime import datetime
from PyQt6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QScrollArea, QTableWidget, QFrame, QVBoxLayout,
    QWidget,
)
from ...ui import AudioTable, ElidedLabel, label
from ...model_dates import publication_metadata


DOWNLOAD_STATES = {"available": "可下载", "downloading": "下载中", "verifying": "校验中",
                   "pausing": "正在暂停", "paused": "已暂停", "failed": "下载失败", "done": "已下载",
                   "extracting": "正在安装", "waiting": "已安装，等待当前生成结束后启用"}

ARCHIVE_STATES = {**DOWNLOAD_STATES, "available": "待下载", "downloaded": "下载完成",
                  "pending_verification": "待校验", "done": "已拥有"}


def size_text(size):
    return f"{size / (1024 ** 3):.2f} GB" if size >= 1024 ** 3 else f"{size / (1024 ** 2):.1f} MB"


def publication_text(item):
    metadata = publication_metadata(item)
    if not metadata["published_at"]:
        return "发布时间\n未知", "来源未提供此 GGUF 文件版本的有效提交日期；扫描支持模型可尝试补全。"
    date = datetime.fromisoformat(metadata["published_at"].replace("Z", "+00:00")).astimezone()
    tooltip = f"文件版本发布时间：{date:%Y-%m-%d %H:%M:%S %z}\n来源：当前 GGUF 文件版本的仓库提交日期。"
    if metadata["publication_commit"]:
        tooltip += "\n提交：" + metadata["publication_commit"]
    return f"发布时间\n{date:%Y-%m-%d}", tooltip


def preset_text(item):
    source = item.get("provenance")
    if source is None:
        return "旧记录 · 来源未记录"
    if not source.get("preset_used"):
        return "未使用预设"
    return (source.get("preset_name") or source.get("preset_id") or "预设") + ("（已调整）" if source.get("preset_modified") else "")


class ModelTable(QTableWidget):
    """Show every row and let the enclosing page handle wheel scrolling."""
    def __init__(self, rows, columns, scroll_area):
        super().__init__(rows, columns)
        self.scroll_area = scroll_area
        self.setObjectName("modelTable")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalHeader().setObjectName("modelSectionHeader")
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.horizontalHeader().setFixedHeight(28)
        self.setFixedHeight(28)
        self.horizontalHeader().viewport().installEventFilter(self)

    def wheelEvent(self, event):
        viewport = self.scroll_area.viewport()
        forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                                event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                event.buttons(), event.modifiers(), event.phase(), event.inverted())
        QCoreApplication.sendEvent(viewport, forwarded)
        event.accept()

    def eventFilter(self, watched, event):
        from PyQt6 import sip
        if sip.isdeleted(self):
            return False
        if event.type() == QEvent.Type.Wheel:
            self.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)

    def fit_rows(self):
        self.setFixedHeight((0 if self.horizontalHeader().isHidden() else self.horizontalHeader().height())
                            + self.verticalHeader().length() + 2 * self.frameWidth())
        self.setVisible(self.rowCount() > 0)


class SettingsComponents:
    def table(self, headers, whole_page=False):
        table = (ModelTable(0, len(headers), self.models_scroll) if whole_page
                 else AudioTable(0, len(headers)))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return table

    def settings_group(self, rows):
        group = QFrame()
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for index, row in enumerate(rows):
            if index:
                divider = QFrame()
                divider.setObjectName("settingsDivider")
                divider.setFixedHeight(1)
                layout.addWidget(divider)
            layout.addWidget(row)
        return group

    def settings_row(self, title, hint=None, controls=()):
        row = QWidget()
        row.setObjectName("settingsRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(16)
        words = QVBoxLayout()
        words.setSpacing(4)
        words.addWidget(label(title, "settingLabel"))
        if hint is not None:
            note = hint if isinstance(hint, QWidget) else ElidedLabel(hint)
            note.setProperty("role", "muted")
            words.addWidget(note)
        layout.addLayout(words, 1)
        for control in controls:
            layout.addWidget(control)
        return row

    def scroll_page(self, name):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName(name)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 12, 0)
        layout.setSpacing(20)
        scroll.setWidget(page)
        return scroll, layout

    def cell(self, table, row, col, text, tooltip=None):
        return self.owner.update_table_cell(table, row, col, text, tooltip)

    def table_rows(self, table, ids):
        previous = getattr(table, "record_ids", None)
        if previous != ids:
            table.clearContents()
            table.setRowCount(len(ids))
            table.record_ids = ids

    def action_cell(self, table, row, col, key, create):
        control = table.cellWidget(row, col)
        if control is None or control.property("action_key") != key:
            control = create()
            control.setProperty("action_key", key)
            table.setCellWidget(row, col, control)
            if isinstance(table, ModelTable):
                control.installEventFilter(table)
        return control
