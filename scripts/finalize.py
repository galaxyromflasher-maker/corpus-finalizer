"""G4 定稿：回滚不通过段落 -> 重排结构 -> 双格式产出 -> 汇总报告。

用法：
    python finalize.py --project <博主目录> --key-file <路径> --model qwen-plus --workers 2

产物：
    转写/<序号>_<ID>/base.md    回滚后（结构同原文）
    转写/<序号>_<ID>/human.md   人读定稿（结构可重排）
    转写/<序号>_<ID>/ai.jsonl   AI 读定稿
    校对/校对结果.csv
    校对/校对报告.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

API = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
PARA_RE = re.compile(r"^\[(\d{2}:\d{2})–(\d{2}:\d{2})\]\s*(.*)$")
SENT_RE = re.compile(r"[^。！？!?]+[。！？!?]?")

ROLLBACK_WHOLE = 0.30
LEN_MIN, LEN_MAX = 0.85, 1.05
FINGER_HIT = 0.95
DENSITY_FLOOR = 3.0
MAX_FAIL_RATE = 0.20

SYSTEM = """你在整理一段视频口播的字幕稿。输入是按顺序编号的段落，每段一句话或几句话。

任务：把段落重新划分成更适合阅读的版本。只动分段，不改字。

可以做的：
1. 把过短的相邻段落合并（尤其单段不足 40 字）。
2. 把同一话题的连续段落合成一段。

不可以做的：
1. 不许删除任何内容，不许增加任何内容，不许改写句子。
2. 不许调整段落顺序。
3. 不许改标点之外的任何字符。

输出严格 JSON 数组，不要代码块。每项表示一个新段落由原文的哪些段合并而成：
[{"from": 1, "to": 3, "text": "第 1 到 3 段合并后的完整文字"},
 {"from": 4, "to": 4, "text": "第 4 段原文"}]

