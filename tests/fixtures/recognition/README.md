Synthetic 0.2-second 440 Hz stereo tones, generated locally at 44.1 kHz.
No speech or user recordings are included. The WAV fixture uses 32-bit float.

Generated once with FFmpeg (a test-fixture tool, not a runtime dependency):

```
ffmpeg -f lavfi -i sine=frequency=440:sample_rate=44100:duration=0.2 -ac 2 -c:a CODEC tone.EXT
```

Codecs: FLAC, libmp3lame, AAC (M4A/ADTS), libvorbis, libopus, wmav2,
pcm_s16be (AIFF/AIF), pcm_f32le (WAV).

`clip.*` carries the same tone inside a video container — 1 second, 160x120, 5 fps —
so dropping a video can be shown to yield a readable WAV. `silent.mp4` is the same
picture with no audio track, to guard the refusal:

```
ffmpeg -f lavfi -i color=c=black:s=160x120:d=1:r=5 -f lavfi -i sine=frequency=440:sample_rate=44100:duration=1 \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 32k -shortest clip.mp4
```

Containers: MP4 (h264 + AAC), MKV (h264 + AAC), MOV (h264 + AAC),
AVI (mpeg4 + MP3), WEBM (VP9 + Opus).
