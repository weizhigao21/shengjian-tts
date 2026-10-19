"""Creation modes, persistent editor state and per-dialogue actions."""
class CreationController:
    def navigate(self, index):
        if index == 0:
            self.open_creation_home()
        else:
            self.set_page(index)

    def open_creation_home(self):
        self.audio_editor.controller.save()
        self.save_document()
        self.recognition.controller.pause()
        self.text_edit.completer.popup().hide()
        self.dialogue_editor.close_popups()
        self.clip_list.hide()
        self.set_page(0)
        self.creation_pages.setCurrentIndex(0)
        self.resume_creation.setVisible(bool(self.selected_job or self.text_edit.toPlainText().strip()
                                             or any(r.text.toPlainText().strip() for r in self.dialogue_editor.rows)))

    def resume_creation_editor(self):
        self.choose_creation_mode(self.editor_mode, dirty=False)

    def start_creation(self, mode):
        if mode == "float":
            self.open_float_mode()
            return
        if mode == "audio":
            self.open_audio_editor()
            return
        if mode == "recognition":
            self.open_recognition()
            return
        if self.submitting:
            return
        from ..api_client import checked
        self.new_document()
        self.choose_creation_mode(mode, dirty=False)
        if self.ready:
            self.submitting = True
            self.update_generation_actions()
            body = self.document_payload()
            def created(job):
                self.submitting = False
                self.selected_job = job["id"]
                self.render_job(job)
                self.update_generation_actions()
                self.document_changed()
                self.poll()
            self.call(lambda c: checked(c.post("/jobs/draft", json=body)), created, self.submission_failed)

    def open_audio_editor(self, path=None, text="", name=""):
        self.save_document()
        self.clear_autoplay()
        self.player.stop()
        self.reference_player.stop()
        self.clip_list.hide()
        self.set_page(0)
        self.creation_pages.setCurrentIndex(2)
        self.audio_editor.controller.refresh()
        if path:
            self.audio_editor.controller.import_audio(str(path), text, name)

    def open_recognition(self):
        self.audio_editor.controller.save()
        self.save_document()
        self.clear_autoplay()
        self.player.stop()
        self.reference_player.stop()
        self.clip_list.hide()
        self.set_page(0)
        self.creation_pages.setCurrentIndex(3)
        self.recognition.controller.resume()

    def creation_page_changed(self, index):
        if hasattr(self, "transport"):
            self.transport.setVisible(index not in (2, 3) or self.tabs.currentIndex() != 0)
        if index != 2:
            self.audio_editor.controller.stop()
        if index != 3:
            self.recognition.controller.pause()

    def choose_creation_mode(self, mode, dirty=True):
        if self.selected_job and not self.loading_document and mode != self.editor_mode:
            self.statusBar().showMessage("任务类型已固定，请返回创作入口新建另一种任务。", 6000)
            return
        self.capture_dialogue_parameters()
        self.editor_mode = mode
        self.creation_pages.setCurrentIndex(1)
        self.editor_stack.setCurrentIndex(0 if mode == "article" else 1)
        self.editor_header.removeWidget(self.generate_button)
        self.generation_actions.removeWidget(self.generate_button)
        if mode == "article":
            self.generation_actions.insertWidget(1, self.generate_button)
        else:
            self.editor_header.insertWidget(2, self.generate_button)
        self.generate_button.show()
        self.import_control.setVisible(mode == "article")
        self.generate_button.setText("全部生成" if mode == "dialogue" else "开始生成")
        self.warmup_button.setVisible(mode == "article")
        self.format_hint.setText("Enter 新增对白 · Shift+Enter 换行" if mode == "dialogue" else "TXT / LRC / SRT / VTT")
        if mode == "dialogue":
            row = self.dialogue_editor.selected_row() or self.dialogue_editor.rows[0]
            self.dialogue_editor.select(row.identity)
            if not self.loading_document:
                self.select_dialogue_entry(row.identity)
            row.text.setFocus()
        else:
            self.text_edit.setFocus()
        if dirty:
            self.document_changed()
        self.update_script_stats()
        self.refresh_audio_scope()

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

    def dialogue_name_changed(self, identity):
        if not self.loading_document and self.editor_mode == "dialogue":
            self.refresh_audio_scope()

    def generate_dialogue_entry(self, identity):
        if self.submitting:
            return
        try:
            self.dialogue_editor.select(identity)
            self.capture_dialogue_parameters()
            self.submit(self.dialogue_editor.segments(self.generation_voice(), identity, force=True),
                        self.title_edit.text() or "未命名配音")
        except Exception as exc:
            self.show_error(str(exc))

    def play_dialogue_entry(self, index):
        item = next((s for s in (self.job_data or {}).get("segments", []) if s["idx"] == index), None)
        if item and item.get("source_id"):
            self.dialogue_editor.select(item["source_id"])
        row = next((r for r, item in enumerate(self.visible_segments) if item["idx"] == index), -1)
        if row >= 0:
            self.clear_autoplay()
            self.segments_table.selectRow(row)
            self.preview_segment()
            self.play()

    def update_dialogue_records(self):
        self.dialogue_editor.update_records((self.job_data or {}).get("segments", []), self.generation_voice(),
                                           self.ready and not self.submitting)

    def select_dialogue_entry(self, identity):
        if self.closing or self.loading_document or self.editor_mode != "dialogue" or self._binding_dialogue:
            return
        row = next((r for r in self.dialogue_editor.rows if r.identity == identity), None)
        if not row:
            return
        self._binding_dialogue = True
        try:
            base_id = row.voice_id or row.model_id
            base = next((v for v in self.voices if v["id"] == base_id), None)
            if base:
                self.active_voice_snapshot = None
                self.refresh_generation_models(base["id"])
                if not row.voice_id:
                    self.voice_combo.blockSignals(True)
                    self.voice_combo.setCurrentIndex(0)
                    self.voice_combo.blockSignals(False)
                effective = {**base, **(row.overrides or {})}
                self.model_controls.bind(effective, preserve_edits=False, custom=not bool(row.voice_id))
                self.refresh_main_settings()
            elif not base_id and self.current_voice():
                row.model_id = self.current_voice()["id"]
                row.overrides = self.model_controls.overrides()
                self.voice_combo.blockSignals(True)
                self.voice_combo.setCurrentIndex(0)
                self.voice_combo.blockSignals(False)
        finally:
            self._binding_dialogue = False
        self.refresh_audio_scope()
        self.update_generation_actions()

    def dialogue_voice_changed(self, identity):
        self.dialogue_editor.select(identity)
        self.select_dialogue_entry(identity)

    def capture_dialogue_parameters(self):
        if self.closing or self.editor_mode != "dialogue" or self._binding_dialogue or self.loading_document:
            return
        row, voice = self.dialogue_editor.selected_row(), self.current_voice()
        if not row or not voice:
            return
        # A removed preset must remain an explicit error until the user replaces it.
        if row.voice_id and not any(v["id"] == row.voice_id for v in self.voices):
            return
        preset = self.selected_preset()
        values = (preset["id"] if preset else None, preset["name"] if preset else "当前参数",
                  voice["id"], self.model_controls.overrides())
        if values != (row.voice_id, row.voice_name, row.model_id, row.overrides):
            row.voice_id, row.voice_name, row.model_id, row.overrides = values
            row.refresh_voice()
            self.document_changed()

    def refresh_audio_scope(self):
        if not hasattr(self, "segments_table"):
            return
        scope = (self.selected_job, self.editor_mode,
                 self.dialogue_editor.selected_id if self.editor_mode == "dialogue" else None)
        if scope != self.audio_scope:
            self.audio_scope = scope
            self.clear_autoplay()
            self.player.stop()
            self.segments_table.blockSignals(True)
            self.segments_table.setCurrentCell(-1, -1)
            self.segments_table.blockSignals(False)
            self.auto_select_audio = True
        if self.job_data:
            self.render_job(self.job_data, force=True)
        else:
            self.visible_segments = []
            self.segments_table.setRowCount(0)
            self.preview_segment()

    def scoped_segments(self, job):
        if self.editor_mode == "dialogue":
            return [s for s in job["segments"] if s.get("source_id") == self.dialogue_editor.selected_id and s.get("source_id")]
        return [s for s in job["segments"] if not s.get("source_id")]
