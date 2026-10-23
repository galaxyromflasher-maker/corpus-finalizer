"""G1 入库：定位语料项目 -> 解析 text.md -> 规范化 -> 术语表 -> 待校工单 -> 预算。

只认新布局：
    <项目>/清单/works.csv
    <项目>/转写/<序号>_<作品ID>/text.md

用法：
    python ingest.py
    python ingest.py --project <博主目录>

产物（写入 <项目>/校对/）：
    worklist.jsonl / worklist.csv
    normalized/<序号>_<ID>.md
    术语表.csv
    ingest-summary.json
    G1-方案.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

PARA_RE = re.compile(r"^\[(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\]\s*(.*)$")
HEAD_RE = re.compile(r"^#\s")
QUOTE_RE = re.compile(r"^>\s")
FOOT_RE = re.compile(r"^_.*音频总长.*_$")
FOLDER_RE = re.compile(r"^(\d+)_(\d+)$")

STOP_CHARS = set("的了是在有和就不人都一这那为上个我们你们他们吗呢吧啊呀哦嗯")
LONG_PARA = 1500

COLS = {
    "order": ["序号", "order"],
    "id": ["作品ID", "awemeId", "id"],
    "title": ["标题", "视频标题", "title"],
    "date": ["发布日期", "date"],
    "minutes": ["时长分钟", "时长", "minutes"],
    "category": ["类别", "category"],
}


def pick(row: dict, key: str, default: str = "") -> str:
    for name in COLS[key]:
        if name in row and row[name] not in (None, ""):
            return str(row[name])
    return default


def locate(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"目录不存在：{p}")
        return p
    found = sorted(Path.cwd().glob("抖音博主文案提取/*"))
    if not found:
        raise SystemExit("当前目录下没找到语料项目（抖音博主文案提取/*），请用 --project 指定")
    if len(found) > 1:
        print("发现多个项目，请用 --project 指定其一：")
        for f in found:
            print("  ", f)
        raise SystemExit(2)
    return found[0]


def find_manifest(project: Path) -> Path:
    path = project / "清单" / "works.csv"
    if not path.exists():
        raise SystemExit(f"没有找到 清单/works.csv：{project}\n本 skill 只认新布局，请用 skill1 重新生成语料。")
    return path


def index_texts(project: Path) -> dict[str, Path]:
    """转写/<序号>_<作品ID>/text.md -> {作品ID: 路径}"""
    index: dict[str, Path] = {}
    for path in (project / "转写").glob("*/text.md"):
        m = FOLDER_RE.match(path.parent.name)
        if m:
            index[m.group(2)] = path
    return index


def parse_md(text: str) -> tuple[list[str], list[dict]]:
    head, paras = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = PARA_RE.match(line)
        if m:
            paras.append(
                {
                    "start": f"{int(m.group(1)):02d}:{m.group(2)}",
                    "end": f"{int(m.group(3)):02d}:{m.group(4)}",
                    "text": m.group(5).strip(),
                }
            )
        elif HEAD_RE.match(line) or QUOTE_RE.match(line) or FOOT_RE.match(line):
            head.append(raw.rstrip())
    return head, paras


def render(head: list[str], paras: list[dict], footer: str) -> str:
    lines = list(head) + [""]
    for p in paras:
        lines.append(f"[{p['start']}–{p['end']}] {p['text']}")
        lines.append("")
    lines.append(footer)
    return "\n".join(lines).rstrip() + "\n"


def build_glossary(texts: list[str], extra: list[str], limit: int = 60) -> list[dict]:
    counter: Counter[str] = Counter()
    for text in texts:
        for n in (2, 3, 4, 5):
            for i in range(len(text) - n + 1):
                frag = text[i : i + n]
                if any(ch in STOP_CHARS for ch in frag):
                    continue
                if not all("\u4e00" <= ch <= "\u9fff" for ch in frag):
                    continue
                counter[frag] += 1

    picked: list[tuple[str, int]] = []
    for word, count in sorted(counter.items(), key=lambda x: -x[1]):
        if count < 6:
            break
        if any(word in p and len(word) < len(p) for p, _ in picked):
            continue
        picked.append((word, count))
        if len(picked) >= limit:
            break

    latin: Counter[str] = Counter()
    for text in texts:
        for token in re.findall(r"[A-Za-z][A-Za-z0-9.]{1,9}", text):
            latin[token] += 1

    rows = [
        {"词": w, "出现次数": c, "状态": "已确认" if c >= 10 else "待定", "建议写法": "", "来源": "自动"}
        for w, c in picked
    ]
    rows += [
        {"词": w, "出现次数": c, "状态": "已确认", "建议写法": "", "来源": "自动"}
        for w, c in latin.most_common(20)
        if c >= 3
    ]
    rows += [{"词": w, "出现次数": 0, "状态": "已确认", "建议写法": "", "来源": "用户"} for w in extra]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--extra-glossary", default=None, help="每行一个词，视为已确认")
    args = parser.parse_args()

    project = locate(args.project)
    out = project / "校对"
    (out / "normalized").mkdir(parents=True, exist_ok=True)
    (out / "交接").mkdir(parents=True, exist_ok=True)
    (out / "供AI查阅" / "diff").mkdir(parents=True, exist_ok=True)

    manifest = find_manifest(project)
    rows = list(csv.DictReader(manifest.open(encoding="utf-8-sig")))
    text_index = index_texts(project)

    extra: list[str] = []
    if args.extra_glossary:
        extra = [w.strip() for w in Path(args.extra_glossary).read_text(encoding="utf-8").splitlines() if w.strip()]

    worklist, anomalies = [], []
    corpus: list[str] = []
    total_chars = total_paras = 0

    for row in rows:
        wid = pick(row, "id")
        order = pick(row, "order") or "000"
        md = text_index.get(wid)
        status = "ok"

        if md is None:
            anomalies.append({"order": order, "id": wid, "reason": "no_text"})
            continue

        meta_path = md.parent / "meta.json"
        if meta_path.exists():
            status = json.loads(meta_path.read_text(encoding="utf-8")).get("status", "ok")
        if status == "no_speech":
            anomalies.append({"order": order, "id": wid, "reason": "no_speech"})
            continue

        head, paras = parse_md(md.read_text(encoding="utf-8"))
        if not paras:
            anomalies.append({"order": order, "id": wid, "reason": "unparsable"})
            continue

        long_para = any(len(p["text"]) > LONG_PARA for p in paras)
        footer = f"_音频总长 {paras[-1]['end']}_"
        (out / "normalized" / f"{order}_{wid}.md").write_text(render(head, paras, footer), encoding="utf-8")

        chars = sum(len(p["text"]) for p in paras)
        corpus.append("".join(p["text"] for p in paras))
        total_chars += chars
        total_paras += len(paras)

        worklist.append(
            {
                "order": order,
                "id": wid,
                "category": pick(row, "category"),
                "title": pick(row, "title"),
                "date": pick(row, "date"),
                "minutes": float(pick(row, "minutes", "0") or 0),
                "paragraphs": len(paras),
                "chars": chars,
                "status": status,
                "longPara": long_para,
                "folder": str(md.parent.relative_to(project)).replace("\\", "/"),
                "normalized": f"校对/normalized/{order}_{wid}.md",
                "estTokens": int(chars * 1.6 * 2),
            }
        )

    (out / "worklist.jsonl").write_text(
        "\n".join(json.dumps(w, ensure_ascii=False) for w in worklist), encoding="utf-8"
    )
    with (out / "worklist.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "作品ID", "类别", "标题", "日期", "时长分钟", "段数", "字数", "状态", "超长段"])
        for w in worklist:
            writer.writerow([
                w["order"], w["id"], w["category"], w["title"], w["date"],
                w["minutes"], w["paragraphs"], w["chars"], w["status"],
                "是" if w["longPara"] else "",
            ])

    glossary = build_glossary(corpus, extra)
    with (out / "术语表.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["词", "出现次数", "状态", "建议写法", "来源"])
        writer.writeheader()
        writer.writerows(glossary)

    est = sum(w["estTokens"] for w in worklist)
    pending = [g for g in glossary if g["状态"] == "待定"]
    summary = {
        "project": str(project),
        "manifest": str(manifest),
        "可校篇数": len(worklist),
        "异常篇数": len(anomalies),
        "异常清单": anomalies[:50],
        "超长段篇数": sum(1 for w in worklist if w["longPara"]),
        "总段落数": total_paras,
        "总字数": total_chars,
        "预计token-校对": est,
        "预计token-审核": int(est * 0.6),
        "预计token-重排": int(est * 0.5),
        "术语条目": len(glossary),
        "待定术语": len(pending),
        "生成时间": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (out / "ingest-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    reasons = Counter(a["reason"] for a in anomalies)
    md = f"""# G1 入库 · {project.name}

