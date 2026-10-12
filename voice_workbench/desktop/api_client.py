"""Asynchronous desktop HTTP requests and shared response decoding."""
from __future__ import annotations
import httpx
from PyQt6.QtCore import QThread, pyqtSignal


class ApiWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, url, action):
        super().__init__()
        self.url, self.action = url, action

    def run(self):
        try:
            with httpx.Client(base_url=self.url, timeout=5, trust_env=False) as client:
                self.result.emit(self.action(client))
        except Exception as exc:
            self.error.emit(str(exc))


def checked(response):
    if response.is_error:
        try:
            body = response.json()
            error = body.get("error", {})
            details = "\n".join(f"{item['field']}: {item['message']}" for item in body.get("errors", []))
            raise RuntimeError(body.get("msg", response.text) + ("\n" + error["hint"] if error.get("hint") else "") + ("\n" + details if details else ""))
        except ValueError:
            response.raise_for_status()
    return response.json()
