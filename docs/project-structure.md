# corpuslab 项目结构设计

> 本文定义**代码组织、模块职责、核心契约与扩展方式**。配置语义见 [config-design.md](config-design.md)；
> 检查点与状态库见 [checkpoint-design.md](checkpoint-design.md)；总体架构见 [README.md](../README.md)。

---

## 目录

1. [组织原则](#1-组织原则)
2. [顶层布局](#2-顶层布局)
3. [包结构与模块职责](#3-包结构与模块职责)
4. [核心契约](#4-核心契约)
5. [控制流](#5-控制流)
6. [并发模型](#6-并发模型)
7. [错误处理与可靠性](#7-错误处理与可靠性)
8. [CLI 设计](#8-cli-设计)
9. [测试结构](#9-测试结构)
10. [打包与依赖](#10-打包与依赖)
11. [扩展指南](#11-扩展指南)

---

## 1. 组织原则

| # | 原则 | 落地方式 |
|---|------|----------|
| S1 | **依赖单向** | `cli → config → core → {sources, strategies, stages, judges} → {llm, embedding, store} → sinks`；禁止逆向与横向跳跃（strategies 不得 import judges） |
| S2 | **协议在 core，实现在插件包** | 五抽象（Source/Strategy/Stage/Judge/Sink）的 Protocol 定义在 `core/contracts.py`，各插件包只依赖协议 |
| S3 | **注册表驱动** | 策略/阶段/裁判/**原料/渲染器/存储**通过 entry point 注册，CLI 与引擎按名查找；新增插件零侵入 |
| S4 | **基础设施唯一** | 重试、熔断、并发信号量只存在于 `llm/client.py`；持久化只存在于 `core/store.py`；任何插件不得自建 HTTP、线程池或独立数据库连接 |
| S5 | **纯函数优先** | Stage、Scorer、渲染器均为纯转换；副作用（网络、文件、DB）只出现在 Client、Source、Sink、Store |
| S6 | **测试镜像源码** | `tests/` 目录结构与包结构一一对应，契约测试覆盖每个注册的插件 |

---

## 2. 顶层布局

```
corpuslab/                          # 仓库根
├── pyproject.toml                 # 包元数据 + 依赖 + entry points
├── README.md
├── docs/
│   ├── config-design.md           # 配置设计
│   ├── checkpoint-design.md       # 检查点与状态库设计
│   └── project-structure.md       # 本文
├── corpuslab/                      # 主包（平铺于仓库根）
│   ├── __init__.py
│   ├── cli.py                      # 入口：run/clean/score/validate
│   ├── __main__.py                 # 支持 python -m corpuslab 等价启动
│   ├── config/
│   ├── core/
│   ├── sources/
│   ├── strategies/
│   ├── stages/
│   ├── judges/
│   ├── llm/
│   ├── embedding/
│   └── sinks/
├── examples/
│   ├── corpuslab.yaml              # 完整可跑配置
│   ├── seeds.jsonl
│   └── documents.jsonl
└── tests/
    ├── conftest.py
    ├── config/
    ├── core/
    ├── strategies/
    ├── stages/
    ├── judges/
    └── e2e/
```

> **为何用平铺布局**：仓库只有唯一发行版（`corpuslab`）加 docs/examples/tests，
> 不存在命名冲突风险；开发用 `pip install -e .`，src 布局"防误 import 本地目录"
> 的收益为零，却多一层嵌套。主包直接位于仓库根，更简洁。

---

## 3. 包结构与模块职责

### 3.1 `config/` — 配置加载与解析

| 模块 | 职责 |
|------|------|
| `schema.py` | pydantic 声明式 Schema：字段类型、默认值、必填约束；未知键报错并给别名迁移提示 |
| `loader.py` | YAML → 配置对象：env 回退、端点解析（§10.1）、产量解析（§10.3）、格式推导 |
| `validate.py` | 加载期校验：死键、产量冲突、链合法性、端点引用、资源存在性；**按子命令区分必填集** |

**依赖**：仅依赖 `pydantic`，不依赖任何领域模块。

### 3.2 `core/` — 领域内核

| 模块 | 职责 |
|------|------|
| `sample.py` | `Sample` / `TaskSpec` / `Score` / `RunReport` 数据类：canonical 形态、`id`、`lineage`、`metrics.kept_by` |
| `contracts.py` | 五抽象 Protocol（见 §4）+ `Material` 类型；`StreamingStage` / `BatchStage` 两个阶段协议 |
| `registry.py` | 注册表：`register_strategy / register_stage / register_judge / register_source / register_renderer / register_sink`；entry point 自动发现 |
| `store.py` | **DuckDB 状态库**（S4 唯一持久化点）：建表、事务化写入、指纹/签名/评分/向量缓存、manifest、`cache_cleanup` |
| `pipeline.py` | 阶段编排：全局单实例、流式段分组、批式屏障（pending 表背压）、drop 计数 |
| `planner.py` | 产量解析：weight 归一化分摊、余数分配、count 覆盖、`--strategy` 过滤重分摊 |
| `checkpoint.py` | resume：manifest 兼容性检查、终态集合、LSH 索引重建、对账 |
| `context.py` | `RunContext`：seed、rng、端点解析结果、事件总线（进度/日志） |

### 3.3 `sources/` — 原料读取

| 模块 | 职责 |
|------|------|
| `topics.py` | 主题列表（含 weight 归一化） |
| `seeds.py` | 种子 JSONL + `field_map` 适配 |
| `documents.py` | `.md/.txt/.json/.jsonl` 加载、Unicode 规范化、`field_map` |
| `chunking.py` | 结构分块 / 语义分块（消费 `embedding` client） |
| `tools.py` | tool 规格（OpenAI function schema）校验与加载 |
| `file.py` | **FileSource**：外来 JSONL/DuckDB → canonical Sample 的反向适配（`clean/score` 的入口） |

### 3.4 `strategies/` — 七策略（共享骨架）

| 模块 | 职责 |
|------|------|
| `base.py` | `PlanExecuteStrategy` 骨架：Plan（多样性任务单 + 确定性 id）→ Execute（并发填充）；`_safe()` 统一包「调用 + 解析」 |
| `topic_driven.py` | 概念源：槽位笛卡尔采样 + knowledge 注入 |
| `deep_thinking.py` | 概念源 + 强制 reasoning |
| `seed_driven.py` | 样本源：few-shot / 交叉 / 变异轮盘 |
| `evol_instruct.py` | 多轮进化：深度/广度、`ratio_bounds` 闸门、进化链 lineage、**每轮一个 id** |
| `document_qa.py` | 分块 → 依据原文生成 QA，`source_text` 入 metadata |
| `backtranslation.py` | 反推指令，**源文本锁定为 output** |
| `tool_call.py` | 轨迹生成 + 强校验解析器（未知函数/非法 JSON/重复 ID/断链拒绝） |

### 3.5 `stages/` — 治理阶段

| 模块 | 阶段 | 调度 |
|------|------|------|
| `length.py` | `length` | 流式 |
| `exact_dedup.py` | `exact_dedup`（SHA256，状态入 `fingerprints`） | 流式 |
| `stats.py` | `stats`（特殊字符/重复率/n-gram 多样性） | 流式 |
| `minhash.py` | `minhash_dedup`（签名入 `minhash_sigs`，LSH 索引为视图） | 流式 |
| `semantic.py` | `semantic_dedup` / `cluster_dedup`（pending 表背压 + embeddings 缓存） | 批式 |

### 3.6 `judges/` — 评审

| 模块 | 职责 |
|------|------|
| `llm_judge.py` | LLM-as-Judge：维度提示构造、JSON 分数解析（走 `llm/client` 重试；结果入 `scores` 缓存） |
| `local.py` | 本地 scorer：`fasttext`（可选 extra；未安装时引用报明确错误） |
| `aggregate.py` | 多裁判聚合：语义见 README.md「评审」一节（逐维聚合 → 本地缩放 → 求和；`min_judges` / `max_disagreement` 不满足即 drop） |

### 3.7 `llm/` / `embedding/` — 基础设施

| 模块 | 职责 |
|------|------|
| `llm/client.py` | **唯一**重试原语 `retry_with_backoff`、按端点熔断器、每端点信号量、OpenAI 兼容调用 |
| `llm/endpoints.py` | 端点解析缓存：`resolve(name) → ResolvedEndpoint` |
| `embedding/client.py` | 全局唯一 embedding client：批处理、`store.embeddings` 内容寻址缓存、与 llm 同样的重试语义 |

### 3.8 `sinks/` — 渲染与落盘

| 模块 | 职责 |
|------|------|
| `renderers.py` | `alpaca / chatml / sharegpt / openai` 四渲染器（纯函数）+ `thinking` 的 `<think>` 渲染 + 反向解析（FileSource 用） |
| `duckdb_sink.py` | 主 Sink：渲染后样本入 `samples` 表（事务：投影 + committed 事件 + pending 清理） |
| `jsonl_sink.py` | JSONL 导出（`storage.export_jsonl` 或 `storage.type: jsonl` 传统模式） |
| `report.py` | 运行报告：drop 原因瀑布、评分分布、成本估算 |

---

## 4. 核心契约

`core/contracts.py` 定义五个 Protocol。**插件只依赖协议，引擎只依赖协议**——这是 S2 的全部内容。

```python
from typing import Protocol, AsyncIterator
from corpuslab.core.sample import Sample, TaskSpec, Score, RunReport

class Material(Protocol):        # 原料的统一只读视图
    kind: str                    # "topic" | "seed" | "document" | "tool" | "file"
    payload: dict

class Source(Protocol):
    kind: str
    def open(self, cfg, ctx) -> AsyncIterator[Material]: ...

class Strategy(Protocol):
    type: str
    async def plan(self, materials, budget, ctx) -> AsyncIterator[TaskSpec]: ...
    async def execute(self, specs: AsyncIterator[TaskSpec], ctx) -> AsyncIterator[Sample]: ...

class StreamingStage(Protocol):
    type: str
    async def apply_stream(self, stream: AsyncIterator[Sample], ctx) -> AsyncIterator[Sample]: ...

class BatchStage(Protocol):
    type: str
    async def apply_batch(self, samples: list[Sample], ctx) -> list[Sample]: ...

class Judge(Protocol):
    async def score(self, sample: Sample, ctx) -> Score: ...

class Sink(Protocol):
    async def write(self, stream: AsyncIterator[Sample], ctx) -> RunReport: ...
```

要点：
- **流式与批式是两个协议**：流式阶段实现 `StreamingStage`，批式阶段实现 `BatchStage`（async——批式需要调用 embedding client）；不存在「要求同时实现两个入口」的胖接口；
- **`TaskSpec.id` / `Sample.metadata.id` 在 Plan 期派生**（确定性，见 checkpoint-design.md §3）——断点幂等键；
- `Sample` 是全流水线**唯一**流通单位，四种输出格式只是 Sink 端渲染器（S5）；
- `RunReport` 汇总各阶段 drop 计数与评分分布，由 `sinks/report.py` 产出。

---

## 5. 控制流

### 5.1 `run`（完整流水线，内存直通）

```
cli.run
  → config.loader.load(path)                     # env 回退 + 端点/产量解析
  → config.validate.check(cfg, subcommand)       # 死键/冲突拦截（按子命令区分必填集）
  → core.planner.allocate(cfg)                   # weight 分摊 → 各策略 budget
  → store.open(output)                           # DuckDB 状态库；resume 时走 checkpoint.restore
  → registry 按名装配 sources / strategies / stages / judges / sinks
  → asyncio:
       for strategy in strategies:
           source.open(cfg) → strategy.plan() → planned 入库（跳过 terminal）
                                     │
                                     ▼ execute（并发，端点信号量）
       合并流 ──▶ pipeline.run(全局单实例)        # 流式段与生成并发；批式屏障（pending 表背压）
                     │
                     ▼ judge.score(sample)        # scores 缓存命中即跳过 → aggregate → min_total
                     │
                     ▼ sink.write(render(sample)) # 事务提交：samples + committed 事件
  → RunReport
```

**所有策略的 execute 汇入同一条 pipeline**：去重状态跨策略共享（README.md「治理链」一节）。

### 5.2 `clean` / `score`（同一条流水线的截取）

```
clean : FileSource → pipeline → Sink            # 无策略、无评审
score : FileSource → judge → Sink               # 无策略、无治理
```

`FileSource`（`sources/file.py`）把外来 JSONL/DuckDB 反向适配为 canonical Sample（`--input-format` 指定 alpaca/chatml/sharegpt/openai/flat，`--field-map` 做字段改名）。

### 5.3 断点交互

```
Plan：spec 入 planned 表（id 主键，幂等）
Execute 前：terminal = samples ∪ dropped → 跳过
阶段判定：pass 时状态（指纹/签名）与判定同事务写入；drop 时入 dropped 表
屏障：批式阶段把在途样本写入 pending 表（磁盘背压），屏障后清理
提交：BEGIN; INSERT samples; INSERT events('committed'); DELETE pending; COMMIT
resume：manifest 兼容性检查 → 重建 LSH 索引 → 重载 pending/scores/embeddings → 增量 Plan
```

不变式：**任何时刻中断进程，状态库中 `samples ∪ dropped` 恰等于已终态样本；重跑不产生重复样本**（重放幂等，见 checkpoint-design.md §5/§6）。

---

## 6. 并发模型

```
┌──────────────────────────── run 进程 ────────────────────────────┐
│                                                                  │
│  Strategy.execute ──▶ 端点信号量 (llm.concurrency / endpoints.*) │
│       │                    │                                     │
│       │              retry_with_backoff（唯一重试原语）           │
│       ▼                    │                                     │
│  流式 Stage 组 ──▶ 单消费者协程（pull 模型，天然背压）             │
│       │                                                          │
│  批式 Stage ──▶ 屏障（pending 表背压，apply_batch 一次执行）       │
│       │                                                          │
│  Judge ──▶ 各端点信号量（与生成共享同一把锁）                      │
│                                                                  │
│  Store ──▶ DuckDB 连接只在事件循环内使用（单写者，无锁）           │
└──────────────────────────────────────────────────────────────────┘
```

- **信号量按端点持有**：`llm.concurrency: 10` 与 `endpoints.pro.concurrency: 4` 各自独立计数，生成与评审共享同一把锁，不会超卖；
- **熔断按端点独立计数**：评审端点故障不中止生成；所有在用端点均熔断才中止整轮（退出码 3，状态库保留）；
- **策略层无并发配置**（S4）；
- 全部 I/O 基于 `asyncio`；`fasttext`、MinHash 等 CPU 密集步骤经 `asyncio.to_thread` 卸载，避免阻塞事件循环。

---

## 7. 错误处理与可靠性

| 层 | 机制 | 位置 |
|----|------|------|
| 单次调用 | `retry_with_backoff`：指数退避，`attempts/backoff/max_delay` | `llm/client.py`（唯一实现） |
| 解析失败 | 与网络失败同路径重试（`Strategy._safe` 包「调用 + 解析」） | `strategies/base.py` |
| 整轮运行 | 熔断：滑动窗口重试占比 > `max_retry_ratio` → 中止 + 状态库保留 | `llm/client.py` |
| 样本级 | Stage drop(reason) 不中断运行，计入报告 | `core/pipeline.py` |
| 断点 | DuckDB 事务原子提交；`resume` 幂等续跑 | `core/store.py` / `core/checkpoint.py` |
| 配置 | 加载期拦截（死键/冲突/资源缺失） | `config/validate.py` |

**不变式**：任何时刻中断进程，已提交样本与状态库标记一致；重跑不产生重复样本。

---

## 8. CLI 设计

```
corpuslab run       [-c CONFIG] [--count N] [--preview] [--resume] [--strategy S] [--discard-state]
corpuslab clean     INPUT [-o OUTPUT] [-c CONFIG] [--input-format F] [--field-map JSON]
corpuslab score     INPUT [-o OUTPUT] [-c CONFIG] [--input-format F] [--field-map JSON]
corpuslab validate  [-c CONFIG] [run|clean|score]
```

| 约定 | 说明 |
|------|------|
| `-c` 缺省 | 依次找 `./corpuslab.yaml`、`./corpuslab.yml` |
| `--count` | 覆盖 `plan.count`（仅 run） |
| `--preview` | 覆盖 `run.preview`：小批量（`preview_count`，缺省 8）、不落库、不落断点（**仍调 LLM**） |
| `--discard-state` | manifest 不兼容时丢弃受影响状态并继续（缺省拒绝） |
| `clean/score` 的 `-o` | DuckDB 库路径（`.duckdb`）或 JSONL 路径（`.jsonl` → 自动切 jsonl 模式） |
| 退出码 | 0 成功；2 配置错误；3 运行期熔断/中断（状态库保留）；4 输入资源缺失 |

### 启动链路

```toml
[project.scripts]
corpuslab = "corpuslab.cli:main"     # pip install -e . 后，venv/bin/ 下生成 corpuslab 可执行包装
```

```bash
corpuslab run -c config.yaml        # console script（推荐）
python -m corpuslab run             # 经 __main__.py，效果完全等价
```

**`main()` 内部按固定顺序执行**，任一步失败即短路返回对应退出码：

```
main(argv)
  → argparse 解析子命令与参数
  → 定位配置：-c 路径 ?? ./corpuslab.yaml ?? ./corpuslab.yml        （找不到 → 退出码 2）
  → config.loader.load()    # env 回退、端点解析、产量解析、格式推导
  → config.validate.check() # 死键 / 产量冲突 / 资源存在性（按子命令） （失败 → 退出码 2 / 4）
  → 构造 RunContext         # seed、已解析端点、事件总线
  → registry 按名装配       # sources / strategies / stages / judges / sinks
  → asyncio.run(子命令协程)                                        （熔断 → 退出码 3）
  → RunReport 汇总输出，返回退出码 0
```

CLI 层自身**不含业务逻辑**：只做「解析 → 加载 → 校验 → 装配 → 交给引擎」（S1）。

---

## 9. 测试结构

```
tests/
├── conftest.py              # 通用 fixture：假 LLM（脚本化响应）、样本工厂、tmp 配置
├── config/
│   ├── test_schema.py       # 死键报错、别名提示、必填约束
│   ├── test_loader.py       # env 回退、端点解析、产量解析、格式推导推导
│   └── test_validate.py     # 冲突/链合法性/资源存在/按子命令必填集
├── core/
│   ├── test_pipeline.py     # 流式分组、批式屏障、drop 计数、跨策略去重
│   ├── test_planner.py      # weight 归一化分摊、余数、count 覆盖、--strategy 重分摊
│   ├── test_store.py        # 事务原子性、幂等 upsert、cache_cleanup 分级
│   └── test_checkpoint.py   # resume 幂等、manifest 兼容性、LSH 重建、中断重跑
├── strategies/              # 每策略一个文件：契约测试 + id 确定性 + lineage 断言
├── stages/                  # 每阶段：边界值 + drop 原因 + 状态入库
├── judges/                  # 分数解析、聚合语义（DESIGN §6.3）、分歧过滤
└── e2e/
    ├── test_run_topic.py    # 假 LLM 全流程（DuckDB 断言）
    └── test_clean_score.py  # 独立命令 + FileSource 反向适配
```

**测试策略**：
- LLM 一律 fake（预置响应），E2E 零真实调用；
- 每个注册插件跑同一套契约测试（S6），新插件自动纳入；
- 断点幂等有专门的 resume 测试（中断后重跑，比对 `samples` 集合与去重状态）。

---

## 10. 打包与依赖

```toml
[project]
name = "corpuslab"
requires-python = ">=3.10"
dependencies = [
    "httpx",            # OpenAI 兼容调用
    "pydantic>=2",      # 配置 Schema
    "pyyaml",
    "datasketch",       # MinHash LSH
    "duckdb",           # 状态库后端（唯一持久化点）
]

[project.optional-dependencies]
fasttext = ["fasttext-wheel"]   # 本地 scorer，可选

[project.scripts]
corpuslab = "corpuslab.cli:main"

[project.entry-points."corpuslab.strategies"]
topic_driven = "corpuslab.strategies.topic_driven:TopicDrivenStrategy"
# ... 七策略

[project.entry-points."corpuslab.stages"]
length = "corpuslab.stages.length:LengthStage"
# ... 各阶段

[project.entry-points."corpuslab.sources"]
file = "corpuslab.sources.file:FileSource"

[project.entry-points."corpuslab.judges"]
llm = "corpuslab.judges.llm_judge:LLMJudge"

[project.entry-points."corpuslab.renderers"]
alpaca = "corpuslab.sinks.renderers:render_alpaca"
# ...

[project.entry-points."corpuslab.sinks"]
duckdb = "corpuslab.sinks.duckdb_sink:DuckDBSink"
jsonl = "corpuslab.sinks.jsonl_sink:JsonlSink"
```

- DuckDB 为**默认且唯一内建后端**；JSONL 是导出格式；
- entry points 驱动注册表（S3）：第三方包可发布新策略/阶段/原料/渲染器/存储，无需改 corpuslab 源码；内建插件同时以装饰器注册，**entry point 缺失时内建仍可用**；
- 平铺布局（单一发行版置于仓库根）+ `pip install -e .` 开发。

---

## 11. 扩展指南

### 11.1 新增策略

```
1. corpuslab/strategies/my_strategy.py
   @register_strategy("my_strategy")
   class MyStrategy(PlanExecuteStrategy): ...
2. tests/strategies/test_my_strategy.py       # 契约测试
3. entry point 注册
```

只需实现 `plan / execute`；重试、并发、熔断、断点、持久化由骨架与基础设施承担（S4）。

### 11.2 新增治理阶段

```python
@register_stage("my_filter", scheduling="streaming")
class MyFilter:
    async def apply_stream(self, stream, ctx): ...

@register_stage("my_batch_filter", scheduling="batch")
class MyBatchFilter:
    async def apply_batch(self, samples, ctx) -> list[Sample]: ...
```

在配置 `pipeline` 中按名引用即可；批式阶段只需实现 async 的 `apply_batch`——**不存在被迫实现的空方法**。

### 11.3 新增原料 / 评审 / 渲染器 / 存储

| 扩展点 | 实现 | 注册 |
|--------|------|------|
| 原料 | `Source.open` | `corpuslab.sources` entry point |
| 本地 scorer | `Judge.score` 纯函数 | `corpuslab.judges` |
| 输出格式 | 渲染纯函数 `Sample → dict` | `corpuslab.renderers` |
| 存储 | `Sink.write` | `corpuslab.sinks` |

**共同约束**：插件不得 import `cli` / 具体兄弟插件；不得自开数据库连接——持久化一律经 `ctx.store`（S1/S2/S4）。

**公开扩展面**（各模块以 `__all__` 白名单约束通配导入）：插件作者只依赖
`corpuslab.core.registry`（`register_strategy / register_stage / …`）、
`corpuslab.core.contracts`（五个 Protocol）、`corpuslab.core.sample`
（`Sample / TaskSpec / Score / derive_id`）、`corpuslab.strategies.base`
（`PlanExecuteStrategy`）。内部实现走深导入，不在 `__init__` re-export。

---

*corpuslab 项目结构设计 · 与 README.md「架构」及 checkpoint-design.md 保持一致 · 结构变更须同步更新本文。*
