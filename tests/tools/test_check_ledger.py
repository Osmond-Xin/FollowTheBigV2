"""账本门禁的契约：结构 / 证据与裁决绑定 / 枚举相等（按 AST）/ 语义 append-only / 观测批次。门禁自包含，测试只用内联文本 + 临时仓库根。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from ftbv2.core.raw.ledger import DefectCode, parse_ledger
from ftbv2.io.raw.store import HANDLED_PATCHES
from tools.check_ledger import KNOWN_PATCH_CODES, baseline_oid, check_observed, compare, enum_values, validate

CODES = [c.value for c in DefectCode]
ENUM_SRC = "class DefectCode(Enum):\n" + "".join(f'    X{i} = "{c}"\n' for i, c in enumerate(CODES))
EV = "docs/ev.md"
SHA = hashlib.sha256(b"v1").hexdigest()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ev.md").write_text("v1", encoding="utf-8")
    return tmp_path


def entry(ident: str, code: str, *, days: str = "2024-01-02", status: str = "active", extra: str = "", note: str = "n",
          created: str = "2026-09-01", evidence: str = EV, sha: str = SHA, kind: str = "defect", action: str = "gap") -> str:
    body = f'[[defect]]\nid = "{ident}"\ncode = "{code}"\nkind = "{kind}"\n'
    if days:
        body += f"days = [{days}]\n"
    body += (f'status = "{status}"\n{extra}created_at = {created}\nevidence = "{evidence}"\nevidence_sha256 = "{sha}"\n'
             f'read_layer_action = "{action}"\nnote = "{note}"\n\n')
    return body


def full_ledger(**overrides: str) -> str:
    return "".join(overrides.get(code) or entry(f"D{i:03d}", code, action="patch" if code == "time_6digit" else "gap")
                   for i, code in enumerate(CODES, 1))


def test_real_ledger_validates_and_agrees_with_runtime_parser():
    text = Path("ledger/defects.toml").read_text(encoding="utf-8")
    assert validate(text) == []
    runtime = {d.code for d in parse_ledger(text).entries if d.status in ("active", "pending")}
    assert runtime == enum_values(Path("src/ftbv2/core/raw/ledger.py").read_text(encoding="utf-8"))
    assert set(KNOWN_PATCH_CODES) == HANDLED_PATCHES


def test_enum_must_equal_live_ledger_codes(root):
    text = "".join(entry(f"D{i:03d}", c) for i, c in enumerate(CODES, 1) if c not in ("seq_empty", "time_6digit"))
    assert any("只在代码 ['seq_empty', 'time_6digit']" in p for p in validate(text, root, ENUM_SRC))
    rejected = full_ledger(seq_empty=entry("D002", "seq_empty", status="rejected"))
    assert any("只在代码 ['seq_empty']" in p for p in validate(rejected, root, ENUM_SRC))


def test_structure_errors(root):
    v = lambda t: validate(t, root, ENUM_SRC)  # noqa: E731
    assert v('[[defect]]\nid = "D001"\ncode = "time_6digit"\n')[0].startswith("D001：缺字段")
    assert "id 重复" in v(full_ledger() + entry("D001", "time_6digit"))[0]
    assert "带时间" in v(full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06T00:00:00Z")))[0]
    assert "必须按天登记" in v(full_ledger(seq_empty=entry("D002", "seq_empty", days="")))[0]
    assert "未知 stream" in v(full_ledger(seq_empty=entry("D002", "seq_empty", extra='stream = "bonds"\n')))[0]
    assert "KNOWN_PATCH_CODES" in v(full_ledger(seq_empty=entry("D002", "seq_empty", action="patch")))[0]
    assert "同时出现" in v(full_ledger(seq_empty=entry("D002", "seq_empty", extra=f'decision_ref = "{EV}"\n')))[0]


def test_active_shape_requires_bound_decision(root):
    shape = entry("D002", "seq_empty", kind="shape")
    assert "decision_ref" in validate(full_ledger(seq_empty=shape), root, ENUM_SRC)[0]
    ok = entry("D002", "seq_empty", kind="shape", extra=f'decision_ref = "{EV}"\ndecision_sha256 = "{SHA}"\n')
    assert validate(full_ledger(seq_empty=ok), root, ENUM_SRC) == []
    stale = entry("D002", "seq_empty", kind="shape", extra=f'decision_ref = "{EV}"\ndecision_sha256 = "{"0" * 64}"\n')
    assert any("decision_ref 文件内容" in p for p in validate(full_ledger(seq_empty=stale), root, ENUM_SRC))


def test_superseded_by_rejects_self_and_dead_targets(root):
    self_ref = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D002"\n'))
    assert any("另一条 active / pending" in p for p in validate(self_ref, root, ENUM_SRC))
    dead = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D003"\n'),
                       seq_sparse_dup=entry("D003", "seq_sparse_dup", status="rejected"))
    assert any("另一条 active / pending" in p for p in validate(dead, root, ENUM_SRC))


def test_evidence_is_mandatory_and_bound_within_trust_boundary(root):
    (root / "docs" / "ev.md").write_text("v2", encoding="utf-8")
    assert any("被改写" in p for p in validate(full_ledger(), root, ENUM_SRC))
    (root / "docs" / "ev.md").write_text("v1", encoding="utf-8")
    for bad in ("/etc/hosts", "docs/../../x", "src/s.py"):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "s.py").write_text("v1", encoding="utf-8")
        text = full_ledger(seq_empty=entry("D002", "seq_empty", evidence=bad))
        assert any("仓库内" in p for p in validate(text, root, ENUM_SRC)), bad


def test_append_only_forbids_delete_and_immutable_changes():
    old = full_ledger()
    gone = "".join(entry(f"D{i:03d}", c, action="patch" if c == "time_6digit" else "gap") for i, c in enumerate(CODES, 1) if i != 1)
    assert any("禁止删除" in p for p in compare(old, gone))
    assert any("evidence 不可改" in p for p in compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", evidence="docs/other.md"))))
    assert any("created_at 不可改" in p for p in compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", created="2026-09-02"))))
    assert any("note 不可改" in p for p in compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", note="改了叙事"))))


def test_days_only_grow():
    old = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06"))
    assert compare(old, full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06, 2024-02-08"))) == []
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


def test_new_entries_start_only_as_pending():
    old = full_ledger()
    for status in ("active", "rejected"):
        assert any("新增条目只能是 pending" in p for p in compare(old, old + entry("D900", "ghost", status=status)))
    assert compare(old, old + entry("D900", "ghost", status="pending")) == []


def _observed(tmp_path: Path, codes: str = '["seq_empty"]', tamper: bool = False, name: str = "scan.toml") -> Path:
    f = tmp_path / "scan.toml"
    f.write_text(f"codes = {codes}\n", encoding="utf-8")
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    (tmp_path / "manifest.toml").write_text(
        f'[[batch]]\nfile = "{name}"\nsha256 = "{digest}"\nscanner = "external:x"\nscanner_sha256 = "0"\ninput = "i"\n'
        f'input_sha256 = "0"\nscanned_at = 2026-09-02\n', encoding="utf-8")
    if tamper:
        f.write_text('codes = ["seq_empty", "rescue_partial"]\n', encoding="utf-8")
    return tmp_path


def test_observed_batches_are_hash_bound_and_append_only(tmp_path):
    assert "manifest.toml 不存在" in check_observed(full_ledger(), tmp_path)[0]
    assert check_observed(full_ledger(), _observed(tmp_path)) == []
    assert any("被改写" in p for p in check_observed(full_ledger(), _observed(tmp_path, tamper=True)))
    assert any("只能是 basename" in p for p in check_observed(full_ledger(), _observed(tmp_path, name="../scan.toml")))
    d = _observed(tmp_path, codes='["seq_empty", "never_seen", "rescue_partial"]')
    ledger = full_ledger(rescue_partial=entry("D004", "rescue_partial", status="pending"))
    (problem,) = check_observed(ledger, d)
    assert "never_seen" in problem and "rescue_partial" not in problem     # pending 也算已登记（观测先于裁决）
    d = _observed(tmp_path)
    (d / "extra.toml").write_text("codes = []\n", encoding="utf-8")
    assert any("不在清单批次里" in p for p in check_observed(full_ledger(), d))
    (d / "extra.toml").unlink()
    old_manifest = (d / "manifest.toml").read_text(encoding="utf-8")
    assert check_observed(full_ledger(), d, old_manifest) == []
    (d / "manifest.toml").write_text(old_manifest.replace('input = "i"', 'input = "j"'), encoding="utf-8")
    assert any("不可改不可删" in p for p in check_observed(full_ledger(), d, old_manifest))
    bad_scanner = old_manifest.replace('scanner = "external:x"', 'scanner = "scripts/x.py"')
    (d / "manifest.toml").write_text(bad_scanner, encoding="utf-8")
    assert any("scanner 必须是 tools/" in p for p in check_observed(full_ledger(), d))
    assert check_observed(Path("ledger/defects.toml").read_text(encoding="utf-8"), Path("ledger/observed")) == []


def test_baseline_never_self_compares():
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    oid = baseline_oid(None)
    assert oid is not None and oid != head
    assert baseline_oid("abc") == "abc"


def test_gate_never_passes_unsafe_base_and_tool_imports_nothing_from_src():
    assert "--unsafe-base" not in Path("tools/gate.sh").read_text(encoding="utf-8")
    assert "--unsafe-base" not in Path(".github/workflows/gate.yml").read_text(encoding="utf-8")
    assert "from ftbv2" not in Path("tools/check_ledger.py").read_text(encoding="utf-8")
