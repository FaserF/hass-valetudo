"""Pytest configuration for Valetudo tests."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from unittest.mock import MagicMock

if importlib.util.find_spec("turbojpeg") is None:
    _turbojpeg = ModuleType("turbojpeg")
    _turbojpeg.TurboJPEG = MagicMock()  # type: ignore[attr-defined]
    sys.modules["turbojpeg"] = _turbojpeg
