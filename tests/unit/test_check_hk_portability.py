"""Переносимость CLI прогона HK_Datasets: без модуля resource (Windows) он работает (#203)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_windows_memory_api_uses_pointer_sized_handle(monkeypatch):
    import ctypes
    from ctypes import wintypes

    module = load_script(monkeypatch)
    handle = 0x123456789ABC
    process = Mock(return_value=handle)
    memory = Mock()

    def read(actual, counters, size):
        assert process.argtypes == [] and process.restype is wintypes.HANDLE
        assert memory.argtypes[0] is wintypes.HANDLE
        assert memory.argtypes[1]._type_ is type(counters._obj)
        assert memory.argtypes[2] is wintypes.DWORD and memory.restype is wintypes.BOOL
        assert actual == handle and size == ctypes.sizeof(counters._obj)
        counters._obj.PeakWorkingSetSize = 64 * 1024 * 1024
        return True

    memory.side_effect = read
    kernel = SimpleNamespace(GetCurrentProcess=process, K32GetProcessMemoryInfo=memory)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    assert module._windows_peak_mb() == 64.0


def test_memory_probe_failure_does_not_block_the_dataset_check(monkeypatch):
    import ctypes

    module = load_script(monkeypatch)
    kernel = SimpleNamespace(
        GetCurrentProcess=Mock(return_value=0x123456789ABC),
        K32GetProcessMemoryInfo=Mock(side_effect=ctypes.ArgumentError("unsupported probe")),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    assert module._windows_peak_mb() is None
