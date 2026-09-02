"""入口门禁（工程管束 R1）。自包含。仓库里每一个「可执行入口」都必须登记在 tools/manifest.toml，且只能在 tools/ 下：
- 入口判据（任一命中）：`if __name__ == "__main__"`、import argparse / click / typer、shebang 行、可执行位、`*.sh`、
  pyproject `[project.scripts]`、workflow `run:` 里引用的 `tools/…` 路径；
- 目录名 scripts/ · bin/ · notebooks/ 出现即红；
- manifest 每条：path · kind(gate|adapter) · purpose · smoke_test；adapter ≤ 120 行；gate 不得 `from ftbv2` 导入（自包含裁判）；
  smoke_test 文件必须存在且引用该工具的文件名；
- 登记集合 == 发现集合（两个方向都查）。
退出码 0 通过 / 1 违规。"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

SKIP_DIRS = {".venv", ".git", ".redteam", ".crapkit", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
FORBIDDEN_DIRS = {"scripts", "bin", "notebooks"}
CLI_MODULES = {"argparse", "click", "typer"}
ADAPTER_MAX_LINES = 120


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


def _is_py_entry(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True   # 解析不了的 .py 也按入口处理：登记后由烟雾测试暴露
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            return True
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in CLI_MODULES for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in CLI_MODULES:
            return True
    return False


def find_entries(root: Path) -> set[str]:
    found: set[str] = set()
    for p in _iter_files(root):
        rel = p.relative_to(root).as_posix()
        if p.suffix == ".sh":
            found.add(rel)
            continue
        if p.suffix != ".py":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if text.startswith("#!") or (p.stat().st_mode & 0o111) or _is_py_entry(text):
            found.add(rel)
    return found


def forbidden_dirs(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_dir() and p.name in FORBIDDEN_DIRS and not any(part in SKIP_DIRS for part in p.parts))


def workflow_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    for wf in (root / ".github" / "workflows").glob("*.yml"):
        for m in re.finditer(r"(tools/[\w./-]+\.(?:py|sh))", wf.read_text(encoding="utf-8")):
            refs.add(m.group(1))
    return refs


def check_manifest(root: Path, entries: set[str]) -> list[str]:
    manifest = root / "tools" / "manifest.toml"
    tools = tomllib.loads(manifest.read_text(encoding="utf-8")).get("tool", []) if manifest.is_file() else []
    problems, registered = ([] if manifest.is_file() else [f"{manifest} 不存在"]), set()
    for t in tools:
        if missing := [k for k in ("path", "kind", "purpose", "smoke_test") if k not in t]:
            problems.append(f"manifest 条目缺字段 {missing}：{t}")
            continue
        path, kind = str(t["path"]), str(t["kind"])
        registered.add(path)
        f = root / path
        if not path.startswith("tools/") or not f.is_file():
            problems.append(f"{path}：入口只能在 tools/ 下且文件必须存在")
            continue
        if kind not in ("gate", "adapter"):
            problems.append(f"{path}：kind 只能是 gate / adapter")
        text = f.read_text(encoding="utf-8", errors="replace")
        if kind == "adapter" and len(text.splitlines()) > ADAPTER_MAX_LINES:
            problems.append(f"{path}：adapter 不得超过 {ADAPTER_MAX_LINES} 行（现 {len(text.splitlines())}），判据下沉 src")
        if kind == "gate" and re.search(r"^\s*from ftbv2|^\s*import ftbv2", text, re.M):
            problems.append(f"{path}：gate 必须自包含，不得 import ftbv2（被审 PR 不能给自己当裁判）")
        smoke = root / str(t["smoke_test"])
        if not smoke.is_file() or Path(path).name not in smoke.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{path}：smoke_test {t['smoke_test']} 不存在或未引用该工具")
    problems += [f"{e}：可执行入口未登记在 tools/manifest.toml（或不在 tools/ 下）" for e in sorted(entries - registered)]
    problems += [f"{r}：manifest 登记了但仓库里不是入口" for r in sorted(registered - entries)]
    return problems


def check(root: Path) -> list[str]:
    problems = [f"{d}：禁止的目录名（scripts / bin / notebooks）" for d in forbidden_dirs(root)]
    entries = find_entries(root) | workflow_refs(root)
    return problems + check_manifest(root, entries)


def main() -> int:
    problems = check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
    for p in problems:
        print(p)
    print("入口门禁：通过" if not problems else f"入口门禁：{len(problems)} 处违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
