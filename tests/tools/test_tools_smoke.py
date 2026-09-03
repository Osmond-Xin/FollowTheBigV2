"""每个登记在 tools/manifest.toml 的入口都有一条真正调用它的烟雾测试（入口门禁要求）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.raw.conftest import DAY, order_row, trade_row, write_preserve
from tools.check_entrypoints import check, find_entries

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """工具按绝对路径调用；cwd 用临时目录，让收据（.lineage/receipts，相对 cwd）落在临时目录而不是仓库。"""
    tool, rest = ROOT / args[0], list(args[1:])
    return subprocess.run([sys.executable, str(tool), *rest], capture_output=True, text=True, cwd=cwd or ROOT, env=ENV)


def _preserve(tmp_path: Path) -> Path:
    root = tmp_path / "preserve"
    write_preserve(root, "orders", DAY, [order_row("000001.SZ", "93000000"), order_row("000002.SZ", "093000000")])
    write_preserve(root, "trades", DAY, [trade_row("000001.SZ", "093000000")])
    write_preserve(root, "xinqing", DAY, [{"column_4": "93000000", "_symbol": "000001.SZ"}])
    (root / "manifest").mkdir()
    (root / "manifest" / f"{DAY:%Y%m%d}.json").write_text(
        json.dumps({"day": DAY.isoformat(), "quality": "self_consistent"}), encoding="utf-8")
    return root


def test_shell_entries_parse():
    for sh in ("tools/gate.sh", "tools/redteam/redteam.sh"):
        assert subprocess.run(["bash", "-n", sh], cwd=ROOT).returncode == 0, sh


def test_check_private_imports_runs_clean_on_src():
    r = run("tools/check_private_imports.py", "src", cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_vocab_stage1_finds_candidates():
    from tools.check_vocab import load_table, stage1   # 入口：tools/check_vocab.py（二级语义判定要 API key，这里只跑一级 grep）
    assert stage1(load_table()) is not None


def test_check_entrypoints_on_repo_and_on_a_bad_tree(tmp_path):
    assert check(ROOT) == []
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text('if __name__ == "__main__":\n    pass\n', encoding="utf-8")
    problems = check(tmp_path)
    assert any("禁止的目录名" in p for p in problems) and any("未登记" in p for p in problems)
    assert find_entries(tmp_path) == {"scripts/x.py"}
    r = run("tools/check_entrypoints.py", str(tmp_path), cwd=tmp_path)
    assert r.returncode == 1


def test_ingest_days_tool_reports_skipped_and_exits_nonzero(tmp_path):
    dup = tmp_path / "20220104(1).7z"; dup.write_bytes(b"x")
    r = run("tools/ingest_days.py", str(dup), "--root", str(tmp_path / "root"), "--scratch", str(tmp_path / "s"), "--min-free-gb", "0", "--min-free-pct", "0", cwd=tmp_path)
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["skipped"][0][0] == str(dup) and out["receipt_id"]


def test_audit_preserve_tool_compare_and_mismatch(tmp_path):
    root = _preserve(tmp_path)
    r = run("tools/audit_preserve.py", "compare", "--a", str(root), "--b", str(root), "--day", DAY.isoformat(), cwd=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["identical_modulo_null"] is True
    r = run("tools/audit_preserve.py", "mismatch", "--root", str(root), cwd=tmp_path)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["mismatches"][0]["only_in"] == {"orders": ["000002.SZ"]}


def test_scan_shapes_tool_is_resumable(tmp_path):
    root, out = _preserve(tmp_path), tmp_path / "shapes.tsv"
    assert run("tools/scan_shapes.py", "--root", str(root), "--out", str(out), cwd=tmp_path).returncode == 0
    first = out.read_text(encoding="utf-8")
    assert "orders\t8\t1" in first and "orders\t9\t1" in first
    assert run("tools/scan_shapes.py", "--root", str(root), "--out", str(out), cwd=tmp_path).returncode == 0
    assert out.read_text(encoding="utf-8") == first                  # 已扫的 (day, stream) 不重复追加


def test_bench_read_tool(tmp_path):
    root = _preserve(tmp_path)
    r = run("tools/bench_read.py", "--root", str(root), "--ledger", str(ROOT / "ledger" / "defects.toml"), "--extrapolate-days", "10", cwd=tmp_path)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and set(out["seconds_per_day"]) == {"orders", "trades", "xinqing"} and out["receipt_id"]


@pytest.mark.parametrize(("kind", "first_invariant"), [
    ("LevelBuildThenVanish", "returns_to_zero"),
    ("FillExceedsDisplayed", "fill_reaches_displayed"),
    ("RefillAfterFill", "every_cycle_one_order_fully_eaten"),
])
def test_probe_density_tool(tmp_path, kind, first_invariant):
    """密度回归：夹具里只有一笔委托、没有撤单、没有成交 ⇒ 两条条目都零个候选。

    候选为零时准入必须**拒绝**，不是「通过」：没有事件的实测不构成「量过了」。"""
    root = _preserve(tmp_path)
    r = run("tools/probe_density.py", "--root", str(root), "--kind", kind, "--day", DAY.isoformat(),
            "--ledger", str(ROOT / "ledger" / "defects.toml"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["receipt_id"] and out["n_candidates"] == 0
    assert out["invariants"][0] == first_invariant
    assert out["准入"].startswith("拒绝"), out["准入"]


def test_probe_density_refuses_a_day_missing_a_stream(tmp_path):
    """20220627 只摄取了 orders / trades：上一版把 res.gaps 丢掉，于是「看不见」与「没数据」
    都变成 level = null，那天算出 0 个可见候选——一个看起来像行情、其实是缺文件的数。"""
    root = _preserve(tmp_path)
    (root / "xinqing" / f"date={DAY:%Y%m%d}.parquet").unlink()
    r = run("tools/probe_density.py", "--root", str(root), "--kind", "LevelBuildThenVanish",
            "--day", DAY.isoformat(), "--ledger", str(ROOT / "ledger" / "defects.toml"), cwd=tmp_path)
    assert r.returncode != 0
    assert "拒绝出密度" in r.stderr and "xinqing" in r.stderr


def test_probe_density_rejects_an_unregistered_kind(tmp_path):
    """未登记的条目要当场说「查不到」，不是返回空表当成「量到了零条」。"""
    root = _preserve(tmp_path)
    r = run("tools/probe_density.py", "--root", str(root), "--kind", "NotAnEvent",
            "--day", DAY.isoformat(), "--ledger", str(ROOT / "ledger" / "defects.toml"), cwd=tmp_path)
    assert r.returncode != 0


@pytest.mark.parametrize("tool", ["tools/ingest_days.py", "tools/audit_preserve.py", "tools/scan_shapes.py", "tools/bench_read.py", "tools/probe_density.py"])
def test_adapters_are_thin(tool):
    assert len((ROOT / tool).read_text(encoding="utf-8").splitlines()) <= 120


def test_probe_density_reports_concentration(tmp_path):
    """密度只回答「多不多」，集中度才回答「是不是庄的行为」：
    庄做的是某只股票的某个阶段，大部分标的当天应当一条没有。夹具里零候选 ⇒ 零率 1.0。"""
    root = _preserve(tmp_path)
    r = run("tools/probe_density.py", "--root", str(root), "--kind", "LevelBuildThenVanish",
            "--day", DAY.isoformat(), "--ledger", str(ROOT / "ledger" / "defects.toml"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    con = json.loads(r.stdout)["concentration"]
    assert con["zero_share"] == 1.0 and con["max_per_symbol"] == 0.0

