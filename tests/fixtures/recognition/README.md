Synthetic 0.2-second 440 Hz stereo tones, generated locally at 44.1 kHz.
No speech or user recordings are included. The WAV fixture uses 32-bit float.

Generated once with FFmpeg (a test-fixture tool, not a runtime dependency):

```
ffmpeg -f lavfi -i sine=frequency=440:sample_rate=44100:duration=0.2 -ac 2 -c:a CODEC tone.EXT
```

Codecs: FLAC, libmp3lame, AAC (M4A/ADTS), libvorbis, libopus, wmav2,
pcm_s16be (AIFF/AIF), pcm_f32le (WAV).
