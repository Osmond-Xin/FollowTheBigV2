"""账本门禁的契约：结构 / 证据边界 / 枚举相等（按 AST）/ 语义 append-only / 观测批次。门禁自包含，测试也只用内联文本。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ftbv2.core.raw.ledger import DefectCode, parse_ledger
from tools.check_ledger import baseline_oid, check_observed, compare, enum_values, validate

CODES = [c.value for c in DefectCode]
ENUM_SRC = "class DefectCode(Enum):\n" + "".join(f'    X{i} = "{c}"\n' for i, c in enumerate(CODES))


def entry(ident: str, code: str, *, days: str = "2024-01-02", status: str = "active", extra: str = "", note: str = "n",
          created: str = "2026-09-01", evidence: str = "e", kind: str = "defect", action: str = "gap") -> str:
    body = f'[[defect]]\nid = "{ident}"\ncode = "{code}"\nkind = "{kind}"\n'
    if days:
        body += f"days = [{days}]\n"
    body += f'status = "{status}"\n{extra}created_at = {created}\nevidence = "{evidence}"\nread_layer_action = "{action}"\nnote = "{note}"\n\n'
    return body


def full_ledger(**overrides: str) -> str:
    return "".join(overrides.get(code) or entry(f"D{i:03d}", code) for i, code in enumerate(CODES, 1))


def v(text: str) -> list[str]:
    return validate(text, Path("."), ENUM_SRC)


def test_real_ledger_validates_and_agrees_with_runtime_parser():
    text = Path("ledger/defects.toml").read_text(encoding="utf-8")
    assert validate(text) == []
    runtime = {d.code for d in parse_ledger(text).entries if d.status in ("active", "pending")}
    assert runtime == enum_values(Path("src/ftbv2/core/raw/ledger.py").read_text(encoding="utf-8"))


def test_enum_must_equal_live_ledger_codes():
    (problem,) = v("".join(entry(f"D{i:03d}", c) for i, c in enumerate(CODES, 1) if c != "seq_empty"))
    assert "只在代码 ['seq_empty']" in problem
    rejected = full_ledger(seq_empty=entry("D002", "seq_empty", status="rejected"))
    assert any("只在代码 ['seq_empty']" in p for p in v(rejected))


def test_structure_errors():
    assert v('[[defect]]\nid = "D001"\ncode = "time_6digit"\n')[0].startswith("D001：缺字段")
    assert "id 重复" in v(full_ledger() + entry("D001", "time_6digit"))[0]
    assert "带时间" in v(full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06T00:00:00Z")))[0]
    assert "必须按天登记" in v(full_ledger(seq_empty=entry("D002", "seq_empty", days="")))[0]
    assert "未知 stream" in v(full_ledger(seq_empty=entry("D002", "seq_empty", extra='stream = "bonds"\n')))[0]


def test_active_shape_requires_decision_ref():
    shape = entry("D002", "seq_empty", kind="shape")
    assert "decision_ref" in v(full_ledger(seq_empty=shape))[0]
    assert v(full_ledger(seq_empty=entry("D002", "seq_empty", kind="shape", extra='decision_ref = "design-log/x.md"\n'))) == []


def test_superseded_by_rejects_self_and_dead_targets():
    self_ref = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D002"\n'))
    assert any("另一条 active / pending" in p for p in v(self_ref))
    dead = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D003"\n'),
                       seq_sparse_dup=entry("D003", "seq_sparse_dup", status="rejected"))
    assert any("另一条 active / pending" in p for p in v(dead))


def test_evidence_sha256_binds_repo_file_within_trust_boundary(tmp_path):
    root = tmp_path
    (root / "docs").mkdir()
    ev = root / "docs" / "ev.md"
    ev.write_text("v1", encoding="utf-8")
    digest = hashlib.sha256(b"v1").hexdigest()
    good = full_ledger(seq_empty=entry("D002", "seq_empty", evidence="docs/ev.md（说明）", extra=f'evidence_sha256 = "{digest}"\n'))
    assert validate(good, root, ENUM_SRC) == []
    ev.write_text("v2", encoding="utf-8")
    assert any("证据被改写" in p for p in validate(good, root, ENUM_SRC))
    outside = full_ledger(seq_empty=entry("D002", "seq_empty", evidence="/etc/hosts", extra=f'evidence_sha256 = "{digest}"\n'))
    assert any("仓库内" in p for p in validate(outside, root, ENUM_SRC))
    escape = full_ledger(seq_empty=entry("D002", "seq_empty", evidence="docs/../../x", extra=f'evidence_sha256 = "{digest}"\n'))
    assert any("仓库内" in p for p in validate(escape, root, ENUM_SRC))
    (root / "src").mkdir()
    (root / "src" / "s.py").write_text("v1", encoding="utf-8")
    wrong_dir = full_ledger(seq_empty=entry("D002", "seq_empty", evidence="src/s.py", extra=f'evidence_sha256 = "{digest}"\n'))
    assert any("仓库内" in p for p in validate(wrong_dir, root, ENUM_SRC))


def test_append_only_forbids_delete_and_immutable_changes():
    old = full_ledger()
    gone = "".join(entry(f"D{i:03d}", c) for i, c in enumerate(CODES, 1) if i != 1)
    assert any("禁止删除" in p for p in compare(old, gone))
    assert any("evidence 不可改" in p for p in compare(old, full_ledger(time_6digit=entry("D001", "time_6digit", evidence="rewritten"))))
    assert any("created_at 不可改" in p for p in compare(old, full_ledger(time_6digit=entry("D001", "time_6digit", created="2026-09-02"))))


def test_days_only_grow_and_note_is_free():
    old = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06"))
    assert compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06, 2024-02-08", note="改了"))) == []
    assert any("只许并集" in p for p in compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-08"))))


def test_scope_cannot_be_narrowed_by_adding_fields():
    structural = entry("D005", "enum_drift", days="", action="none")
    old = full_ledger(enum_drift=structural)
    assert any("结构性条目不可加 days" in p for p in compare(old, full_ledger(enum_drift=entry("D005", "enum_drift", action="none"))))
    single = full_ledger(enum_drift=entry("D005", "enum_drift", days="", action="none", extra='stream = "orders"\n'))
    assert any("stream 作用域不可改" in p for p in compare(old, single))


@pytest.mark.parametrize(("before", "after", "ok"), [
    ("pending", "active", True), ("active", "superseded", True), ("pending", "rejected", True),
    ("pending", "superseded", False), ("active", "pending", False), ("superseded", "active", False), ("rejected", "active", False),
])
def test_status_transitions(before, after, ok):
    def e(status):
        return entry("D002", "seq_empty", status=status, extra='superseded_by = "D001"\n' if status == "superseded" else "")
    assert (compare(full_ledger(seq_empty=e(before)), full_ledger(seq_empty=e(after))) == []) is ok


def test_new_entries_start_only_as_pending_or_active():
    old = full_ledger()
    assert any("新增条目只能是 pending / active" in p for p in compare(old, old + entry("D900", "ghost", status="rejected")))
    buried = old + entry("D901", "ghost2", status="superseded", extra='superseded_by = "D001"\n')
    assert any("新增条目只能是 pending / active" in p for p in compare(old, buried))
    old2 = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D001"\n'))
    assert any("superseded_by 一旦设置不可改" in p for p in compare(old2, full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D003"\n'))))


def _observed(tmp_path: Path, codes: str = '["seq_empty"]', tamper: bool = False) -> Path:
    f = tmp_path / "scan.toml"
    f.write_text(f"codes = {codes}\n", encoding="utf-8")
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    (tmp_path / "manifest.toml").write_text(
        f'[[batch]]\nfile = "scan.toml"\nsha256 = "{digest}"\nscanner = "s"\nscanner_sha256 = "0"\ninput = "i"\n'
        f'input_sha256 = "0"\nscanned_at = 2026-09-02\n', encoding="utf-8")
    if tamper:
        f.write_text('codes = ["seq_empty", "rescue_partial"]\n', encoding="utf-8")
    return tmp_path


def test_observed_batches_are_hash_bound_and_append_only(tmp_path):
    assert "manifest.toml 不存在" in check_observed(full_ledger(), tmp_path)[0]
    d = _observed(tmp_path)
    assert check_observed(full_ledger(), d) == []
    assert any("被改写" in p for p in check_observed(full_ledger(), _observed(tmp_path, tamper=True)))
    d = _observed(tmp_path, codes='["seq_empty", "never_seen", "rescue_partial"]')
    ledger = full_ledger(rescue_partial=entry("D004", "rescue_partial", status="pending"))
    assert any("never_seen" in p and "rescue_partial" in p for p in check_observed(ledger, d))
    d = _observed(tmp_path)
    (d / "extra.toml").write_text('codes = []\n', encoding="utf-8")
    assert any("不在清单批次里" in p for p in check_observed(full_ledger(), d))
    (d / "extra.toml").unlink()
    old_manifest = (d / "manifest.toml").read_text(encoding="utf-8")
    assert check_observed(full_ledger(), d, old_manifest) == []
    (d / "manifest.toml").write_text(old_manifest.replace('scanner = "s"', 'scanner = "t"'), encoding="utf-8")
    assert any("不可改不可删" in p for p in check_observed(full_ledger(), d, old_manifest))
    assert check_observed(Path("ledger/defects.toml").read_text(encoding="utf-8"), Path("ledger/observed")) == []


def test_baseline_never_self_compares():
    oid = baseline_oid(None)
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert oid is not None and oid != head
    assert baseline_oid("abc") == "abc"


def test_gate_never_passes_unsafe_base_and_tool_imports_nothing_from_src():
    assert "--unsafe-base" not in Path("tools/gate.sh").read_text(encoding="utf-8")
    assert "--unsafe-base" not in Path(".github/workflows/gate.yml").read_text(encoding="utf-8")
    assert "from ftbv2" not in Path("tools/check_ledger.py").read_text(encoding="utf-8")
