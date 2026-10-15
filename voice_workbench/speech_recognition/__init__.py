"""Local speech recognition: audio in, timestamped transcript lines out.

Recognition reuses the project's own inference engine (audio.cpp): the ASR family
produces the transcript, the forced-aligner family produces word timestamps. No
external speech stack is involved.
"""
