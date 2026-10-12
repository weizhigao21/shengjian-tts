"""Compatibility exports for existing layout and model-label imports."""
from .desktop.layout.workspace import UiLayout
from .desktop.layout.common import card
from .desktop.model_labels import FAMILIES, model_key, model_label

__all__ = ["UiLayout", "card", "FAMILIES", "model_key", "model_label"]
