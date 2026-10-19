"""A process-wide hotkey, so the floating panel can come back while an editor owns focus."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter
from PyQt6.QtWidgets import QApplication

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20

# Ctrl+Alt+Space: unused by the NLEs this panel floats over.
DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT
DEFAULT_KEY = VK_SPACE
DEFAULT_LABEL = "Ctrl+Alt+Space"


class HotkeyFilter(QAbstractNativeEventFilter):
    """Windows delivers WM_HOTKEY to the thread queue; Qt exposes it as a native message."""

    def __init__(self, callback, identity):
        super().__init__()
        self.callback = callback
        self.identity = identity

    def nativeEventFilter(self, event_type, message):
        try:
            kind = bytes(event_type)
        except TypeError:
            kind = event_type
        if kind not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            payload = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError, OSError):
            return False, 0
        if payload.message == WM_HOTKEY and payload.wParam == self.identity:
            self.callback()
            return True, 0
        return False, 0


class GlobalHotkey:
    """Register once for the whole process; a taken combination must not crash the window."""

    def __init__(self, callback, identity=0x534A):
        self.callback = callback
        self.identity = identity
        self.filter = None
        self.registered = False
        self.default = False

    def register(self, modifiers=DEFAULT_MODIFIERS, key=DEFAULT_KEY):
        if sys.platform != "win32" or self.registered:
            return False
        try:
            if not ctypes.windll.user32.RegisterHotKey(None, self.identity, modifiers | MOD_NOREPEAT, key):
                return False
        except (AttributeError, OSError):
            return False
        self.filter = HotkeyFilter(self.callback, self.identity)
        application = QApplication.instance()
        if application is not None:
            application.installNativeEventFilter(self.filter)
        self.registered = True
        return True

    def unregister(self):
        if not self.registered:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, self.identity)
        except (AttributeError, OSError):
            pass
        application = QApplication.instance()
        if application is not None and self.filter is not None:
            application.removeNativeEventFilter(self.filter)
        self.filter, self.registered = None, False
