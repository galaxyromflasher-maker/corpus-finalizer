"""G5 打包：把定稿集合成一个可直接阅读的包（HTML + 全库 jsonl + md + 索引）。

用法：
    python pack.py --project <博主目录>
    python pack.py --project <博主目录> --date 2026-10-23

产物：
    <项目>/阅读包-<日期>/
        <创作者名>-文案合集.html
        全库-段.jsonl
        全库-篇.jsonl
        全库.md
        索引.csv
        manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

PARA_RE = re.compile(r"^\[(\d{2}:\d{2})–(\d{2}:\d{2})\]\s*(.*)$")


def load_worklist(project: Path) -> list[dict]:
    path = project / "校对" / "worklist.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_stats(project: Path) -> dict[str, dict]:
    path = project / "清单" / "works.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            out[str(row.get("作品ID", ""))] = row
    return out


def parse_human(text: str) -> tuple[list[str], list[str]]:
    """返回 (头部行, 正文段落)"""
    head, paras = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("_"):
            head.append(stripped)
        else:
            paras.append(stripped)
    return head, paras


def build_html(docs: list[dict], creator: str, generated: str) -> str:
    payload = json.dumps(docs, ensure_ascii=False, separators=(",", ":"))
    total_chars = sum(d["chars"] for d in docs)
    total_paras = sum(len(d["paras"]) for d in docs)
    title = f"{creator} · 文案合集"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#fbfbfa; --panel:#fff; --ink:#1f2328; --sub:#6b7280; --line:#e6e7e9; --accent:#2f6fed; --mark:#fff3bf; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-size:15px; line-height:1.8;
    font-family:-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif; }}
  .layout {{ display:flex; min-height:100vh; }}
  aside {{ width:340px; flex:0 0 340px; background:var(--panel); border-right:1px solid var(--line);
    height:100vh; position:sticky; top:0; display:flex; flex-direction:column; }}
  .brand {{ padding:14px 16px 10px; border-bottom:1px solid var(--line); }}
  .brand h1 {{ font-size:15px; margin:0 0 3px; }}
  .brand p {{ margin:0; font-size:12px; color:var(--sub); }}
  .tools {{ padding:9px 12px; border-bottom:1px solid var(--line); }}
  .tools input {{ width:100%; padding:7px 10px; border:1px solid var(--line); border-radius:8px; font-size:13px; outline:none; }}
  .tools input:focus {{ border-color:var(--accent); }}
  #list {{ overflow-y:auto; padding:4px 0 40px; }}
  .group {{ padding:8px 16px 4px; font-size:11px; color:var(--sub); letter-spacing:.04em; }}
  .item {{ padding:7px 16px; cursor:pointer; border-left:3px solid transparent; }}
  .item:hover {{ background:#f6f7f9; }}
  .item.active {{ background:#eef3ff; border-left-color:var(--accent); }}
  .item .t {{ font-size:13px; line-height:1.45; }}
  .item .m {{ font-size:11px; color:var(--sub); margin-top:2px; }}
  .badge {{ display:inline-block; font-size:10px; padding:0 5px; border-radius:4px; background:#f1f2f4; color:var(--sub); margin-left:6px; }}
  main {{ flex:1; min-width:0; padding:26px 40px 80px; }}
  article {{ max-width:760px; }}
  h2 {{ font-size:21px; line-height:1.5; margin:0 0 8px; }}
  .meta {{ font-size:12.5px; color:var(--sub); margin-bottom:18px; }}
  .meta a {{ color:var(--accent); text-decoration:none; }}
  p.para {{ margin:0 0 14px; }}
  mark {{ background:var(--mark); }}
  button {{ font:inherit; font-size:12.5px; padding:5px 12px; border-radius:7px; border:1px solid var(--line);
    background:var(--panel); cursor:pointer; }}
  button:hover {{ border-color:var(--accent); color:var(--accent); }}
  .bar {{ margin-bottom:16px; display:flex; gap:8px; }}
  .empty {{ padding:20px; color:var(--sub); font-size:13px; }}
  @media (max-width:900px) {{
    .layout {{ flex-direction:column; }}
    aside {{ width:auto; flex:none; height:auto; position:static; border-right:none; border-bottom:1px solid var(--line); }}
    #list {{ max-height:38vh; }}
    main {{ padding:18px; }}
  }}
</style>
</head>
<body>
<div class="layout">
  <aside>
    <div class="brand">
      <h1>{creator} · 文案合集</h1>
      <p>{len(docs)} 篇 · {total_chars:,} 字 · {total_paras:,} 段 · {generated}</p>
    </div>
    <div class="tools"><input id="q" type="search" placeholder="搜索标题或正文…" autocomplete="off"></div>
    <div id="list"></div>
  </aside>
  <main><article id="view"></article></main>
</div>
<script id="payload" type="application/json">{payload}</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const listEl = document.getElementById('list');
const viewEl = document.getElementById('view');
const qEl = document.getElementById('q');
let current = DATA[0] ? DATA[0].id : null;
let query = '';

function esc(s) {{ return s.replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c])); }}
function hl(text, q) {{
  const safe = esc(text);
  if (!q) return safe;
  const i = safe.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return safe;
  return safe.slice(0, i) + '<mark>' + safe.slice(i, i + q.length) + '</mark>' + safe.slice(i + q.length);
}}

function renderList() {{
  const q = query.toLowerCase();
  const shown = DATA.filter(d => !q || d.title.toLowerCase().includes(q) ||
    d.paras.some(p => p.toLowerCase().includes(q)));
  if (!shown.length) {{ listEl.innerHTML = '<div class="empty">没有匹配的内容</div>'; return; }}
  const groups = {{}};
  shown.forEach(d => {{ (groups[d.category || '未分类'] = groups[d.category || '未分类'] || []).push(d); }});
  let html = '';
  Object.keys(groups).forEach(g => {{
    html += '<div class="group">' + esc(g) + ' · ' + groups[g].length + '</div>';
    groups[g].forEach(d => {{
      html += '<div class="item ' + (d.id === current ? 'active' : '') + '" data-id="' + d.id + '">'
        + '<div class="t">' + esc(d.title.replace(/#\\S+/g, '').trim()) + '</div>'
        + '<div class="m">' + d.date + ' · ' + d.minutes + ' 分钟 · ' + d.chars + ' 字'
        + (d.status === 'rolled_back' ? '<span class="badge">原样</span>' : '')
        + '</div></div>';
    }});
  }});
  listEl.innerHTML = html;
  listEl.querySelectorAll('.item').forEach(el => {{
    el.onclick = () => {{ current = el.dataset.id; renderList(); renderView(); }};
  }});
}}

function renderView() {{
  const d = DATA.find(x => x.id === current);
  if (!d) {{ viewEl.innerHTML = '<p class="empty">从左侧选择一篇</p>'; return; }}
  const paras = d.paras.map(p => '<p class="para">' + hl(p, query) + '</p>').join('');
  viewEl.innerHTML = '<h2>' + hl(d.title, query) + '</h2>'
    + '<div class="meta">' + d.date + ' · ' + d.minutes + ' 分钟 · ' + d.digg + ' 赞 · '
    + '<a href="' + d.url + '" target="_blank" rel="noreferrer">原视频</a>'
    + (d.status === 'rolled_back' ? ' · 本篇未做优化（原样）' : '') + '</div>'
    + '<div class="bar"><button id="copy">复制全文</button></div>'
    + paras;
  document.getElementById('copy').onclick = async () => {{
    const text = d.title + '\\n\\n' + d.paras.join('\\n\\n');
    try {{ await navigator.clipboard.writeText(text); alert('已复制 ' + text.length + ' 字'); }}
    catch (e) {{ alert('复制失败，可手动选中'); }}
  }};
}}

qEl.addEventListener('input', () => {{ query = qEl.value.trim(); renderList(); renderView(); }});
renderList();
renderView();
</script>
</body>
</html>
"""


