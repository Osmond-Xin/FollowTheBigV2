"""架构门禁的契约：覆盖（src ⊆ 声明）、契约生成与比对、跨模块只从顶层 __all__ import、禁动态 import / sys.path。入口：tools/check_architecture.py。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.check_architecture import BEGIN, END, check, check_coverage, check_imports, load, render_contracts, write_setup_cfg

ROOT = Path(__file__).resolve().parents[2]
DECL = '''[pure]
forbidden = ["ftbv2.io", "os"]
[[module]]
name = "core.a"
status = "implemented"
package = "ftbv2.core.a"
kind = "pure"
diagram = "A"
depends_on = []
[[module]]
name = "io.b"
status = "implemented"
package = "ftbv2.io.b"
kind = "io"
diagram = "B"
depends_on = ["core.a"]
[[module]]
name = "core.later"
status = "planned"
package = "ftbv2.core.later"
kind = "pure"
diagram = "later"
depends_on = []
'''


def _tree(tmp_path: Path, *, b_init: str = 'from ftbv2.core.a import x\n__all__ = ["y"]\ny = x\n', a_init: str = '__all__ = ["x"]\nx = 1\n') -> Path:
    for pkg in ("ftbv2", "ftbv2/core", "ftbv2/io", "ftbv2/core/a", "ftbv2/io/b"):
        (tmp_path / "src" / pkg).mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src/ftbv2/core/a/__init__.py").write_text(a_init, encoding="utf-8")
    (tmp_path / "src/ftbv2/io/b/__init__.py").write_text(b_init, encoding="utf-8")
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "architecture.toml").write_text(DECL, encoding="utf-8")
    write_setup_cfg(tmp_path, load(tmp_path))
    return tmp_path


def test_real_repo_passes_and_generated_block_matches():
    assert check(ROOT) == []
    text = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert BEGIN in text and END in text and render_contracts(load(ROOT)) in text
    r = subprocess.run([sys.executable, "tools/check_architecture.py"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout


def test_undeclared_package_and_planned_package_present(tmp_path):
    root = _tree(tmp_path)
    assert check(root) == []
    (root / "src/ftbv2/io/rogue").mkdir()
    (root / "src/ftbv2/io/rogue/__init__.py").write_text("", encoding="utf-8")
    assert any("图上没有的模块" in p for p in check(root))
    (root / "src/ftbv2/core/later").mkdir()
    (root / "src/ftbv2/core/later/__init__.py").write_text("", encoding="utf-8")
    assert any("标为 planned 但 src 里已有" in p for p in check_coverage(root, {m["name"]: m for m in load(root)["module"]}))


def test_setup_cfg_must_match_declaration(tmp_path):
    root = _tree(tmp_path)
    cfg = root / "setup.cfg"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("    os\n", ""), encoding="utf-8")
    assert any("不一致" in p for p in check(root))
    cfg.write_text("[metadata]\n", encoding="utf-8")
    assert any("缺少 architecture 生成区块" in p for p in check(root))


def test_cross_module_imports_only_from_top_level_all(tmp_path):
    mods = {m["name"]: m for m in load(_tree(tmp_path))["module"]}
    root = _tree(tmp_path, b_init='from ftbv2.core.a.inner import x\n__all__ = []\n')
    (root / "src/ftbv2/core/a/inner.py").write_text("x = 1\n", encoding="utf-8")
    assert any("只能从 ftbv2.core.a 顶层 import" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='from ftbv2.core.a import hidden\n__all__ = []\n', a_init='__all__ = ["x"]\nx = 1\nhidden = 2\n')
    assert any("不在 ftbv2.core.a 的 __all__ 里" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, a_init="x = 1\n")
    assert any("没有 __all__" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='import importlib, sys\nsys.path.insert(0, "x")\nm = importlib.import_module("ftbv2.core.a")\n__all__ = []\n')
    problems = check_imports(root, mods)
    assert any("动态 import" in p for p in problems) and any("sys.path" in p for p in problems)
    root = _tree(tmp_path, b_init='from ftbv2.io.b.helper import z\n__all__ = ["z"]\n')
    (root / "src/ftbv2/io/b/helper.py").write_text("z = 1\n", encoding="utf-8")
    assert check_imports(root, mods) == []          # 模块内部可以深路径 import