要求：
- from 必须从 1 开始，连续覆盖到最后一段，不能有空隙或重叠。
- text 必须等于对应段落原文的直接拼接，一个字都不能变。
"""


def load_key(args) -> str:
    if args.key:
        return args.key.strip()
    return Path(args.key_file).read_text(encoding="utf-8").strip()


def parse_md(text: str) -> tuple[list[str], list[dict]]:
    head, paras = [], []
    for line in text.splitlines():
        stripped = line.strip()
        m = PARA_RE.match(stripped)
        if m:
            paras.append({"start": m.group(1), "end": m.group(2), "text": m.group(3).strip()})
        elif stripped and (stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("_")):
            head.append(line.rstrip())
    return head, paras


def render_plain(head: list[str], blocks: list[str]) -> str:
    footer = next((h for h in head if h.startswith("_")), "")
    body = [h for h in head if not h.startswith("_")]
    lines = list(body) + [""]
    for block in blocks:
        lines.append(block)
        lines.append("")
    lines.append(footer)
    return "\n".join(lines).rstrip() + "\n"


def render_numbered(head: list[str], paras: list[dict]) -> str:
    footer = next((h for h in head if h.startswith("_")), "")
    body = [h for h in head if not h.startswith("_")]
    lines = list(body) + [""]
    for p in paras:
        lines.append(f"[{p['start']}–{p['end']}] {p['text']}")
        lines.append("")
    lines.append(footer)
    return "\n".join(lines).rstrip() + "\n"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.findall(text) if len(s.strip()) >= 6]


class Finalizer:
    def __init__(self, project: Path, key: str, model: str) -> None:
        self.project = project
        self.key = key
        self.model = model
        self.lock = threading.Lock()
        self.verdicts: dict[tuple[str, str, int], str] = {}
        self.unchecked = False
        self._load_verdict()

    def _load_verdict(self) -> None:
        path = self.project / "校对" / "交接" / "verdict.json"
        if not path.exists():
            self.unchecked = True
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("items") or []:
            self.verdicts[(str(entry["order"]), str(entry["id"]), int(entry["para"]))] = entry["verdict"]

    def _call(self, paras: list[dict]) -> list[dict]:
        numbered = "\n".join(f"{i}. {p['text']}" for i, p in enumerate(paras, start=1))
        for attempt in range(3):
            try:
                resp = requests.post(
                    API,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": f"共 {len(paras)} 段：\n{numbered}"},
                        ],
                        "temperature": 0,
                        "enable_thinking": False,
                    },
                    timeout=180,
                )
                payload = resp.json()
                if resp.status_code != 200:
                    raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:200])
                content = payload["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
                return json.loads(content)
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("unreachable")

    def process(self, item: dict) -> dict:
        order, wid = item["order"], item["id"]
        folder = self.project / item["folder"]
        src = self.project / item["normalized"]
        draft_path = folder / "draft.md"
        human_path = folder / "human.md"
        ai_path = folder / "ai.jsonl"

        if human_path.exists() and ai_path.exists():
            return {"order": order, "id": wid, "status": "skip"}

        head, orig = parse_md(src.read_text(encoding="utf-8"))
        if not draft_path.exists():
            return {"order": order, "id": wid, "status": "no_draft"}
        _, draft = parse_md(draft_path.read_text(encoding="utf-8"))
        if len(orig) != len(draft):
            return {"order": order, "id": wid, "status": "structure_mismatch"}

        # 1) 回滚
        rolled_idx = []
        base = []
        for i, (o, d) in enumerate(zip(orig, draft), start=1):
            verdict = self.verdicts.get((order, wid, i), "pass")
            if verdict == "rollback":
                rolled_idx.append(i)
                base.append({"start": d["start"], "end": d["end"], "text": o["text"]})
            else:
                base.append(d)

        ratio = len(rolled_idx) / max(1, len(base))
        whole = ratio > ROLLBACK_WHOLE

        if whole:
            base = orig
            rolled_idx = list(range(1, len(orig) + 1))

        (folder / "base.md").write_text(render_numbered(head, base), encoding="utf-8")

        # 2) 重排
        status = "ok"
        blocks, segs = [], []
        if self.unchecked:
            blocks = self._fallback_blocks(base)
            segs = self._fallback_segs(base)
            status = "unchecked"
        elif whole:
            blocks = self._fallback_blocks(base)
            segs = self._fallback_segs(base)
            status = "rolled_back"
        else:
            try:
                plan = self._call(base)
                ok, msg = self._validate_plan(plan, base, rolled_idx)
                if not ok:
                    raise ValueError(msg)
                blocks = [str(p["text"]).strip() for p in plan]
                segs = [
                    {"start": base[int(p["from"]) - 1]["start"], "end": base[int(p["to"]) - 1]["end"],
                     "text": str(p["text"]).strip()}
                    for p in plan
                ]
            except Exception as error:  # noqa: BLE001
                blocks = self._fallback_blocks(base)
                segs = self._fallback_segs(base)
                status = "restructure_failed"
                with self.lock:
                    with (self.project / "校对" / "供AI查阅" / "finalize-log.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"order": order, "action": "restructure_failed",
                                             "error": str(error)[:160]}, ensure_ascii=False) + "\n")

        human_path.write_text(render_plain(head, blocks), encoding="utf-8")
        with ai_path.open("w", encoding="utf-8") as fh:
            for seq, seg in enumerate(segs, start=1):
                fh.write(json.dumps(
                    {"order": order, "id": wid, "category": item.get("category", ""),
                     "title": item["title"], "date": item["date"], "seq": seq,
                     "ts_start": seg["start"], "ts_end": seg["end"],
                     "chars": len(seg["text"]), "text": seg["text"]},
                    ensure_ascii=False) + "\n")

        meta_path = folder / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["proofreadStatus"] = "rolled_back" if whole else "finalized"
            meta["finalizeStatus"] = status
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "order": order, "id": wid, "category": item.get("category", ""), "title": item["title"],
            "chars": item["chars"], "paragraphs": len(orig), "newParagraphs": len(blocks),
            "changed": item.get("changed", 0), "rolled": len(rolled_idx),
            "rollbackRatio": round(ratio, 3), "status": status if not whole else "rolled_back",
        }

    @staticmethod
    def _fallback_blocks(paras: list[dict]) -> list[str]:
        return [p["text"] for p in paras]

    @staticmethod
    def _fallback_segs(paras: list[dict]) -> list[dict]:
        return [{"start": p["start"], "end": p["end"], "text": p["text"]} for p in paras]

    @staticmethod
    def _validate_plan(plan: list[dict], base: list[dict], rolled_idx: list[int]) -> tuple[bool, str]:
        if not plan:
            return False, "空计划"
        cursor = 1
        for entry in plan:
            try:
                start, end = int(entry["from"]), int(entry["to"])
            except (KeyError, TypeError, ValueError):
                return False, "字段缺失"
            if start != cursor or end < start or end > len(base):
                return False, f"区间不连续（期望 {cursor}，实际 {start}）"
            cursor = end + 1
        if cursor != len(base) + 1:
            return False, "未覆盖全部段落"

        new_text = "".join(str(p["text"]) for p in plan)
        base_text = "".join(p["text"] for p in base)
        ratio = len(new_text) / max(1, len(base_text))
        if not (LEN_MIN <= ratio <= LEN_MAX):
            return False, f"长度比 {ratio:.2f} 越界"

        hit = sum(1 for s in sentences(base_text) if s[:6] in new_text)
        total = max(1, len(sentences(base_text)))
        if hit / total < FINGER_HIT:
            return False, f"句指纹命中 {hit/total:.0%}"

        for idx in rolled_idx:
            if idx - 1 >= len(base):
                continue
            if base[idx - 1]["text"] not in new_text:
                return False, f"回滚段 {idx} 原句丢失"
        return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--key", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    key = load_key(args)
    project = Path(args.project).resolve()
    items = [json.loads(l) for l in (project / "校对" / "worklist.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    diff_dir = project / "校对" / "供AI查阅" / "diff"
    for item in items:
        d = diff_dir / f"{item['order']}_{item['id']}.json"
        if d.exists():
            item["changed"] = json.loads(d.read_text(encoding="utf-8"))["stats"]["changed"]

    runner = Finalizer(project, key, args.model)
    if runner.unchecked:
        print("未检测到 verdict.json，按「未审核」模式运行（只合并碎段，不去口语）")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(runner.process, item): item for item in items}
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if done % 20 == 0 or done == len(items):
                print(f"[{done}/{len(items)}]", flush=True)

    rows = [r for r in results if "chars" in r]
    failed = [r for r in rows if r["status"] == "restructure_failed"]
    with (project / "校对" / "校对结果.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "作品ID", "类别", "标题", "字数", "段数", "改动数",
                         "回滚段", "回滚率", "重排后段数", "状态"])
        for r in sorted(rows, key=lambda x: x["order"]):
            writer.writerow([r["order"], r["id"], r["category"], r["title"], r["chars"],
                             r["paragraphs"], r["changed"], r["rolled"], r["rollbackRatio"],
                             r["newParagraphs"], r["status"]])

    total_chars = sum(r["chars"] for r in rows)
    changes = sum(r["changed"] for r in rows)
    rolled = sum(r["rolled"] for r in rows)
    before_p = sum(r["paragraphs"] for r in rows)
    after_p = sum(r["newParagraphs"] for r in rows)
    density = round(changes / max(1, total_chars) * 1000, 2)
    fail_rate = len(failed) / max(1, len(rows))

    audit = project / "校对" / "供AI查阅" / "audit-summary.json"
    audit_info = json.loads(audit.read_text(encoding="utf-8")) if audit.exists() else {}

    md = f"""# 校对报告 · {project.name}

