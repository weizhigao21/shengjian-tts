"""Translation knowledge base: terms, memory and the bounded agent instruction box."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QSpinBox, QTabWidget, QTableWidget,
                             QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from ...translation.service import LANGUAGES
from ...ui import MUTED, STYLE, button, button_icon, label

SIZE = (840, 700)
LANGUAGE_CHOICES = [("不限", "")] + list(LANGUAGES)
TERM_PAGE = 50
MEMORY_PAGE = 50
SEARCH_DELAY_MS = 350


class TranslationKnowledgeDialog(QDialog):
    def __init__(self, owner, controller):
        super().__init__(owner)
        self.owner, self.controller = owner, controller
        self.session_id = ""
        self.editing_term = None
        self.busy = False
        self.term_loaded = False
        self.memory_loaded = False
        self.term_loading = False
        self.memory_loading = False
        self.term_more = False
        self.memory_more = False
        self.term_request = 0
        self.memory_request = 0
        self.term_action = ""
        self.term_pending = None
        self.memory_pending = None
        self.known_term_ids_before = set()
        self.known_memory_ids_before = set()
        self.term_notice = ""
        self.memory_notice = ""
        self.known_term_ids = set()
        self.known_memory_ids = set()
        self.tab_status = {0: "正在加载术语…", 1: "正在加载翻译记忆…",
                           2: "Agent 指令需要模型支持工具调用。"}
        self.term_search_timer = QTimer(self)
        self.term_search_timer.setSingleShot(True)
        self.term_search_timer.setInterval(SEARCH_DELAY_MS)
        self.term_search_timer.timeout.connect(lambda: self.refresh_terms())
        self.memory_search_timer = QTimer(self)
        self.memory_search_timer.setSingleShot(True)
        self.memory_search_timer.setInterval(SEARCH_DELAY_MS)
        self.memory_search_timer.timeout.connect(lambda: self.refresh_memory())
        self.setWindowTitle("翻译知识库")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(*SIZE)
        self.setStyleSheet(STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 14)
        outer.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(label("翻译知识库", "dialogTitle"))
        header.addStretch()
        outer.addLayout(header)
        outer.addWidget(label("术语和翻译记忆保存在本地；Agent 指令只能通过受控工具修改它们。", "hint"))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._term_tab(), "术语库")
        self.tabs.addTab(self._memory_tab(), "翻译记忆")
        self.tabs.addTab(self._agent_tab(), "Agent 指令")
        self.tabs.currentChanged.connect(self.tab_changed)
        outer.addWidget(self.tabs, 1)

        self.status = QLabel(self.tab_status[self.tabs.currentIndex()])
        self.status.setProperty("role", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(32)
        outer.addWidget(self.status)

    def set_tab_status(self, index, text):
        """Keep each tab's message separate; the shared footer follows the active tab."""
        self.tab_status[index] = str(text or "")
        if self.tabs.currentIndex() == index:
            self.status.setText(self.tab_status[index])

    # ------------------------------------------------------------------ term tab

    def _term_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        search = QHBoxLayout()
        self.term_search = QLineEdit()
        self.term_search.setPlaceholderText("搜索原文、译文或说明…")
        self.term_search.textChanged.connect(lambda: self.term_search_timer.start())
        self.term_search.returnPressed.connect(lambda: self.refresh_terms())
        search.addWidget(self.term_search, 1)
        self.term_filter = QComboBox()
        self.term_filter.setMinimumWidth(120)
        self.controller.fill(self.term_filter, LANGUAGE_CHOICES, preferred="")
        self.term_filter.currentIndexChanged.connect(lambda: self.refresh_terms())
        search.addWidget(self.term_filter)
        layout.addLayout(search)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        self.term_source = QLineEdit()
        self.term_source.setPlaceholderText("原文词条，例如：声笺")
        self.term_source.setToolTip("同一原文 + 同一语言对只会保留一条；再次新增会更新已有术语的译法。")
        self.term_target = QLineEdit()
        self.term_target.setPlaceholderText("固定译法，例如：Shengjian")
        self.term_note = QLineEdit()
        self.term_note.setPlaceholderText("说明（可选）")
        self.term_priority = QSpinBox()
        self.term_priority.setRange(0, 100)
        self.term_priority.setValue(20)
        self.term_priority.setToolTip("优先级越高，命中时越先用于提示词。")
        self.term_source_lang = QComboBox()
        self.term_target_lang = QComboBox()
        self.controller.fill(self.term_source_lang, LANGUAGE_CHOICES, preferred="")
        self.controller.fill(self.term_target_lang, LANGUAGE_CHOICES, preferred="en")
        self.term_source.returnPressed.connect(lambda: self.term_target.setFocus())
        self.term_target.returnPressed.connect(self.submit_term)
        self.term_note.returnPressed.connect(self.submit_term)
        form.addWidget(label("原文", "muted"), 0, 0)
        form.addWidget(label("译文", "muted"), 0, 1)
        form.addWidget(self.term_source, 1, 0)
        form.addWidget(self.term_target, 1, 1)
        form.addWidget(label("说明", "muted"), 2, 0)
        form.addWidget(label("优先级", "muted"), 2, 1)
        form.addWidget(self.term_note, 3, 0)
        form.addWidget(self.term_priority, 3, 1)
        form.addWidget(label("源语言", "muted"), 4, 0)
        form.addWidget(label("目标语言", "muted"), 4, 1)
        form.addWidget(self.term_source_lang, 5, 0)
        form.addWidget(self.term_target_lang, 5, 1)
        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.term_add_button = button("新增术语", self.submit_term, "plus", "primary")
        self.term_add_button.setToolTip("同一原文 + 同一语言对只会保留一条；再次新增会更新已有术语的译法。")
        self.term_edit_button = button("编辑选中", lambda: self.load_selected_term(), "pen", "ghost")
        self.term_edit_button.hide()
        self.term_disable_button = button("停用选中", self.disable_term, "trash", "ghost")
        self.term_disable_button.hide()
        self.term_clear_button = button("取消编辑", lambda: self.clear_term(), "close", "ghost")
        self.term_clear_button.hide()
        self.term_more_button = button("加载更多术语", lambda: self.load_more_terms(), "chevron-down", "ghost")
        self.term_more_button.hide()
        self.term_refresh_button = button("刷新", lambda: self.refresh_terms(), "retry", "ghost")
        actions.addWidget(self.term_add_button)
        actions.addWidget(self.term_edit_button)
        actions.addWidget(self.term_disable_button)
        actions.addWidget(self.term_clear_button)
        actions.addWidget(self.term_more_button)
        actions.addWidget(self.term_refresh_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.term_table = QTableWidget(0, 6)
        self.term_table.setHorizontalHeaderLabels(["原文", "译文", "语言", "说明", "优先级", "状态"])
        self.term_table.verticalHeader().hide()
        self.term_table.verticalHeader().setDefaultSectionSize(32)
        self.term_table.setShowGrid(False)
        self.term_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.term_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.term_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.term_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.term_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.term_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.term_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.term_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.term_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.term_table.itemSelectionChanged.connect(self.term_selected)
        self.term_table.itemDoubleClicked.connect(self.load_selected_term)
        layout.addWidget(self.term_table, 1)
        self.update_term_actions()
        return page

    def refresh_terms(self, reset=True):
        query = self.term_search.text().strip()
        target = self.term_filter.currentData() or ""
        self.term_search_timer.stop()
        self.term_request += 1
        token = self.term_request
        self.term_offset = 0 if reset else self.term_table.rowCount()
        self.term_loading = True
        self.set_tab_status(0, "正在加载术语…" if reset else "正在加载更多术语…")
        self.update_term_actions()

        def done(terms):
            if token != self.term_request:
                return
            self.term_loading = False
            self.render_terms(terms, reset=reset)

        def failed(message):
            if token != self.term_request:
                return
            self.term_loading = False
            self.show_failure(message)

        self.controller.translation_terms(query, target, done, failed,
                                          limit=TERM_PAGE, offset=self.term_offset)

    def render_terms(self, terms, reset=True):
        values = list(terms or [])
        start = 0 if reset else self.term_table.rowCount()
        self.term_table.blockSignals(True)
        if reset:
            self.term_table.setRowCount(0)
        self.term_table.setRowCount(start + len(values))
        for index, term in enumerate(values):
            row = start + index
            source = QTableWidgetItem(term.get("source") or "")
            source.setData(Qt.ItemDataRole.UserRole, term.get("id"))
            target = QTableWidgetItem(term.get("target") or "")
            language = QTableWidgetItem(self.language_pair(term.get("source_lang"), term.get("target_lang")))
            language.setData(Qt.ItemDataRole.UserRole,
                             (term.get("source_lang") or "", term.get("target_lang") or ""))
            note = QTableWidgetItem(term.get("note") or "")
            priority = QTableWidgetItem(str(term.get("priority") or 0))
            status = QTableWidgetItem("启用" if term.get("enabled") else "已停用")
            if not term.get("enabled"):
                status.setForeground(QColor(MUTED))
            for column, item in enumerate((source, target, language, note, priority, status)):
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.term_table.setItem(row, column, item)
        if reset:
            self.term_table.clearSelection()
            self.term_table.setCurrentCell(-1, -1)
        self.term_table.blockSignals(False)
        if reset:
            self.known_term_ids = set()
        self.known_term_ids.update(str(term.get("id")) for term in values if term.get("id"))
        self.term_more = len(values) >= TERM_PAGE
        self.term_loading = False
        self.term_loaded = True
        count = self.term_table.rowCount()
        notice = self.term_notice
        self.term_notice = ""
        if not count:
            status = "术语库为空，可新增第一条术语。"
        elif self.term_more:
            status = f"术语库：已加载 {count} 条，点「加载更多术语」继续。"
        else:
            status = f"术语库：{count} 条。"
        if notice:
            status = f"{status} {notice}"
        self.set_tab_status(0, status)
        self.update_term_actions()

    def term_selected(self):
        # Selection only enables the row actions. Loading the form on every refresh
        # used to leave the save button saying "保存修改" while the user typed the
        # next term, so the next save overwrote the previous one.
        self.update_term_actions()

    def update_term_actions(self):
        selected = self.term_table.currentRow() >= 0 and self.term_table.rowCount() > 0
        editing = bool(self.editing_term)
        self.term_add_button.setText("保存修改" if editing else "新增术语")
        self.term_add_button.setIcon(button_icon("check" if editing else "plus", "#ffffff"))
        self.term_add_button.setEnabled(not self.busy and not self.term_loading)
        self.term_edit_button.setVisible(selected and not editing)
        self.term_edit_button.setEnabled(selected and not self.busy and not self.term_loading)
        self.term_disable_button.setVisible(selected)
        self.term_disable_button.setEnabled(selected and not self.busy and not self.term_loading)
        self.term_clear_button.setVisible(editing)
        self.term_clear_button.setEnabled(not self.busy)
        self.term_more_button.setVisible(self.term_more)
        self.term_more_button.setEnabled(self.term_more and not self.busy and not self.term_loading)
        self.term_refresh_button.setEnabled(not self.busy and not self.term_loading)
        self.term_source.setEnabled(not editing)
        self.term_source_lang.setEnabled(not editing)
        self.term_target_lang.setEnabled(not editing)

    def load_selected_term(self, _item=None):
        row = self.term_table.currentRow()
        if row < 0 or self.busy:
            return
        source = self.term_table.item(row, 0)
        if source is None:
            return
        self.editing_term = source.data(Qt.ItemDataRole.UserRole)
        self.term_source.setText(source.text())
        self.term_target.setText(self.term_table.item(row, 1).text() if self.term_table.item(row, 1) else "")
        self.term_note.setText(self.term_table.item(row, 3).text() if self.term_table.item(row, 3) else "")
        priority = self.term_table.item(row, 4)
        if priority:
            try:
                self.term_priority.setValue(int(priority.text()))
            except ValueError:
                pass
        language = self.term_table.item(row, 2)
        pair = language.data(Qt.ItemDataRole.UserRole) if language else None
        if pair:
            self.set_language(self.term_source_lang, pair[0])
            self.set_language(self.term_target_lang, pair[1])
        self.update_term_actions()
        self.term_target.setFocus()
        self.set_tab_status(0, "正在编辑选中术语；改译文、说明或优先级后点「保存修改」。")

    def set_language(self, combo, value):
        index = combo.findData(value or "")
        if index >= 0:
            combo.setCurrentIndex(index)

    def clear_term(self, message="表单已清空。", focus=True):
        self.editing_term = None
        self.term_source.clear()
        self.term_target.clear()
        self.term_note.clear()
        self.term_priority.setValue(20)
        self.term_table.blockSignals(True)
        self.term_table.clearSelection()
        self.term_table.setCurrentCell(-1, -1)
        self.term_table.blockSignals(False)
        self.update_term_actions()
        if message:
            self.set_tab_status(0, message)
        if focus and self.isVisible():
            self.term_source.setFocus()

    def submit_term(self):
        if self.editing_term:
            self.save_term()
        else:
            self.add_term()

    def term_body(self):
        source = self.term_source.text().strip()
        target = self.term_target.text().strip()
        if not source or not target:
            self.set_tab_status(0, "原文和译文都不能为空。")
            return None
        return {"source": source, "target": target,
                "source_lang": self.term_source_lang.currentData() or "",
                "target_lang": self.term_target_lang.currentData() or "",
                "note": self.term_note.text().strip(), "priority": self.term_priority.value(),
                "preserve_metadata": True}

    def add_term(self):
        body = self.term_body()
        if body is None or self.busy:
            return
        self.busy = True
        self.term_action = "add"
        self.term_pending = dict(body)
        self.known_term_ids_before = set(self.known_term_ids)
        self.update_term_actions()
        self.set_tab_status(0, "正在新增术语…")
        self.controller.add_translation_term(body, self.term_saved, self.show_failure)

    def save_term(self):
        if not self.editing_term or self.busy:
            return
        body = {"target": self.term_target.text().strip(), "note": self.term_note.text().strip(),
                "priority": self.term_priority.value()}
        if not body["target"]:
            self.set_tab_status(0, "译文不能为空。")
            return
        self.busy = True
        self.term_action = "update"
        self.term_pending = None
        self.update_term_actions()
        self.set_tab_status(0, "正在保存术语…")
        self.controller.update_translation_term(self.editing_term, body, self.term_saved, self.show_failure)

    def disable_term(self):
        row = self.term_table.currentRow()
        if row < 0 or self.busy:
            return
        identity = self.term_table.item(row, 0).data(Qt.ItemDataRole.UserRole) if self.term_table.item(row, 0) else None
        if not identity:
            return
        self.busy = True
        self.update_term_actions()
        self.set_tab_status(0, "正在停用术语…")
        self.controller.delete_translation_term(identity, self.term_deleted, self.show_failure)

    def term_saved(self, result):
        self.busy = False
        identity = str((result or {}).get("id") or "")
        action = self.term_action
        if action == "update":
            message = "术语修改已保存。"
        elif identity and identity in self.known_term_ids_before:
            message = "原文和语言对已存在，已更新该条术语的译法，没有新增重复条目。"
        else:
            message = "术语已新增，可继续输入下一条。"
        if action == "add" and self.term_pending and not self.term_fields_match(self.term_pending):
            # The request was in flight while the user typed the next term; keep it.
            self.editing_term = None
            self.term_table.blockSignals(True)
            self.term_table.clearSelection()
            self.term_table.setCurrentCell(-1, -1)
            self.term_table.blockSignals(False)
            self.update_term_actions()
            message += " 新输入的内容已保留。"
        else:
            self.clear_term(message=None, focus=False)
        self.term_notice = message
        self.refresh_terms()
        if self.isVisible() and self.term_source.isEnabled():
            self.term_source.setFocus()

    def term_fields_match(self, body):
        return (self.term_source.text().strip() == body.get("source")
                and self.term_target.text().strip() == body.get("target")
                and self.term_note.text().strip() == body.get("note")
                and self.term_priority.value() == body.get("priority")
                and (self.term_source_lang.currentData() or "") == body.get("source_lang")
                and (self.term_target_lang.currentData() or "") == body.get("target_lang"))

    def term_deleted(self, _result):
        self.busy = False
        self.clear_term(message=None)
        self.term_notice = "术语已停用，不再参与翻译。"
        self.refresh_terms()

    def load_more_terms(self):
        if self.term_loading or not self.term_more:
            return
        self.refresh_terms(reset=False)

    # ---------------------------------------------------------------- memory tab

    def _memory_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        search = QHBoxLayout()
        self.memory_search = QLineEdit()
        self.memory_search.setPlaceholderText("搜索原文或译文…")
        self.memory_search.textChanged.connect(lambda: self.memory_search_timer.start())
        self.memory_search.returnPressed.connect(lambda: self.refresh_memory())
        search.addWidget(self.memory_search, 1)
        self.memory_filter = QComboBox()
        self.memory_filter.setMinimumWidth(120)
        self.controller.fill(self.memory_filter, LANGUAGE_CHOICES, preferred="")
        self.memory_filter.currentIndexChanged.connect(lambda: self.refresh_memory())
        search.addWidget(self.memory_filter)
        layout.addLayout(search)

        form = QHBoxLayout()
        form.setSpacing(8)
        self.memory_source = QLineEdit()
        self.memory_source.setPlaceholderText("原文句子")
        self.memory_target = QLineEdit()
        self.memory_target.setPlaceholderText("已有译文")
        self.memory_source_lang = QComboBox()
        self.memory_target_lang = QComboBox()
        self.controller.fill(self.memory_source_lang, LANGUAGE_CHOICES, preferred="")
        self.controller.fill(self.memory_target_lang, LANGUAGE_CHOICES, preferred="en")
        self.memory_source.returnPressed.connect(lambda: self.memory_target.setFocus())
        self.memory_target.returnPressed.connect(self.add_memory)
        self.memory_add_button = button("新增记忆", self.add_memory, "plus", "primary")
        form.addWidget(self.memory_source, 2)
        form.addWidget(self.memory_target, 2)
        form.addWidget(self.memory_source_lang)
        form.addWidget(self.memory_target_lang)
        form.addWidget(self.memory_add_button)
        layout.addLayout(form)

        self.memory_table = QTableWidget(0, 4)
        self.memory_table.setHorizontalHeaderLabels(["原文", "译文", "使用次数", "来源"])
        self.memory_table.verticalHeader().hide()
        self.memory_table.verticalHeader().setDefaultSectionSize(32)
        self.memory_table.setShowGrid(False)
        self.memory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.memory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.memory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.memory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.memory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.memory_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.memory_table.itemSelectionChanged.connect(self.update_memory_actions)
        layout.addWidget(self.memory_table, 1)
        actions = QHBoxLayout()
        self.memory_delete_button = button("删除选中记忆", self.disable_memory, "trash", "ghost")
        self.memory_delete_button.hide()
        self.memory_more_button = button("加载更多记忆", lambda: self.load_more_memory(), "chevron-down", "ghost")
        self.memory_more_button.hide()
        self.memory_refresh_button = button("刷新", lambda: self.refresh_memory(), "retry", "ghost")
        actions.addWidget(self.memory_delete_button)
        actions.addWidget(self.memory_more_button)
        actions.addWidget(self.memory_refresh_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.update_memory_actions()
        return page

    def refresh_memory(self, reset=True):
        query = self.memory_search.text().strip()
        target = self.memory_filter.currentData() or ""
        self.memory_search_timer.stop()
        self.memory_request += 1
        token = self.memory_request
        self.memory_offset = 0 if reset else self.memory_table.rowCount()
        self.memory_loading = True
        self.set_tab_status(1, "正在加载翻译记忆…" if reset else "正在加载更多翻译记忆…")
        self.update_memory_actions()

        def done(memories):
            if token != self.memory_request:
                return
            self.memory_loading = False
            self.render_memory(memories, reset=reset)

        def failed(message):
            if token != self.memory_request:
                return
            self.memory_loading = False
            self.show_failure(message)

        self.controller.translation_memory(query, target, done, failed,
                                           limit=MEMORY_PAGE, offset=self.memory_offset)

    def tab_changed(self, index):
        self.status.setText(self.tab_status.get(index, ""))
        if index != 0:
            self.term_search_timer.stop()
        if index != 1:
            self.memory_search_timer.stop()
        # Load a tab only when it is first shown. The old dialog fetched all 200
        # memories behind the term tab as well, before the user ever asked for them.
        if index == 0 and not self.term_loaded and not self.term_loading:
            self.refresh_terms()
        elif index == 1 and not self.memory_loaded and not self.memory_loading:
            self.refresh_memory()

    def render_memory(self, memories, reset=True):
        values = list(memories or [])
        start = 0 if reset else self.memory_table.rowCount()
        self.memory_table.blockSignals(True)
        if reset:
            self.memory_table.setRowCount(0)
        self.memory_table.setRowCount(start + len(values))
        for index, memory in enumerate(values):
            row = start + index
            source = QTableWidgetItem(memory.get("source") or "")
            source.setData(Qt.ItemDataRole.UserRole, memory.get("id"))
            target = QTableWidgetItem(memory.get("target") or "")
            uses = QTableWidgetItem(str(memory.get("uses") or 0))
            origin = QTableWidgetItem(self.memory_origin(memory))
            session_id = str(memory.get("session_id") or "")
            if session_id:
                origin.setToolTip(session_id)
            for column, item in enumerate((source, target, uses, origin)):
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.memory_table.setItem(row, column, item)
        if reset:
            self.memory_table.clearSelection()
            self.memory_table.setCurrentCell(-1, -1)
        self.memory_table.blockSignals(False)
        if reset:
            self.known_memory_ids = set()
        self.known_memory_ids.update(str(memory.get("id")) for memory in values if memory.get("id"))
        self.memory_more = len(values) >= MEMORY_PAGE
        self.memory_loading = False
        self.memory_loaded = True
        count = self.memory_table.rowCount()
        notice = self.memory_notice
        self.memory_notice = ""
        if not count:
            status = "翻译记忆为空，翻译完成后会自动写入。"
        elif self.memory_more:
            status = f"翻译记忆：已加载 {count} 条，点「加载更多记忆」继续。"
        else:
            status = f"翻译记忆：{count} 条。"
        if notice:
            status = f"{status} {notice}"
        self.set_tab_status(1, status)
        self.update_memory_actions()

    @staticmethod
    def memory_origin(memory):
        session_id = str(memory.get("session_id") or "")
        if session_id:
            return f"识别记录 {session_id[:8]}"
        return str(memory.get("provider") or "手动")

    def update_memory_actions(self):
        selected = self.memory_table.currentRow() >= 0 and self.memory_table.rowCount() > 0
        self.memory_add_button.setEnabled(not self.busy and not self.memory_loading)
        self.memory_delete_button.setVisible(selected)
        self.memory_delete_button.setEnabled(selected and not self.busy and not self.memory_loading)
        self.memory_more_button.setVisible(self.memory_more)
        self.memory_more_button.setEnabled(self.memory_more and not self.busy and not self.memory_loading)
        self.memory_refresh_button.setEnabled(not self.busy and not self.memory_loading)

    def load_more_memory(self):
        if self.memory_loading or not self.memory_more:
            return
        self.refresh_memory(reset=False)

    def add_memory(self):
        if self.busy:
            return
        source = self.memory_source.text().strip()
        target = self.memory_target.text().strip()
        if not source or not target:
            self.set_tab_status(1, "记忆的原文和译文都不能为空。")
            return
        body = {"source": source, "target": target,
                "source_lang": self.memory_source_lang.currentData() or "",
                "target_lang": self.memory_target_lang.currentData() or "",
                "preserve_metadata": True}
        self.busy = True
        self.memory_pending = dict(body)
        self.known_memory_ids_before = set(self.known_memory_ids)
        self.memory_add_button.setEnabled(False)
        self.set_tab_status(1, "正在写入翻译记忆…")
        self.controller.add_translation_memory(body, self.memory_saved, self.show_failure)

    def memory_fields_match(self, body):
        return (self.memory_source.text().strip() == body.get("source")
                and self.memory_target.text().strip() == body.get("target")
                and (self.memory_source_lang.currentData() or "") == body.get("source_lang")
                and (self.memory_target_lang.currentData() or "") == body.get("target_lang"))

    def memory_saved(self, memory):
        self.busy = False
        self.memory_add_button.setEnabled(True)
        identity = str((memory or {}).get("id") or "")
        if identity and identity in self.known_memory_ids_before:
            message = "已有相同句子，已更新该条记忆的译文。"
        else:
            message = "翻译记忆已写入。"
        if self.memory_pending and not self.memory_fields_match(self.memory_pending):
            message += " 新输入的内容已保留。"
        else:
            self.memory_source.clear()
            self.memory_target.clear()
        self.memory_notice = message
        self.refresh_memory()
        if self.isVisible():
            self.memory_source.setFocus()

    def disable_memory(self):
        row = self.memory_table.currentRow()
        if row < 0 or self.busy:
            return
        identity = self.memory_table.item(row, 0).data(Qt.ItemDataRole.UserRole) if self.memory_table.item(row, 0) else None
        if not identity:
            return
        self.busy = True
        self.update_memory_actions()
        self.set_tab_status(1, "正在删除翻译记忆…")
        self.controller.delete_translation_memory(identity, self.memory_deleted, self.show_failure)

    def memory_deleted(self, _result):
        self.busy = False
        self.memory_notice = "翻译记忆已删除，不再参与复用。"
        self.refresh_memory()

    # ----------------------------------------------------------------- agent tab

    def _agent_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(label("目标语言", "muted"))
        self.agent_target = QComboBox()
        self.agent_target.setMinimumWidth(120)
        self.controller.fill(self.agent_target, list(LANGUAGES), preferred="en")
        row.addWidget(self.agent_target)
        row.addStretch()
        row.addWidget(label("模型需支持 tools；可直接改字幕、术语和记忆", "hint"))
        layout.addLayout(row)

        self.agent_log = QTextBrowser()
        self.agent_log.setOpenExternalLinks(False)
        self.agent_log.setPlaceholderText(
            "指令示例：检查字幕，把识别错的 Nambi 改成 Bambi；或把“声笺”加入术语库，译作 Shengjian。")
        layout.addWidget(self.agent_log, 1)
        send = QHBoxLayout()
        self.agent_input = QLineEdit()
        self.agent_input.setPlaceholderText("让 Agent 校对字幕、维护术语或翻译记忆…")
        self.agent_input.returnPressed.connect(self.send_agent)
        self.agent_send_button = button("发送指令", self.send_agent, "sparkle", "primary")
        send.addWidget(self.agent_input, 1)
        send.addWidget(self.agent_send_button)
        layout.addLayout(send)
        return page

    def send_agent(self):
        if self.busy:
            return
        message = self.agent_input.text().strip()
        if not message:
            self.set_tab_status(2, "先输入一条 Agent 指令。")
            return
        self.busy = True
        self.agent_send_button.setEnabled(False)
        self.append_agent("你：" + message)
        self.set_tab_status(2, "Agent 正在处理…")
        session_id = self.session_id or ((self.controller.session or {}).get("id") or "")
        self.controller.agent_chat({
            "message": message, "session_id": session_id,
            "target": self.agent_target.currentData() or "en"},
            self.agent_replied, self.show_failure)

    def agent_replied(self, payload):
        self.busy = False
        self.agent_send_button.setEnabled(True)
        self.agent_input.clear()
        self.append_agent("Agent：" + (payload.get("reply") or ""))
        for step in (payload.get("trace") or []):
            self.append_agent(f"  · {step.get('tool')} · {step.get('status')} · {step.get('detail')}")
        self.set_tab_status(2, "Agent 指令完成。")
        if any(step.get("tool") == "correct_lines" and step.get("status") == "ok"
               for step in (payload.get("trace") or [])):
            # The subtitle behind this modal has changed; refresh the recognition page
            # immediately instead of waiting for its next poll.
            self.controller.refresh()
        # The agent may have changed either list; reload only the tab the user is on.
        self.term_loaded = False
        self.memory_loaded = False
        if self.tabs.currentIndex() == 0:
            self.refresh_terms()
        elif self.tabs.currentIndex() == 1:
            self.refresh_memory()

    def append_agent(self, text):
        self.agent_log.append(text)

    # ------------------------------------------------------------------ opening

    def open_window(self, session_id="", target=""):
        self.session_id = session_id or ""
        self.term_search_timer.stop()
        self.memory_search_timer.stop()
        self.term_request += 1
        self.memory_request += 1
        self.term_loaded = self.memory_loaded = False
        self.term_loading = self.memory_loading = False
        self.term_more = self.memory_more = False
        self.known_term_ids, self.known_memory_ids = set(), set()
        self.known_term_ids_before, self.known_memory_ids_before = set(), set()
        self.term_pending = self.memory_pending = None
        self.term_notice = self.memory_notice = ""
        self.clear_term(message=None, focus=False)
        self.term_table.blockSignals(True)
        self.term_table.setRowCount(0)
        self.term_table.blockSignals(False)
        self.memory_table.blockSignals(True)
        self.memory_table.setRowCount(0)
        self.memory_table.blockSignals(False)
        self.update_memory_actions()
        for control in (self.term_search, self.memory_search, self.term_filter, self.memory_filter):
            control.blockSignals(True)
        self.term_search.clear()
        self.memory_search.clear()
        self.term_filter.setCurrentIndex(0)
        self.memory_filter.setCurrentIndex(0)
        for control in (self.term_search, self.memory_search, self.term_filter, self.memory_filter):
            control.blockSignals(False)
        self.controller.fill(self.term_source_lang, LANGUAGE_CHOICES, preferred="")
        self.controller.fill(self.term_target_lang, LANGUAGE_CHOICES, preferred=target or "en")
        self.controller.fill(self.memory_source_lang, LANGUAGE_CHOICES, preferred="")
        self.controller.fill(self.memory_target_lang, LANGUAGE_CHOICES, preferred=target or "en")
        self.controller.fill(self.agent_target, list(LANGUAGES), preferred=target or "en")
        self.tab_status[0], self.tab_status[1] = "切换到术语库时加载。", "切换到翻译记忆时加载。"
        self.status.setText(self.tab_status[self.tabs.currentIndex()])
        # Fetch only the visible tab. Opening on the term tab must not wait for 200
        # memories to be transferred and painted.
        if self.tabs.currentIndex() == 1:
            self.refresh_memory()
        elif self.tabs.currentIndex() == 0:
            self.refresh_terms()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        self.term_search_timer.stop()
        self.memory_search_timer.stop()
        super().closeEvent(event)

    def show_failure(self, message):
        self.busy = False
        self.term_loading = False
        self.memory_loading = False
        self.term_add_button.setEnabled(True)
        self.memory_add_button.setEnabled(True)
        self.agent_send_button.setEnabled(True)
        self.update_term_actions()
        self.update_memory_actions()
        self.set_tab_status(self.tabs.currentIndex(), str(message))

    def language_pair(self, source, target):
        def name(code):
            for label_, value in LANGUAGES:
                if value == code:
                    return label_
            return code or "不限"
        return f"{name(source)} → {name(target)}"

