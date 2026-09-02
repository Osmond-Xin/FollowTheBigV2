"""架构门禁（工程管束 R5）。自包含。architecture.toml 是「图上没有的模块视为不存在」的那张图：
- src/ftbv2 下每个包 / 顶层模块都必须落在某个声明模块的 package 之下（ftbv2 / ftbv2.core / ftbv2.io 是命名空间根，不算模块）；
- 从声明生成 import-linter 契约（layers · pure 禁 IO · 每模块 depends_on 的 forbidden），与 setup.cfg 的生成区块逐字节比对；
  本地 `--write` 重新生成，CI 只比对；
- src/ftbv2 与 tools 里的跨模块 import 只能 `from <模块顶层包> import 名字` 且名字在该包 __init__ 的 __all__ 里；
  模块内部可以深路径 import；
- 禁 `importlib.import_module` / `__import__` 指向 ftbv2、禁 `sys.path` 篡改。
退出码 0 通过 / 1 违规。"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

BEGIN, END = "# generated:architecture begin（tools/check_architecture.py --write；手改即红）", "# generated:architecture end"
NAMESPACE_ROOTS = {"ftbv2", "ftbv2.core", "ftbv2.io"}


def load(root: Path) -> dict:
    return tomllib.loads((root / "architecture.toml").read_text(encoding="utf-8"))


def _modules(decl: dict) -> dict[str, dict]:
    return {m["name"]: m for m in decl.get("module", [])}


def _dotted(py: Path, src: Path) -> str:
    rel = py.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_of(dotted: str, mods: dict[str, dict]) -> str | None:
    hits = [m for m, d in mods.items() if dotted == d["package"] or dotted.startswith(d["package"] + ".")]
    return max(hits, key=lambda m: len(mods[m]["package"])) if hits else None


def check_coverage(root: Path, mods: dict[str, dict]) -> list[str]:
    src = root / "src"
    problems = []
    for py in sorted((src / "ftbv2").rglob("*.py")):
        dotted = _dotted(py, src)
        if dotted in NAMESPACE_ROOTS:
            continue
        if module_of(dotted, mods) is None:
            problems.append(f"{py.relative_to(root)}：不在 architecture.toml 声明的任何模块之下（图上没有的模块视为不存在）")
    for m, d in mods.items():
        pkg = src / Path(*d["package"].split("."))
        exists = pkg.is_dir() or pkg.with_suffix(".py").is_file()
        if d.get("status", "implemented") == "planned" and exists:
            problems.append(f"architecture.toml：{m} 标为 planned 但 src 里已有 {d['package']}（先改 status = implemented 并补 __all__）")
        if d.get("status", "implemented") == "implemented" and not exists:
            problems.append(f"architecture.toml 声明的模块 {m}（{d['package']}）在 src 里不存在")
        for dep in d.get("depends_on", []):
            if dep not in mods:
                problems.append(f"architecture.toml：{m} 依赖未声明的模块 {dep}")
    return problems


def render_contracts(decl: dict) -> str:
    mods = _modules(decl)
    lines = [BEGIN, "[importlinter]", "root_package = ftbv2", "include_external_packages = True", "exclude_type_checking_imports = True", "",
             "[importlinter:contract:layers]", "name = IO 层在上，纯逻辑核在下；核不得反向依赖", "type = layers", "layers =", "    ftbv2.io", "    ftbv2.core", "",
             "[importlinter:contract:core-no-io]", "name = 纯逻辑核不得 import IO 层与任何 IO 库", "type = forbidden", "source_modules =", "    ftbv2.core",
             "forbidden_modules ="]
    lines += [f"    {f}" for f in decl.get("pure", {}).get("forbidden", [])]
    lines += ["allow_indirect_imports = False", ""]
    for name, d in mods.items():
        if d.get("status", "implemented") != "implemented":
            continue                                   # planned 模块不生成契约（包不存在，import-linter 会报找不到）
        others = sorted(mods[o]["package"] for o in mods if o != name and o not in d.get("depends_on", []) and mods[o].get("status", "implemented") == "implemented")
        if not others:
            continue
        lines += [f"[importlinter:contract:module-{name}]", f"name = 架构声明：{name} 只能依赖 {d.get('depends_on', [])}", "type = forbidden",
                  "source_modules =", f"    {d['package']}", "forbidden_modules ="]
        lines += [f"    {o}" for o in others]
        lines += ["allow_indirect_imports = False", ""]
    lines.append(END)
    return "\n".join(lines) + "\n"


def check_setup_cfg(root: Path, decl: dict) -> list[str]:
    cfg = root / "setup.cfg"
    text = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n", text, re.S)
    if m is None:
        return ["setup.cfg 缺少 architecture 生成区块（先 tools/check_architecture.py --write）"]
    if m.group(0) != render_contracts(decl):
        return ["setup.cfg 的 import-linter 契约与 architecture.toml 不一致（手改了 setup.cfg 或改了声明没重新生成：--write）"]
    return []


def write_setup_cfg(root: Path, decl: dict) -> None:
    cfg = root / "setup.cfg"
    text = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    block = render_contracts(decl)
    pattern = re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n"
    text = re.sub(pattern, lambda _: block, text, flags=re.S) if re.search(pattern, text, re.S) else text.rstrip("\n") + "\n\n" + block
    cfg.write_text(text, encoding="utf-8")


def _exports(root: Path, package: str) -> set[str] | None:
    src = root / "src"
    init = src / Path(*package.split(".")) / "__init__.py"
    file = src / Path(*package.split(".")).with_suffix(".py")
    target = init if init.is_file() else file if file.is_file() else None
    if target is None:
        return None
    for node in ast.walk(ast.parse(target.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    return None


def _from_problem(rel: str, node: ast.ImportFrom, own: str | None, mods: dict[str, dict], exports: dict[str, set[str] | None]) -> list[str]:
    target = module_of(node.module or "", mods)
    if not (node.module or "").startswith("ftbv2") or target is None or target == own:
        return []
    if node.module != mods[target]["package"]:
        return [f"{rel}：跨模块只能从 {mods[target]['package']} 顶层 import（现为 {node.module}）"]
    allowed = exports.get(target)
    if allowed is None:
        return [f"{rel}：模块 {target} 的 __init__ 没有 __all__，无法定义公开接口"]
    return [f"{rel}：{a.name} 不在 {mods[target]['package']} 的 __all__ 里" for a in node.names if a.name not in allowed]


def _plain_import_problems(rel: str, node: ast.Import, own: str | None, mods: dict[str, dict]) -> list[str]:
    out = []
    for a in node.names:
        target = module_of(a.name, mods) if a.name.startswith("ftbv2") else None
        if target is not None and target != own and a.name != mods[target]["package"]:
            out.append(f"{rel}：跨模块只能 import {mods[target]['package']}（现为 {a.name}）")
    return out


def _call_problems(rel: str, node: ast.Call) -> list[str]:
    fn = ast.unparse(node.func)
    out = []
    if fn in ("__import__", "importlib.import_module") and "ftbv2" in ast.unparse(node.args):
        out.append(f"{rel}：禁止动态 import ftbv2（{fn}）")
    if fn in ("sys.path.insert", "sys.path.append", "sys.path.extend"):
        out.append(f"{rel}：禁止篡改 sys.path")
    return out


def _import_problems(py: Path, root: Path, mods: dict[str, dict], exports: dict[str, set[str] | None]) -> list[str]:
    rel = py.relative_to(root).as_posix()
    own = module_of(_dotted(py, root / "src"), mods) if rel.startswith("src/") else None
    problems = []
    for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            problems += _from_problem(rel, node, own, mods, exports)
        elif isinstance(node, ast.Import):
            problems += _plain_import_problems(rel, node, own, mods)
        elif isinstance(node, ast.Call):
            problems += _call_problems(rel, node)
    return problems


def check_imports(root: Path, mods: dict[str, dict]) -> list[str]:
    exports = {m: _exports(root, d["package"]) for m, d in mods.items()}
    files = list((root / "src" / "ftbv2").rglob("*.py")) + list((root / "tools").rglob("*.py"))
    return [p for py in sorted(files) for p in _import_problems(py, root, mods, exports)]


def check(root: Path) -> list[str]:
    decl = load(root)
    mods = _modules(decl)
    return check_coverage(root, mods) + check_setup_cfg(root, decl) + check_imports(root, mods)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--write", action="store_true", help="按 architecture.toml 重新生成 setup.cfg 的契约区块（本地用）")
    args = ap.parse_args()
    root = Path(args.root)
    if args.write:
        write_setup_cfg(root, load(root))
    problems = check(root)
    for p in problems:
        print(p)
    print("架构门禁：通过" if not problems else f"架构门禁：{len(problems)} 处违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
