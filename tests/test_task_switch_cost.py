"""The pieces a task switch repeats must cost nothing when nothing changed."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QComboBox

from voice_workbench.model_controls import populate
from voice_workbench.ui import icon


def test_the_same_glyph_is_only_rasterised_once(qtbot):
    """A switch asked for the same play and trash icons hundreds of times."""
    first = icon("play", "#ffffff")
    assert icon("play", "#ffffff") is first
    assert icon("play", "#18181a") is not first
    assert icon("trash", "#ffffff") is not first


def test_an_unchanged_picker_is_left_alone(qtbot):
    combo = QComboBox()
    qtbot.addWidget(combo)
    entries = [("a", "甲"), ("b", "乙")]
    populate(combo, entries, "b")
    rebuilt, clear = [], combo.clear
    combo.clear = lambda: (rebuilt.append(1), clear())[1]
    populate(combo, list(entries), "b")
    assert rebuilt == [] and combo.currentData() == "b"
    populate(combo, entries, "a")
    assert len(rebuilt) == 1 and combo.currentData() == "a"
    populate(combo, [("a", "甲")], "a")
    assert len(rebuilt) == 2 and combo.count() == 1
