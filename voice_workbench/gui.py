"""Compatibility entry point for desktop launch and existing integrations."""
from .desktop.window import Window, configure_font, configure_language, launch, QTimer, subprocess
from .desktop.api_client import ApiWorker, checked
from .desktop.constants import STATES, BACKENDS

__all__ = ["Window", "ApiWorker", "checked", "configure_font", "configure_language", "launch", "STATES", "BACKENDS"]
