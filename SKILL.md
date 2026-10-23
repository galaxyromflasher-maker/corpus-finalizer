---
name: corpus-finalizer
description: This skill should be used after a transcript corpus already exists and needs to be turned into a readable, publishable version — "把转写稿整理成能读的", "校对一下这批文案", "把错字改掉再出一个合集". It proofreads paragraph by paragraph, hands the audit to a second agent (it cannot audit itself), rolls back anything that drifts from the original meaning, re-flows paragraphs for readability, and packs everything into one HTML collection plus machine-readable JSONL. Source files are never modified.
---

# 语料校对与定稿

把原始转写稿变成两份可用的成果：一份人读的合集（HTML），一份机器读的结构化文本（JSONL）。

## 五条红线

1. **原文只读**：`text.md`、`text.txt`、`transcript.json`、`meta.json`、清单，一个字不动
2. **不调用语音模型**：不再转写，只基于现有文字改
3. **不增信息**：不许补例子、结论、背景、解释
4. **不改事实**：数字、金额、年份、否定、程度、因果方向
5. **不改立场**：褒贬、肯定否定、因果方向不能反

第 3–5 条约束内容。结构可以在新文档里重排，只要时间轴可追溯。

## 四阶段

| 阶段 | 谁做 | 产物 | 之后 |
|---|---|---|---|
| G1 入库 | 本 skill | worklist、normalized、术语表、方案 | **停**，等确认 |
| G2 校对 | 本 skill（模型 A） | draft.md、diff.json | **停**，交出审核任务包 |
| G3 审核 | **另一个 Agent** | 直接写 `verdict.json` | 本 skill 自动检测 |
| G4 定稿 + 打包 | 本 skill | human.md、ai.jsonl、阅读包 | 交付 |

## G1 入库

```bash
python scripts/ingest.py                          # 自动找 ./抖音博主文案提取/*/
python scripts/ingest.py --project <博主目录>
```

只认新布局（`清单/works.csv` + `转写/<序号>_<ID>/text.md`）。旧布局不支持。

产物写在 `校对/`：`worklist.jsonl`、`normalized/`、`术语表.csv`、`ingest-summary.json`、`G1-方案.md`

**停**：交一页表 + `G1-方案.md`，等「确认」。

## G2 校对

```bash
python scripts/proofread.py --project <目录> --key-file <路径> --model qwen-plus --workers 3
```

逐段调用，模型三选一输出：`keep` / `fix` / `uncertain`。

| 判断 | 动作 |
|---|---|
| 不是错误（口语、重复、专名、引述） | `keep`，原样放过 |
| 能推就改（音近/形近 + 语义等价 + 上下文支持） | `fix`，允许从句内增删 ≤2 字 |
| **疑似乱码但不确定**（缩写、黑话、专名） | `uncertain`，**原样保留**，写入 `uncertain.jsonl` |

`uncertain` 是 v2 新增的关键出口。实测教训：遇到「埃批」「册殊」「斗势红」这类乱码，模型会按语义猜，一半猜对一半猜错，直接污染语料。**宁可保留乱码，也不许猜。**

模型只输出正文，`diff.json` 由程序生成。硬校验：数字集合一致、单段长度 ±20%。

状态写 `校对/供AI查阅/proofread-state.jsonl`，**不改写 `meta.json`**（原文只读）。

**本阶段结构不变**——段数与时间戳逐段一致，方便审核对照。

## G3 审核（必须交给另一个 Agent）

```bash
python scripts/handoff.py build --project <目录>
```

生成 `校对/交接/审核任务.md` 与 `待审段.jsonl`。把这份 md 整份复制给另一个 Agent。

任务书里**只写相对路径**（「写到与本文件同级的 verdict.json」），换电脑不会失效。

对方跑完后：

```bash
python scripts/handoff.py check --project <目录>
```

自动检测 `校对/交接/verdict.json`（以及分片 `verdict-part-*`），输出审核回执：

- 判了多少段、回滚多少、给正解多少、回滚率
- 理由聚类（丢否定 / 实词替换 / 补内容 / **缩写误判** / 漏义）
- 回滚集中的篇目
- **建议优先复核的段落（按长度排序前 5）**
- 决策：`continue`（≤10% 自动继续）/ `ask`（10–30% 停下问）/ `stop`（>30% 建议改标准）

