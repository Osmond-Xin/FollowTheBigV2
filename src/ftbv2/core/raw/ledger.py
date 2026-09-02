"""缺陷与形状账本：按天变化的数据缺陷、以及语料里登记过的形状（含良性），都是**登记数据**，不是散在代码里的 if。
未登记的形状一律硬失败。

账本文件 ledger/defects.toml（纯数据，git 跟踪，事实单源）。本模块只解析文本（纯），读文件是 IO 层的事。
每条：id · code · kind(defect|shape) · stream · days|结构性 · status(pending|active|superseded) · superseded_by ·
created_at · evidence · read_layer_action(patch|gap|none) · note。
- 读取层只看 **active** 条目：pending 是待裁决（不得作为数据或门禁依据），rejected 已否决，superseded 已被替换；
- 形状（kind = shape）转 active 必须带 decision_ref（裁决出处）；patch / gap 必须按天登记（非空 days）；
- 代码枚举 DefectCode = active ∪ pending 的 code 集合；superseded / rejected 的 code 只作字符串保留在账本，不进枚举；
- `read_layer_action`：patch = 改变解码行为；gap = 缺口归因时转述；none = 保留并打标，下游由样本宇宙消费；
- 代码里的 DefectCode 枚举只是账本 code 集合的投影，CI 校验两者相等（tools/check_ledger.py）；
- 账本 append-only 是语义级的（按 id 比较，不是文本 diff）：禁删 id、禁改 code/kind/stream/created_at/evidence、
  days 只许并集、状态只许 pending→active→superseded，废弃只能 superseded_by 指向已存在 id 且原条目保留。
红队修正：IO 阶段只能基于账本与元数据分支，不能基于研究统计结果分支。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import tomllib
from dataclasses import dataclass
from enum import Enum

from ftbv2.core.raw.schema import STREAMS, Stream


class DefectCode(Enum):
    TIME_6DIGIT = "time_6digit"                    # column_4 混有 HHMMSS / HMMSS；补丁：按行归一化
    SEQ_EMPTY = "seq_empty"                        # trades column_5 整列空（厂商侧）
    SEQ_SPARSE_DUP = "seq_sparse_dup"              # trades column_5 稀疏重复键
    RESCUE_PARTIAL = "rescue_partial"              # 7z Data Error 后救援入库：orders / trades 标的集合不一致
    ENUM_DRIFT = "enum_drift"                      # 2025 起枚举新值（' ' S / C I J O）——只登记事实，读取不做映射
    INT32_OVERFLOW = "int32_overflow"              # xinqing column_13 超 int32，必须 int64
    NUL_SENTINEL_SH = "nul_sentinel_sh"            # SH trades column_6/7 全为 '\x00'
    QUOTE_ONLY = "quote_only"                      # 停牌心跳：标的只有行情、无委托无成交（合法形状，收据 quote_only_symbols）
    EMPTY_FILE = "empty_file"                      # 某标的某 stream 的 CSV 是 0 字节（合法形状，收据 empty_files；2026-09-02 裁定）
    NO_TRAILING_COMMA = "no_trailing_comma"        # 三条流各少尾逗号幽灵列；枚举空值为 null 而非空格
    STREAM_SYMBOL_MISMATCH = "stream_symbol_mismatch"  # 非救援日标的级单边缺失（有成交无委托等），原因不可考
    LEADING_ZERO_TIME = "leading_zero_time"        # 时间全带前导零（8 位占比 0）：202604 导出程序指纹
    DUPLICATE_OPEN_FRAME = "duplicate_open_frame"  # 09:30 / 13:00 每股各发两帧重复开盘快照；preserve 不删，事件提取须处理


KINDS = ("defect", "shape")
STATUSES = ("pending", "active", "rejected", "superseded")
LIVE = ("active", "pending")          # 进代码枚举的状态
ACTIONS = ("patch", "gap", "none")
REQUIRED = ("id", "code", "kind", "status", "created_at", "evidence", "read_layer_action")


@dataclass(frozen=True)
class Defect:
    id: str                        # 稳定主键；语义 append-only 按它比较
    code: str                      # active / pending 时必在 DefectCode 值域；superseded / rejected 只作字符串保留
    stream: Stream | None          # None = 三个 stream 都受影响
    days: tuple[dt.date, ...]      # 空元组 = 结构性、对所有天成立
    kind: str = "defect"
    status: str = "active"
    superseded_by: str | None = None
    created_at: dt.date | None = None
    evidence: str = ""             # 收据 id 或 design-log 引用
    read_layer_action: str = "gap"
    decision_ref: str | None = None   # 形状转 active 的裁决出处（design-log / PR）
    evidence_sha256: str | None = None   # 证据内容哈希（收据规则落地后必填；有则门禁核对）
    note: str = ""


@dataclass(frozen=True)
class DefectLedger:
    entries: tuple[Defect, ...]    # 全部条目（含 pending / superseded），供门禁与文档生成
    sha256: str                    # 账本文本的内容哈希，进 ScanPlan / 证据指纹

    def active(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.entries if d.status == "active")

    def for_day(self, day: dt.date, stream: Stream) -> tuple[Defect, ...]:
        """该天该 stream 登记在案的 active 条目（含结构性），顺序 = 账本顺序。"""
        return tuple(
            d for d in self.active()
            if (d.stream is None or d.stream == stream) and (not d.days or day in d.days)
        )

    def day_scoped_codes(self, day: dt.date, stream: Stream) -> tuple[str, ...]:
        """该天该 stream **按天登记**且读取层要理会（action ≠ none）的码，去重保序。缺口归因用它。"""
        out: list[str] = []
        for d in self.for_day(day, stream):
            if d.days and d.read_layer_action != "none" and d.code not in out:
                out.append(d.code)
        return tuple(out)

    def patches(self, day: dt.date, stream: Stream) -> tuple[str, ...]:
        """该天该 stream 触发的补丁码（account 里 read_layer_action = patch 的按天条目）。"""
        return tuple(d.code for d in self.for_day(day, stream) if d.days and d.read_layer_action == "patch")


def _date(value: object) -> dt.date:
    """只接受精确的日期：TOML 的 datetime（date 的子类）会让 `day in days` 永远不命中，必须拒绝。"""
    if isinstance(value, dt.datetime):
        raise ValueError(f"账本日期必须是 YYYY-MM-DD，不接受带时间的 {value!r}")
    if isinstance(value, dt.date):
        return value
    text = str(value)
    if len(text) != 10:
        raise ValueError(f"账本日期必须是 YYYY-MM-DD：{text!r}")
    return dt.date.fromisoformat(text)


def _parse_entry(n: int, row: dict) -> Defect:
    missing = [k for k in ("id", "code") if k not in row]
    if missing:
        raise ValueError(f"账本第 {n} 条缺字段 {missing}")
    ident = str(row["id"])
    stream = row.get("stream")
    if stream is not None and stream not in STREAMS:
        raise ValueError(f"账本里未知的 stream {stream!r}（合法值 {STREAMS}）")
    missing = [k for k in REQUIRED if k not in row]
    if missing:
        raise ValueError(f"账本 {ident} 缺字段 {missing}")
    code, kind, status, action = str(row["code"]), row["kind"], row["status"], row["read_layer_action"]
    if kind not in KINDS or status not in STATUSES or action not in ACTIONS:
        raise ValueError(f"账本 {ident} 的 kind/status/read_layer_action 不在登记值域：{kind}/{status}/{action}")
    if status in LIVE:
        DefectCode(code)                                  # 未知 code 在这里抛 ValueError
    if (status == "superseded") != ("superseded_by" in row):
        raise ValueError(f"账本 {ident}：superseded 与 superseded_by 必须同时出现")
    days = tuple(_date(d) for d in row.get("days", []))
    if action in ("patch", "gap") and not days:
        raise ValueError(f"账本 {ident}：read_layer_action = {action} 必须按天登记（非空 days），否则运行时永远不触发")
    if kind == "shape" and status == "active" and not row.get("decision_ref"):
        raise ValueError(f"账本 {ident}：形状转 active 必须带 decision_ref（裁决出处）")
    return Defect(ident, code, stream, days, kind, status, row.get("superseded_by"), _date(row["created_at"]),
                  str(row["evidence"]), action, row.get("decision_ref"), row.get("evidence_sha256"), row.get("note", ""))


def parse_ledger(text: str) -> DefectLedger:
    """解析 defects.toml 文本（格式见模块 docstring）。superseded_by 必须指向账本里已存在的 id。"""
    data = tomllib.loads(text)
    rows = data.get("defect", [])
    ids = [str(row["id"]) for row in rows if "id" in row]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"账本 id 重复：{dup[0]}")
    entries = tuple(_parse_entry(n, row) for n, row in enumerate(rows, 1))
    by_id = {d.id: d for d in entries}
    for d in entries:
        if d.superseded_by is None:
            continue
        target = by_id.get(d.superseded_by)
        if target is None or target.id == d.id or target.status not in LIVE:
            raise ValueError(f"账本 {d.id} 的 superseded_by 必须指向另一条 active / pending 条目（现为 {d.superseded_by}）")
    for d in entries:
        if d.evidence_sha256 is not None and len(d.evidence_sha256) != 64:
            raise ValueError(f"账本 {d.id} 的 evidence_sha256 必须是 64 位十六进制")
    return DefectLedger(entries, hashlib.sha256(text.encode("utf-8")).hexdigest())
