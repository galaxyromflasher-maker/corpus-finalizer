"""G3 交接：生成审核任务包；回收并校验 verdict.json，输出审核回执。

用法：
    python handoff.py build --project <博主目录>
    python handoff.py check --project <博主目录>

产物：
    校对/交接/待审段.jsonl     只含被改动的段落（before/after 配对）
    校对/交接/审核任务.md       交给另一个 Agent 的完整任务书（只用相对路径）
    校对/供AI查阅/audit-summary.json

设计要点：
    - 任务书里不出现任何盘符，只写「与本文件同级的 verdict.json」
    - check 时用项目根 + 固定相对路径定位文件，不解析 md 内容
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

TASK_NAME = "校对/交接/审核任务.md"
PAIRS_NAME = "校对/交接/待审段.jsonl"
VERDICT_NAME = "校对/交接/verdict.json"

CLUSTERS = [
    ("丢否定", r"否定|不是|不|没|别|未|反转"),
    ("程度词丢失", r"程度|限定|大概|左右|几乎|稍微|都|才"),
    ("补了原文没有的内容", r"新增|补|添加|原文没有|编造|例子"),
    ("漏义", r"漏|丢|删掉|省略|缺"),
    ("其他", r""),
]


def cluster(reason: str) -> str:
    for name, pattern in CLUSTERS:
        if pattern and re.search(pattern, reason):
            return name
    return "其他"


def load_worklist(project: Path) -> list[dict]:
    path = project / "校对" / "worklist.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build(project: Path) -> int:
    items = load_worklist(project)
    diff_dir = project / "校对" / "供AI查阅" / "diff"
    pairs = []

    for item in items:
        path = diff_dir / f"{item['order']}_{item['id']}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for change in data.get("changes") or []:
            pairs.append(
                {
                    "order": item["order"],
                    "id": item["id"],
                    "title": item["title"][:40],
                    "para": change["para"],
                    "before": change["before"],
                    "after": change["after"],
                }
            )

    out = project / "校对" / "交接"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "待审段.jsonl").open("w", encoding="utf-8") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, ensure_ascii=False) + "\n")

    cards = "\n".join(
        [
            "| 维度 | 含义 | 例子 |",
            "|---|---|---|",
            "| 编造 | 出现原文没有的信息、例子、结论、背景 | 原文没提数字，整理后补了具体金额 |",
            "| 漏义 | 丢掉关键意思，尤其否定、限定、程度 | 「不全是」被写成「都是」 |",
            "| 反转 | 意思说反 | 「不适合补差」被写成「适合补差」 |",
        ]
    )

    task = f"""# 审核任务：核对 {project.name} 的文案整理结果

## 一、背景

这是 {len(items)} 篇视频口播的转写稿，已经由另一个模型做过一次错字校对。
现在需要你核查：**这次校对有没有改变原意。**

## 二、你的角色

你是事实核查员，不是校对员。

- 不挑错别字，不评价用词好坏
- 不提出新的改法
- 不因为「可以写得更顺」就判失败
- **拿不准时判 pass**

## 三、输入

与本文件同级的 `待审段.jsonl`，每行一段：

```json
{{"order": "013", "id": "7660476565372931364", "title": "……", "para": 5,
  "before": "校对前的原文", "after": "校对后的文字"}}
```

共 **{len(pairs)}** 段待审。

## 四、怎么判

只判断三件事有没有发生：

{cards}

## 五、输出

写到**与本文件同级**的 `verdict.json`，格式：

```json
{{
  "auditorModel": "你使用的模型名",
  "auditedAt": "2026-10-23T20:10:00+08:00",
  "items": [
    {{"order": "013", "id": "7660476565372931364", "para": 5, "verdict": "pass", "reason": ""}},
    {{"order": "013", "id": "7660476565372931364", "para": 8, "verdict": "rollback",
      "reason": "把「不全是」改成「都是」，否定被丢"}}
  ]
}}
```

要求：

1. **直接写入文件**，不要只贴在回复里
2. `verdict` 只能是 `pass` 或 `rollback`
3. `reason` 控制在 20 字内，`pass` 可以留空
4. 每一条待审段都要出现一条结论，不要漏

## 六、位置说明

路径一律用相对位置，不要用盘符：

- 读：`./待审段.jsonl`
- 写：`./verdict.json`

如果你在另一台机器上跑，把 `verdict.json` 放回你拿到本任务文件的同一目录即可。

## 七、规模

