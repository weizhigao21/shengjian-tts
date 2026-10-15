"""Visible catalog categories, including engine-declared non-speech tasks."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTabBar


SPEECH_TASKS = {"tts", "clone", "design", "vdes"}
RECOGNITION_TASKS = {"stt", "asr"}
ALIGN_TASKS = {"align", "forced_align"}
EDIT_TASKS = {"edit", "vc", "svc", "s2s"}


def adapted_model(model):
    return bool(model.get("adapted", True) or model.get("edit_adapted")
                or model.get("recognition_adapted") or model.get("align_adapted"))


def included_model(model, category):
    tasks = set(model.get("tasks") or [])
    if not tasks:
        for key, task in (("edit_adapted", "edit"), ("recognition_adapted", "asr"), ("align_adapted", "align")):
            if model.get(key):
                tasks.add(task)
        if not tasks and model.get("adapted", True):
            tasks.add("tts")
    groups = {"tts": SPEECH_TASKS, "stt": RECOGNITION_TASKS, "align": ALIGN_TASKS, "edit": EDIT_TASKS}
    if category == "all":
        return True
    if category == "adapted":
        return adapted_model(model)
    if category == "other":
        return not bool(tasks & (SPEECH_TASKS | RECOGNITION_TASKS | ALIGN_TASKS | EDIT_TASKS))
    return bool(tasks & groups.get(category, set()))


class ModelCategories(QTabBar):
    """Compact tabs with stable data keys for mode-specific navigation."""
    def __init__(self):
        super().__init__()
        self.setObjectName("modelCategories")
        self.setAccessibleName("模型用途分类")
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setStyleSheet("QTabBar#modelCategories::tab { padding: 8px 12px; margin-right: 4px;"
                           " border: 1px solid transparent; border-radius: 6px; color: #737377; }"
                           "QTabBar#modelCategories::tab:selected { background: #fff2e9;"
                           " border-color: #f1c4a6; color: #ac4c16; }"
                           "QTabBar#modelCategories::tab:hover { background: #f4f4f2; }")
        for title, value in (("全部", "all"), ("语音生成", "tts"), ("语音识别", "stt"),
                             ("时间对齐", "align"), ("音频编辑", "edit"), ("其他音频", "other"), ("已适配", "adapted")):
            self.setTabData(self.addTab(title), value)

    def currentData(self):
        return self.tabData(self.currentIndex())

    def findData(self, value):
        return next((i for i in range(self.count()) if self.tabData(i) == value), -1)
