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

SKIP_DIRS = {".venv", ".git", ".redteam", ".crapkit", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
             "research"}   # research/ 是探索沙箱（2026-09-03 用户裁定不设限），不是工具；进 tools/ 之前先经此门
FORBIDDEN_DIRS = {"scripts", "bin", "notebooks"}
CLI_MODULES = {"argparse", "click", "typer"}
ADAPTER_MAX_LINES = 120
EXEC_FUNCS = {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_output", "subprocess.check_call",
              "os.system", "os.popen", "runpy.run_module", "runpy.run_path", "exec", "eval"}


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


def _dynamic_import_targets(node: ast.AST) -> set[str]:
    """`__import__("x")` / `importlib.import_module("x")` 的字符串参数。"""
    if not isinstance(node, ast.Call):
        return set()
    name = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
    if name not in ("__import__", "import_module"):
        return set()
    return {a.value.split(".")[0] for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}


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
        if _dynamic_import_targets(node) & CLI_MODULES:
            return True
    return False


def gate_touches_src(text: str) -> bool:
    """gate 必须自包含：静态 import、动态 import、以及 subprocess / runpy / os.system 里出现 ftbv2 都算碰了被审代码。"""
    if re.search(r"^\s*(from|import)\s+ftbv2", text, re.M):
        return True
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if "ftbv2" in _dynamic_import_targets(node):
            return True
        if isinstance(node, ast.Call) and ast.unparse(node.func) in EXEC_FUNCS and "ftbv2" in ast.unparse(node.args):
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


RUN_RE = re.compile(r"(?:python3?|uv run python|bash|sh)\s+(?:-m\s+([\w.]+)|([\w./-]+\.(?:py|sh)))")


def workflow_refs(root: Path) -> set[str]:
    """workflow（.yml / .yaml）`run:` 里执行的仓库文件或 `python -m 模块`；含 `${{ }}` 插值的行整行登记为动态入口，必须在 manifest 显式允许。"""
    refs: set[str] = set()
    wf_dir = root / ".github" / "workflows"
    for wf in list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")):
        for line in wf.read_text(encoding="utf-8").splitlines():
            if "${{" in line and RUN_RE.search(line):
                refs.add(f"dynamic:{line.strip()}")
                continue
            for module, path in RUN_RE.findall(line):
                if module:
                    refs.add(f"module:{module}")
                elif path and not path.startswith("/") and not path.startswith("$"):
                    refs.add(path)
    return refs


def project_scripts(root: Path) -> set[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
    return {f"script:{name}={target}" for name, target in scripts.items()}


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
        if path.startswith(("module:", "dynamic:", "script:")):
            continue                                   # 显式允许的非文件入口（模块、动态 run 行、console script）
        f = root / path
        if not path.startswith("tools/") or not f.is_file():
            problems.append(f"{path}：入口只能在 tools/ 下且文件必须存在")
            continue
        if kind not in ("gate", "adapter"):
            problems.append(f"{path}：kind 只能是 gate / adapter")
        text = f.read_text(encoding="utf-8", errors="replace")
        if kind == "adapter" and len(text.splitlines()) > ADAPTER_MAX_LINES:
            problems.append(f"{path}：adapter 不得超过 {ADAPTER_MAX_LINES} 行（现 {len(text.splitlines())}），判据下沉 src")
        if kind == "gate" and path.endswith(".py") and gate_touches_src(text):
            problems.append(f"{path}：gate 必须自包含，不得以任何方式（import / importlib / subprocess）碰 ftbv2（被审 PR 不能给自己当裁判）")
        smoke = root / str(t["smoke_test"])
        smoke_text = smoke.read_text(encoding="utf-8", errors="replace") if smoke.is_file() else ""
        if Path(path).name not in smoke_text or not re.search(r"subprocess\.run|import tools\.|from tools\.|from tools", smoke_text):
            problems.append(f"{path}：smoke_test {t['smoke_test']} 不存在、未引用该工具、或没有真正调用（subprocess / import）")
    problems += [f"{e}：可执行入口未登记在 tools/manifest.toml（或不在 tools/ 下）" for e in sorted(entries - registered)]
    problems += [f"{r}：manifest 登记了但仓库里不是入口" for r in sorted(registered - entries)]
    return problems


def check(root: Path) -> list[str]:
    problems = [f"{d}：禁止的目录名（scripts / bin / notebooks）" for d in forbidden_dirs(root)]
    entries = find_entries(root) | workflow_refs(root) | project_scripts(root)
    return problems + check_manifest(root, entries)


def main() -> int:
    problems = check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
    for p in problems:
        print(p)
    print("入口门禁：通过" if not problems else f"入口门禁：{len(problems)} 处违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