{len(items)} 篇 / {len(pairs)} 段。
"""
    (out / "审核任务.md").write_text(task, encoding="utf-8")

    print(f"交接包已生成：{out / '审核任务.md'}")
    print(f"待审段 {len(pairs)} 段 -> {out / '待审段.jsonl'}")
    print()
    print("请把下面这个文件整份复制给另一个 Agent：")
    print(f"  {TASK_NAME}")
    print("它跑完会把 verdict.json 写在同一目录，我下次自动读取。")
    return 0


def check(project: Path) -> int:
    pairs_path = project / PAIRS_NAME
    verdict_path = project / VERDICT_NAME

    pairs = [json.loads(l) for l in pairs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    key = {(p["order"], p["id"], p["para"]) for p in pairs}

    if not verdict_path.exists():
        print(f"还没检测到审核结果：{VERDICT_NAME}")
        print("如果外部 Agent 已跑完，请确认它把 verdict.json 写到了 校对/交接/ 下。")
        return 1

    try:
        data = json.loads(verdict_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"verdict.json 无法解析：{error}")
        return 2

    items = data.get("items") or []
    valid, dropped, bad_value = [], 0, 0
    for entry in items:
        k = (str(entry.get("order")), str(entry.get("id")), entry.get("para"))
        if k not in key:
            dropped += 1
            continue
        verdict = entry.get("verdict")
        if verdict not in {"pass", "rollback"}:
            bad_value += 1
            verdict = "pass"
        valid.append({**entry, "verdict": verdict})

    judged = {str(i["order"]) + "|" + str(i["id"]) + "|" + str(i["para"]) for i in valid}
    expected = {str(p["order"]) + "|" + str(p["id"]) + "|" + str(p["para"]) for p in pairs}
    unjudged = len(expected - judged)

    rollbacks = [i for i in valid if i["verdict"] == "rollback"]
    total, rolled = len(valid), len(rollbacks)
    rate = rolled / max(1, total)

    clusters = Counter(cluster(i.get("reason") or "") for i in rollbacks)
    by_doc: Counter[str] = Counter()
    doc_total: Counter[str] = Counter()
    for entry in valid:
        doc_total[entry["order"]] += 1
    for entry in rollbacks:
        by_doc[entry["order"]] += 1
    heavy = [
        (o, by_doc[o], doc_total[o])
        for o in by_doc
        if doc_total[o] and by_doc[o] / doc_total[o] > 0.2
    ]
    heavy.sort(key=lambda x: -x[1] / x[2])

    unjudged_rate = unjudged / max(1, len(pairs))
    decision = "continue"
    reasons = []
    if unjudged_rate > 0.5:
        decision = "unchecked"
        reasons.append(f"未审率 {unjudged_rate:.0%}，视为未审核")
    elif rate > 0.3:
        decision = "stop"
        reasons.append(f"回滚率 {rate:.1%} 过高，建议先改标准卡")
    elif rate > 0.1:
        decision = "ask"
        reasons.append(f"回滚率 {rate:.1%}，需你确认")

    summary = {
        "auditorModel": data.get("auditorModel", ""),
        "auditedAt": data.get("auditedAt", ""),
        "待审段": len(pairs),
        "已判定": total,
        "通过": total - rolled,
        "回滚": rolled,
        "回滚率": round(rate, 4),
        "未审段": unjudged,
        "未审率": round(unjudged_rate, 4),
        "无效条目": dropped,
        "非法取值": bad_value,
        "回滚理由聚类": dict(clusters),
        "回滚集中篇目": [{"order": o, "rollback": r, "judged": t} for o, r, t in heavy[:10]],
        "decision": decision,
        "decisionReasons": reasons,
    }
    (project / "校对" / "供AI查阅" / "audit-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("【审核回执】")
    print(f"待审 {len(pairs)} 段 ｜ 已判 {total} ｜ 通过 {total - rolled} ｜ 回滚 {rolled}（{rate:.1%}）")
    print(f"审核者：{data.get('auditorModel') or '未注明'} ｜ 未审 {unjudged} 段（{unjudged_rate:.0%}）")
    if dropped or bad_value:
        print(f"异常：无法匹配 {dropped} 条，非法取值 {bad_value} 条")
    if clusters:
        print("回滚理由聚类：")
        for name, count in clusters.most_common():
            print(f"  {name} {count} 段")
    if heavy:
        print("回滚集中篇目：")
        for order, r, t in heavy[:5]:
            print(f"  #{order} 回滚 {r}/{t} 段")
    print(f"决策：{decision}" + (f"（{'；'.join(reasons)}）" if reasons else "（可自动继续 G4）"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "check"])
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    return build(project) if args.action == "build" else check(project)


if __name__ == "__main__":
    raise SystemExit(main())
