"""跨模块 import 私有符号检查。

import-linter 是模块粒度，ruff PLC2701 只对第三方包生效——这条约束没有现成工具
（见 docs/design-log/2026-09-01-门禁清单.md §0）。规则：

- `from a.b.c import _x`：`_x` 的属主包是 `a.b`；引用方不在 `a.b` 内即拦。
- `from a.b._secret import y` / `import a.b._secret`：`_secret` 的属主包是 `a.b`，同上。
- 相对 import（level > 0）视为同包，放行。dunder（`__all__`）放行。
- 别名（`as ok`）不逃逸。

用法：python tools/check_private_imports.py src  → 有违规则打印并 exit 1。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _module_name(py: Path, root: Path) -> str:
    rel = py.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _owner_package(module: str, symbol: str | None) -> str | None:
    """返回私有成分的属主包；路径与符号都不含私有成分时返回 None。"""
    parts = module.split(".")
    for i, part in enumerate(parts):
        if _is_private(part):
            return ".".join(parts[:i])
    if symbol is not None and _is_private(symbol):
        return ".".join(parts[:-1])
    return None


def _inside(importer: str, package: str) -> bool:
    return importer == package or importer.startswith(package + ".")


def _violations_in(py: Path, root: Path) -> list[str]:
    importer = _module_name(py, root)
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0 or node.module is None:
                continue
            for alias in node.names:
                owner = _owner_package(node.module, alias.name)
                if owner is not None and not _inside(importer, owner):
                    out.append(f"{py}:{node.lineno}  private `{alias.name}` from `{node.module}`")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                owner = _owner_package(alias.name, None)
                if owner is not None and not _inside(importer, owner):
                    out.append(f"{py}:{node.lineno}  private module `{alias.name}`")
    return out


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "src")
    violations = [v for py in sorted(root.rglob("*.py")) for v in _violations_in(py, root)]
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
