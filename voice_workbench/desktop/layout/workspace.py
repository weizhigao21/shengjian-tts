"""UiLayout responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QStackedWidget,
    QWidget,
)
from ... import __version__
from ...ui import application_icon, STYLE, ElidedLabel, RecentTasks, button, label
from .generation import GenerationLayout
from .player import PlayerLayout
from .workflow import WorkflowLayout
from ..components.inference_status import InferenceStatus


class UiLayout(GenerationLayout, PlayerLayout, WorkflowLayout):
    def _build(self):
        self.setStyleSheet(STYLE)
        self.setMinimumSize(1080, 720)
        self.resize(1380, 900)
        shell = QWidget()
        shell.setObjectName("shell")
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(12, 16, 12, 10)
        nav.setSpacing(8)
        brand = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(application_icon().pixmap(QSize(28, 28)))
        brand.addWidget(mark)
        brand.addWidget(label("声笺", "brand"))
        brand.addStretch()
        nav.addLayout(brand)
        nav.addSpacing(12)
        self.new_document_button = button("新建配音", self.new_document, "plus")
        nav.addWidget(self.new_document_button)
        nav.addSpacing(12)
        self.nav_buttons = []
        for index, (text, symbol) in enumerate((("语音创作", "pen"), ("声音库", "voices"), ("工作流联动", "link"), ("设置", "settings"))):
            control = button(text, lambda _, i=index: self.navigate(i), symbol, "nav")
            control.setCheckable(True)
            self.nav_buttons.append(control)
            nav.addWidget(control)
        nav.addSpacing(24)
        nav.addWidget(label("最近作品", "muted"))
        self.jobs_table = RecentTasks()
        self.jobs_table.setObjectName("recentJobs")
        self.configure_table(self.jobs_table)
        self.jobs_table.horizontalHeader().hide()
        self.jobs_table.setColumnHidden(1, True)
        self.jobs_table.setColumnHidden(2, True)
        self.jobs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.jobs_table.itemSelectionChanged.connect(self.select_job)
        self.jobs_table.delete_requested.connect(self.delete_job)
        self.jobs_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.jobs_table.customContextMenuRequested.connect(self.job_context_menu)
        nav.addWidget(self.jobs_table, 1)
        self.jobs_empty = label("新建作品后，生成记录保存在作品里", "muted")
        self.jobs_empty.setWordWrap(True)
        nav.addWidget(self.jobs_empty)
        footer = QWidget()
        footer.setObjectName("sidebarFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        self.version_label = label(f"v{__version__}", "muted")
        self.version_label.setObjectName("appVersion")
        self.version_label.setToolTip("声笺 · 本地配音工作台")
        footer_layout.addWidget(self.version_label)
        self.banner = ElidedLabel("连接本地服务…")
        self.banner.setObjectName("banner")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.banner.setToolTip(self.url)
        footer_layout.addWidget(self.banner, 1)
        nav.addWidget(footer)
        outer.addWidget(sidebar)

        sheet = QWidget()
        sheet.setObjectName("sheet")
        main = QVBoxLayout(sheet)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self.workspace_header = QWidget()
        header = QHBoxLayout(self.workspace_header)
        header.setContentsMargins(18, 8, 16, 6)
        self.page_title = label("工作台  /  语音创作", "muted")
        header.addWidget(self.page_title)
        header.addStretch()
        self.inference_status = InferenceStatus()
        header.addWidget(self.inference_status, 1)
        main.addWidget(self.workspace_header)
        self.tabs = QStackedWidget()
        # Connect after all pages exist.
        main.addWidget(self.tabs, 1)
        self._generation_tab()
        self._voices_tab()
        self._service_tab()
        from ..settings import SettingsPanel
        self.settings = SettingsPanel(self)
        self.tabs.addWidget(self.settings)
        self.tabs.currentChanged.connect(self.sync_navigation)
        main.addWidget(self._transport())
        outer.addWidget(sheet, 1)
        self.setCentralWidget(shell)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().hide()
        self.toast = QFrame(self)
        self.toast.setObjectName("toast")
        self.toast_layout = QHBoxLayout(self.toast)
        self.toast_layout.setContentsMargins(12, 7, 12, 7)
        self.toast_text = label("", "hint")
        self.toast_text.setWordWrap(True)
        self.toast_layout.addWidget(self.toast_text)
        self.toast.hide()
        self.statusBar().messageChanged.connect(self.show_toast)
        self.sync_navigation(0)
        self.text_edit.setFocus()

    @staticmethod
    def configure_table(table):
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _voices_tab(self):
        from ...library import LibraryPanel
        self.library = LibraryPanel(self)
        self.tabs.addWidget(self.library)
