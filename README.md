<div align="center">

# corpuslab

**声明式 LLM 训练数据流水线**

[快速开始](#-快速开始) · [配置总览](#-配置总览) · [扩展](#-扩展) · [设计文档](#-文档索引)

</div>

---

## 为什么需要它

写合成脚本、清洗脚本、去重脚本、评分脚本——这些步骤互相割裂，脚本越堆越多。
corpuslab 把它们收敛成**一条声明式流水线**：你只声明「要什么数据、怎么治理、怎么评审」，
引擎负责调度、并发、重试、熔断与断点。

| 你声明 | 引擎承担 |
|---|---|
| 原料类型与产量（`strategies` / `plan.count`） | 并发调度 · 端点信号量 |
| 治理链顺序（`pipeline`） | 重试退避 · 熔断（按端点独立计数） |
| 评审维度与阈值（`judge`） | 流式/批式调度 · 磁盘背压 |
| 输出位置（`output.path`） | 全部状态落 DuckDB，断点幂等续跑 |

## 一个最小例子

```yaml
llm:
  model: deepseek-v4-flash
  lang: zh
  concurrency: 2

plan: {count: 5}

strategies:
  - type: topic_driven
    topics:
      - {topic: Python 基础语法}
      - {topic: 机器学习入门}
    dimensions: [{name: difficulty, vals: [easy, medium]}]

pipeline:
  - {type: length, instruction: [5, 4000], output: [10, 8000]}
  - {type: exact_dedup}

judge:
  dimensions:
    - {name: helpfulness, label: 实用性, max: 10}
  min_total: 6

output:
  path: ./out          # 输出是一整个文件夹
```

```bash
corpuslab run -c config.yaml
```

产出长这样（真实的 DeepSeek flash 结果）：

```text
out/
├── corpuslab.duckdb    状态库：samples + 断点/去重/评审/embedding 全部状态
└── samples.parquet     列式导出（默认开启），DuckDB/Polars/Pandas 直接读

parquet 列: id / strategy / instruction / output / reasoning / messages / tools / metadata / total_score
```

```jsonc
// samples.parquet 里的一条样本
{
  "instruction": "请用简单例子说明 Python 中的动态类型是什么？并编写一个函数来展示这一点。",
  "output": "动态类型意味着变量在运行时可以指向任意类型的对象……x = 5 之后 x = 'hello'……",
  "metadata": {
    "id": "…", "strategy": "topic_driven",
    "lineage": {"topic": "Python 基础语法", "difficulty": "easy"},
    "metrics": {"total_score": 10, "kept_by": ["length", "exact_dedup"]}
  }
}
```

## 安装

```bash
pip install -e .                # httpx / pydantic / pyyaml / datasketch / duckdb
pip install -e ".[fasttext]"    # 可选：本地 scorer / 语言检测
```

> 凭证放 `.env`（`API_KEY=sk-...`），loader 自动读取且不入库。

## 快速开始

```bash
corpuslab validate -c examples/corpuslab.yaml      # 校验配置（死键/冲突/资源缺失）
corpuslab run -c examples/corpuslab.yaml           # 合成、治理、评审，输出到目录
corpuslab run -c examples/corpuslab.yaml --resume  # 断点续跑：已终态样本跳过，零重复花钱
corpuslab clean input.jsonl -o out -c corpuslab.yaml --input-format alpaca   # 对既有语料重跑治理段
corpuslab score input.duckdb -o scored -c corpuslab.yaml                      # 补评审

CORPUSLAB_FAKE_LLM=1 corpuslab run -c examples/corpuslab.yaml   # 离线冒烟：假 LLM，零网络
```

<details>
<summary>退出码约定</summary>

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 配置错误 / manifest 不兼容 |
| `3` | 运行期熔断或中断（状态库保留，可 --resume） |
| `4` | 输入资源缺失 |

</details>

## 工作原理

### 原料与策略

四类原料对应七个策略——策略的本质是「原料 × 变异算子」：

| 原料族 | 回答的问题 | 策略 |
|---|---|---|
| 概念源 | 知道聊什么 | `topic_driven`（槽位笛卡尔采样）· `deep_thinking`（强制思维链） |
| 样本源 | 知道长什么样 | `seed_driven`（few-shot/交叉/变异轮盘）· `evol_instruct`（多轮进化+链路溯源） |
| 文档源 | 知道事实是什么 | `document_qa`（依据原文出 QA）· `instruction_backtranslation`（反推指令，锁定原文为答案） |
| 规格源 | 知道能做什么 | `tool_call`（工具调用轨迹 + 强校验解析器） |

所有策略共享同一套 **Plan-Execute** 骨架：Plan 产出多样性任务单（带确定性 id，
LLM 调用之前就能判断"做没做过"），Execute 并发填充。新增策略只需实现两个方法，
重试、并发、熔断、断点由基础设施承担。

### 治理：阶段链

写即生效、不写即关闭；用户只声明顺序，不声明调度。

**内置阶段**

| 阶段 `type` | 调度 | 做什么 |
|---|---|---|
| `length` | 流式 | 长度门禁（instruction/output 上下限） |
| `exact_dedup` | 流式 | SHA256 精确去重，指纹入 `fingerprints` 表 |
| `stats` | 流式 | 统计清洗：特殊字符比 / 重复率 / n-gram 多样性 |
| `minhash_dedup` | 流式 | LSH 近似去重，签名入 `minhash_sigs`（阈值改动 resume 兼容） |
| `semantic_dedup` | 批式 | embedding 余弦去重，向量进内容寻址缓存 |
| `cluster_dedup` | 批式 | LSH 聚簇后对拥挤簇做语义精排 |

**两种调度形态**由引擎按阶段类型自动推导：

- **流式** —— 与生成并发执行，坏样本早失败、省 token；
- **批式** —— 屏障执行：在途样本先落 `pending` 表（磁盘背压），攒批后一次算完，
  崩溃恢复时屏障语义不丢。

### 评审：双通道

**远端** LLM-as-Judge（维度自定义、多裁判 `mean/min/max/median` 聚合 +
`min_judges / max_disagreement / min_total` 治理）与**本地 scorer**
（fasttext 质量分 · perplexity 困惑度评分）共用同一评分协议，
阈值过滤统一走 `judge.min_total`。得分进 `scores` 表缓存，resume 不重评。

### 输出：DuckDB 单后端

一个 `.duckdb` 文件承载十张表
（`samples / events / pending / embeddings / fingerprints / minhash_sigs / scores / dropped / planned / kv`）：
事务即原子性；`sample_id` 在 Plan 期确定性派生；
因此 `--resume` 续跑**零重复样本、零重复花钱**。

## 配置总览

```yaml
run:        {seed, preview, preview_count}                       # 运行控制
llm:        {model, base_url, api_key, lang, concurrency,
             params, retry, breaker}                             # 全局默认端点
embedding:  {model, batch_size}                                  # 全局唯一 embedding 端点
endpoints:  {pro: {model: ...}}                                  # 命名端点，逐项继承 llm，按名引用
plan:       {count}                                              # 产量唯一入口
strategies: [{type, weight, count, field_map, ...}]              # ≥1 条，四族七策略
pipeline:   [{type, ...}]                                        # 有序治理链
judge:      {dimensions, min_total, judges, aggregation,
             min_judges, max_disagreement, scorers, perplexity}  # 双通道评审
output:     {path, format, multi_turn, thinking, resume,
             storage: {type, export_parquet, export_jsonl}}      # 落盘与导出
```

完整字段、解析规则（§10）与校验规则（§11）见 [docs/config-design.md](docs/config-design.md)。

### 常用配方

<details>
<summary>问答数据集（单轮 alpaca）</summary>

```yaml
plan: {count: 1000}
strategies:
  - type: topic_driven
    topics:
      - {topic: Python 编程基础, weight: 3, knowledge: "Python 是动态类型语言"}
      - {topic: 高中数学, weight: 2}
    dimensions: [{name: difficulty, vals: [easy, medium, hard]}]
judge:
  dimensions: [{name: correctness, max: 10}, {name: completeness, max: 10}]
  min_total: 15                     # 低质 QA 直接 drop
output: {path: ./qa_out, format: alpaca}
```

</details>

<details>
<summary>多轮对话数据</summary>

```yaml
strategies:
  - type: topic_driven
    multi_turn: true               # 生成多轮 user/assistant 交互
output: {path: ./chat_out, format: chatml}
```

</details>

<details>
<summary>基于真实文档出 QA</summary>

```yaml
strategies:
  - type: document_qa
    document_file: ./docs.jsonl    # 你的语料
    chunking: {enabled: true}      # 长文先分块再逐块出 QA
```

</details>

<details>
<summary>工具调用训练集（function calling SFT）</summary>

```yaml
strategies:
  - type: tool_call
    tools:                         # OpenAI function schema，原样透传
      - type: function
        function:
          name: get_weather
          parameters: {...}
output: {path: ./tool_out}         # tool_call 自动推导 format=openai
```

输出 = 标准 OpenAI 轨迹（`messages + tools`），assistant 的 `tool_calls[].id`
与 tool 消息的 `tool_call_id` 严格配对，可直接喂给训练框架。

</details>

## 扩展

新增策略：实现 `plan` / `execute`，用 `@register_strategy` 注册即可接入。

```python
from corpuslab.core.registry import register_strategy
from corpuslab.strategies.base import PlanExecuteStrategy

@register_strategy("my_strategy")
class MyStrategy(PlanExecuteStrategy):
    async def _plan(self, materials, budget, ctx): ...   # 产出确定性 id 的 TaskSpec
    async def _execute_one(self, spec, ctx): ...          # 返回 Sample 或 None
```

新增治理阶段：流式实现 `apply_stream`，批式实现 `apply_batch`
（async，两个协议互不干扰，没有被迫实现的空方法）。

```python
from corpuslab.core.registry import register_stage

@register_stage("my_filter", scheduling="streaming")
class MyFilter:
    async def apply_stream(self, stream, ctx): ...

@register_stage("my_batch", scheduling="batch")
class MyBatch:
    async def apply_batch(self, samples, ctx) -> list: ...
```

还可扩展：原料（Source）、本地 scorer（Judge）、渲染器（Renderer）、存储后端（Sink），
一律 entry point 注册、零侵入。

> 共同约束：插件不自建 HTTP / 线程池 / 数据库连接——基础设施一律经 `ctx`
> （`ctx.chat / ctx.embed / ctx.store`）。

## 文档索引

| 文档 | 内容 |
|---|---|
| [config-design.md](docs/config-design.md) | 配置 Schema、解析/校验规则、alembic 迁移表 |
| [data-format.md](docs/data-format.md) | Sample JSON Schema、DuckDB 行格式、版本契约 |
| [checkpoint-design.md](docs/checkpoint-design.md) | DuckDB 状态库、崩溃一致性、resume 协议 |
| [project-structure.md](docs/project-structure.md) | 代码组织、核心契约、并发模型 |

## 测试

```bash
python -m pytest tests/ -q
```

全部走假 LLM / 假 embedding（`corpuslab/testing.py`），零真实 API 调用。
覆盖：断点幂等（中断后 resume 无重复）· 跨策略共享去重状态 · 七策略契约
（id 唯一性与 seed 确定性）· 六个治理阶段边界值 · 评审聚合语义 · 输出布局往返。

---

*corpuslab — declare your corpus, run the lab.*
