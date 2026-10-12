"""Compatibility entry point for the modular settings page."""
from .desktop.settings import SettingsPanel
from .desktop.settings.common import ModelTable, size_text, preset_text, DOWNLOAD_STATES, ARCHIVE_STATES

__all__ = ["SettingsPanel", "ModelTable", "size_text", "preset_text", "DOWNLOAD_STATES", "ARCHIVE_STATES"]
