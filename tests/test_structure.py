"""结构冒烟：两半必须都能 import，且纯逻辑核不得携带 IO 依赖。"""

import importlib
import sys


def test_core_imports_without_io():
    sys.modules.pop("ftbv2.io", None)
    importlib.import_module("ftbv2.core")
    assert "ftbv2.io" not in sys.modules


def test_io_imports():
    importlib.import_module("ftbv2.io")
