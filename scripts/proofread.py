"""G2 校对：逐段改字，结构与原文一致。改动记录由本脚本对 diff 生成。

用法：
    python proofread.py --project <博主目录> --key-file <路径> --model qwen-plus --workers 3
    python proofread.py --project <博主目录> --key <Key> --limit 5

产物：
    转写/<序号>_<ID>/draft.md
    校对/供AI查阅/diff/<序号>_<ID>.json
    校对/供AI查阅/proofread-log.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

API = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
PARA_RE = re.compile(r"^\[(\d{2}:\d{2})–(\d{2}:\d{2})\]\s*(.*)$")
NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
MAX_LEN_DELTA = 0.20
MAX_PARA = 1500

SYSTEM = """你是中文语音转写稿的校对员。输入是视频口播的一小段转写文本，内容多为教育、教培、高中物理。

任务：只修掉「语音识别错误」，让这段文字保持原意、读得通。不做改写。

必须遵守：
1. 只修同音字、近音字、专有名词、明显漏字。允许在同一从句内增删不超过 2 个字。
2. 禁止：改写句子、调整语序、增删整句、合并或拆分句子、润色表达、删除口语词与语气词。
3. 数字、分数、比例、年份、金额一律不动，即使读着别扭。
4. 人名、机构名、课程名拿不准时不动。
5. 正常口语、重复、口头禅（嗯、啊、那个、就是说、对吧）属于原话，不要改。
6. 改动必须能被上下文佐证；没有把握就不改。

最重要的第 7 条：
7. 遇到疑似识别错乱的缩写、专有名词、行业黑话（例如「埃批」可能是 IP、「册殊」可能是测试、
   「斗势红」可能是抖音号），如果你不能百分之百确定原词是什么，**必须走 uncertain，不要猜**。
   宁可保留乱码，也不要写一个"看起来合理"的词。
   绝对禁止把实词替换成另一个实词（校长→指令、教培→体培、抖音→豆包 这类）。
   绝对禁止凭空补出原文没有的名词（百万、资料、文档、评论区）。