结论：可校 {len(worklist)} 篇、{total_paras} 段、{total_chars:,} 字，预计校对 token 约 {est:,}。

## 范围
清单 {len(rows)} 条，可校 {len(worklist)} 篇，异常 {len(anomalies)} 篇{('（' + '、'.join(f'{k} {v}' for k, v in reasons.items()) + '）') if anomalies else ''}。

## 依据
- 解析成功 {len(worklist)} 篇，段落与时间戳已对齐
- 术语表 {len(glossary)} 条，待定 {len(pending)} 条
- 预计 token：校对 {est:,} / 审核 {int(est*0.6):,} / 重排 {int(est*0.5):,}

## 风险
- 异常篇目不进入校对：{len(anomalies)} 篇
- 数字一律不改，转写错的数字会保留
- 超长单段 {summary['超长段篇数']} 篇，会单独处理

## 已排除
- 不改原始文件，不动段落结构（结构改动留到 G4）
- 不做内容提炼

## 下一步
回复「确认」开始批量校对
"""
    (out / "G1-方案.md").write_text(md, encoding="utf-8")

    print(f"项目：{project}")
    print(f"可校 {len(worklist)} 篇 / {total_paras} 段 / {total_chars:,} 字，异常 {len(anomalies)} 篇")
    print(f"术语 {len(glossary)} 条（待定 {len(pending)}）")
    print(f"预计 token：校对 {est:,} / 审核 {int(est*0.6):,} / 重排 {int(est*0.5):,}")
    if anomalies:
        print("异常：" + "、".join(f"{k}={v}" for k, v in reasons.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
