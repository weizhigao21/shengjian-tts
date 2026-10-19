"""AudioSettings responsibilities, composed by the owning Qt widget."""
from datetime import datetime
import json
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLineEdit, QMenu, QMessageBox, QVBoxLayout, QWidget,
)
from ...ui import audio_sort_combo, button, icon, label, generation_elapsed, sorted_audio
from .common import preset_text


class AudioSettings:
    def _audio_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        categories = QHBoxLayout()
        self.audio_categories = {}
        self.audio_category = "all"
        for value, text in (("all", "全部音频"), ("article", "文章音频"), ("dialogue", "对话音频")):
            control = button(text, lambda checked=False, kind=value: self.set_audio_category(kind), kind="segment")
            control.setCheckable(True)
            control.setChecked(value == "all")
            categories.addWidget(control)
            self.audio_categories[value] = control
        categories.addStretch()
        layout.addLayout(categories)
        tools = QHBoxLayout()
        self.select_all = QCheckBox("全选")
        self.select_all.clicked.connect(self.toggle_all)
        tools.addWidget(self.select_all)
        self.audio_search = QLineEdit()
        self.audio_search.setPlaceholderText("搜索作品、文本、模型或预设…")
        self.audio_search.textChanged.connect(self.render_audio)
        tools.addWidget(self.audio_search, 1)
        self.audio_sort = audio_sort_combo()
        self.audio_sort.currentIndexChanged.connect(self.render_audio)
        tools.addWidget(self.audio_sort)
        self.selection_label = label("", "muted")
        tools.addWidget(self.selection_label)
        self.delete_selected = button("删除所选", lambda: self.delete_audio(), "trash")
        self.delete_all = button("全部删除", lambda: self.delete_audio(delete_all=True), "trash", "ghost")
        tools.addWidget(self.delete_selected)
        tools.addWidget(self.delete_all)
        layout.addLayout(tools)
        self.audio_table = self.table(("", "作品 / 生成文本", "模型 / 模式", "声音预设", "生成时间 / 时长", "生成耗时", "试听", "详情", "删除"))
        self.audio_table.enable_audio_drag()
        self.audio_table.resolve_audio = self.audio_row_files
        self.audio_table.describe_audio = self.audio_row_title
        self.audio_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.audio_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.audio_table.horizontalHeader().setMinimumSectionSize(28)
        for col, width in ((0, 32), (2, 165), (3, 120), (4, 150), (5, 96), (6, 42), (7, 42), (8, 42)):
            self.audio_table.setColumnWidth(col, width)
        self.audio_table.itemChanged.connect(self.checked_item)
        self.audio_table.cellDoubleClicked.connect(lambda row, _: self.audio_details(self.visible_audio[row]))
        self.audio_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.audio_table.customContextMenuRequested.connect(self.audio_context_menu)
        layout.addWidget(self.audio_table, 1)
        footer = QHBoxLayout()
        self.audio_count = label("还没有生成的音频", "muted")
        footer.addWidget(self.audio_count)
        footer.addStretch()
        self.playback_label = label("", "muted")
        footer.addWidget(self.playback_label)
        layout.addLayout(footer)
        self.pages.addWidget(page)
        self.visible_audio = []
        self.update_selection()

    def render_audio(self, *_):
        text = self.audio_search.text().strip().casefold()
        self.visible_audio = [a for a in self.audio_data if (self.audio_category == "all" or a.get("content_type", "article") == self.audio_category)
            and (not text or text in " ".join((a["work_title"], a["text"], a["model"], a["mode"], preset_text(a),
                a.get("source_label") or "", a.get("source_name") or "", a.get("source_id") or "")).casefold())]
        self.visible_audio = sorted_audio(self.visible_audio, self.audio_sort.currentData())
        table = self.audio_table
        scroll = table.verticalScrollBar().value()
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        self.table_rows(table, [a["id"] for a in self.visible_audio])
        for row, a in enumerate(self.visible_audio):
            item = self.cell(table, row, 0, "")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, a["id"])
            item.setCheckState(Qt.CheckState.Checked if a["id"] in self.selected_ids else Qt.CheckState.Unchecked)
            name = f"{a.get('audio_label') or a['work_title']} · 第 {a['generation']} 次 / {a['idx']}" + ("（作品已移除）" if a.get("work_deleted") else "")
            self.cell(table, row, 1, name + "\n" + a["text"], name + "\n" + a["text"] + "\n作品：" + a["work_title"]
                + "\n对白 ID：" + (a.get("source_id") or "—") + "\n" + a["audio"])
            self.cell(table, row, 2, a["model"] + "\n" + a["mode"])
            self.cell(table, row, 3, preset_text(a))
            stamp = datetime.fromtimestamp(a["created"]).strftime("%Y-%m-%d %H:%M:%S") if a.get("created") else "时间未记录"
            duration = f"{a['duration']:.2f} 秒" if a.get("duration") is not None else "时长未知"
            date, _, clock = stamp.partition(" ")
            self.cell(table, row, 4, date + "\n" + (clock + " · " if clock else "") + duration, stamp + "\n" + duration)
            elapsed, elapsed_tip = generation_elapsed(a)
            timing = self.cell(table, row, 5, elapsed, elapsed_tip)
            timing.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            play = self.action_cell(table, row, 6, a["id"], lambda v=a: button("", lambda: self.play_audio(v), "play", "ghost"))
            play.setToolTip("试听 / 暂停")
            play.setEnabled(a["available"])
            details = self.action_cell(table, row, 7, a["id"], lambda v=a: button("", lambda: self.audio_details(v), "info", "ghost"))
            details.setToolTip("查看音频、完整文本与生成配置")
            delete = self.action_cell(table, row, 8, a["id"], lambda v=a: button("", lambda: self.delete_audio([v["id"]]), "trash", "ghost"))
            delete.setToolTip("删除此音频")
            table.setRowHeight(row, 64)
        table.verticalScrollBar().setValue(scroll)
        table.blockSignals(False)
        table.setUpdatesEnabled(True)
        self.audio_count.setText(f"当前列表 {len(self.visible_audio)} 条 · 共 {len(self.audio_data)} 条音频")
        self.update_selection()
        self.update_play_buttons()

    def audio_row_files(self, row):
        """Dragging a row hands the file itself to the target program."""
        record = self.visible_audio[row] if 0 <= row < len(self.visible_audio) else None
        if not record or not record.get("available") or not record.get("audio"):
            return []
        return [self.owner.data_dir / "audio" / record["audio"]]

    def audio_row_title(self, row):
        record = self.visible_audio[row] if 0 <= row < len(self.visible_audio) else None
        if not record:
            return ""
        return f"{record.get('work_title') or '音频'} · {(record.get('text') or '')[:14]}"

    def set_audio_category(self, category):
        self.audio_category = category
        self.selected_ids.clear()
        for value, control in self.audio_categories.items():
            control.setChecked(value == category)
        self.render_audio()

    def checked_item(self, item):
        if item.column() != 0:
            return
        identity = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_ids.add(identity)
        else:
            self.selected_ids.discard(identity)
        self.update_selection()

    def audio_context_menu(self, position):
        row = self.audio_table.rowAt(position.y())
        if not 0 <= row < len(self.visible_audio):
            return
        # Keep the clicked record even if polling or filtering changes the table.
        item = dict(self.visible_audio[row])
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        reference = menu.addAction(icon("voices"), "添加为参考音频…")
        reference.setEnabled(item["available"] and self.owner.ready and not self.deleting)
        hint = ("音频文件已不存在，请刷新列表。" if not item["available"] else
                "本地服务未连接，请连接后再添加。" if not self.owner.ready else
                "带入音频与生成文本，保存到声音库；参考音频需要 1–60 秒。")
        reference.setToolTip(hint)
        edit = menu.addAction(icon("audio"), "在音频编辑中打开")
        edit.setEnabled(item["available"] and self.owner.ready)
        details = menu.addAction(icon("info"), "查看详情")
        chosen = menu.exec(self.audio_table.viewport().mapToGlobal(position))
        menu.deleteLater()
        if chosen is reference:
            self.add_audio_reference(item)
        elif chosen is edit:
            self.owner.open_audio_editor(self.owner.data_dir / "audio" / item["audio"], item.get("text", ""),
                                         name=item.get("audio_label") or item.get("display_title") or "音频编辑")
        elif chosen is details:
            self.audio_details(item)

    def add_audio_reference(self, item):
        folder = (self.owner.data_dir / "audio").resolve()
        path = (folder / item["audio"]).resolve()
        if path.parent != folder or not path.is_file():
            self.owner.show_error("音频文件已不存在，请刷新列表后重试。")
            return
        self.stop()
        source = {"reference_audio": str(path),
                  "name": f"{item['work_title']} · 片段 {item['idx']}"[:100],
                  "reference_text": item["text"]}
        return self.owner.import_reference(source=source, selected=lambda _: self.owner.statusBar().showMessage(
            "已添加到声音库 → 参考声音，原生成音频保留。", 5000))

    def toggle_all(self, checked):
        ids = {a["id"] for a in self.visible_audio}
        self.selected_ids.update(ids) if checked else self.selected_ids.difference_update(ids)
        self.render_audio()

    def update_selection(self):
        visible_ids = {a["id"] for a in self.visible_audio}
        count = len(self.selected_ids)
        self.selection_label.setText(f"已选 {count} 条" if count else "")
        self.delete_selected.setEnabled(count > 0 and not self.deleting)
        self.delete_all.setEnabled(bool(self.visible_audio) and not self.deleting)
        self.select_all.setChecked(bool(visible_ids) and visible_ids <= self.selected_ids)

    def play_audio(self, item):
        self.owner.player.pause()
        self.owner.stop_reference()
        if self.playing_id == item["id"]:
            self.player.pause() if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else self.player.play()
            return
        self.player.stop()
        self.playing_id = item["id"]
        self.player.setSource(QUrl(self.owner.url + item["audio_url"]))
        self.playback_label.setText(item["work_title"])
        self.player.play()

    def update_play_buttons(self, *_):
        for row, a in enumerate(self.visible_audio):
            control = self.audio_table.cellWidget(row, 6)
            if control:
                active = a["id"] == self.playing_id and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                control.setIcon(icon("pause" if active else "play"))

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.playing_id = None
        self.playback_label.clear()

    def audio_details(self, item):
        voice = item["voice"]
        stamp = datetime.fromtimestamp(item["created"]).strftime("%Y-%m-%d %H:%M:%S") if item.get("created") else "未知"
        duration = f"{item['duration']:.2f} 秒" if item.get("duration") is not None else "未知"
        details = (f"作品：{item['work_title']}\n生成：第 {item['generation']} 次，第 {item['idx']} 段\n"
                   f"音频：{item['audio']}\n模型：{item['model']}\n模型路径：{voice.get('model_dir', '')}\n"
                   f"模式：{item['mode']}\n预设：{preset_text(item)}\n时间：{stamp}\n"
                   f"时长：{duration}\n生成耗时：{generation_elapsed(item)[0]}\n字幕时间：{item.get('timestamp') or '无'}\n\n生成文本：\n{item['text']}")
        import json
        self.owner.show_details(details + "\n\n生成时配置：\n" + json.dumps(voice, ensure_ascii=False, indent=2))

    def delete_audio(self, ids=None, delete_all=False):
        if self.deleting:
            return
        ids = [a["id"] for a in self.visible_audio] if delete_all else ids if ids is not None else sorted(self.selected_ids)
        count = len(ids)
        if count == 0:
            return
        result = QMessageBox.question(self, "删除生成音频", (f"删除当前列表全部 {count} 条音频？" if delete_all else f"删除所选 {count} 条音频？") + "\n对应生成记录和不再使用的音频文件会删除，无法撤销。作品文本会保留。",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if result != QMessageBox.StandardButton.Yes:
            return
        # Release the Qt media source before Windows removes the WAV file.
        self.stop()
        self.owner.player.stop()
        self.owner.player.setSource(QUrl())
        self.deleting = True
        self.update_selection()
        from ..api_client import checked
        def done(value):
            self.deleting = False
            self.selected_ids.difference_update(value["deleted"])
            self.refresh()
            self.owner.poll()
            self.owner.statusBar().showMessage(f"已删除 {len(value['deleted'])} 条音频。", 5000)
            if value["errors"]:
                self.owner.show_error("部分音频未删除：\n" + "\n".join(value["errors"]))
        def failed(message):
            self.deleting = False
            self.update_selection()
            self.owner.show_error(message)
        self.owner.call(lambda c: checked(c.post("/generated-audio/delete", json={"ids": ids})), done, failed)
