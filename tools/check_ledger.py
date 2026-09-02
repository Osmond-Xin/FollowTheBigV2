"""缺陷与形状账本门禁（工程管束 R3）。**自包含**：不从 src 导入解析器或值域——被审 PR 不能给自己当裁判；
代码里的 DefectCode 只作为被校验对象，按 AST 读文本。CI 另用 origin/main 那一版本文件再跑一遍（gate.yml）。

校验：
- 结构：必填字段、值域、id 唯一、stream 值域、日期只接受精确 date（TOML datetime 会让按天匹配永远不命中）、
  patch / gap 必须有 days、形状转 active 必须带 decision_ref、superseded ⇔ superseded_by 且指向另一条 active / pending；
- 证据：evidence（必填）与 decision_ref 都必须是仓库内 docs/ · ledger/ · .lineage/ 下的文件（resolve 后仍在仓库内，防 symlink / .. 越界），
  并带 *_sha256 绑定内容；证据快照放 ledger/evidence/<id>.md，写一次不改；
- 枚举：src/ftbv2/core/raw/ledger.py 里 DefectCode 的值集合 == 账本 active ∪ pending 的 code 集合；
- 语义 append-only（对基线，按 id，不看文本 diff）：禁删；code / kind / created_at / evidence / decision_ref / evidence_sha256 不可改；
  stream 作用域不可改、结构性条目不可加 days；days 只许并集；状态只许 pending→active / pending→rejected / active→superseded；
  superseded_by 不可改；note 不可改（改叙事 = 新条目）；新增条目只能 pending；patch 码 ∈ KNOWN_PATCH_CODES；
- 观测：ledger/observed/manifest.toml 必填，每个批次记文件哈希与扫描器 / 输入哈希（scanner 为 tools/ 路径时核对内容哈希，external: 只登记）；
  所有观测文件都在批次里、文件名只能是 basename、哈希相符；观测 code ⊆ active ∪ pending 登记；基线里已有的批次不可改不可删（只能追加）。

基线：PR 上 = merge-base(HEAD, origin/main)；直接 push 到 main 时 merge-base 就是 HEAD 自己，此时退化为 HEAD^（上一版 main），
绝不自比较。`--unsafe-base` 仅本地调试，gate.sh / workflow 不得出现（tests/tools 有 grep 门禁）。
退出码 0 通过 / 1 违规 / 2 基线不可得（fail-closed）。
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

STREAMS = ("orders", "trades", "xinqing")
KINDS = ("defect", "shape")
STATUSES = ("pending", "active", "rejected", "superseded")
LIVE = ("active", "pending")
ACTIONS = ("patch", "gap", "none")
REQUIRED = ("id", "code", "kind", "status", "created_at", "evidence", "evidence_sha256", "read_layer_action")
IMMUTABLE = ("code", "kind", "created_at", "evidence", "evidence_sha256", "decision_ref", "decision_sha256", "note", "read_layer_action")
TRANSITIONS = {("pending", "active"), ("active", "superseded"), ("pending", "rejected")}
INITIAL = ("pending",)              # 新增条目只能 pending：转 active 必须是后续 PR，带裁决引用
EVIDENCE_ROOTS = ("docs", "ledger", ".lineage")
ENUM_SOURCE = Path("src/ftbv2/core/raw/ledger.py")
PATCH_SOURCE = Path("src/ftbv2/io/raw/store.py")   # HANDLED_PATCHES 按 AST 从这里读：新处理器与新补丁码同 PR 落地，基线版门禁不会卡住
ZERO64 = "0" * 64
BATCH_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.toml$")
ALL_STREAMS, STRUCTURAL = "<all streams>", "<structural: all days>"


# ----------------------------------------------------------------- 结构


def _rows(text: str) -> list[dict]:
    return list(tomllib.loads(text).get("defect", []))


def _bad_date(value: object) -> str | None:
    if isinstance(value, dt.datetime):
        return f"带时间的 {value!r}（必须是 YYYY-MM-DD）"
    if isinstance(value, dt.date):
        return None
    try:
        return None if len(str(value)) == 10 and dt.date.fromisoformat(str(value)) else f"{value!r} 不是 YYYY-MM-DD"
    except ValueError:
        return f"{value!r} 不是 YYYY-MM-DD"


def _domain_problems(ident: str, row: dict, patch_codes: set[str]) -> list[str]:
    out = []
    kind, status, action = row["kind"], row["status"], row["read_layer_action"]
    if kind not in KINDS or status not in STATUSES or action not in ACTIONS:
        out.append(f"{ident}：kind/status/read_layer_action 不在值域：{kind}/{status}/{action}")
    if "stream" in row and row["stream"] not in STREAMS:
        out.append(f"{ident}：未知 stream {row['stream']!r}")
    for value in [row["created_at"], *row.get("days", [])]:
        if (why := _bad_date(value)) is not None:
            out.append(f"{ident}：日期 {why}")
    if action in ("patch", "gap") and not row.get("days"):
        out.append(f"{ident}：read_layer_action = {action} 必须按天登记（非空 days）")
    if action == "patch" and row["code"] not in patch_codes:
        out.append(f"{ident}：read_layer_action = patch 的 code 必须有读取层处理器（store.HANDLED_PATCHES），{row['code']} 没有")
    return out


def _lifecycle_problems(ident: str, row: dict, live_ids: set[str]) -> list[str]:  # noqa: ARG001 — 链检查在 _chain_problems
    out = []
    if row["kind"] == "shape" and row["status"] == "active" and not row.get("decision_ref"):
        out.append(f"{ident}：形状转 active 必须带 decision_ref")
    if ("decision_ref" in row) != ("decision_sha256" in row):
        out.append(f"{ident}：decision_ref 与 decision_sha256 必须同时出现")
    if (row["status"] == "superseded") != ("superseded_by" in row):
        out.append(f"{ident}：superseded 与 superseded_by 必须同时出现")
    if row["status"] in LIVE and "decision_ref" in row and str(row["decision_ref"]) == str(row["evidence"]):
        out.append(f"{ident}：decision_ref 不得与 evidence 是同一文件（裁决不能自证；历史 rejected / superseded 条目不再受此约束）")
    return out


def _chain_problems(rows: list[dict]) -> list[str]:
    """superseded_by 链：每跳指向存在且非 rejected 的另一条，允许 ≥ 2 层；终点必须 active / pending；无环。"""
    by_id = {str(r["id"]): r for r in rows if "id" in r}
    problems = []
    for r in rows:
        ident, cur, seen = str(r.get("id", "?")), r, {str(r.get("id"))}
        while cur.get("superseded_by") is not None:
            nxt = by_id.get(str(cur["superseded_by"]))
            if nxt is None or str(nxt["id"]) in seen or nxt.get("status") == "rejected":
                problems.append(f"{ident}：superseded_by 链断裂或成环（在 {cur.get('id')} → {cur['superseded_by']}）")
                break
            seen.add(str(nxt["id"]))
            cur = nxt
        else:
            if cur is not r and cur.get("status") not in LIVE:
                problems.append(f"{ident}：superseded_by 链终点 {cur.get('id')} 不是 active / pending")
    return problems


def handled_patches(source_text: str) -> set[str]:
    """从 store.py 文本按 AST 取 HANDLED_PATCHES = frozenset({...}) 的字符串集合。"""
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "HANDLED_PATCHES" for t in node.targets):
            return {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    return set()


def _row_problems(row: dict, live_ids: set[str], patch_codes: set[str]) -> list[str]:
    ident = str(row.get("id", "?"))
    missing = [k for k in REQUIRED if k not in row]
    if missing:
        return [f"{ident}：缺字段 {missing}"]
    return _domain_problems(ident, row, patch_codes) + _lifecycle_problems(ident, row, live_ids)


def _bound_file(ident: str, label: str, raw: str, digest: object, root: Path) -> list[str]:
    """仓库内 docs/ · ledger/ · .lineage/ 下的相对路径 + 内容哈希绑定；resolve 后仍须在仓库内（防 symlink / .. 越界）。"""
    if not (isinstance(digest, str) and len(digest) == 64):
        return [f"{ident}：{label}_sha256 必须是 64 位十六进制"]
    root = root.resolve()
    target = (root / raw).resolve()
    if Path(raw).is_absolute() or not target.is_relative_to(root) or not any(
        target.is_relative_to(root / r) for r in EVIDENCE_ROOTS
    ):
        return [f"{ident}：{label} 必须是仓库内 {'/'.join(EVIDENCE_ROOTS)} 下的相对路径（{raw}）"]
    if not target.is_file():
        return [f"{ident}：{label} 文件不存在 {raw}"]
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        return [f"{ident}：{label} 文件内容与 {label}_sha256 不符（被改写）"]
    return []


def _evidence_problems(row: dict, root: Path) -> list[str]:
    problems = _bound_file(row["id"], "evidence", str(row["evidence"]), row.get("evidence_sha256"), root)
    if "decision_ref" in row:
        problems += _bound_file(row["id"], "decision_ref", str(row["decision_ref"]), row.get("decision_sha256"), root)
    return problems


def enum_values(source_text: str) -> set[str]:
    """从 ledger.py 文本按 AST 取 DefectCode 的值集合，不 import 被审代码。"""
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.ClassDef) and node.name == "DefectCode":
            return {
                stmt.value.value for stmt in node.body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
            }
    return set()


def validate(text: str, root: Path = Path("."), enum_source: str | None = None, patch_source: str | None = None) -> list[str]:
    try:
        rows = _rows(text)
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        return [f"账本解析失败：{exc}"]
    ids = [str(r["id"]) for r in rows if "id" in r]
    problems = [f"账本 id 重复：{i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    live_ids = {str(r["id"]) for r in rows if r.get("status") in LIVE and "id" in r}
    patch_codes = handled_patches(patch_source if patch_source is not None else (root / PATCH_SOURCE).read_text(encoding="utf-8"))
    for row in rows:
        row_problems = _row_problems(row, live_ids, patch_codes)
        problems += row_problems or _evidence_problems(row, root)
    problems += _chain_problems(rows)
    if problems:
        return problems
    if enum_source is None:
        enum_source = (root / ENUM_SOURCE).read_text(encoding="utf-8")
    enum, live = enum_values(enum_source), {str(r["code"]) for r in rows if r["status"] in LIVE}
    if enum != live:
        problems.append(f"代码枚举必须等于账本 active ∪ pending 的 code 集合：只在代码 {sorted(enum - live)}，只在账本 {sorted(live - enum)}")
    return problems


# ----------------------------------------------------------------- 语义 append-only


def _norm(value: object) -> object:
    return value.isoformat() if type(value) is dt.date else value


def _days(row: dict) -> set[object]:
    return {_norm(d) for d in row.get("days", [])}


def _scope(row: dict) -> tuple[object, object]:
    return row.get("stream", ALL_STREAMS), STRUCTURAL if "days" not in row else None


def _entry_changes(ident: str, before: dict, after: dict) -> list[str]:
    problems = []
    for field_name in IMMUTABLE:
        if field_name in before and _norm(before[field_name]) != _norm(after.get(field_name)):
            problems.append(f"{ident}：{field_name} 不可改（{before[field_name]!r} → {after.get(field_name)!r}）")
    if _scope(before)[0] != _scope(after)[0]:
        problems.append(f"{ident}：stream 作用域不可改（{_scope(before)[0]} → {_scope(after)[0]}）")
    if _scope(before)[1] == STRUCTURAL and _scope(after)[1] != STRUCTURAL:
        problems.append(f"{ident}：结构性条目不可加 days 缩窄成按天")
    if not _days(before) <= _days(after):
        problems.append(f"{ident}：days 只许并集，不许删天")
    b_status, a_status = before.get("status", "active"), after.get("status", "active")
    if b_status != a_status and (b_status, a_status) not in TRANSITIONS:
        problems.append(f"{ident}：状态只许 pending→active / pending→rejected / active→superseded（{b_status} → {a_status}）")
    if "superseded_by" in before and before["superseded_by"] != after.get("superseded_by"):
        problems.append(f"{ident}：superseded_by 一旦设置不可改")
    return problems


def compare(old_text: str, new_text: str) -> list[str]:
    """old 是基线，new 是当前；基线里没有的元数据字段不比（结构迁移只能加字段），作用域字段按语义比。"""
    old = {str(r["id"]): r for r in _rows(old_text) if "id" in r}
    new = {str(r["id"]): r for r in _rows(new_text) if "id" in r}
    problems = [
        f"{ident}：新增条目只能是 pending（转 active 是后续 PR 的裁决），现为 {after.get('status')}"
        for ident, after in new.items() if ident not in old and after.get("status", "active") not in INITIAL
    ]
    for ident, before in old.items():
        after = new.get(ident)
        problems += [f"{ident}：账本 append-only，禁止删除条目"] if after is None else _entry_changes(ident, before, after)
    return problems


# ----------------------------------------------------------------- 观测集合

BATCH_FIELDS = ("file", "sha256", "scanner", "scanner_sha256", "input", "input_sha256", "scanned_at")


def _scanner_problem(b: dict, observed_dir: Path, is_new: bool = True) -> str | None:
    scanner = str(b["scanner"])
    if scanner.startswith("tools/"):
        sp = observed_dir.parent.parent / scanner
        if not sp.is_file() or hashlib.sha256(sp.read_bytes()).hexdigest() != b["scanner_sha256"]:
            return f"{observed_dir / 'manifest.toml'}：scanner {scanner} 不存在或内容与 scanner_sha256 不符"
        return None
    if not scanner.startswith("external:"):
        return f"{observed_dir / 'manifest.toml'}：scanner 必须是 tools/ 下的路径或 external:（{scanner}）"
    if is_new and (str(b["scanner_sha256"]) == ZERO64 or len(str(b["scanner_sha256"])) != 64):
        return f"{observed_dir / 'manifest.toml'}：external 扫描器也必须给出真实的 scanner_sha256（{b['file']}）"   # 基线里既有的批次不可改，规则只对新批次生效
    return None


def _check_batch(b: dict, observed_dir: Path, registered: set[str], is_new: bool = True) -> list[str]:
    if missing := [k for k in BATCH_FIELDS if k not in b]:
        return [f"{observed_dir / 'manifest.toml'}：批次缺字段 {missing}"]
    if not BATCH_FILE_RE.match(str(b["file"])):
        return [f"{observed_dir / 'manifest.toml'}：批次文件名只能是 basename（{b['file']}）"]
    if (why := _scanner_problem(b, observed_dir, is_new)) is not None:
        return [why]
    f = observed_dir / b["file"]
    if not f.is_file():
        return [f"{observed_dir / 'manifest.toml'}：批次文件不存在 {b['file']}"]
    problems = []
    if hashlib.sha256(f.read_bytes()).hexdigest() != b["sha256"]:
        problems.append(f"{f}：内容与清单 sha256 不符（观测文件被改写）")
    try:
        codes = set(tomllib.loads(f.read_text(encoding="utf-8")).get("codes", []))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [*problems, f"{f}：观测文件不可解析（{exc}），fail-closed"]
    if unknown := sorted(codes - registered):
        problems.append(f"{f}：观测到未登记的 code {unknown}")
    return problems


def _load_batches(manifest: Path) -> list[dict] | str:
    if not manifest.is_file():
        return f"{manifest} 不存在：观测集合门禁不可选，fail-closed"
    try:
        return list(tomllib.loads(manifest.read_text(encoding="utf-8")).get("batch", []))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return f"{manifest} 不可解析（{exc}），fail-closed"


def check_observed(ledger_text: str, observed_dir: Path, old_manifest: str | None = None) -> list[str]:
    manifest = observed_dir / "manifest.toml"
    batches = _load_batches(manifest)
    if isinstance(batches, str):
        return [batches]
    registered = {str(r["code"]) for r in _rows(ledger_text) if r.get("status") in LIVE}   # 观测先于裁决：pending 也算已登记
    old_files = {b.get("file") for b in tomllib.loads(old_manifest).get("batch", [])} if old_manifest else set()
    problems = [p for b in batches for p in _check_batch(b, observed_dir, registered, b.get("file") not in old_files)]
    listed = {b.get("file") for b in batches}
    problems += [f"{f}：观测文件不在清单批次里" for f in sorted(observed_dir.glob("*.toml"))
                 if f.name != "manifest.toml" and f.name not in listed]
    if old_manifest is not None:
        now = {b["file"]: b for b in batches if "file" in b}
        for name, b in {b["file"]: b for b in tomllib.loads(old_manifest).get("batch", []) if "file" in b}.items():
            if now.get(name) != b:
                problems.append(f"{manifest}：基线里的批次 {name} 不可改不可删（只能追加新批次）")
    return problems


# ----------------------------------------------------------------- CLI


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def baseline_oid(unsafe_base: str | None, path: str = "ledger/defects.toml") -> str | None:
    """基线：merge-base(HEAD, origin/main)。当 HEAD 就在 origin/main 上（merge-base == HEAD）分两种情况：
    工作区账本相对 HEAD 有改动（本地未提交的分支工作）⇒ 基线就是 HEAD；工作区干净（CI 的 push 到 main 事件）⇒ 退化为 HEAD^，绝不自比较。"""
    if unsafe_base is not None:
        return unsafe_base
    head, mb = _git("rev-parse", "HEAD"), _git("merge-base", "HEAD", "origin/main")
    if head is None or mb is None:
        return None
    if mb != head:
        return mb
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", path], capture_output=True).returncode != 0
    return head if dirty else _git("rev-parse", "HEAD^")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="ledger/defects.toml")
    ap.add_argument("--unsafe-base", default=None, help="仅本地调试：覆盖基线 ref。CI / gate.sh 不得使用")
    args = ap.parse_args()
    path = Path(args.path)
    text = path.read_text(encoding="utf-8")
    problems = validate(text)
    if not problems:
        oid = baseline_oid(args.unsafe_base, args.path)
        old = _git("show", f"{oid}:{args.path}") if oid else None
        if oid is None or old is None:
            print("账本基线不可得，门禁 fail-closed", file=sys.stderr)
            return 2
        print(f"账本基线：{oid[:12]}" + ("（--unsafe-base，仅调试）" if args.unsafe_base else ""))
        old_manifest = _git("show", f"{oid}:{path.parent / 'observed' / 'manifest.toml'}")
        problems += compare(old, text)
        problems += check_observed(text, path.parent / "observed", old_manifest)
    for p in problems:
        print(p)
    print("账本门禁：通过" if not problems else f"账本门禁：{len(problems)} 处违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
