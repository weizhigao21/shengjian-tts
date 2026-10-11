"""离屏渲染本项目界面供布局检查，不操作用户桌面。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import time
import argparse
from pathlib import Path
import httpx
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.gui import Window, configure_font
from voice_workbench.gui_layout import model_key

app = QApplication([])
app.setStyle("Fusion")
configure_font(app)
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
parser.add_argument("--check-settings-playback", action="store_true")
args = parser.parse_args()
window = Window(args.port, args.data_dir)
deadline = time.monotonic() + 25
while not window.ready and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.05)
if not window.ready:
    print(window.service_log.toPlainText())
    window.close()
    raise RuntimeError("预览服务未能启动")
window.timer.stop()
with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False) as client:
    health = client.get("/health").json()
    voices = client.get("/voices").json()
    references = client.get("/references").json()
    jobs = client.get("/jobs").json()
    selected = next((j for j in jobs if j["status"] == "done" and j["title"] == "真实语音生成验证"),
                    next((j for j in jobs if j["status"] == "done"), jobs[0] if jobs else None))
    window.selected_job = selected["id"] if selected else None
    job = client.get(f"/jobs/{selected['id']}").json() if selected else None
window.render((health, voices, jobs, job, references))
if job:
    window.open_job_data(job)
window.title_edit.setText("双语旁白 · 开场")
window.text_edit.setPlainText("[00:00.00]你好，这是本地配音工作台的语音测试。\n[00:05.00]欢迎使用双语语音工作流。")
window.show()
app.processEvents()
if job:
    completed_row = next((i for i, s in enumerate(job["segments"]) if s["status"] == "done"), None)
    if completed_row is not None:
        window.segments_table.selectRow(completed_row)
        app.processEvents()
folder = ROOT / "artifacts" / "ui"
folder.mkdir(parents=True, exist_ok=True)
if job and job["segments"]:
    window.toggle_clip_list()
    app.processEvents()
    window.grab().save(str(folder / "player-list.png"))
    window.clip_list.hide()
for index, name in ((0, "generation"), (1, "voices"), (2, "service"), (3, "settings-models")):
    window.tabs.setCurrentIndex(index)
    app.processEvents()
    if index == 3:
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False) as client:
            window.settings.model_data = client.get("/models").json()
            window.settings.audio_data = client.get("/generated-audio").json()
        window.settings.timer.stop()
        window.settings.render_models()
        window.settings.render_audio()
        app.processEvents()
    window.grab().save(str(folder / f"{name}.png"))
window.settings.set_section(1)
app.processEvents()
window.grab().save(str(folder / "settings-audio.png"))
window.resize(1080, 720)
app.processEvents()
window.grab().save(str(folder / "settings-audio-compact.png"))
window.resize(1380, 900)
kokoro = next((v for v in voices if v.get("family") == "kokoro_tts"), None)
if kokoro:
    window.model_combo.setCurrentIndex(window.model_combo.findData(model_key(kokoro)))
    window.voice_combo.setCurrentIndex(window.voice_combo.findData(kokoro["id"]))
    window.tabs.setCurrentIndex(0)
    app.processEvents()
    window.grab().save(str(folder / "kokoro-generation.png"))
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(folder / "kokoro-model.png"))
window.library.set_page("references")
app.processEvents()
window.grab().save(str(folder / "reference-library.png"))
window.library.set_page("presets")
app.processEvents()
vox = next((v for v in voices if v.get("family") == "voxcpm2"), None)
if vox:
    window.model_combo.setCurrentIndex(window.model_combo.findData(model_key(vox)))
    window.tabs.setCurrentIndex(0)
    window.model_controls.mode.setCurrentIndex(window.model_controls.mode.findData("design"))
    window.model_controls.description.setText("低沉、温暖的成年男声，语气自然，带一点笑意")
    app.processEvents()
    window.grab().save(str(folder / "vox-design.png"))
    window.model_controls.advanced_toggle.setChecked(True)
    app.processEvents()
    window.inspector_scroll.verticalScrollBar().setValue(0)
    app.processEvents()
    window.grab().save(str(folder / "vox-advanced.png"))
    window.model_controls.runtime_toggle.setChecked(True)
    app.processEvents()
    window.inspector_scroll.verticalScrollBar().setValue(window.inspector_scroll.verticalScrollBar().maximum())
    app.processEvents()
    window.grab().save(str(folder / "inline-advanced.png"))
if kokoro:
    window.model_combo.setCurrentIndex(window.model_combo.findData(model_key(kokoro)))
window.tabs.setCurrentIndex(0)
window.resize(1080, 720)
app.processEvents()
window.grab().save(str(folder / "compact.png"))
print("rendered", window.width(), window.height(), folder)
window.resize(1380, 900)
window.tabs.setCurrentIndex(0)
window.show()
app.processEvents()
from PyQt6.QtGui import QPainter, QColor
from PyQt6.QtCore import QPoint
if jobs:
    menu = window.job_menu(0)
    menu.popup(window.jobs_table.viewport().mapToGlobal(QPoint(90, 26)))
    app.processEvents()
    background = window.grab()
    painter = QPainter(background)
    painter.drawPixmap(menu.pos() - window.pos(), menu.grab())
    painter.end()
    background.save(str(folder / "task-menu.png"))
    menu.close()
    app.processEvents()
window.tabs.setCurrentIndex(1)
def dialog_preview(dialog, name):
    app.processEvents()
    background = window.grab()
    painter = QPainter(background)
    foreground = dialog.grab()
    painter.drawPixmap(QPoint((background.width() - foreground.width()) // 2, (background.height() - foreground.height()) // 2), foreground)
    painter.end()
    background.save(str(folder / name))
    dialog.reject()
    app.processEvents()
if references:
    dialog_preview(window.edit_reference(references[0]), "reference-editor.png")
    from voice_workbench.library import ReferencePicker
    picker = window.open_dialog(ReferencePicker(window, window.model_controls))
    picker.choose(references[0])
    dialog_preview(picker, "reference-picker.png")
if kokoro:
    dialog_preview(window.edit_library_voice(kokoro), "preset-editor.png")
if args.check_settings_playback and window.settings.audio_data:
    from PyQt6.QtMultimedia import QMediaPlayer
    errors = []
    window.show_error = errors.append
    window.tabs.setCurrentIndex(3)
    window.settings.set_section(1)
    window.settings.output.setVolume(0)
    clip = window.settings.audio_data[0]
    window.settings.play_audio(clip)
    deadline = time.monotonic() + 8
    while window.settings.player.duration() == 0 and not errors and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.02)
    assert not errors, errors
    assert window.settings.player.duration() > 0
    assert window.settings.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    window.settings.play_audio(clip)
    assert window.settings.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    print("Settings HTTP audio preview and play/pause verified (muted).")
window.close()
