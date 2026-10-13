"""Main window assembly and Qt application lifecycle."""
from __future__ import annotations
import os
import subprocess
import sys
import httpx
from pathlib import Path
from PyQt6.QtCore import QLibraryInfo, QLocale, QTimer, QTranslator, QUrl
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication, QDialog, QMainWindow, QMessageBox, QPushButton, QTextEdit, QVBoxLayout,
)
from .. import __version__
from ..ui import application_icon
from .api_client import ApiWorker, checked
from .draft_saves import DraftSaveQueue
from .voice_checks import VoiceCheckCache
from .controllers.documents import DocumentController
from .controllers.generation import GenerationController
from .controllers.playback import PlaybackController
from .controllers.service import ServiceController
from .controllers.voices import VoiceLibraryController
from .controllers.creation import CreationController
from .layout.workspace import UiLayout


class Window(CreationController, DocumentController, GenerationController, PlaybackController, VoiceLibraryController, ServiceController, UiLayout, QMainWindow):
    def __init__(self, port, data_dir, autostart=True):
        super().__init__()
        self.port, self.data_dir = port, Path(data_dir).resolve()
        self.url = f"http://127.0.0.1:{port}"
        self.workers = set()
        self.closing = False
        self.close_requested = False
        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.close)
        self.process = None
        self.process_log = None
        self.log_offset = 0
        self.service_output_text = ""
        self.connecting_service = False
        self.service_launch_error = None
        self.owns_service = False
        self.voices = []
        self.references = []
        self.jobs = []
        self.dialogs = set()
        self.deleted_job_ids = set()
        self.active_voice_snapshot = None
        self.generation_base_id = None
        self.pending_autoplay = None
        self.autoplay_indices = None
        self.selected_job = None
        self.job_data = None
        self.visible_segments = []
        self.polling = False
        self.ready = False
        self.submitting = False
        self.loading_document = False
        self.editor_mode = "article"
        self._binding_dialogue = False
        self.audio_scope = None
        self.document_dirty = False
        self.document_save_running = False
        self.after_save = []
        self.draft_saves = DraftSaveQueue(self.data_dir / "pending-drafts.json")
        self.voice_checks = VoiceCheckCache()
        self.auto_select_audio = True
        self.document_timer = QTimer(self)
        self.document_timer.setSingleShot(True)
        self.document_timer.timeout.connect(self.save_document)
        self.setWindowTitle(f"声笺 v{__version__} · 本地配音工作台")
        self.setWindowIcon(application_icon())
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.reference_output = QAudioOutput(self)
        self.reference_player = QMediaPlayer(self)
        self.reference_player.setAudioOutput(self.reference_output)
        self.reference_preview_origin = None
        self._build()
        if self.draft_saves.load_error:
            self.service_log.append("本地待存草稿读取失败，请检查 pending-drafts.json：" + self.draft_saves.load_error)
        self.load_saved_jobs()
        self.reference_player.playbackStateChanged.connect(self.update_reference_icon)
        self.reference_player.errorOccurred.connect(self.reference_playback_error)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1800)
        if autostart:
            self.reconnect_service()

    def call(self, action, success=None, failure=None):
        worker = ApiWorker(self.url, action)
        self.workers.add(worker)
        def guarded(callback):
            def deliver(value):
                from PyQt6 import sip
                if not sip.isdeleted(self) and not self.closing:
                    callback(value)
            return deliver
        if success:
            worker.result.connect(guarded(success))
        worker.error.connect(guarded(failure or self.show_error))
        worker.finished.connect(lambda: self.workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def show_error(self, message):
        if self.close_requested:
            self.service_log.append("退出前请求失败：" + str(message))
            return
        QMessageBox.warning(self, "操作未完成", str(message))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "dialog_shade"):
            self.dialog_shade.setGeometry(self.rect())
        if hasattr(self, "clip_list"):
            self.clip_list.hide()
        if hasattr(self, "toast") and self.toast.isVisible():
            self.show_toast(self.statusBar().currentMessage())

    def show_details(self, text):
        dialog = QDialog(self)
        dialog.setWindowTitle("片段详情与原始异常")
        dialog.resize(850, 650)
        layout = QVBoxLayout(dialog)
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)
        button = QPushButton("复制详情")
        button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        layout.addWidget(button)
        dialog.exec()

    def closeEvent(self, event):
        self.capture_document_save()
        if self.submitting and self.workers:
            # Let the GUI receive a new task's ID before flushing its latest edits.
            self.close_requested = True
            self.setEnabled(False)
            self.close_timer.start(50)
            event.ignore()
            return
        self.close_timer.stop()
        self.closing = True
        self.clear_autoplay()
        self.timer.stop()
        self.document_timer.stop()
        self.clip_list.hide()
        self.settings.close_management()
        self.text_edit.completer.popup().hide()
        self.dialogue_editor.close_popups()
        self.player.stop()
        self.player.setSource(QUrl())
        self.reference_player.stop()
        self.reference_player.setSource(QUrl())
        for worker in list(self.workers):
            worker.blockSignals(True)
            worker.wait(30000)
            worker.deleteLater()
        self.workers.clear()
        if self.draft_saves.pending:
            try:
                with httpx.Client(base_url=self.url, trust_env=False, timeout=2) as client:
                    for snapshot in list(self.draft_saves.pending.values()):
                        try:
                            checked(client.patch(f"/jobs/{snapshot.task_id}/document", json=snapshot.body))
                            self.draft_saves.acknowledge(snapshot)
                        except Exception as exc:
                            self.service_log.append("退出时草稿提交失败：" + str(exc))
            except Exception as exc:
                self.service_log.append("退出时草稿保存失败：" + str(exc))
        if self.owns_service and self.process and self.process.poll() is None:
            try:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    client.post(self.url + "/shutdown")
            except httpx.HTTPError:
                pass
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process_log:
            self.process_log.close()
        event.accept()

    def show_toast(self, message):
        self.toast_text.setText(message)
        self.toast.setVisible(bool(message))
        if message:
            self.toast_text.setMaximumWidth(max(400, self.width() - 300))
            self.toast.adjustSize()
            self.toast.move(max(218, self.width() - self.toast.width() - 18), self.height() - self.transport.height() - self.toast.height() - 8)
            self.toast.raise_()


def configure_font(app):
    # 显式加载系统中文字体，也支持离屏布局检查。
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))


def configure_language(app):
    """Localize Qt's standard actions and keep the translator alive."""
    QLocale.setDefault(QLocale("zh_CN"))
    if getattr(app, "_chinese_translator", None) is not None:
        return
    translator = QTranslator(app)
    translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load("qtbase_zh_CN", translations):
        raise RuntimeError("缺少 Qt 简体中文资源 qtbase_zh_CN.qm，请保留完整的 _internal 文件夹。")
    app.installTranslator(translator)
    app._chinese_translator = translator


def launch(port, data_dir):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Shengjian.VoiceWorkbench")
    app = QApplication(sys.argv)
    configure_language(app)
    app.setApplicationName("声笺")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Shengjian")
    app.setWindowIcon(application_icon())
    app.setStyle("Fusion")
    configure_font(app)
    window = Window(port, data_dir)
    window.show()
    raise SystemExit(app.exec())