输出严格 JSON，三选一，不要代码块：
{"action": "keep"}
{"action": "fix", "text": "改正后的完整段落，只改同音字与漏字", "note": "改了什么"}
{"action": "uncertain", "guess": "你猜的原词", "reason": "为什么不能确定"}
"""


def load_key(args) -> str:
    """优先级：--key > 环境变量 DSK > --key-file。
    DSK 是推荐方式：既不落盘，也不进命令行历史。"""
    if args.key:
        return args.key.strip()
    env = os.environ.get("DSK", "").strip()
    if env:
        return env
    if args.key_file:
        return Path(args.key_file).read_text(encoding="utf-8").strip()
    raise SystemExit("必须提供 --key、环境变量 DSK，或 --key-file")


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


def render(head: list[str], paras: list[dict]) -> str:
    footer = next((h for h in head if h.startswith("_")), "")
    body = [h for h in head if not h.startswith("_")]
    lines = list(body) + [""]
    for p in paras:
        lines.append(f"[{p['start']}–{p['end']}] {p['text']}")
        lines.append("")
    lines.append(footer)
    return "\n".join(lines).rstrip() + "\n"


def numbers(text: str) -> list[str]:
    return sorted(NUM_RE.findall(text))


class Proofreader:
    def __init__(self, project: Path, key: str, model: str) -> None:
        self.project = project
        self.key = key
        self.model = model
        self.lock = threading.Lock()
        self.log_path = project / "校对" / "供AI查阅" / "proofread-log.jsonl"
        self.uncertain_path = project / "校对" / "供AI查阅" / "uncertain.jsonl"
        self.state_path = project / "校对" / "供AI查阅" / "proofread-state.jsonl"
        self.glossary = self._load_glossary()

    def _load_glossary(self) -> list[str]:
        import csv

        path = self.project / "校对" / "术语表.csv"
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig") as handle:
            return [r["词"] for r in csv.DictReader(handle) if r.get("词")]

    def _hints(self, text: str) -> str:
        hit = [w for w in self.glossary if w in text]
        return ("参考术语（同篇已确认写法）：" + "、".join(hit[:8])) if hit else ""

    def _call(self, text: str) -> dict:
        user = (self._hints(text) + "\n\n原文：\n" + text).strip()
        for attempt in range(3):
            try:
                resp = requests.post(
                    API,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                        "temperature": 0,
                        "enable_thinking": False,
                    },
                    timeout=120,
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

    def _log(self, record: dict) -> None:
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _uncertain(self, record: dict) -> None:
        with self.lock:
            with self.uncertain_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _state(self, record: dict) -> None:
        with self.lock:
            with self.state_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def process(self, item: dict) -> dict:
        order, wid = item["order"], item["id"]
        src = self.project / item["normalized"]
        folder = self.project / item["folder"]
        draft_path = folder / "draft.md"
        diff_path = self.project / "校对" / "供AI查阅" / "diff" / f"{order}_{wid}.json"

        if draft_path.exists() and diff_path.exists():
            return {"order": order, "id": wid, "status": "skip"}

        head, paras = parse_md(src.read_text(encoding="utf-8"))
        changes = []
        stats = {"kept": 0, "changed": 0, "uncertain": 0, "rolled_back": 0, "failed": 0, "long": 0}

        for index, para in enumerate(paras, start=1):
            before = para["text"]
            if len(before) > MAX_PARA:
                stats["long"] += 1
                continue
            try:
                result = self._call(before)
            except Exception as error:  # noqa: BLE001
                stats["failed"] += 1
                self._log({"order": order, "para": index, "action": "failed", "error": str(error)[:120]})
                continue

            action = result.get("action")
            if action is None:  # 兼容旧格式
                action = "fix" if result.get("changed") else "keep"

            if action == "uncertain":
                # 不写进 draft，原样保留，单独列清单交人工/审核处理
                stats["uncertain"] += 1
                self._uncertain(
                    {
                        "order": order,
                        "id": wid,
                        "para": index,
                        "text": before,
                        "guess": (result.get("guess") or "")[:60],
                        "reason": (result.get("reason") or "")[:120],
                    }
                )
                continue

            if action != "fix":
                stats["kept"] += 1
                continue

            after = (result.get("text") or before).strip()
            if after == before:
                stats["kept"] += 1
                continue

            if numbers(before) != numbers(after):
                stats["rolled_back"] += 1
                self._log({"order": order, "para": index, "action": "rollback", "reason": "数字不一致"})
                continue
            if abs(len(after) - len(before)) / max(1, len(before)) > MAX_LEN_DELTA:
                stats["rolled_back"] += 1
                self._log({"order": order, "para": index, "action": "rollback", "reason": "长度超限"})
                continue

            para["text"] = after
            stats["changed"] += 1
            changes.append(
                {
                    "para": index,
                    "start": para["start"],
                    "end": para["end"],
                    "before": before,
                    "after": after,
                    "note": (result.get("note") or "")[:120],
                }
            )

        draft_path.write_text(render(head, paras), encoding="utf-8")

        density = round(stats["changed"] / max(1, item["chars"]) * 1000, 2)
        diff_path.write_text(
            json.dumps(
                {"order": order, "id": wid, "title": item["title"], "chars": item["chars"],
                 "stats": stats, "density": density, "changes": changes},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 红线 1：meta.json 属于原始文件，不允许改写。状态改记到阶段状态文件。
        self._state(
            {
                "order": order,
                "id": wid,
                "proofreadStatus": "drafted",
                "changed": stats["changed"],
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

        self._log({"order": order, "id": wid, "action": "done", **stats, "density": density})
        return {"order": order, "id": wid, "status": "ok", **stats, "density": density}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--key", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    key = load_key(args)
    project = Path(args.project).resolve()
    items = [json.loads(l) for l in (project / "校对" / "worklist.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        items = items[: args.limit]

    runner = Proofreader(project, key, args.model)
    print(f"待校对 {len(items)} 篇，模型 {args.model}，并发 {args.workers}", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(runner.process, item): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if done % 20 == 0 or done == len(items):
                print(
                    f"[{done}/{len(items)}] {result['order']} "
                    f"改{result.get('changed', 0)} 回滚{result.get('rolled_back', 0)} 保留{result.get('kept', 0)}",
                    flush=True,
                )

    print("G2 完成。下一步：生成审核交接包")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
