"""Creation modes, persistent editor state and per-dialogue actions."""
class CreationController:
    def navigate(self, index):
        if index == 0:
            self.open_creation_home()
        else:
            self.set_page(index)

    def open_creation_home(self):
        self.save_document()
        self.text_edit.completer.popup().hide()
        self.dialogue_editor.close_popups()
        self.clip_list.hide()
        self.set_page(0)
        self.creation_pages.setCurrentIndex(0)
        self.resume_creation.setVisible(bool(self.selected_job or self.text_edit.toPlainText().strip()
                                             or any(r.text.toPlainText().strip() for r in self.dialogue_editor.rows)))

    def resume_creation_editor(self):
        self.choose_creation_mode(self.editor_mode, dirty=False)

    def choose_creation_mode(self, mode, dirty=True):
        self.editor_mode = mode
        self.creation_pages.setCurrentIndex(1)
        self.editor_stack.setCurrentIndex(0 if mode == "article" else 1)
        for key, control in self.mode_buttons.items():
            control.setChecked(key == mode)
        self.import_control.setVisible(mode == "article")
        self.generate_button.setText("生成对话" if mode == "dialogue" else "开始生成")
        self.format_hint.setText("Enter 新增对白 · Shift+Enter 换行" if mode == "dialogue" else "TXT / LRC / SRT / VTT")
        if mode == "dialogue":
            self.dialogue_editor.rows[0].text.setFocus()
        else:
            self.text_edit.setFocus()
        if dirty:
            self.document_changed()
        self.update_script_stats()

    def document_payload(self):
        dialogue = self.editor_mode == "dialogue"
        return {"title": self.title_edit.text().strip() or "未命名配音",
                "script": self.dialogue_editor.script() if dialogue else self.text_edit.toPlainText(),
                "script_mentions": [] if dialogue else self.text_edit.mentions(),
                "editor_state": {"mode": self.editor_mode, "article_text": self.text_edit.toPlainText(),
                    "article_mentions": self.text_edit.mentions(), "dialogue_entries": self.dialogue_editor.entries()}}

    def document_segments(self):
        return self.dialogue_editor.segments(self.generation_voice()) if self.editor_mode == "dialogue" else self.text_edit.segments()

    def document_text(self):
        return self.dialogue_editor.script() if self.editor_mode == "dialogue" else self.text_edit.toPlainText()

    def load_editor_state(self, job):
        state = job.get("editor_state")
        if state:
            self.text_edit.setPlainText(state["article_text"])
            self.text_edit.restore_mentions(state.get("article_mentions"))
            self.dialogue_editor.load(state.get("dialogue_entries"))
            self.choose_creation_mode(state.get("mode", "article"), dirty=False)
        else:
            # Legacy works retain their article content. Conversion is explicit.
            self.dialogue_editor.load([])
            self.choose_creation_mode("article", dirty=False)

    def dialogue_changed(self):
        if self.loading_document:
            return
        self.document_changed()
        self.update_script_stats()

    def generate_dialogue_entry(self, identity):
        if self.submitting:
            return
        try:
            self.submit(self.dialogue_editor.segments(self.generation_voice(), identity, force=True),
                        self.title_edit.text() or "未命名配音")
        except Exception as exc:
            self.show_error(str(exc))

    def play_dialogue_entry(self, index):
        row = next((r for r, item in enumerate(self.visible_segments) if item["idx"] == index), -1)
        if row >= 0:
            self.clear_autoplay()
            self.segments_table.selectRow(row)
            self.preview_segment()
            self.play()

    def update_dialogue_records(self):
        self.dialogue_editor.update_records((self.job_data or {}).get("segments", []), self.generation_voice(),
                                           self.ready and not self.submitting)
