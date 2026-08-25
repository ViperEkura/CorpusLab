# corpuslab

**声明式 LLM 训练数据流水线：原料 → 合成 → 治理 → 评审 → 输出。**
一份 YAML 编排、一条命令执行，从主题 / 种子 / 文档 / 工具四类原料，产出经过质量门禁与评审的训练语料——SFT 指令数据与预训练语料都适用。

corpuslab 把「写合成脚本、写清洗脚本、写去重脚本、写评分脚本」这些互相割裂的步骤，收敛成一条**声明式流水线**：你只声明「要什么数据、怎么治理、怎么评审」，引擎负责调度、并发、重试、熔断与断点。所有状态与输出落在唯一的 DuckDB 后端，断点续跑天然幂等。

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
└── samples.parquet     列式导出（默认开启），可直接用 DuckDB/Polars/Pandas 读

parquet 列: id / strategy / instruction / output / reasoning / messages / tools / metadata / total_score
```

```jsonc
// samples.parquet 里的一条样本
{
  "instruction": "请用简单例子说明 Python 中的动态类型是什么？并编写一个函数来展示这一点。",
  "output": "动态类型意味着变量在运行时可以指向任意类型的对象……x = 5 之后 x = 'hello'……",
  "metadata": {"id": "…", "strategy": "topic_driven",
               "lineage": {"topic": "Python 基础语法", "difficulty": "easy"},
               "metrics": {"total_score": 10, "kept_by": ["length", "exact_dedup"]}}
}
```

## 安装

```bash
pip install -e .                # httpx / pydantic / pyyaml / datasketch / duckdb
pip install -e ".[fasttext]"    # 可选：本地 scorer
```

## 快速开始

```bash
corpuslab validate -c examples/corpuslab.yaml      # 校验配置（死键/冲突/资源缺失）
corpuslab run -c examples/corpuslab.yaml           # 合成 → 治理 → 评审 → 输出目录
corpuslab run -c examples/corpuslab.yaml --resume  # 断点续跑：已终态样本跳过，零重复花钱
corpuslab clean input.jsonl -o out -c corpuslab.yaml --input-format alpaca   # 对既有语料重跑治理段
corpuslab score input.duckdb -o scored -c corpuslab.yaml                      # 补评审

# 凭证放 .env（已自动加载，不入库）
CORPUSLAB_FAKE_LLM=1 corpuslab run -c examples/corpuslab.yaml   # 离线冒烟：假 LLM，零网络
```

退出码：`0` 成功 · `2` 配置错误 · `3` 运行期熔断/中断（状态库保留）· `4` 输入资源缺失。

## 工作原理

**原料 → 策略。** 四类原料对应七个策略，策略的本质是「原料 × 变异算子」：

- 概念源（知道聊什么）：`topic_driven` 用题型 × 难度 × 主题做正交槽位采样；`deep_thinking` 强制带思维链；
- 样本源（知道长什么样）：`seed_driven` 用 few-shot / 交叉 / 变异轮盘；`evol_instruct` 多轮深度/广度进化并保留进化链；
- 文档源（知道事实是什么）：`document_qa` 依据原文生成问答；`instruction_backtranslation` 反推指令并**锁定原文为答案**（防事实漂移）；
- 规格源（知道能做什么）：`tool_call` 生成工具调用轨迹并强校验。

所有策略共享同一套 `Plan → Execute` 骨架：Plan 产出多样性任务单（带确定性 id），Execute 并发填充。新增策略只需实现这两个方法，重试、并发、熔断、断点由基础设施承担。

**治理 → 阶段链。** 治理是一个有序的阶段链，写即生效、不写即关闭：

```
length（长度门禁）→ exact_dedup（SHA256）→ stats（统计清洗）→ minhash_dedup（LSH 近似）
→ semantic_dedup / cluster_dedup（embedding 语义去重，批式屏障）
```

流式阶段与生成并发执行（早失败省 token）；批式阶段作为屏障，缓冲落在 DuckDB 的 `pending` 表（磁盘背压）。用户只声明顺序，不声明调度。

**评审 → 双通道。** 远端 LLM-as-Judge（维度自定义、多裁判 `mean/min/max/median` 聚合 + `min_judges / max_disagreement / min_total` 治理）与本地 scorer（可选 fasttext）共用同一评分协议，阈值过滤统一走 `judge.min_total`。

**输出 → DuckDB 单后端。** 输出是一个文件夹：`corpuslab.duckdb` 状态库 + `samples.parquet` 列式导出。一个 `.duckdb` 文件承载 `samples / events / pending / embeddings / fingerprints / minhash_sigs / scores / dropped / planned / manifest` 十张表——事务即原子性，`sample_id` 在 Plan 期确定性派生，因此 `--resume` 续跑零重复样本、零重复花钱。

## 配置

```yaml
run:        {seed, preview, preview_count}    # 运行控制
llm:        {model, base_url, api_key, lang, concurrency, params, retry, breaker}
embedding:  {model, batch_size}               # 全局唯一 embedding 端点
endpoints:  {pro: {model: ...}}               # 命名端点，逐项继承 llm，按名引用
plan:       {count}                           # 产量唯一入口
strategies: [{type, weight, ...}]             # ≥1 条，四族七策略
pipeline:   [{type, ...}]                     # 有序治理链
judge:      {dimensions, min_total, judges, aggregation, min_judges, max_disagreement, scorers}
output:     {path, format, resume, storage: {type: duckdb, export_parquet, export_jsonl}}
```

完整字段、解析规则与校验规则见 [docs/config-design.md](docs/config-design.md)。

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

新增治理阶段：流式实现 `apply_stream`，批式实现 `apply_batch`（async，两个协议互不干扰）。

```python
from corpuslab.core.registry import register_stage

@register_stage("my_filter", scheduling="streaming")
class MyFilter:
    async def apply_stream(self, stream, ctx): ...

@register_stage("my_batch", scheduling="batch")
class MyBatch:
    async def apply_batch(self, samples, ctx) -> list: ...
```

共同约束：插件不自建 HTTP / 线程池 / 数据库连接——基础设施一律经 `ctx`（`ctx.chat / ctx.embed / ctx.store`）。

## 文档索引

- [docs/config-design.md](docs/config-design.md) — 配置 Schema、解析/校验规则、alembic 迁移表
- [docs/checkpoint-design.md](docs/checkpoint-design.md) — DuckDB 状态库、崩溃一致性、resume 协议
- [docs/project-structure.md](docs/project-structure.md) — 代码组织、核心契约、并发模型

## 测试

```bash
python -m pytest tests/ -q
```

全部走假 LLM / 假 embedding（`corpuslab/testing.py`），零真实 API 调用；覆盖断点幂等、跨策略去重、策略契约（id 唯一性与 seed 确定性）、输出布局与 parquet 往返。