def load_states(project: Path) -> dict[str, dict]:
    """阶段状态从 state.jsonl 读，不再依赖 meta.json（已改为只读）。"""
    states: dict[str, dict] = {}
    for name in ("proofread-state.jsonl", "finalize-state.jsonl"):
        path = project / "校对" / "供AI查阅" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            doc_id = record.get("id")
            if doc_id:
                states[doc_id] = {**states.get(doc_id, {}), **record}
    return states


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    creator = project.name.split("-")[0]

    worklist = load_worklist(project)
    stats = load_stats(project)
    states = load_states(project)

    out = project / f"阅读包-{date}"
    out.mkdir(parents=True, exist_ok=True)

    docs, seg_lines, doc_lines, md_blocks = [], [], [], []
    index_rows = []

    for item in worklist:
        folder = project / item["folder"]
        human = folder / "human.md"
        ai = folder / "ai.jsonl"
        if not human.exists():
            continue

        head, paras = parse_human(human.read_text(encoding="utf-8"))
        url = f"https://www.douyin.com/video/{item['id']}"
        digg = (stats.get(item["id"]) or {}).get("点赞数", "")
        status = states.get(item["id"], {}).get("proofreadStatus", "ok")

        doc = {
            "id": item["id"], "order": item["order"], "category": item.get("category", ""),
            "title": item["title"], "date": item["date"], "minutes": item["minutes"],
            "digg": digg, "chars": sum(len(p) for p in paras), "url": url,
            "status": status, "paras": paras,
        }
        docs.append(doc)

        if ai.exists():
            for line in ai.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    seg_lines.append(line.strip())

        full = "\n\n".join(paras)
        doc_lines.append(json.dumps(
            {"doc_id": item["id"], "order": item["order"], "category": doc["category"],
             "title": item["title"], "date": item["date"], "durationSec": int(item["minutes"] * 60),
             "paragraphs": len(paras), "chars": doc["chars"], "text": full},
            ensure_ascii=False))
        md_blocks.append(f"## {item['order']} · {item['title']}\n\n{full}\n")

        index_rows.append([item["order"], item["id"], doc["category"], item["title"], item["date"],
                           item["minutes"], digg, doc["chars"], len(paras), status, url])

    (out / "全库-段.jsonl").write_text("\n".join(seg_lines) + "\n", encoding="utf-8")
    (out / "全库-篇.jsonl").write_text("\n".join(doc_lines) + "\n", encoding="utf-8")
    (out / "全库.md").write_text(f"# {creator} · 文案合集（{len(docs)} 篇）\n\n" + "\n".join(md_blocks), encoding="utf-8")

    with (out / "索引.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "作品ID", "类别", "标题", "日期", "时长分钟", "点赞", "字数", "段数", "状态", "链接"])
        writer.writerows(index_rows)

    html_name = f"{creator}-文案合集.html"
    (out / html_name).write_text(build_html(docs, creator, date), encoding="utf-8")

    total_chars = sum(d["chars"] for d in docs)
    manifest = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "creator": creator,
        "documents": len(docs),
        "segments": len(seg_lines),
        "chars": total_chars,
        "rollingBack": sum(1 for d in docs if d["status"] == "rolled_back"),
        "fields": {
            "doc_id": "作品ID",
            "seq": "篇内段序，从 1 开始",
            "ts_start": "该段在音频中的起始时间 mm:ss",
            "text": "正文，已校对重排",
        },
        "files": {
            "html": html_name,
            "segments": "全库-段.jsonl",
            "documents": "全库-篇.jsonl",
            "plain": "全库.md",
            "index": "索引.csv",
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"阅读包：{out}")
    print(f"  {html_name}　{len(docs)} 篇 / {total_chars:,} 字")
    print(f"  全库-段.jsonl　{len(seg_lines):,} 段")
    print(f"  全库-篇.jsonl　{len(doc_lines)} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
