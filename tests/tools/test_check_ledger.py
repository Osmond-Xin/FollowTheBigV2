"""账本门禁的契约：结构 / 枚举相等 / 语义 append-only。用内联 TOML，不碰真账本以外的文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ftbv2.core.raw.ledger import DefectCode
from tools.check_ledger import check_observed, compare, validate

FULL = {c.value: i for i, c in enumerate(DefectCode, 1)}


def entry(ident: str, code: str, *, days: str = "", status: str = "active", extra: str = "", note: str = "n",
          created: str = "2026-09-01", evidence: str = "e") -> str:
    body = f'[[defect]]\nid = "{ident}"\ncode = "{code}"\nkind = "defect"\n'
    if days:
        body += f"days = [{days}]\n"
    body += f'status = "{status}"\n{extra}created_at = {created}\nevidence = "{evidence}"\nread_layer_action = "gap"\nnote = "{note}"\n\n'
    return body


def full_ledger(**overrides: str) -> str:
    return "".join(overrides.get(code) or entry(f"D{i:03d}", code) for code, i in FULL.items())


def test_real_ledger_validates():
    assert validate(Path("ledger/defects.toml").read_text(encoding="utf-8")) == []


def test_enum_must_equal_ledger_codes():
    text = "".join(entry(f"D{i:03d}", code) for code, i in FULL.items() if code != "seq_empty")
    (problem,) = validate(text)
    assert "只在代码 ['seq_empty']" in problem


def test_parse_errors_are_reported_not_raised():
    assert validate('[[defect]]\nid = "D001"\ncode = "time_6digit"\n')[0].startswith("账本解析失败")


def test_append_only_forbids_delete_and_immutable_changes():
    old = full_ledger()
    gone = "".join(entry(f"D{i:03d}", code) for code, i in FULL.items() if i != 1)
    assert any("禁止删除" in p for p in compare(old, gone + entry("D999", "time_6digit")))
    changed = full_ledger(time_6digit=entry("D001", "time_6digit", evidence="rewritten"))
    assert any("evidence 不可改" in p for p in compare(old, changed))
    later = full_ledger(time_6digit=entry("D001", "time_6digit", created="2026-09-02"))
    assert any("created_at 不可改" in p for p in compare(old, later))


def test_days_only_grow_and_note_is_free():
    old = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06"))
    grown = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06, 2024-02-08", note="改了"))
    assert compare(old, grown) == []
    shrunk = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-08"))
    assert any("只许并集" in p for p in compare(old, shrunk))


@pytest.mark.parametrize(("before", "after", "ok"), [
    ("pending", "active", True), ("active", "superseded", True), ("pending", "superseded", True),
    ("active", "pending", False), ("superseded", "active", False),
])
def test_status_transitions(before, after, ok):
    def e(status):
        extra = 'superseded_by = "D001"\n' if status == "superseded" else ""
        return entry("D002", "seq_empty", status=status, extra=extra)
    problems = compare(full_ledger(seq_empty=e(before)), full_ledger(seq_empty=e(after)))
    assert (problems == []) is ok


def test_superseded_by_must_exist_and_is_immutable():
    dangling = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D404"\n'))
    assert validate(dangling)[0].startswith("账本解析失败")
    old = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D001"\n'))
    moved = full_ledger(seq_empty=entry("D002", "seq_empty", status="superseded", extra='superseded_by = "D003"\n'))
    assert any("superseded_by 一旦设置不可改" in p for p in compare(old, moved))


def test_observed_codes_must_be_registered(tmp_path):
    (tmp_path / "scan.toml").write_text('codes = ["seq_empty", "never_seen"]\n', encoding="utf-8")
    (problem,) = check_observed(full_ledger(), tmp_path)
    assert "never_seen" in problem
    assert check_observed(full_ledger(), tmp_path / "missing") == []
