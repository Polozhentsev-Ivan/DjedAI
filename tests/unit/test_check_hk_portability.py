"""Переносимость CLI прогона HK_Datasets: без модуля resource (Windows) он работает (#203)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/check_hk_datasets.py"


def load_script(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("check_hk_datasets_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_imports_and_reports_without_resource(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)  # как в CPython для Windows
    module = load_script(monkeypatch)
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.peak_rss_mb() is None
    assert module.mb(None) == "—"


def test_windows_memory_falls_back_to_none_without_win32_api(monkeypatch):
    module = load_script(monkeypatch)
    real_platform = sys.platform
    monkeypatch.setattr(module.sys, "platform", "win32")
    if real_platform != "win32":
        assert module.peak_rss_mb() is None  # ctypes.WinDLL недоступен — замер не мешает прогону
    else:
        assert module.peak_rss_mb() > 0


@pytest.mark.skipif(sys.platform == "win32", reason="resource есть только на Unix")
def test_unix_memory_is_measured(monkeypatch):
    module = load_script(monkeypatch)
    assert module.peak_rss_mb() > 0
