"""词汇门禁：易混淆词 + 定义冲突，两级检查，没有豁免名单。

数据源是 CONTEXT.md 第八节的表：左列定义词，右列外界会混用的说法。
一级 grep 右列找候选，只找不裁决；二级把候选交给便宜的模型（MiniMax-M3，Anthropic 兼容接口）逐条判定：
候选处若在指代左列的概念即冲突；数学与固定搭配、引用或描述 V1 与第三方原文、讨论规则自身、泛指他义皆合法。
第三步对相对 base 改动的文本文件检查：有没有新定义了与左列重叠或冲突的概念。

任一冲突、判定不可用、判定结果不完整 ⇒ exit 1（fail-closed）。
用法：python tools/check_vocab.py [--dry] [--all] [--base <ref>]
  --dry   只列候选，不调模型。   --all   判定全仓候选（默认只判定相对 base 改动文件里的候选）
  --base  对比基线（默认 origin/main，不存在则 HEAD~1）
密钥：环境变量 MINIMAX_API_KEY，或本机 ~/.mmx/config.json 的 api_key。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.minimaxi.com/anthropic/v1/messages"
MODEL = "MiniMax-M3"
CONTEXT_LEN = 220
DEF_FILE_CAP = 60_000

# ----------------------------------------------------------------- 数据源：CONTEXT.md 第八节


def load_table() -> list[tuple[str, list[str]]]:
    text = Path("CONTEXT.md").read_text(encoding="utf-8")
    sec = text[text.index("## 八、易混淆词表") :]
    rows = []
    for line in sec.splitlines():
        m = re.match(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|$", line)
        if not m or m.group(1) in ("定义词", "---"):
            continue
        confusables = [w.strip() for w in re.split(r"[、,，]", re.sub(r"（.*?）", "", m.group(2))) if w.strip()]
        rows.append((m.group(1), confusables))
    if not rows:
        raise RuntimeError("CONTEXT.md 第八节没有解析出任何行")
    return rows


def defined_terms() -> list[str]:
    text = Path("CONTEXT.md").read_text(encoding="utf-8")
    body = text[: text.index("## 六、")]
    return sorted({m.group(1) for m in re.finditer(r"^\*\*([^*（]+?)(?:（[^）]*）)?\*\*\s*$", body, re.M)})


def _pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])") if word.isascii() else re.compile(re.escape(word))


# ----------------------------------------------------------------- git 与文件


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True, check=True).stdout


def _text_files(paths: list[str]) -> list[Path]:
    out = []
    for raw in paths:
        p = Path(raw)
        if not raw or not p.is_file() or p.name == Path(__file__).name:
            continue
        try:
            p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.append(p)
    return out


def tracked_files() -> list[Path]:
    return _text_files(_git("ls-files", "-z").decode("utf-8").split("\0"))


def changed_files(base: str) -> list[Path]:
    names = _git("diff", "-z", "--name-only", base).decode("utf-8").split("\0")
    names += _git("ls-files", "-z", "--others", "--exclude-standard").decode("utf-8").split("\0")
    return _text_files(sorted(set(names)))


def resolve_base(explicit: str | None) -> str:
    for ref in ([explicit] if explicit else []) + ["origin/main", "HEAD~1"]:
        if subprocess.run(["git", "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0:
            return ref
    raise RuntimeError("找不到可用的 base")


# ----------------------------------------------------------------- 一级：候选


def stage1(table: list[tuple[str, list[str]]]) -> list[dict]:
    pats = [(term, w, _pattern(w)) for term, ws in table for w in ws]
    hits: list[dict] = []
    for p in tracked_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for term, w, pat in pats:
                m = pat.search(line)
                if not m:
                    continue
                text = line.strip()
                if len(text) > CONTEXT_LEN:
                    lo = max(0, text.find(w) - CONTEXT_LEN // 2)
                    text = "…" + text[lo : lo + CONTEXT_LEN] + "…"
                note = "归档的过程文档：第三方原文与改名前的历史记录" if str(p).startswith("docs/design-log/") else ""
                hits.append({"id": len(hits) + 1, "file": str(p), "line": i, "word": w, "term": term, "text": text, "note": note})
    return hits


# ----------------------------------------------------------------- 二级：语义判定


def _api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY")
    if key:
        return key
    cfg = Path.home() / ".mmx" / "config.json"
    return json.loads(cfg.read_text()).get("api_key", "") if cfg.exists() else ""


def ask(prompt: str, max_tokens: int = 4000) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError("没有 MINIMAX_API_KEY，语义判定不可用")
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": key,
        "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")


def parse_json_array(text: str) -> list[dict]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise RuntimeError(f"判定输出不是 JSON 数组：{text[:200]!r}")
    return json.loads(text[start : end + 1])


def table_text(table: list[tuple[str, list[str]]]) -> str:
    return "\n".join(f"- 定义词「{t}」 ← 外界会混用的说法：{'、'.join(ws)}" for t, ws in table)


def stage2(hits: list[dict], table: list[tuple[str, list[str]]]) -> list[dict]:
    listing = "\n".join(
        f"[{h['id']}] {h['file']}:{h['line']}  （候选词「{h['word']}」，可能混淆「{h['term']}」{'；文件性质：' + h['note'] if h['note'] else ''}）  {h['text']}"
        for h in hits)
    prompt = (
        "本项目有一份词汇表，同一个概念只许用一个词。下面是定义词与外界会混用的说法：\n"
        f"{table_text(table)}\n\n"
        "判定标准只有一个：候选处的那个词，是不是在**指代对应定义词的概念**（例如用「特征」指本项目里应叫「因子」的东西）。"
        "是 ⇒ 冲突。以下皆合法：数学或固定搭配（特征值、性能特征）；引用、转述或描述 V1 及第三方原文里的用法；"
        "定义、讨论、检查这份词汇表或门禁本身；泛指他义（如「边界记录」「做庄信号」）；"
        "标注为归档过程文档的文件里，第三方原文与改名前的历史记录照实保留，不算冲突。\n\n"
        "逐条判定。只输出一个 JSON 数组，不要任何其他文字，每个元素形如 "
        '{"id": 编号, "verdict": "冲突" 或 "合法", "reason": "十字以内"}。\n\n'
        f"{listing}"
    )
    by_id = {int(v["id"]): v for v in parse_json_array(ask(prompt))}
    missing = [h["id"] for h in hits if h["id"] not in by_id]
    if missing:
        raise RuntimeError(f"判定结果不完整，缺编号 {missing}")
    return [by_id[h["id"]] for h in hits]


# ----------------------------------------------------------------- 三级：定义冲突（只看改动文件）


def stage3(files: list[Path], terms: list[str]) -> list[dict]:
    findings: list[dict] = []
    for p in files:
        if p.name == "CONTEXT.md":
            continue
        body = p.read_text(encoding="utf-8")
        if len(body) > DEF_FILE_CAP:
            body = body[:DEF_FILE_CAP] + "\n…（截断）"
        prompt = (
            "本项目的词汇表已经定义了这些词（每个概念只许一个名字）：\n" + "、".join(terms) + "\n\n"
            "阅读下面这份文件，找出它是否**引入或定义了新的名字来指代上述已定义的概念**，或**重新定义**了上述词。"
            "描述、引用、使用已定义的词不算；提到 V1 或第三方的旧叫法并说明对应关系也不算。\n"
            "只输出一个 JSON 数组，不要任何其他文字；没有发现就输出 []。每个元素形如 "
            '{"new_term": "文件里的新说法", "conflicts_with": "已定义的词", "evidence": "原文片段二十字以内"}。\n\n'
            f"===== 文件 {p} =====\n{body}"
        )
        for f in parse_json_array(ask(prompt, max_tokens=1500)):
            findings.append({"file": str(p), **f})
    return findings


# ----------------------------------------------------------------- 主流程


def main(argv: list[str]) -> int:
    table = load_table()
    hits = stage1(table)
    print(f"词汇门禁：一级 grep 找到 {len(hits)} 处候选（数据源 CONTEXT.md 第八节，{len(table)} 行）")
    if "--dry" in argv:
        for h in hits:
            print(f"  [{h['id']}] {h['file']}:{h['line']}  「{h['word']}」→「{h['term']}」  {h['text']}")
        return 0
    base_arg = argv[argv.index("--base") + 1] if "--base" in argv else None
    try:
        conflicts = 0
        base = resolve_base(base_arg)
        files = changed_files(base)
        if "--all" not in argv:
            changed = {str(f) for f in files}
            hits = [h for h in hits if h["file"] in changed]
            print(f"词汇门禁：二级只判定相对 {base} 改动文件里的 {len(hits)} 处候选（--all 判定全仓）")
        if hits:
            for h, v in zip(hits, stage2(hits, table)):
                bad = v.get("verdict") == "冲突"
                conflicts += bad
                print(f"  {'✗' if bad else '·'} {h['file']}:{h['line']}  「{h['word']}」 {v.get('verdict')}（{v.get('reason', '')}）")
        print(f"词汇门禁：三级定义冲突检查，相对 {base} 改动 {len(files)} 个文本文件")
        for f in stage3(files, defined_terms()):
            conflicts += 1
            print(f"  ✗ {f['file']}  新说法「{f.get('new_term')}」与「{f.get('conflicts_with')}」重叠：{f.get('evidence')}")
    except Exception as e:  # noqa: BLE001 - 任何判定故障都必须让门禁失败
        print(f"词汇门禁：语义判定失败 ⇒ 不放行。{e}")
        return 1
    if conflicts:
        print(f"词汇门禁：{conflicts} 处冲突")
        return 1
    print("词汇门禁：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
