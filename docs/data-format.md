# corpuslab 内部数据格式

> 本文定义**全流水线流通单位（Sample）与 DuckDB 状态库各表的行格式**——字段、类型、约束与版本。
> 这是数据契约：实现与迁移以此为准（对应 `corpuslab/core/sample.py` 与 `corpuslab/core/store.py`）。

---

## 1. 格式版本

| 常量 | 值 | 位置 |
|------|-----|------|
| `FORMAT_VERSION` | `"1"` | `corpuslab/core/sample.py` |
| `SCHEMA_VERSION` | `"1"` | `corpuslab/core/store.py`（DuckDB 表结构版本） |

两者都写入状态库 `kv` 表（manifest，见 checkpoint-design.md §7）：`format_version` 变更是**破坏性变更**，resume 时若不一致直接拒绝；`SCHEMA_VERSION` 变更需迁移脚本。

---

## 2. Sample —— 唯一流通单位

### 2.1 JSON Schema（canonical 形态）

```jsonc
{
  // ── 必需 ──────────────────────────────────────────────
  "id": "topic:0:Python|easy|concept|3",      // string，Plan 期确定性派生，非空
  "strategy": "topic_driven",                 // string，策略类型，非空
  "metadata": {                               // object，必需
    "lineage": {...},                         // 溯源（结构由策略决定，见 2.3）
    "metrics": {...}                          // 治理/评审结果（见 2.4）
  },
  // ── 二选一形态（互斥约束）──────────────────────────────
  // 形态 A：instruction/output 单样本（alpaca 类）
  "instruction": "…", "output": "…",
  "reasoning": "…",                            // 可选，思维链
  // 形态 B：messages 多轮 / tool_call（chatml/sharegpt/openai 类）
  "messages": [{"role": "…", "content": "…"}],
  "tools": [/* OpenAI function 定义 */]        // 可选，随 messages
}
```

### 2.2 约束（`Sample.validate()` 强制，违反抛 `ValueError`）

| # | 约束 |
|---|------|
| C1 | `id` 非空字符串（断点幂等键，checkpoint-design.md §3） |
| C2 | `strategy` 非空字符串 |
| C3 | **形态互斥**：`messages` 非空 与 `instruction/output` 非空**二选一**（可都空？否——至少一个形态存在） |
| C4 | `metadata` 为 object；其中 `lineage` / `metrics` 若存在必须为 object |
| C5 | `id` 与 `metadata.lineage` 等派生字段**只读不改写**：去重/统计口径见 `text_for_dedup()` |

校验点：`Sample.from_dict()`（读取状态库 / pending 恢复）与 `FileSource.open()`（外来数据入口）强制校验，非法数据在**入口即拒绝**并带字段级错误消息。

### 2.3 `metadata.lineage`（策略写入，P6 溯源）

| 策略 | 结构 |
|------|------|
| topic_driven / deep_thinking | `{source: topic, topic, <slot>…}`（槽位展开） |
| seed_driven | `{source: seed, seed_id, operator, seed_id_b?, mutation?}` |
| evol_instruct | `{source: seed, seed_id, evolution_round, mutation, evolution_chain[]}` |
| document_qa / backtranslation | `{source: document, source_id, chunk: [start, end]}` |
| tool_call | `{source: tool, topic}` |
| file（clean/score 入口） | `{input: file}` |

### 2.4 `metadata.metrics`（治理/评审写入）

| 键 | 写入者 | 说明 |
|----|--------|------|
| `kept_by: []` | 各通过阶段 | 阶段名列表（排障「数据为什么少了」） |
| `stats: {}` | stats 阶段 | 统计特征（如 ngram_diversity） |
| `scores: {}` / `total_score` / `score_source` | AggregateJudge | 评审结果（DESIGN/README「评审」语义） |

---

## 3. TaskSpec / Score

```jsonc
// TaskSpec（Plan 产物，持久化于 planned 表）
{"id": "…", "strategy": "…", "payload": {...}, "lineage": {...}}
// Score（评审协议，持久化于 scores 表）
{"scores": {"helpfulness": 9.0}, "total": 9.0, "source": "llm"}
```

---

## 4. DuckDB 状态库行格式

（库文件 = 输出目录下 `corpuslab.duckdb`；完整表语义见 checkpoint-design.md §4）

| 表 | 行格式 | 约束 |
|----|--------|------|
| `samples` | `id, strategy, payload(JSON=Sample), rendered(JSON=渲染后), total_score, created_at` | id 主键；`payload` 满足 §2 |
| `events` | `seq, t, id, strategy, data(JSON)` | 追加；t ∈ planned/committed/dropped/execute_failed |
| `pending` | `id, strategy, sample(JSON)` | id 主键；批式屏障背压缓冲 |
| `embeddings` | `text_hash, model, vec(DOUBLE[])` | (text_hash, model) 主键；内容寻址 |
| `fingerprints` | `hash, sample_id` | hash 主键；SHA256 去重状态 |
| `minhash_sigs` | `sample_id, sig(JSON: uint64[])` | sample_id 主键；**JSON 字符串存储防浮点精度损失** |
| `scores` | `id, endpoint, scores(JSON), total` | (id, endpoint) 主键；部分裁判结果也保留 |
| `dropped` | `id, strategy, stage, reason` | id 主键；终态（resume 跳过依据） |
| `planned` | `id, strategy, spec(JSON=TaskSpec)` | id 主键；幂等 |
| `kv` | `k, v` | manifest（format_version / schema_version / config_hash / seed / num_perm / embedding_model） |

---

## 5. 变更流程

1. 改 `Sample` 形态（加字段/改约束）时 `FORMAT_VERSION + 1` 并同步本文 §2；
2. DuckDB 表结构变更时 `SCHEMA_VERSION + 1` 加迁移脚本，并同步本文 §4；
3. 渲染器（alpaca/chatml/sharegpt/openai）**只读** canonical 形态，不反向影响内部格式。

---

*corpuslab 内部数据格式 · 与 checkpoint-design.md 及 project-structure.md §4 保持一致 · 数据契约变更须同步更新本文。*
