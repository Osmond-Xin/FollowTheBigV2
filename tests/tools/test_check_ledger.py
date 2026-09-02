"""账本门禁的契约：结构 / 枚举相等 / 语义 append-only。用内联 TOML，不碰真账本以外的文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ftbv2.core.raw.ledger import DefectCode
from tools.check_ledger import check_observed, compare, validate

FULL = {c.value: i for i, c in enumerate(DefectCode, 1)}


def entry(ident: str, code: str, *, days: str = "2024-01-02", status: str = "active", extra: str = "", note: str = "n",
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


def test_enum_must_equal_live_ledger_codes():
    text = "".join(entry(f"D{i:03d}", code) for code, i in FULL.items() if code != "seq_empty")
    (problem,) = validate(text)
    assert "只在代码 ['seq_empty']" in problem
    # superseded / rejected 的 code 不进枚举：把 seq_empty 标成 rejected 后，枚举里多出的 seq_empty 就是违规
    rejected = full_ledger(seq_empty=entry("D002", "seq_empty", status="rejected"))
    assert any("只在代码 ['seq_empty']" in p for p in validate(rejected))
    # superseded 条目的 code 可以是枚举里没有的字符串
    gone = full_ledger(seq_empty=entry("D002", "seq_empty_old", status="superseded", extra='superseded_by = "D001"\n'))
    assert any("只在代码 ['seq_empty']" in p for p in validate(gone))


def test_datetime_is_rejected_as_day():
    text = full_ledger(seq_empty=entry("D002", "seq_empty", days="2024-02-06T00:00:00Z"))
    assert "带时间" in validate(text)[0]


def test_patch_and_gap_require_days():
    body = entry("D002", "seq_empty").replace("days = [2024-01-02]\n", "")   # gap 却没有 days
    assert "必须按天登记" in validate(full_ledger(seq_empty=body))[0]


def test_active_shape_requires_decision_ref():
    shape = entry("D002", "seq_empty", days="2024-02-06").replace('kind = "defect"', 'kind = "shape"')
    assert "decision_ref" in validate(full_ledger(seq_empty=shape))[0]
    ok = shape.replace('note = "n"', 'decision_ref = "design-log/x.md"\nnote = "n"')
    assert validate(full_ledger(seq_empty=ok)) == []


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
    ("pending", "active", True), ("active", "superseded", True), ("pending", "rejected", True),
    ("pending", "superseded", False), ("active", "pending", False), ("superseded", "active", False),
    ("rejected", "active", False),
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


def test_observed_codes_must_be_active_registered(tmp_path):
    (tmp_path / "scan.toml").write_text('codes = ["seq_empty", "never_seen", "rescue_partial"]\n', encoding="utf-8")
    ledger = full_ledger(rescue_partial=entry("D004", "rescue_partial", status="pending"))
    (problem,) = check_observed(ledger, tmp_path)
    assert "never_seen" in problem and "rescue_partial" in problem      # pending 不算已登记
    assert check_observed(full_ledger(), tmp_path / "missing") == []
    (tmp_path / "bad.toml").write_text("codes = [", encoding="utf-8")
    assert any("不可解析" in p for p in check_observed(full_ledger(), tmp_path))
