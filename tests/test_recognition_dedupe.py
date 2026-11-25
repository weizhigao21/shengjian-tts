"""Guard against a decoder loop turning into dozens of identical subtitle lines.

A real session repeated ``oh`` 257 times over 45 seconds of music and the aligner
faithfully built twenty 2-second lines from it. These tests pin the cleanup that
runs between recognition and line export.
"""
from voice_workbench.speech_recognition.dedupe import (dedupe_lines, dedupe_text,
                                                       dedupe_transcript, dedupe_units)


def _oh_units(count=12):
    return [("oh,", index * 0.17, (index + 1) * 0.17) for index in range(count)]


def test_a_run_of_repeated_words_keeps_only_the_first():
    units = [("Before.", 0.0, 0.4)] + _oh_units() + [("After.", 2.1, 2.6)]
    cleaned, removed = dedupe_units(units)
    assert removed == 11
    assert [word for word, _, _ in cleaned] == ["Before.", "oh,", "After."]


def test_a_short_repeat_is_left_alone():
    units = [("no", index * 0.2, (index + 1) * 0.2) for index in range(5)] + [("stop.", 1.2, 1.6)]
    cleaned, removed = dedupe_units(units)
    assert removed == 0 and len(cleaned) == 6


def test_raw_text_compresses_a_loop_and_keeps_one_punctuation_mark():
    text = "Before. " + "oh, " * 40 + "After."
    cleaned, removed = dedupe_text(text)
    assert removed == 39
    assert cleaned.lower().count("oh") == 1
    assert cleaned.startswith("Before.") and cleaned.endswith("After.")


def test_cjk_repetition_is_compressed_too():
    cleaned, removed = dedupe_text("啊，" * 20 + "结束。")
    assert removed == 19
    assert cleaned.count("啊") == 1 and cleaned.endswith("结束。")


def test_non_consecutive_repetition_is_not_touched():
    text = "Bambi sleep. Bambi sleep. Bambi sleep."
    cleaned, removed = dedupe_text(text)
    assert removed == 0 and cleaned == text


def test_units_and_text_stay_consistent_for_line_building():
    units = [("Before.", 0.0, 0.4)] + _oh_units(40) + [("After.", 7.0, 7.4)]
    text = "Before. " + "oh, " * 40 + "After."
    cleaned_text, cleaned_units, removed = dedupe_transcript(text, units)
    assert removed == 39
    assert len(cleaned_units) == 3
    assert cleaned_text.lower().count("oh") == 1


def test_estimated_paragraphs_are_compressed_at_the_line_level():
    loop = "oh, " * 12
    lines = [{"index": index + 1, "start": index * 2.0, "end": (index + 1) * 2.0,
              "text": loop.strip() if index == 0 else loop.strip()}
             for index in range(20)]
    cleaned, removed = dedupe_lines(lines)
    assert len(cleaned) == 1
    assert cleaned[0]["text"].lower().count("oh") == 1
    assert cleaned[0]["index"] == 1
    assert removed == 19 + 20 * 11


def test_a_short_adjacent_repeat_is_kept():
    lines = [{"index": index + 1, "start": index, "end": index + 1, "text": "no."}
             for index in range(2)]
    cleaned, removed = dedupe_lines(lines)
    assert len(cleaned) == 2 and removed == 0
