"""架构门禁的契约：覆盖（src ⊆ 声明）、契约生成与比对、跨模块只从顶层 __all__ import、禁动态 import / sys.path。入口：tools/check_architecture.py。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.check_architecture import BEGIN, END, check, check_coverage, check_imports, compare_declarations, load, render_contracts, write_setup_cfg

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
    assert check(root, baseline={}) == []
    (root / "src/ftbv2/io/rogue").mkdir()
    (root / "src/ftbv2/io/rogue/__init__.py").write_text("", encoding="utf-8")
    assert any("图上没有的模块" in p for p in check(root, baseline={}))
    (root / "src/ftbv2/core/later").mkdir()
    (root / "src/ftbv2/core/later/__init__.py").write_text("", encoding="utf-8")
    assert any("标为 planned 但 src 里已有" in p for p in check_coverage(root, {m["name"]: m for m in load(root)["module"]}))


def test_setup_cfg_must_match_declaration(tmp_path):
    root = _tree(tmp_path)
    cfg = root / "setup.cfg"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("    os\n", ""), encoding="utf-8")
    assert any("不一致" in p for p in check(root, baseline={}))
    cfg.write_text("[metadata]\n", encoding="utf-8")
    assert any("缺少 architecture 生成区块" in p for p in check(root, baseline={}))


def test_cross_module_imports_only_from_top_level_all(tmp_path):
    mods = {m["name"]: m for m in load(_tree(tmp_path))["module"]}
    root = _tree(tmp_path, b_init='from ftbv2.core.a.inner import x\n__all__ = []\n')
    (root / "src/ftbv2/core/a/inner.py").write_text("x = 1\n", encoding="utf-8")
    assert any("只能从 ftbv2.core.a 顶层 import" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='from ftbv2.core.a import hidden\n__all__ = []\n', a_init='__all__ = ["x"]\nx = 1\nhidden = 2\n')
    assert any("不在 ftbv2.core.a 的 __all__ 里" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, a_init="x = 1\n")
    assert any("没有 __all__" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='import importlib as il\nimport sys\np = sys.path\np[0:0] = ["x"]\n__all__ = []\n')
    problems = check_imports(root, mods)
    assert any("importlib" in p for p in problems) and any("sys.path" in p for p in problems)
    root = _tree(tmp_path, b_init='from importlib import import_module\n__all__ = []\n')
    assert any("from importlib" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='import ftbv2.core.a\n__all__ = []\n')
    assert any("跨模块禁止 `import ftbv2…`" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='from ftbv2.core import a\n__all__ = []\n')
    assert any("命名空间根" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='from ...core.a.inner import x\n__all__ = []\n')
    (root / "src/ftbv2/core/a/inner.py").write_text("x = 1\n", encoding="utf-8")
    assert any("只能从 ftbv2.core.a 顶层 import" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, a_init='from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ftbv2.io.b import y\n__all__ = ["x"]\nx = 1\n')
    assert any("未声明依赖 io.b" in p for p in check_imports(root, mods))
    root = _tree(tmp_path, b_init='from ftbv2.io.b.helper import z\n__all__ = ["z"]\n')
    (root / "src/ftbv2/io/b/helper.py").write_text("z = 1\n", encoding="utf-8")
    assert check_imports(root, mods) == []          # 模块内部可以深路径 import


def test_declaration_is_monotonic_against_baseline(tmp_path):
    root = _tree(tmp_path)
    old = load(root)
    import tomllib
    weaker = tomllib.loads(DECL.replace('forbidden = ["ftbv2.io", "os"]', 'forbidden = ["ftbv2.io"]'))
    assert any("只能增不能删" in p for p in compare_declarations(old, weaker, root))
    downgraded = tomllib.loads(DECL.replace('kind = "pure"\ndiagram = "A"', 'kind = "io"\ndiagram = "A"'))
    assert any("降级" in p for p in compare_declarations(old, downgraded, root))
    moved = tomllib.loads(DECL.replace('package = "ftbv2.core.a"', 'package = "ftbv2.core.a2"'))
    assert any("package 不可改" in p for p in compare_declarations(old, moved, root))
    gone = tomllib.loads(DECL.split("[[module]]\nname = \"core.later\"")[0])
    assert any("不得删除" in p for p in compare_declarations(old, gone, root))
    more = tomllib.loads(DECL.replace('depends_on = []\n[[module]]\nname = "io.b"', 'depends_on = []\n[[module]]\nname = "io.b"').replace('depends_on = ["core.a"]', 'depends_on = ["core.a", "core.later"]'))
    assert any("必须带 deps_decision" in p for p in compare_declarations(old, more, root))
    (root / "docs").mkdir(); (root / "docs" / "adr.md").write_text("ok", encoding="utf-8")
    justified = tomllib.loads(DECL.replace('depends_on = ["core.a"]', 'depends_on = ["core.a", "core.later"]\ndeps_decision = "docs/adr.md"'))
    assert compare_declarations(old, justified, root) == []
    assert compare_declarations({}, old, root) == []              # 引导：基线里没有声明文件