结论：{len(rows)} 篇完成，{changes} 处改动（{density} 处/千字），回滚 {rolled} 段，段数由 {before_p} 精简为 {after_p}。

## 范围
可校 {len(rows)} 篇 / {before_p} 段 / {total_chars:,} 字。

## 依据
- 校对逐段处理，改动记录由程序 diff 生成
- 审核：{('外部 Agent 判定 ' + str(audit_info.get('已判定', 0)) + ' 段，回滚 ' + str(audit_info.get('回滚', 0)) + ' 段') if audit_info else '未审核'}
- 重排只合并段落，不改字；四道校验任一不过即退回 base.md
- 重排失败 {len(failed)} 篇（{fail_rate:.0%}）

## 风险
- 数字一律未改，转写错的数字会保留
- 人名、机构名、金额未改
- 整篇回滚 {sum(1 for r in rows if r['status'] == 'rolled_back')} 篇（等同原文）
- 改动密度 < 3 处/千字：{sum(1 for r in rows if r['changed'] / max(1, r['chars']) * 1000 < DENSITY_FLOOR)} 篇

## 已排除
- 未修改原始文件、未调用语音模型、未做内容提炼

## 下一步
打开 阅读包/<创作者>-文案合集.html 通读；抽 10 段对照 text.md
"""
    (project / "校对" / "校对报告.md").write_text(md, encoding="utf-8")

    print(f"完成 {len(rows)} 篇，段数 {before_p} -> {after_p}，改动 {changes} 处")
    print(f"重排失败 {len(failed)} 篇（{fail_rate:.0%}）")
    if fail_rate > MAX_FAIL_RATE:
        print("警告：重排失败率超过 20%，建议换模型或调参数后再跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
