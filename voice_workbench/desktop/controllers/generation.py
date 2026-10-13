"""GenerationController responsibilities, composed by the owning Qt widget."""
from __future__ import annotations
from pathlib import Path
from PyQt6.QtWidgets import QFileDialog
from ...subtitles import parse_text
from ...model_info import selected_mode
from ...model_controls import populate
from ..api_client import checked
from ..model_labels import model_key, model_label


class GenerationController:
    def import_text(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入字幕或文本", "", "字幕与文本 (*.txt *.lrc *.srt *.vtt);;所有文件 (*)")
        if not filename:
            return
        try:
            raw = Path(filename).read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("gb18030")
            parse_text(content)
            self.text_edit.setPlainText(content)
            self.title_edit.setText(Path(filename).stem)
        except Exception as exc:
            self.show_error(str(exc))

    def submit(self, segments, title):
        if self.submitting:
            return
        voice = self.current_voice()
        if voice is None:
            self.show_error("请等待服务连接并选择声音。")
            return
        self.document_timer.stop()
        self.document_dirty = False
        use_preset = self.selected_preset() is not None
        body = {**self.document_payload(), "voice_id": voice["model_name"] if use_preset else voice["id"], "use_preset": use_preset,
                "title": title, "segments": segments,
                "overrides": self.model_controls.overrides()}
        self.submission_title = title
        target = self.selected_job
        self.submitting = True
        self.update_generation_actions()
        def send():
            self.call(lambda c: checked(c.post(f"/jobs/{target}/generate" if target else "/jobs", json=body)), self.submitted, self.submission_failed)
        if self.document_save_running:
            self.after_save.append(send)
        else:
            send()

    def submitted(self, result):
        self.submitting = False
        if self.selected_job != result["job_id"]:
            self.job_data = None
            self.segments_table.setRowCount(0)
            self.segments_table.setCurrentCell(-1, -1)
            self.auto_select_audio = True
            self.preview_segment()
        self.selected_job = result["job_id"]
        indices = result.get("segment_indices", [])
        self.pending_autoplay = {"job_id": result["job_id"], "indices": set(indices)} if indices else None
        if result.get("title") and self.title_edit.text() == getattr(self, "submission_title", None):
            self.title_edit.blockSignals(True)
            self.title_edit.setText(result.get("display_title", result["title"]))
            self.title_edit.blockSignals(False)
        self.update_generation_actions()
        if self.document_dirty:
            self.document_changed()
        self.statusBar().showMessage("已加入当前作品的生成列表，完成后自动保存并播放本次音频。", 5000)
        self.poll()

    def submission_failed(self, message):
        self.submitting = False
        self.update_generation_actions()
        self.show_error(message)

    def generate(self):
        try:
            self.capture_dialogue_parameters()
            self.submit(self.document_segments(), self.title_edit.text() or "配音任务")
        except Exception as exc:
            self.show_error(str(exc))

    def warmup(self):
        self.submit([{"text": "你好，这是本地配音工作台的语音测试。"}], self.title_edit.text() or "未命名配音")

    def retry(self):
        if self.selected_job:
            self.call(lambda c: checked(c.post(f"/jobs/{self.selected_job}/retry")), self.render_job)

    def cancel(self):
        if self.selected_job:
            self.call(lambda c: checked(c.post(f"/jobs/{self.selected_job}/cancel")), self.render_job)

    def selected_preset(self):
        return next((v for v in self.voices if v["id"] == self.voice_combo.currentData() and not v.get("is_template")), None)

    def current_voice(self):
        selected = self.voice_combo.currentData() or self.generation_base_id
        return next((v for v in self.voices if v["id"] == selected), None)

    def refresh_generation_models(self, preferred=None):
        custom = preferred is None and self.voice_combo.count() > 0 and self.voice_combo.currentData() is None
        reapply = preferred is not None
        preferred = preferred or (self.current_voice() or {}).get("id")
        voice = next((v for v in self.voices if v["id"] == preferred), None)
        if not voice and custom:
            voice = next((v for v in self.voices if model_key(v) == self.model_combo.currentData()), None)
        voice = voice or next((v for v in self.voices if v["backend"] == "audio_cpp"), self.voices[0] if self.voices else None)
        entries, seen = [], set()
        for value in self.voices:
            key = model_key(value)
            if key not in seen:
                entries.append((key, model_label(value)))
                seen.add(key)
        populate(self.model_combo, entries, model_key(voice) if voice else None)
        self.model_controls.set_references(self.references)
        self.populate_generation_presets(voice["id"] if voice else None, custom=custom, reapply=reapply)

    def populate_generation_presets(self, preferred=None, custom=False, reapply=False):
        from ...library import friendly_name
        voices = [v for v in self.voices if model_key(v) == self.model_combo.currentData()]
        base = next((v for v in voices if v["id"] == preferred), voices[0] if voices else None)
        self.generation_base_id = base["id"] if base else None
        presets = [(v["id"], friendly_name(v)) for v in voices if not v.get("is_template")]
        choice = None if custom or (base and base.get("is_template")) else self.generation_base_id
        populate(self.voice_combo, [(None, "无预设（当前参数）")] + presets, choice)
        self.update_voice_summary(reapply=reapply)
        self.update_link_config()

    def select_generation_model(self):
        self.stop_reference()
        self.active_voice_snapshot = None
        self.populate_generation_presets(reapply=True)

    def generation_voice(self):
        voice = self.current_voice()
        if not voice:
            return None
        return {**voice, **self.model_controls.overrides()}

    def update_generation_actions(self):
        if not hasattr(self, "generate_button"):
            return
        from ...runner import check_voice
        voice = self.generation_voice()
        usable = self.ready and bool(voice)
        message = "路径检查通过 · 可短句试音" if usable else "请选择模型"
        if usable:
            try:
                check_voice(voice)
            except Exception as exc:
                usable = False
                message = str(exc)
        elif not self.ready:
            message = "等待本地服务连接"
        self.voice_health.setText(message)
        self.new_document_button.setEnabled(not getattr(self, "submitting", False))
        self.return_button.setEnabled(not self.submitting)
        for control in self.creation_choices.values():
            control.setEnabled(not self.submitting)
        self.jobs_table.setEnabled(not getattr(self, "submitting", False))
        self.warmup_button.setEnabled(usable and not getattr(self, "submitting", False))
        generate_usable = usable
        if self.ready and self.document_text().strip():
            try:
                segments = self.document_segments()
                tagged = {s.get("voice_id") for s in segments if s.get("voice_id")}
                if tagged:
                    for segment in segments:
                        if segment.get("voice_id"):
                            preset = next(v for v in self.voices if v["id"] == segment["voice_id"])
                            check_voice({**preset, **(segment.get("overrides") or {})})
                    generate_usable = usable or all(s.get("voice_id") for s in segments)
                    if generate_usable:
                        message = "参数仅作用于选中的对白框" if self.editor_mode == "dialogue" else f"使用 {len(tagged)} 个音色预设"
            except (ValueError, StopIteration) as exc:
                generate_usable = False
                message = str(exc) or "声音预设已删除，请重新选择"
            except Exception as exc:
                generate_usable = False
                message = "@ 音色配置不可用：" + str(exc)
        self.voice_health.setText(message)
        self.generate_button.setEnabled(generate_usable and not getattr(self, "submitting", False) and bool(self.document_text().strip()))
        self.save_generation_button.setEnabled(usable)
        self.model_controls.advanced_toggle.setEnabled(bool(voice))
        self.update_dialogue_records()

    def update_voice_summary(self, *args, reapply=False):
        if args:
            self.active_voice_snapshot = None
            if self.voice_combo.currentData() is not None:
                self.generation_base_id = self.voice_combo.currentData()
        voice = getattr(self, "active_voice_snapshot", None) or self.current_voice()
        if voice:
            custom = self.voice_combo.currentData() is None
            previous = self.model_controls.voice
            keep_custom = (custom and not reapply and previous and previous["id"] == voice["id"]
                           and all(previous.get(k) == voice.get(k) for k in ("model_dir", "engine_executable", "device")))
            if not keep_custom:
                changed = self.model_controls.key is None or self.model_controls.key[0] != voice.get("model_name", voice["id"])
                if changed:
                    self.stop_reference()
                self.model_controls.bind(voice, preserve_edits=not (reapply or (args and self.selected_preset())), custom=custom)
                if changed:
                    self.inspector_scroll.verticalScrollBar().setValue(0)
        self.refresh_main_settings()

    def refresh_main_settings(self):
        preset = self.selected_preset()
        if preset and self.model_controls.info:
            current = self.generation_voice()
            if (self.model_controls.mode.currentData() != selected_mode(preset)
                    or model_key(current) != model_key(preset)):
                self.voice_combo.blockSignals(True)
                self.voice_combo.setCurrentIndex(0)
                self.voice_combo.blockSignals(False)
                self.stop_reference()
                self.update_link_config()
                preset = None
                self.statusBar().showMessage("已切换为无预设，当前模型与编辑参数保留。", 5000)
        from ...library import friendly_name
        for index in range(1, self.voice_combo.count()):
            source = next((v for v in self.voices if v["id"] == self.voice_combo.itemData(index)), None)
            if source and (not preset or source["id"] != preset["id"]):
                self.voice_combo.setItemText(index, friendly_name(source))
        if preset:
            from ...schemas import VoiceConfig
            expected = VoiceConfig(**preset).model_dump()
            expected["voice_mode"] = selected_mode(preset)
            defaults = {s["name"]: s["default"] for s in self.model_controls.generation_controls.specs}
            from ...generation_options import compatible_options
            options, _ = compatible_options(expected, self.model_controls.info)
            expected["request_options"] = {**defaults, **options}
            adjusted = any(expected.get(k) != v for k, v in self.model_controls.overrides().items())
            caption = friendly_name(preset) + (" · 已调整" if adjusted else "")
            self.voice_combo.setItemText(self.voice_combo.currentIndex(), caption)
            self.voice_combo.setToolTip("当前参数已调整；保存声音预设后可用于联动。" if adjusted else "使用已保存的声音预设。")
        else:
            self.voice_combo.setToolTip("无预设：使用当前模型与参数；选择已保存的预设会重新应用其配置。")
        voice = self.generation_voice()
        if voice:
            info = self.model_controls.info
            self.voice_family.setText(("audio.cpp 统一引擎" if voice["backend"] == "audio_cpp" else "官方 Python 引擎" if voice["backend"] == "index_tts" else "系统语音引擎") +
                                      ("  ·  CUDA" if voice.get("device") == "cuda" and voice["backend"] != "windows_sapi" else ""))
            self.model_combo.setToolTip(voice.get("model_dir") or "Windows 系统声音")
            mode = self.model_controls.mode.currentData()
            if mode == "builtin":
                summary = "使用模型内置音色" if info["builtin_voices"] else "使用 Windows 系统声音"
            elif mode == "design":
                summary = "按声音描述设计音色，无需参考音频"
            else:
                summary = "精细克隆需填写参考原文" if mode == "continuation" else "使用参考音频克隆声音"
            if info["language_auto"]:
                summary += "\n自动识别文本语言"
            summary += "\n当前设置可直接生成；保存预设后可用于双语工作流。"
            self.voice_summary.setText(summary)
        self.capture_dialogue_parameters()
        self.update_generation_actions()
