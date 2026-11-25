"""Subtitle translation dialog: configure a provider, translate, export the result.

This is the first framework pass: the OpenAI-compatible path works end to end,
while the local-model choice explains the reserved seam instead of pretending to
run. The original recognition text stays untouched; a translation is stored
beside it in the session.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

from ...speech_recognition.exporting import is_chinese, srt_text, translated_lines
from ...ui import MUTED, STYLE, button, label

SIZE = (760, 720)


class TranslationDialog(QDialog):
    def __init__(self, owner, controller):
        super().__init__(owner)
        self.owner, self.controller = owner, controller
        self.session = None
        self.options = {}
        self.result = None
        self.busy = False
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1500)
        self.refresh_timer.timeout.connect(self.refresh_external_result)
        self.setWindowTitle("音频翻译")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(*SIZE)
        self.setStyleSheet(STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(label("音频翻译", "dialogTitle"))
        header.addStretch()
        outer.addLayout(header)
        self.session_label = label("未打开识别结果", "hint")
        outer.addWidget(self.session_label)

        outer.addWidget(label("翻译设置", "groupTitle"))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(label("提供方", "muted"))
        self.provider = QComboBox()
        self.provider.setMinimumWidth(190)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        row.addWidget(self.provider)
        row.addSpacing(8)
        row.addWidget(label("目标语言", "muted"))
        self.target = QComboBox()
        self.target.setMinimumWidth(120)
        self.target.currentIndexChanged.connect(self.sync_export_label)
        row.addWidget(self.target)
        row.addStretch()
        self.save_button = button("保存设置", self.save_settings, "check", "ghost")
        row.addWidget(self.save_button)
        outer.addLayout(row)

        agent_row = QHBoxLayout()
        agent_row.setSpacing(8)
        self.use_agent = QCheckBox("使用翻译 Agent")
        self.use_agent.setChecked(True)
        self.use_agent.setToolTip("检索术语和翻译记忆、质检并记录运行过程；关闭时走普通翻译。")
        agent_row.addWidget(self.use_agent)
        self.knowledge_button = button("术语库 / 记忆", self.open_knowledge, "article", "ghost")
        agent_row.addWidget(self.knowledge_button)
        agent_row.addStretch()
        outer.addLayout(agent_row)

        self.api_fields = QVBoxLayout()
        self.api_fields.setSpacing(4)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.openai.com/v1")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("例如 gpt-4o-mini、deepseek-chat、本地服务的模型名")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空表示不修改已保存的密钥；部分本地服务不需要")
        for title, control in (("Base URL", self.base_url), ("模型", self.model_name), ("API Key", self.api_key)):
            self.api_fields.addWidget(label(title, "muted"))
            self.api_fields.addWidget(control)
        outer.addLayout(self.api_fields)
        self.provider_hint = label("", "hint")
        self.provider_hint.setWordWrap(True)
        outer.addWidget(self.provider_hint)

        outer.addSpacing(4)
        outer.addWidget(label("翻译结果", "groupTitle"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["原文", "译文"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.table, 1)
        self.status = QLabel("设置好翻译服务后，点「开始翻译」。")
        self.status.setProperty("role", "hint")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(34)
        outer.addWidget(self.status)
        self.agent_label = label("", "hint")
        self.agent_label.setWordWrap(True)
        self.agent_label.hide()
        outer.addWidget(self.agent_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.translate_button = button("开始翻译", self.translate, "sparkle", "primary")
        actions.addWidget(self.translate_button)
        self.export_button = button("导出译文 SRT", self.export, "export", "ghost")
        actions.addWidget(self.export_button)
        actions.addStretch()
        actions.addWidget(button("关闭", self.close, "close", "ghost"))
        outer.addLayout(actions)
        self.update_actions()

    # ------------------------------------------------------------------ opening

    def open_window(self, session):
        self.session = session
        state = (session or {}).get("state") or {}
        self.session_label.setText("当前识别：" + (state.get("name") or "未命名音频"))
        self.result = state.get("translation")
        self.load_options()
        self.render_result()
        self.agent_label.hide()
        self.agent_label.clear()
        if self.result:
            self.status.setText(f"已加载上次译文（{len(self.result.get('lines') or [])} 行，目标语言 "
                                f"{(self.result or {}).get('target') or ''}）。")
            self.show_agent_result(self.result)
        else:
            self.status.setText("设置好翻译服务后，点「开始翻译」。")
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh_timer.start()
        self.refresh_external_result()

    def load_options(self):
        self.controller.translation_options(self.options_loaded, self.show_failure)

    def open_knowledge(self):
        identity = (self.session or {}).get("id") or ""
        self.controller.open_translation_knowledge(identity, self.target.currentData() or "")

    def options_loaded(self, options):
        self.options = options
        settings = options.get("settings") or {}
        self.controller.fill(self.provider, [(item["name"], item["id"]) for item in options.get("providers") or []],
                             preferred=(self.result or {}).get("provider") or settings.get("provider"))
        self.controller.fill(self.target, list(options.get("languages") or []),
                             preferred=(self.result or {}).get("target") or settings.get("target"))
        self.base_url.setText(settings.get("base_url") or "")
        self.model_name.setText(settings.get("model") or "")
        self.api_key.clear()
        self.api_key.setPlaceholderText("已保存密钥，留空保持不变" if settings.get("has_key")
                                        else "可留空：部分本地服务不需要密钥")
        self.provider_changed()
        self.sync_export_label()

    def provider_changed(self):
        provider = self.provider.currentData()
        api = provider == "api"
        for index in range(self.api_fields.count()):
            widget = self.api_fields.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(api)
        if api:
            self.provider_hint.setText("Base URL 可填 …/v1，也可直接填完整的 …/chat/completions；"
                                       "本地服务例如 http://127.0.0.1:1234/v1。")
        else:
            self.provider_hint.setText("本地模型入口已预留：当前版本选择后会明确提示尚未接入，"
                                       "不会偷偷改走在线 API。")
        self.update_actions()

    # ---------------------------------------------------------------- settings

    def body(self):
        values = {"provider": self.provider.currentData(), "target": self.target.currentData(),
                  "base_url": self.base_url.text().strip(), "model_name": self.model_name.text().strip()}
        if self.api_key.text().strip():
            values["api_key"] = self.api_key.text().strip()
        return values

    def save_settings(self):
        if self.busy:
            return
        self.busy = True
        self.update_actions()
        self.controller.save_translation_settings(self.body(), self.saved, self.show_failure)

    def saved(self, settings):
        self.busy = False
        self.api_key.clear()
        self.api_key.setPlaceholderText("已保存密钥，留空保持不变" if settings.get("has_key")
                                        else "可留空：部分本地服务不需要密钥")
        self.status.setText("翻译设置已保存。")
        self.update_actions()

    # --------------------------------------------------------------- translate

    def translate(self):
        if self.busy or not self.session:
            return
        if not (self.session.get("state") or {}).get("lines"):
            self.status.setText("当前识别结果没有可翻译的文本。")
            return
        self.busy = True
        self.update_actions()
        self.agent_label.hide()
        self.status.setText("正在" + ("由 Agent " if self.use_agent.isChecked() else "") + "翻译…首次请求可能需要加载模型。")
        self.controller.save_translation_settings(
            self.body(), lambda _: self.request_translation(self.use_agent.isChecked()), self.show_failure)

    def request_translation(self, agent=False):
        identity = self.session["id"]
        body = {"provider": self.provider.currentData(), "target": self.target.currentData()}
        if agent:
            self.controller.translate_session_agent(identity, {**body, "use_memory": True},
                                                    self.agent_translated, self.show_failure)
        else:
            self.controller.translate_session(identity, body, self.translated, self.show_failure)

    def agent_translated(self, payload):
        self.busy = False
        self.result = (payload or {}).get("translation") or {}
        self.session.setdefault("state", {})["translation"] = self.result
        self.render_result()
        summary = (self.result or {}).get("agent") or {}
        self.status.setText(f"Agent 翻译完成：{len(self.result.get('lines') or [])} 行 · "
                            f"{self.result.get('seconds', 0)} 秒 · 术语 {summary.get('terms', 0)} 条 · "
                            f"记忆 {summary.get('memory_hits', 0)} 行 · 质检 {len(summary.get('issues') or [])} 处")
        self.show_agent_result(self.result, (payload or {}).get("run"))
        self.controller.panel.status.setText("译文已生成，可在「翻译当前结果」中导出。")
        self.controller.render_session()
        self.update_actions()

    def translated(self, result):
        self.busy = False
        self.result = result
        self.session.setdefault("state", {})["translation"] = result
        self.render_result()
        self.agent_label.hide()
        self.status.setText(f"翻译完成：{len(result.get('lines') or [])} 行 · "
                            f"{result.get('seconds', 0)} 秒 · 目标语言 "
                            f"{self.language_name(result.get('target'))}")
        self.controller.panel.status.setText("译文已生成，可在「翻译当前结果」中导出。")
        self.controller.render_session()
        self.update_actions()

    def render_result(self):
        lines = (self.result or {}).get("lines") or []
        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            source = QTableWidgetItem(line.get("text") or "")
            source.setToolTip(line.get("text") or "")
            translated = QTableWidgetItem(line.get("translated") or "")
            translated.setToolTip(line.get("translated") or "")
            source.setForeground(QColor(MUTED))
            translated.setForeground(QColor("#2f6f9f"))
            self.table.setItem(row, 0, source)
            self.table.setItem(row, 1, translated)
        self.sync_export_label()
        self.update_actions()

    def show_agent_result(self, result, run=None):
        summary = (result or {}).get("agent") or {}
        if not summary:
            self.agent_label.hide()
            return
        text = (f"Agent：术语 {summary.get('terms', 0)} 条 · 记忆复用 {summary.get('memory_hits', 0)} 行"
                f" · 质检 {len(summary.get('issues') or [])} 处")
        if summary.get("repaired"):
            text += " · 已逐行修复"
        details = []
        for step in ((run or {}).get("trace") or []):
            details.append(f"{step.get('tool')} · {step.get('status')} · {step.get('detail')}")
        for issue in (summary.get("issues") or []):
            details.append(f"第 {int(issue.get('index', 0)) + 1} 行：{issue.get('detail')}")
        self.agent_label.setText(text)
        self.agent_label.setToolTip("\n".join(details[:30]))
        self.agent_label.show()

    def sync_export_label(self, *_):
        target = (self.result or {}).get("target") or self.target.currentData() or ""
        chinese = is_chinese(target)
        self.export_button.setText("导出中文字幕 SRT" if chinese else "导出译文 SRT")
        self.export_button.setToolTip("只写译文，不混入原文；时间码沿用原字幕。")

    def export(self):
        lines = translated_lines(self.result)
        if not lines:
            self.status.setText("还没有译文可以导出。")
            return
        state = (self.session or {}).get("state") or {}
        source = Path(state.get("source_path") or "")
        folder = source.parent if source.parent.is_dir() else Path.home()
        stem = source.stem or state.get("name") or "识别结果"
        target = (self.result or {}).get("target") or "translated"
        chinese = is_chinese(target)
        label = "中文字幕" if chinese else "译文"
        suggested = str(folder / f"{stem}.{target}.srt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出" + label, suggested, "SRT 字幕 (*.srt)")
        if not path:
            return
        try:
            Path(path).write_text(srt_text(lines), encoding="utf-8")
        except OSError as exc:
            self.status.setText("导出失败：" + str(exc))
            return
        self.status.setText(label + "已导出：" + path)

    # ------------------------------------------------------ external refresh

    def refresh_external_result(self):
        """Pick up a whole translation written by WorkBuddy / Doubao Work."""
        if not self.isVisible() or self.busy or not self.session:
            return
        identity = self.session.get("id")
        if not identity:
            return

        def done(detail):
            if not self.isVisible():
                return
            state = (detail or {}).get("state") or {}
            result = state.get("translation") or {}
            if not result.get("lines") or result == self.result:
                return
            self.session = detail
            self.result = result
            self.render_result()
            self.status.setText(f"外部翻译已写回：{len(result.get('lines') or [])} 行 · "
                                f"目标语言 {self.language_name(result.get('target'))}")
            self.show_agent_result(result)

        self.controller.translation_session(identity, done, lambda _: None)

    # --------------------------------------------------------------- presentation

    def language_name(self, code):
        for name, value in (self.options.get("languages") or []):
            if value == code:
                return name
        return code or ""

    def show_failure(self, message):
        self.busy = False
        self.status.setText(str(message))
        self.update_actions()

    def update_actions(self):
        ready = bool(self.session and (self.session.get("state") or {}).get("lines"))
        self.translate_button.setEnabled(ready and not self.busy)
        self.export_button.setEnabled(bool((self.result or {}).get("lines")) and not self.busy)
        self.save_button.setEnabled(not self.busy)
        self.use_agent.setEnabled(not self.busy)
        self.knowledge_button.setEnabled(not self.busy)
        for control in (self.provider, self.target, self.base_url, self.model_name, self.api_key):
            control.setEnabled(not self.busy)

    def closeEvent(self, event):
        self.refresh_timer.stop()
        super().closeEvent(event)

