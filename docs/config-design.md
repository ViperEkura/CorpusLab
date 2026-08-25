# corpuslab 配置设计

> 配置是 corpuslab 唯一的行为入口。本文定义配置的**目标 Schema、解析规则、校验规则与迁移路径**。
> 总体架构见 [README.md](../README.md)；代码组织见 [project-structure.md](project-structure.md)；
> 检查点与状态库见 [checkpoint-design.md](checkpoint-design.md)。

---

## 目录

1. [设计原则](#1-设计原则)
2. [配置总览](#2-配置总览)
3. [run — 运行控制](#3-run--运行控制)
4. [llm / endpoints / embedding — 端点](#4-llm--endpoints--embedding--端点)
5. [plan — 产量](#5-plan--产量)
6. [strategies — 策略](#6-strategies--策略)
7. [pipeline — 治理流水线](#7-pipeline--治理流水线)
8. [judge — 评审](#8-judge--评审)
9. [output — 落盘](#9-output--落盘)
10. [解析规则](#10-解析规则)
11. [校验规则](#11-校验规则)
12. [从 alembic 迁移](#12-从-alembic-迁移)
13. [完整示例](#13-完整示例)

---

## 1. 设计原则

配置面是行为契约，本文所有规则由以下七条裁决（与 README.md「设计原则」一节一致）：

| # | 原则 | 在配置上的体现 |
|---|------|----------------|
| P1 | 一处生效 | 同一语义（长度、去重、并发、温度、产量）只出现一次 |
| P2 | 无死开关 | 不提供无行为差异的键；未实现的能力不入 Schema |
| P3 | 分层默认，显式三层封顶 | `llm` 全局默认 → `endpoints.<name>` 差异声明；第三层**只允许 `params` 类字段**（`phases.<p>`） |
| P4 | 阶段即插件 | `pipeline` 是有序阶段链，每个阶段一个 `type` + 参数 |
| P5 | 产量声明唯一 | `plan.count` 声明总量，`weight` 声明比例，策略 `count` 仅作份额覆盖；边界见 §10.3 |
| P6 | 可溯源、尽量可复现 | `run.seed` 控制全部本地随机源；id 确定性保证断点可续 |
| P7 | 状态即库 | DuckDB 单后端承载输出与检查点，配置里没有"检查点间隔"这类旋钮 |

**补充约束**：
- **环境变量优先**：密钥与端点缺省读 `$API_KEY / $BASE_URL / $EMBEDDING_API_KEY / $EMBEDDING_BASE_URL`，配置文件里不鼓励出现明文密钥；
- **缺省可推导**：能从上下文推导的（如 `format` 由策略推导、`storage.path` 即 `output.path`）不要求显式声明；**全部推导规则在 §10 逐条列明，别无隐式规则**；
- **别名不进 Schema**：历史别名（`total_count` 等）由 loader 给出迁移提示，不作为合法键。

---

## 2. 配置总览

```yaml
run:        {seed, preview, preview_count}    # 运行控制
llm:        {model, api_key, base_url, ...}   # 全局默认端点
embedding:  {model, api_key, base_url, ...}   # 全局唯一 embedding 端点
endpoints:  {<name>: {...}}                   # 可选命名端点（差异声明）
plan:       {count}                           # 产量（唯一入口）
strategies: [{type, weight, ...}]             # 合成策略（run 必填）
pipeline:   [{type, ...}]                     # 治理阶段（有序）
judge:      {dimensions, judges, scorers}     # 评审
output:     {path, format, ...}               # 落盘（DuckDB 后端）
```

| 段 | 必填（run） | 必填（clean/score） | 缺省行为 |
|----|------|------|----------|
| `run` | 否 | 否 | 不固定种子；非预览模式 |
| `llm` | **是**（至少 `model`） | score 需要 | 其余字段走 env / 内置默认 |
| `embedding` | 否 | 否 | 首次被语义阶段引用时才需要 |
| `endpoints` | 否 | 否 | 不声明则所有消费者用 `llm` |
| `plan` | 条件 | 否 | 见 §10.3 |
| `strategies` | **是**（≥1 条） | **不适用**（clean/score 无策略） | — |
| `pipeline` | 否 | 否 | 空链 = 不治理（validate 警告） |
| `judge` | 否 | 否 | 不评审 |
| `output` | **是**（至少 `path`） | 由 `-o` 或本段给出 | 其余字段推导 |

---

## 3. run — 运行控制

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `seed` | int | — | 固定后本地随机源可控：采样、槽位组合、变异选择、打乱全部受控 |
| `preview` | bool | false | 预览模式：小批量、不写输出、不落断点。**仍会调用 LLM、产生费用** |
| `preview_count` | int | 8 | 预览批量（显式声明，避免"小批量"歧义） |

> 原名 `dry_run` 弃用：dry-run 业界预期零副作用，与本语义相悖。

---

## 4. llm / endpoints / embedding — 端点

### 4.1 `llm` — 全局默认端点

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `model` | string | **必填** | 模型名 |
| `api_key` | string | `$API_KEY` | 留空读环境变量 |
| `base_url` | string | `$BASE_URL` | 留空读环境变量；OpenAI 兼容协议 |
| `lang` | string | `en` | 提示语言（`zh`/`en`），作用于生成与评审 |
| `concurrency` | int | 1 | 该端点并行调用上限（信号量） |
| `params` | dict | `{}` | 采样参数：`temperature`、`max_tokens`、`top_p` 等原样透传 |
| `retry` | dict | 见下 | 重试：`attempts`(3)、`backoff`(2.0)、`max_delay`(30s) |
| `breaker` | dict | 见下 | 熔断：`window`(50s)、`max_retry_ratio`(0.9)；**按端点独立计数** |

```yaml
llm:
  model: deepseek-v4-flash
  lang: zh
  concurrency: 10
  params: {temperature: 0.8, max_tokens: 2048}
  retry:   {attempts: 3, backoff: 2.0, max_delay: 30}
  breaker: {window: 50, max_retry_ratio: 0.9}
```

**`retry` 与 `breaker` 的分工**：`retry` 管「单次调用的韧性」（指数退避重试）；`breaker` 管「整轮运行的安全」（滑动窗口内重试占比超阈值 → 中止并保留状态库）。二者语义不同，缺一不可。

### 4.2 `endpoints` — 命名端点（差异声明）

```yaml
endpoints:
  pro:  {model: deepseek-v4-pro}          # 只写与 llm 的差异
  flash: {}                               # 无差异（显式具名，便于引用）
```

解析规则见 §10.1。消费者按名引用：`judge.endpoint: pro`、`judges[].endpoint: flash`。

### 4.3 `embedding` — 全局唯一 embedding 端点

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `model` | string | `text-embedding-v3` | 模型名 |
| `api_key` | string | 回退链见 §10.4 | 显式值 → `$EMBEDDING_API_KEY` |
| `base_url` | string | 同上 | 显式值 → `$EMBEDDING_BASE_URL` |
| `batch_size` | int | 32 | 批大小 |

消费方两处：`document_qa` 语义分块、`semantic_dedup`/`cluster_dedup` 阶段。**单一端点，两处消费，不重复配置。**

---

## 5. plan — 产量

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `count` | int | — | 总生成条数；CLI `--count` 可覆盖 |

产量解析算法见 §10.3。原则：**总量在 `plan`，比例在 `weight`，策略显式 `count` 只作份额覆盖**。
`plan.count` 是**生成条数**（过滤前）；最终产量受 pipeline 与 `judge.min_total` 支配（运行报告给出 drop 瀑布），不设自动补偿生成。

---

## 6. strategies — 策略

通用字段（所有策略共享）：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `type` | string | **必填** | 策略类型，见下 |
| `weight` | float | 1.0 | `plan.count` 分摊比例（**自动归一化**，validate 提示） |
| `count` | int | — | 显式覆盖本策略份额（边界见 §10.3） |
| `field_map` | dict | `{}` | 输入文件字段 → 规范字段映射（外来数据适配） |

### 6.1 七策略参数表

#### `topic_driven` — 概念源

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `topics` | array | **必填** | 主题列表 |
| `topics[].topic` | string | **必填** | 主题名 |
| `topics[].weight` | int | 1 | 主题内配比（自动归一化） |
| `topics[].knowledge` | string | — | 知识背景，注入提示以保证内容准确 |
| `dimensions` | array | 内置 3 维 | 正交槽位：`[{name: difficulty, vals: [...]}]`，Plan 阶段笛卡尔采样 |
| `multi_turn` | bool | 继承 `output.multi_turn` | 生成多轮对话 |
| `require_reasoning` | bool | false | 要求 `reasoning` 字段 |

> 原 `total_count / samples_per_topic / max_samples_per_request / execution_max_per_request / concurrency / two_stage` 全部移除：产量并入 `plan`，批大小是引擎常量，两段式是唯一实现路径。

#### `deep_thinking` — 概念源 + reasoning

与 `topic_driven` 同族，支持其全部字段（`topics / dimensions / multi_turn`）；固定产出 `reasoning` + `output`；`output.thinking: true` 时渲染 `<think>` 块。

#### `seed_driven` — 样本源

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `seed_file` | string | **必填** | 种子 JSONL |
| `example_num` | int | 3 | few-shot 参考样例数（用户可感知的语义，保留） |
| `topic` | string | 随机 | 固定主题；种子自带 topic 时优先 |
| `evolution` | dict | — | 遗传算子，见下 |

`evolution`（轮盘选择：`crossover` → `mutate` → 其余 few-shot）：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `crossover` | float | 0.0 | 交叉概率 |
| `mutate` | float | 0.0 | 变异概率（两者之和 >1 时自动归一化） |
| `mode` | string | `instruction_output` | `instruction_output`（A 指令 + B 输出风格）/ `compose`（合并主题） |
| `mutations[]` | array | — | 变异类型：`{name, prompt, values?, override_field?}`；`prompt` 支持 `{value}` 占位 |

#### `evol_instruct` — 样本源 + 多轮进化

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `seed_file` | string | **必填** | 种子 JSONL |
| `max_rounds` | int | 3 | 最大进化轮数（≥1） |
| `depth_rate` | float | 0.7 | 每轮每指令走深度进化的概率 |
| `branch_factor` | int | 1 | 每轮每指令广度分支数；0 禁用广度 |
| `depth_mutations` | array | 4 算子 | `[{name, prompt}]`；默认 `add_constraint / deepen / concretize / increase_reasoning` |
| `ratio_bounds` | [float, float] | [0.5, 5.0] | 进化后/原指令长度比上下界，越界丢弃 |
| `generate_output` | bool | true | false 则只产指令 |
| `require_reasoning` | bool | false | 回答附推理链 |
| `include_seeds` | bool | false | 输出包含原始种子（round=0） |
| `phases.evolve` | dict | 继承 `llm.params` | 进化阶段参数覆盖：`{temperature, max_tokens}` |
| `phases.answer` | dict | 继承 `llm.params` | 回答阶段参数覆盖 |

每条样本携带 `lineage.evolution_chain`（完整进化链）；**每轮一个 id**（`evol:{seed_id}:r{round}:{k}`），支持进化中途续跑。

#### `document_qa` — 文档源

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `document_file` | string | **必填** | `.md/.markdown/.txt/.json/.jsonl` |
| `chunking.enabled` | bool | false | 是否分块 |
| `chunking.mode` | string | `structure` | `structure` / `semantic` |
| `chunking.min_chunk_length` | int | 200 | 建议最小块长 |
| `chunking.max_chunk_length` | int | 1500 | 块长上限 |
| `chunking.similarity_threshold` | float | 0.55 | 相邻余弦低于此值切块（semantic 模式） |
| `max_instruction_length` | int | 500 | 指令上限 |
| `max_output_length` | int | 4000 | 回答上限 |
| `reject_context_references` | bool | true | 拒绝「根据上文/给定文档」类指令 |

> embedding 模型与批大小来自全局 `embedding` 段，不再在 chunking 内重复。文档加载前自动做 Unicode 规范化（BOM/零宽/控制符清除，Markdown 与中文标点保留）。

#### `instruction_backtranslation` — 文档源 + 反推

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `document_file` | string | **必填** | 源文档 JSONL（默认 `text` 字段） |
| `min_document_length` | int | 50 | 源文本最小字符数 |
| `max_document_length` | int | 4000 | 源文本最大字符数 |
| `max_instruction_length` | int | 500 | 反推指令上限 |
| `reject_context_references` | bool | true | 拒绝依赖缺失上下文的指令 |
| `shuffle` | bool | false | 截取 `count` 前打乱 |
| `phases.backtranslate` | dict | 继承 `llm.params` | 反推调用参数覆盖 |

**不变式**：模型返回的 `output` 一律丢弃，最终 `output` = 规范化源文本（防事实漂移）。

#### `tool_call` — 规格源

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `tools` | array | **必填** | OpenAI function tool 定义 |
| `topics` | array | 内置通用任务 | 轮换生成主题 |
| `system_prompt` | string | 内置英文提示 | 导出数据中的 system 消息 |
| `max_tool_calls_per_sample` | int | 2 | 每样本最多工具调用数 |

固定导出 `messages + tools`；解析器拒绝未知函数、非法参数 JSON、重复调用 ID、断裂调用关系。

---

## 7. pipeline — 治理流水线

有序阶段链，**写即生效、不写即关闭**。每阶段一个 `type` + 参数。

| 阶段类型 | 调度 | 参数（默认） | 语义 |
|----------|------|--------------|------|
| `length` | 流式 | `instruction: [5, 4000]`、`output: [10, 8000]` | 长度门禁 |
| `exact_dedup` | 流式 | — | SHA256 精确去重（状态入 `fingerprints` 表） |
| `stats` | 流式 | `max_special_char_ratio: 0.3`、`max_word_repetition: 0.5`、`max_char_repetition: 0.5`、`min_ngram_diversity: 0.2`、`ngram_n: 3`、`unit: char` | 统计清洗 |
| `minhash_dedup` | 流式 | `threshold: 0.7`、`num_perm: 128`、`ngram_n: 3` | LSH 近似去重（签名入 `minhash_sigs` 表；threshold 改动 resume 兼容） |
| `semantic_dedup` | 批式 | `threshold: 0.85` | embedding 余弦去重（`pending` 表背压 + `embeddings` 缓存） |
| `cluster_dedup` | 批式 | — | LSH 聚簇 → 拥挤簇语义精排 |

```yaml
pipeline:
  - {type: length, instruction: [5, 4000], output: [10, 8000]}
  - {type: exact_dedup}
  - {type: stats, min_ngram_diversity: 0.2}
  - {type: minhash_dedup, threshold: 0.7}
  - {type: semantic_dedup, threshold: 0.85}
```

调度形态由引擎按阶段类型推导（流式段与生成并发、批式设屏障），用户只声明顺序。

---

## 8. judge — 评审

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `endpoint` | string | `llm` | 远端评审所用端点名（单裁判） |
| `dimensions` | array | **必填**（启用评审时） | `[{name, label?, max?}]`，`max` 默认 10，分值 1~N |
| `min_total` | float | 0.0 | 总分阈值，低于即过滤（量纲 = Σ各维 max，见下） |
| `judges` | array | `[]` | 多裁判：`[{endpoint}]`；**非空时以 judges 为准，`endpoint` 被忽略（validate 提示）** |
| `aggregation` | string | `mean` | `mean/min/max/median`，作用于「同维度 × 各裁判」 |
| `min_judges` | int | 1 | 每样本最少成功裁判数，不足 → drop(`insufficient_judges`) |
| `max_disagreement` | float | 0 | 单维 (max−min) 分差上限；超出 → drop(`judge_disagreement`)；0 = 不启用 |
| `scorers` | array | `[]` | 本地打分器：`[{type: fasttext, model_path, weight}]`（fasttext 为可选 extra） |

**聚合语义**（精确定义，实现以此为准）：

1. 每维度：远端各裁判分数按 `aggregation` 聚合 → 维度值；
2. 本地 scorer 输出 [0,1] 原始值，缩放为 `维度 max × weight` 写入**同名维度**；同名维度远端优先，本地仅作 `score_source` 标注；
3. `total_score = Σ 维度值`（绝对分量纲）；`min_total` 与之同量纲比较；
4. `min_total` 不满足 → drop(`min_total`)。

---

## 9. output — 落盘（DuckDB 后端）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `path` | string | **必填** | **DuckDB 库文件路径**（唯一后端；缺省扩展名 `.duckdb`） |
| `format` | string | 按策略推导 | `alpaca / chatml / sharegpt / openai`；tool_call 自动推导为 `openai` |
| `multi_turn` | bool | false | 多轮对话（策略可覆盖） |
| `thinking` | bool | false | 把 `reasoning` 渲染进 `<think>` 块（原始 `reasoning` 字段始终保留） |
| `resume` | bool | false | 从状态库续跑（manifest 兼容性检查见 checkpoint-design.md §7） |
| `cache_cleanup` | bool | true | 成功后只清瞬时表（审计事件、在途缓冲）；**终态集合、去重状态、scores、embeddings 全部保留**（保证 resume 精确） |
| `storage.type` | string | `duckdb` | `duckdb`（默认）或 `jsonl`（传统纯文件模式，`path` 即 JSONL 文件） |
| `storage.table` | string | `samples` | DuckDB 表名 |
| `storage.export_jsonl` | string | — | 可选：成功后把渲染行导出为 JSONL 文件 |

> `storage.type: duckdb` 时 `output.path` 就是库文件——不存在"必填的 jsonl path + 另一个 duckdb path"的双路径矛盾。

---

## 10. 解析规则

**本节是全部隐式行为的完整清单，此外无任何推导。**

### 10.1 端点解析（P3 落地）

```
resolve_endpoint(name):
    base = config.llm                          # 全局默认
    if name in config.endpoints:
        base = merge(base, config.endpoints[name])   # 逐项覆盖，只写差异
    return base
```

- `endpoints.<name>` 未声明的字段逐项继承 `llm`，不整段替换；
- 阶段覆盖（`strategies[].phases.<p>`）**只允许** `params` 类字段（`temperature / max_tokens`），这是唯一允许的第三层。

### 10.2 格式推导

```
format = output.format                                  # 显式声明最优先
      ?? (存在 tool_call 策略 ? openai : alpaca)        # 缺省按策略推导
```

### 10.3 产量解析（P5 落地）

```
优先级：CLI --count > plan.count > Σ策略显式 count

plan.count 存在:
    weight 归一化后分摊（weight 之和不必为 1，自动归一，validate 提示）
    余数分配给权重最大者；策略显式 count 覆盖其份额
    显式 count 之和 > plan.count → validate error（防产量静默超发）
plan.count 缺省:
    各策略显式 count 之和；任一策略无 count → validate error（消息指明缺哪个策略）
--strategy S 过滤:
    被选中策略间按上述规则重新分摊；未选中策略不参与
```

### 10.4 环境变量回退

```
api_key  : 显式值 → $API_KEY
base_url : 显式值 → $BASE_URL
embedding.api_key  : 显式值 → $EMBEDDING_API_KEY（不再回退 llm.api_key，防密钥误发第三方端点）
embedding.base_url : 显式值 → $EMBEDDING_BASE_URL
```

---

## 11. 校验规则

`corpuslab validate [run|clean|score]` 在加载期拦截（也内嵌于各命令前置检查）；**必填集按子命令区分**：

| 类别 | 规则 | 级别 |
|------|------|------|
| 死键 | 未知字段、历史别名（`total_count` → 提示改 `count`） | error |
| 必填（run） | `llm.model`、`strategies` 非空、`output.path`、启用评审时 `judge.dimensions` | error |
| 必填（score） | `llm.model`、`judge.dimensions`（或 scorers 非空）、输入存在 | error |
| 必填（clean） | 输入存在 | error |
| 产量冲突 | 规则见 §10.3（含"显式 count 之和 > plan.count"） | error |
| 链合法性 | `pipeline` 为空 | warning |
| 链合法性 | 批式阶段之后还有流式阶段 | warning（语义可运行但流式收益丢失） |
| 维度冲突 | `scorers` 维度与 `dimensions` 重名且语义不符 | warning |
| 端点引用 | `judge.endpoint` / `judges[].endpoint` 指向未声明端点 | error |
| 端点冗余 | `judges` 非空且 `judge.endpoint` 同时显式声明 | warning |
| 资源存在 | `seed_file / document_file / scorers[].model_path` 可读 | error |
| 范围 | `crossover + mutate ≤ 1`（超出自动归一化并提示）、`ratio_bounds[0] < [1]` | warning |
| 量纲 | `min_total / Σdimensions.max` 比值 < 0.3 或 > 1 | warning |

---

## 12. 从 alembic 迁移

| alembic | corpuslab | 说明 |
|---------|-----------|------|
| `api.*` | `llm.*` | 段改名；`retry.max_retries → attempts` |
| `api.auto_stop.*` | `llm.breaker.*` | 改名正名 |
| `api.lang` / `scoring.lang` | `llm.lang` | 合一 |
| `scoring.{model,api_key,base_url}` | `endpoints.<name>` + `judge.endpoint` | 引用替代复制 |
| `scoring.retry` / `scoring.params` | 端点继承 / `endpoints.<name>.params` | 合一 |
| `count`（全局） | `plan.count` | 移段 |
| `strategies[].total_count / target_count` | `strategies[].count` | 别名收敛 |
| `topic_driven.samples_per_topic` | 删除 | 用 `plan.count` + `topics[].weight` |
| `topic_driven.two_stage` | 删除 | 恒真，非配置 |
| `topic_driven.{concurrency,max_samples_per_request,execution_max_per_request}` | 删除 | 引擎内部常量 |
| `evol_instruct.evol_{concurrency,temperature,max_tokens}` | `phases.evolve` | 收敛为 params 覆盖 |
| `evol_instruct.answer_{temperature,max_tokens}` | `phases.answer` | 同上 |
| `evol_instruct.{min,max}_evolution_ratio` | `ratio_bounds` | 二合一 |
| `instruction_backtranslation.{temperature,max_tokens}` | `phases.backtranslate` | 同上 |
| `quality.*` + `cleaner.{instruction,output}_{min,max}_len` | `pipeline` 的 `length` 阶段 | 两份合一 |
| `quality.dedup` | `pipeline` 的 `exact_dedup` 阶段 | 开关变阶段 |
| `quality.remove_truncated` | 删除 | 死配置 |
| `cleaner.{minhash_dedup,embedding_dedup,dedup_chain}` | `pipeline` 各去重阶段 | 开关变阶段 |
| `cleaner.embedding_*` | 全局 `embedding.*` | 单点 |
| `cleaner.input_format / field_map` | CLI 参数 / `strategies[].field_map` | 归属理顺 |
| `scoring.dimensions[].max_score` | `dimensions[].max` | 缩短 |
| `scoring.min_total_score` | `judge.min_total` | 缩短 |
| `judges[].{model,api_key,base_url,api_key_env,base_url_env}` | `endpoints.<name>` + `judges[].endpoint` | 引用替代复制 |
| `dry_run` | `run.preview`（+ `preview_count`） | 改名正名 |
| `random_seed` | `run.seed` | 移段 |
| `output.checkpoint_interval` | 删除 | 状态库即检查点 |
| `output.cache_cleanup_on_success` | `output.cache_cleanup` | 缩短 |
| `output.path`（jsonl） | `output.path`（.duckdb） | DuckDB 唯一后端；jsonl 经 `storage.export_jsonl` 导出 |

---

## 13. 完整示例

```yaml
run:
  seed: 42
  preview: false

llm:
  model: deepseek-v4-flash
  lang: zh
  concurrency: 10
  params: {temperature: 0.8, max_tokens: 2048}
  retry:   {attempts: 3, backoff: 2.0, max_delay: 30}
  breaker: {window: 50, max_retry_ratio: 0.9}

embedding:
  model: text-embedding-v3
  batch_size: 32

endpoints:
  pro:  {model: deepseek-v4-pro}
  flash: {}

plan:
  count: 1000

strategies:
  - type: topic_driven
    weight: 0.5
    topics:
      - {topic: Python 编程基础, weight: 3, knowledge: "Python 是动态类型语言，支持面向对象与函数式编程。"}
      - {topic: 机器学习, weight: 2}
      - {topic: 数据库与 SQL, weight: 1}
    dimensions: [{name: difficulty, vals: [easy, medium, hard]}]

  - type: evol_instruct
    weight: 0.3
    seed_file: ./seeds.jsonl
    max_rounds: 3
    depth_rate: 0.7
    branch_factor: 1
    ratio_bounds: [0.5, 5.0]
    phases:
      evolve: {temperature: 0.8, max_tokens: 1024}
      answer: {temperature: 0.6, max_tokens: 2048}

  - type: seed_driven
    weight: 0.2
    seed_file: ./seeds.jsonl
    example_num: 3
    evolution:
      crossover: 0.3
      mutate: 0.3
      mode: instruction_output
      mutations:
        - {name: difficulty, values: [beginner, advanced],
           prompt: "Change the difficulty to '{value}'"}

pipeline:
  - {type: length, instruction: [5, 4000], output: [10, 8000]}
  - {type: exact_dedup}
  - {type: stats, max_special_char_ratio: 0.3, min_ngram_diversity: 0.2, ngram_n: 3, unit: char}
  - {type: minhash_dedup, threshold: 0.7, num_perm: 128, ngram_n: 3}

judge:
  dimensions:
    - {name: correctness, label: 准确性, max: 10}
    - {name: helpfulness, label: 实用性, max: 10}
    - {name: completeness, label: 完整性, max: 10}
  min_total: 20
  judges: [{endpoint: pro}, {endpoint: flash}]
  aggregation: mean
  min_judges: 2
  max_disagreement: 2
  scorers:
    - {type: fasttext, model_path: /path/cc.zh.300.bin, weight: 1.0}

output:
  path: ./generated_sft.duckdb      # DuckDB 状态库（唯一后端）
  format: alpaca
  multi_turn: false
  thinking: false
  resume: true
  storage:
    type: duckdb
    table: samples
    export_jsonl: ./generated_sft.jsonl
```

---

*corpuslab 配置设计 · 与 README.md「设计原则/配置」及 checkpoint-design.md 保持一致 · 配置面变更须同步更新本文。*