`verdict` 是三值，不是二值：

| 值 | 含义 |
|---|---|
| `pass` | 校对没问题 |
| `rollback` | 改错了，退回原文 |
| **`correct`** | 方向对但改错，审核者直接给出正确写法（带 `text`） |

实测教训：「埃批→LBP」这种情况退回「埃批」是浪费，让审核直接给「IP」才有用。

## G4 定稿 + 打包

```bash
# 零 API 模式（推荐起点）：只回滚 + 输出双格式，不需要 Key
python scripts/finalize.py --project <目录> --no-reflow

# 需要段落重排时才调模型
python scripts/finalize.py --project <目录> --key-file <路径> --model qwen-plus --workers 2

python scripts/pack.py --project <目录>
```

`finalize.py` 做两件事：

1. **回滚**：按 verdict 处理——`rollback` 换回原文，`correct` 采用审核给的正解。
   整篇回退的分母是**本篇被改动的段**（不是全部段），且改动段 ≥3 才允许整篇回退。
   历史缺陷：用全部段做分母，只改 2 段的篇目被全回滚会算成 40%，白白退回原文。
2. **重排**：按篇调用，模型只输出 `{"from": 1, "to": 3, "text": "..."}` 的段落划分，时间戳由程序计算。
   `--no-reflow` 可跳过，跳过时正文沿用校对稿原有分段。

重排后四道校验，任一不过整篇退回 `base.md`：

| 校验 | 阈值 |
|---|---|
| 区间连续覆盖 | from 从 1 开始无空洞 |
| 长度变化 | 0.85–1.05 |
| 句指纹命中 | ≥ 95% |
| 回滚段原句 | 必须逐字出现 |

`pack.py` 生成阅读包：

```
阅读包-<日期>/
├─ <创作者>-文案合集.html     人读主入口，单文件双击即开
├─ 全库-段.jsonl             段级：检索 / RAG
├─ 全库-篇.jsonl             篇级：整篇归纳
├─ 全库.md                   整批纯文本
├─ 索引.csv
└─ manifest.json
```

## 目录

```
<博主目录>/
├─ 校对/
│  ├─ G1-方案.md / 标准卡.md / worklist.jsonl
│  ├─ normalized/ / 术语表.csv
│  ├─ 校对结果.csv / 校对报告.md
│  ├─ 交接/{审核任务.md, 待审段.jsonl, verdict.json}
│  └─ 供AI查阅/{diff/, audit-summary.json, proofread-log.jsonl}
├─ 转写/<序号>_<ID>/{text.md, draft.md, base.md, human.md, ai.jsonl}
└─ 阅读包-<日期>/
```

## 参数默认值

| 参数 | 值 |
|---|---|
| 校对模型 | `qwen-plus` |
| 审核模型 | 外部 Agent 自选，必须与校对不同 |
| 重排模型 | `qwen-plus` |
| 并发 | 校对 3 / 重排 2 |
| 整篇回滚阈值 | 30% |
| 重排字数区间 | 0.85–1.05 |
| 句指纹命中 | ≥ 95% |
| 密度下限 | 3 处/千字 |
| 重排失败率上限 | 20% |

## 禁止事项

1. 修改任何原始文件
2. 让模型自己写改动日志（必须程序 diff）
3. 整篇一次性丢给模型做校对（必须逐段）
4. 同模型既改又审
5. 审核时提新改法或挑错别字
6. 把「拿不准」判成 rollback
7. 报告中写「质量良好」「准确无误」等未经核实的结论
8. 调用任何语音识别接口
9. 改写 `meta.json`（属于原始文件，状态一律写 `校对/供AI查阅/*-state.jsonl`）
10. 把 Key 写进命令行、脚本或日志（优先用环境变量 `DSK`，脚本已支持）

## Key 的三种给法

| 方式 | 安全性 | 说明 |
|---|---|---|
| 环境变量 `DSK` | **推荐** | 不落盘、不进命令行历史 |
| `--key-file` | 可用 | 路径进命令行，但 Key 本身不露 |
| `--key` | 不推荐 | 会留在 shell 历史里 |
