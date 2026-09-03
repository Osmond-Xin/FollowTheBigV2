"""注册表的查询入口与版本。事件提取与因子都只从这里取定义，不得各自持有一份。

版本规则（CONTEXT.md「事件流版本」）：改切割算法或参数 = major（全量重跑，隔离旧流）；
新增独立事件类型 = minor（增量追加）；纯重构、产物哈希不变 = patch。
`digest()` 是全部条目的内容摘要——改了任何一条，摘要就变；契约测试用金标准摘要盯住它，
逼得每一次改动都必须同时动 REGISTRY_VERSION，改不动版本就改不了定义。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from enum import Enum

from ftbv2.core.registry.seeds import DAY_BOUNDARY, SEEDS
from ftbv2.core.registry.types import DayBoundarySpec, EventSpec, Param

REGISTRY_VERSION = "0.1.0"
"""注册表版本。第一批种子，尚未在真实数据上跑过。"""

_BY_KIND: dict[str, EventSpec] = {s.kind: s for s in SEEDS}
if len(_BY_KIND) != len(SEEDS):
    raise ValueError("注册表有重名条目：kind 是事件流分表的表名，必须唯一")


def kinds() -> tuple[str, ...]:
    """全部事件类型名，按登记顺序。**每类事件各自一张表**——这也是落盘的表名。"""
    return tuple(_BY_KIND)


def spec(kind: str) -> EventSpec:
    """取一条条目。未登记的 kind 抛 KeyError 并列出已登记的——「查不到 = 没有」被禁止。"""
    try:
        return _BY_KIND[kind]
    except KeyError:
        raise KeyError(f"未登记的事件类型 {kind!r}；已登记：{', '.join(_BY_KIND)}") from None


def day_boundary() -> DayBoundarySpec:
    """日界事件的 schema。不是注册表条目（由驱动层产生），放在这里只为事实单源。"""
    return DAY_BOUNDARY


def extraction_params() -> dict[str, tuple[Param, ...]]:
    """全部提取参数，按事件类型分组。进证据指纹：假设不得修改它，每个假设必须声明所依赖的事件流版本。"""
    return {s.kind: s.params for s in SEEDS}


def version() -> str:
    """注册表版本号。事件流版本 = 它 + 提取参数集 + 原始层数据清单摘要。"""
    return REGISTRY_VERSION


def digest() -> str:
    """全部条目（含日界事件 schema）的内容摘要，sha256 前 16 位。
    条目的任何改动都会改变它——契约测试用金标准盯住，改定义必须同时改版本。"""
    payload = {
        "version": REGISTRY_VERSION,
        "events": [_canonical(s) for s in SEEDS],
        "day_boundary": _canonical(DAY_BOUNDARY),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _canonical(obj: EventSpec | DayBoundarySpec) -> dict[str, object]:
    return asdict(obj, dict_factory=lambda kv: {k: _plain(v) for k, v in kv})


def _plain(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value
