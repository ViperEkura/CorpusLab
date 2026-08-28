# corpuslab 检查点与状态设计

> 本文定义**检查点的语义、状态分类、DuckDB 状态库 Schema、崩溃一致性协议与 resume 算法**。
> 总体架构见 [README.md](../README.md)；配置语义见 [config-design.md](config-design.md)；代码组织见 [project-structure.md](project-structure.md)。

---

## 目录

1. [设计立场](#1-设计立场)
2. [状态分类学](#2-状态分类学)
3. [sample_id：在花钱之前确定](#3-sample_id在花钱之前确定)
4. [DuckDB：唯一后端](#4-duckdb唯一后端)
5. [崩溃一致性协议](#5-崩溃一致性协议)
6. [resume 算法](#6-resume-算法)
7. [兼容性清单（manifest）](#7-兼容性清单manifest)
8. [不可恢复清单](#8-不可恢复清单)

---

## 1. 设计立场

检查点不是「落盘进度的副产物」，而是**唯一事实源的状态库**：

- 原 §7.3「逐样本即时落盘即断点粒度」的论证只在纯流式链下成立：批式屏障（`semantic_dedup / cluster_dedup`）会把样本拦在 Sink 之前，崩溃时一条都写不出去。本文以**独立的状态库**取代该论证；
- 输出表（`samples`）是状态库的**投影**，不是状态本身：存在「指纹已入状态、样本却被后续阶段 drop」的样本，它们不在输出里，但必须留在去重状态中，否则其重复样本会在续跑中漏网；
- DuckDB 提供事务，**原子性由事务边界保证**，不再依赖手写 WAL 顺序（先记事件后生效），这比 jsonl+fsync 方案更强：崩溃窗口内不会出现「输出有、日志无」或「日志有、输出无」的中间态。

---

## 2. 状态分类学

状态分三类，恢复策略完全不同：

| 类别 | 例子 | 恢复策略 | 丢失代价 |
|------|------|----------|----------|
| **A. 可免费重算** | length/stats 判定（纯函数）、LSH 索引结构（由签名重建）、聚类结果 | 不持久化，resume 时重算 | CPU 秒级，零成本 |
| **B. 花钱不可重算** | LLM 生成结果、embedding 向量、judge 分数 | **必须持久化**（`samples` / `embeddings` / `scores` 表） | 重新调 API，真金白银 |
| **C. 纯进度** | 哪些 TaskSpec 已规划 / 已终态 | `planned` / `samples`+`dropped`（终态集合） | 重跑 = 浪费 B 类 |

各 Stage 的状态处理：

| Stage | 调度 | 持久化 | resume 时 |
|-------|------|--------|-----------|
| `length` | 流式 | 无（A 类） | 无需恢复 |
| `stats` | 流式 | 无（A 类） | 无需恢复 |
| `exact_dedup` | 流式 | `fingerprints`（SHA256 到 sample_id 的映射） | 直接读表 |
| `minhash_dedup` | 流式 | `minhash_sigs`（签名） | 签名重放重建 LSH 索引（索引是视图，不是状态） |
| `semantic_dedup` | 批式 | `pending`（样本缓冲）+ `embeddings`（向量，B 类） | 重载缓冲与向量后执行屏障 |
| `cluster_dedup` | 批式 | 同上；聚类本身 A 类 | 向量重载后重算聚类 |

**「签名是状态，索引是视图」**的红利：MinHash 签名与 `threshold` 无关，改阈值后 resume 仍兼容（重建索引用新参数）；只有 `num_perm`（维度）变化才使签名失效，由 manifest 精确判定。

---

## 3. sample_id：在花钱之前确定

id 必须满足：**在 LLM 调用之前就能算出**（resume 要在「是否生成」这个决策点使用它），且**确定性**（同 seed 同配置得到同 id）。因此 id 在 **Plan 阶段**由任务单的槽位坐标派生，而非内容哈希：

```
id = sha256( "|".join(parts) )[:16]        # 16 位十六进制串（非可读坐标）
```

各策略的 parts（`derive_id(*parts)` 的入参，`|` 连接后哈希）：

```
topic_driven : "topic" | {type} | {topic} | {k=v}…(槽位按名排序) | {n}
deep_thinking: 同上（{type} = deep_thinking，即首个 parts 仍为 "topic"）
evol_instruct: "evol" | {seed_id} | "r{round}" | {k}
document_qa  : "docqa" | {doc_id} | {chunk_start} | {chunk_end}
backtrans    : "bt" | {doc_id} | {n} | {len(text)}
seed_driven  : "seed" | {seed_id} | {operator} | {n}
tool_call    : "tool" | {topic} | {n}
```

- id 写入 `TaskSpec.id`，随样本进入 `metadata.id`（补齐原契约缺口：project-structure §5.3 依赖 `sample_id` 而 Sample 无此字段）；
- 进化类策略**每轮发一个事件**：崩溃在第 2 轮能从第 2 轮续，不必从种子重来；
- id 确定性红利：`plan.count` 调大后 resume，Plan 增量生成新槽位，天然不与已完成 id 冲突。

---

## 4. DuckDB：唯一后端

一个 `.duckdb` 文件承载全部状态与输出（README.md「只写一个文件」由此字面成立）：

```sql
-- 输出投影（B 类终态）
CREATE TABLE samples(
  id VARCHAR PRIMARY KEY, strategy VARCHAR, payload JSON,
  rendered JSON, total_score DOUBLE, created_at TIMESTAMP DEFAULT now()
);
-- 事件审计日志（追加；报告与排障用）
CREATE TABLE events(
  seq BIGINT, t VARCHAR, id VARCHAR, strategy VARCHAR, data JSON
);
-- 批式屏障的磁盘背压缓冲（B 类在途样本）
CREATE TABLE pending(
  id VARCHAR PRIMARY KEY, strategy VARCHAR, sample JSON
);
-- embedding 内容寻址缓存（跨 run 可复用的 B 类资产）
CREATE TABLE embeddings(
  text_hash VARCHAR, model VARCHAR, vec DOUBLE[], PRIMARY KEY(text_hash, model)
);
-- 流式去重状态
CREATE TABLE fingerprints(hash VARCHAR PRIMARY KEY, sample_id VARCHAR);
CREATE TABLE minhash_sigs(sample_id VARCHAR PRIMARY KEY, sig VARCHAR);  -- 签名为 JSON 字符串（uint64 数组序列化，防浮点精度损失）
-- 评审缓存（按端点；部分裁判完成也保留）
CREATE TABLE scores(
  id VARCHAR, endpoint VARCHAR, scores JSON, total DOUBLE,
  PRIMARY KEY(id, endpoint)
);
-- 终态之二：被 drop 的样本（进度 C 类）
CREATE TABLE dropped(
  id VARCHAR PRIMARY KEY, strategy VARCHAR, stage VARCHAR, reason VARCHAR
);
-- 已规划任务单（进度 C 类；Plan 结果本身不花钱，但记录可省重规划）
CREATE TABLE planned(
  id VARCHAR PRIMARY KEY, strategy VARCHAR, spec JSON
);
-- manifest：兼容性判定
CREATE TABLE kv(k VARCHAR PRIMARY KEY, v VARCHAR);
```

- `output.path` 即该库文件路径（缺省扩展名 `.duckdb`）；
- JSONL 成为**导出格式**（`storage.export_format: jsonl`），不再是主存储；
- `cache_cleanup` 分级：成功后只清**瞬时表**（`events` 审计日志、`pending` 在途缓冲）；**终态集合（`samples / dropped / planned`）、去重状态（`fingerprints / minhash_sigs`）、`scores`、`embeddings` 全部保留**——否则后续 `--resume` 会重新生成所有曾 drop 的样本（白花 LLM 钱）或让重复样本漏网。不变式：重跑既不产生重复样本，也不在已终态的 id 上重复花钱。

---

## 5. 崩溃一致性协议

**原子性 = DuckDB 事务**。凡「产生效果」的操作，其全部写入包在一个事务里：

```
BEGIN
  INSERT INTO samples(...)          -- 投影
  INSERT INTO events('committed')
  DELETE FROM pending WHERE id=?    -- 若来自批式缓冲
COMMIT
```

- 崩溃在事务中则回滚，既无输出也无标记，该样本按 id 幂等重做；
- 阶段状态（指纹、签名）与 drop 记录同样在事务中写入，与判定同时生效；
- **无锁单写者**：所有事务在事件循环内串行（asyncio 单线程）；DuckDB 连接不跨线程共享，CPU 密集步骤经 `asyncio.to_thread` 卸载时使用独立只读连接或回到主循环写；
- **幂等重放**：所有 upsert 以 id / hash 为主键，resume 重放多少遍都安全。

---

## 6. resume 算法

```
resume():
  1. 打开 .duckdb，读 manifest（见 §7）
     └ 不兼容: 拒绝（exit 2），或 --discard-state 丢弃受影响状态并警告
  2. terminal = samples.id ∪ dropped.id               # 进度
  3. 流式状态：fingerprints / minhash_sigs 直接读；
     minhash LSH 索引由签名重放重建（当前 threshold）
  4. 批式缓冲：pending 表中样本重新进入屏障等待
  5. 评审缓存：scores 按 (id, endpoint) 命中即跳过该端点重评
  6. 对账：planned 中未 executed 且未 terminal 的任务单重新执行
  7. 7. Plan 增量补任务单（跳过 terminal），随后 Execute、Pipeline、Judge、Sink
```

控制流前提（对应 README.md「治理链」一节）：**所有策略的 execute 汇入同一条 pipeline**——一份状态、一个全局去重视图，跨策略重复才可见。

---

## 7. 兼容性清单（manifest）

`kv` 表记录以下键，resume 时逐项比对：

| 键 | 不一致时的处置 |
|----|----------------|
| `version` | 拒绝（DuckDB 表结构大版本变更） |
| `format_version` | 拒绝（Sample 形态破坏性变更） |
| `config_hash` | 拒绝（提示 `--discard-state`） |
| `seed` | 拒绝（id 派生受 seed 影响） |
| `minhash_num_perm` | 丢弃 `minhash_sigs` 并警告 |
| `embedding_model` | 丢弃 `embeddings` 并警告 |

> 后两个键仅在配置了对应阶段时写入（`num_perm` / `embedding_model` 缺省不落 manifest）。

---

## 8. 不可恢复清单

| 项 | 原因 | 后果 |
|----|------|------|
| 崩溃瞬间的在途 HTTP 响应 | 响应未落地 | 按 id 幂等重发，无重复副作用 |
| 熔断器滑动窗口的计时 | 时间不可持久化 | 窗口清零重来，语义可接受 |
| `preview` 模式的一切 | 设计上不落状态库 | 不变 |
| manifest 不兼容的旧状态 | 参数变了状态语义就变 | 显式丢弃 + 告警，绝不静默误用 |

---

*corpuslab 检查点设计 · DuckDB 单后端 · 与 project-structure.md §3.2/§5.3 保持一致 · 状态语义变更须同步更新本文。*
