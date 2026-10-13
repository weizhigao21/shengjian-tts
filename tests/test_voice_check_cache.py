from voice_workbench.desktop.voice_checks import VoiceCheckCache


def test_same_voice_reuses_validation_but_file_or_parameter_changes_recheck(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"original")
    voice = {"backend": "audio_cpp", "reference_audio": str(reference), "language": "zh"}
    cache, calls = VoiceCheckCache(), []
    check = lambda v: calls.append(dict(v))
    for _ in range(100):
        assert cache.problem(voice, check) is None
    assert len(calls) == 1
    reference.write_bytes(b"changed length")
    cache.problem(voice, check)
    cache.problem({**voice, "language": "en"}, check)
    reference.unlink()
    cache.problem(voice, check)
    assert len(calls) == 4


def test_failure_is_cached_only_until_ttl_and_entries_are_bounded(monkeypatch):
    time = [0]
    monkeypatch.setattr("voice_workbench.desktop.voice_checks.monotonic", lambda: time[0])
    cache, calls = VoiceCheckCache(ttl=2, limit=2), []
    def check(voice):
        calls.append(voice)
        raise ValueError("参考声音不存在")
    assert cache.problem({"id": "a"}, check) == "参考声音不存在"
    cache.problem({"id": "a"}, check)
    assert len(calls) == 1
    time[0] = 3
    cache.problem({"id": "a"}, check)
    cache.problem({"id": "b"}, check)
    cache.problem({"id": "c"}, check)
    assert len(calls) == 4 and len(cache.results) == 2
