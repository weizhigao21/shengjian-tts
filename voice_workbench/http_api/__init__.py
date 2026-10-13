"""Explicit HTTP router registration; no application starts at import time."""
from . import system, models, voices, jobs, audio, workflow, legacy, audio_editing

ROUTERS = (system.router, models.router, voices.router, jobs.router, audio.router, workflow.router, legacy.router, audio_editing.router)
