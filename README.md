# corpuslab

**声明式 SFT 数据流水线：原料 → 合成 → 清洗 → 评审 → DuckDB 落盘。**

一条命令、一份 YAML，把主题 / 种子 / 文档 / 工具规格四类原料，合成带质量门禁与评审的高质量 SFT 训练数据。所有阶段是可组合的插件，**全部状态与输出落在唯一的 DuckDB 后端**——断点续跑、去重状态、embedding 缓存、评审缓存，都在一个 `.duckdb` 文件里。

## 特性

- **四族七策略**：概念源（topic_driven / deep_thinking）、样本源（seed_driven / evol_instruct）、文档源（document_qa / instruction_backtranslation）、规格源（tool_call），共享 Plan → Execute 骨架，新增策略只需实现一组变异算子；
- **治理链即插件**：`length → exact_dedup → stats → minhash_dedup`（流式，与生成并发）→ `semantic_dedup → cluster_dedup`（批式屏障），写即生效、不写即关闭；
- **DuckDB 单后端**：事务即原子性，输出是投影、状态是库（见 [docs/checkpoint-design.md](docs/checkpoint-design.md)）；
- **resume 精确幂等**：`sample_id` 在 Plan 期确定性派生，终态集合与去重状态持久化——续跑零重复样本、零重复花钱；
- **双通道评审**：LLM-as-Judge（多裁判聚合）+ 本地 scorer（fasttext，可选），统一 `scores + total_score + score_source` 协议；
- **配置审计**：死配置删除、同义键收敛、别名迁移提示，`corpuslab validate` 加载期拦截。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/config-design.md](docs/config-design.md) | 配置 Schema、解析规则（端点继承/格式推导/产量分配）、校验规则、alembic 迁移表 |
| [docs/checkpoint-design.md](docs/checkpoint-design.md) | 状态分类、DuckDB 状态库 Schema、崩溃一致性协议、resume 算法、manifest 兼容性 |
| [docs/project-structure.md](docs/project-structure.md) | 代码组织、核心契约（Protocol）、控制流、并发模型、打包与扩展 |

## 安装

```bash
pip install -e .                # 核心依赖：httpx / pydantic / pyyaml / datasketch / duckdb
pip install -e ".[fasttext]"    # 可选：本地 scorer
```

## 快速开始

```bash
# 1. 校验配置
corpuslab validate -c examples/corpuslab.yaml

# 2. 完整流水线（合成 → 治理 → 评审 → DuckDB 落盘）
corpuslab run -c examples/corpuslab.yaml

# 3. 断点续跑（已终态样本跳过，不重复调用 LLM）
corpuslab run -c examples/corpuslab.yaml --resume

# 4. 对既有数据重跑治理段 / 补评分
corpuslab clean input.jsonl -o cleaned.duckdb -c corpuslab.yaml --input-format alpaca
corpuslab score input.duckdb -o scored.duckdb -c corpuslab.yaml

# 5. 离线冒烟（假 LLM / 假 embedding，零网络调用，适合 CI）
CORPUSLAB_FAKE_LLM=1 CORPUSLAB_FAKE_EMBED=1 corpuslab run -c examples/corpuslab.yaml
```

## CLI

```
corpuslab run       [-c CONFIG] [--count N] [--preview] [--resume] [--strategy S] [--discard-state]
corpuslab clean     INPUT [-o OUTPUT] [-c CONFIG] [--input-format F] [--field-map JSON]
corpuslab score     INPUT [-o OUTPUT] [-c CONFIG] [--input-format F] [--field-map JSON]
corpuslab validate  [-c CONFIG] [run|clean|score]
```

| 选项 | 说明 |
|------|------|
| `-c` 缺省 | 依次找 `./corpuslab.yaml`、`./corpuslab.yml` |
| `--count` | 覆盖 `plan.count`（仅 run） |
| `--preview` | 小批量（`run.preview_count`，缺省 8）、不落库、不落断点（**仍调 LLM**） |
| `--strategy S` | 只运行指定策略，产量在被选中策略间重新分摊 |
| `--resume` | 从状态库续跑（manifest 不兼容时默认拒绝，`--discard-state` 放行） |
| `clean/score` 的 `-o` | `.duckdb` → 状态库；`.jsonl` → 纯文件模式 |

**退出码**：`0` 成功 · `2` 配置错误 · `3` 运行期熔断/中断（状态库保留）· `4` 输入资源缺失

## 配置速览

```yaml
run:        {seed, preview, preview_count}   # 运行控制（seed 保证本地随机源可复现）
llm:        {model, api_key, base_url, lang, concurrency, params, retry, breaker}
embedding:  {model, batch_size}              # 全局唯一 embedding 端点
endpoints:  {pro: {model: ...}}              # 命名端点，按名引用，逐项继承 llm
plan:       {count: 1000}                    # 产量唯一入口（CLI --count 可覆盖）
strategies: [{type, weight, ...}]            # ≥1 条，四族七策略
pipeline:   [{type, ...}]                    # 有序治理链，写即生效
judge:      {dimensions, min_total, judges, aggregation, min_judges, max_disagreement, scorers}
output:     {path: out.duckdb, format, resume, storage: {type: duckdb, export_jsonl}}
```

完整字段与规则见 [docs/config-design.md](docs/config-design.md)。

## 设计原则

| # | 原则 | 含义 |
|---|------|------|
| P1 | 一处生效 | 一个语义（长度、去重、并发、温度、产量）只出现一次 |
| P2 | 无死开关 | 不提供写了也没行为差异的配置 |
| P3 | 分层默认，显式三层封顶 | `llm` → `endpoints.<name>` → `phases.<p>`；第三层只允许 `params` 类字段 |
| P4 | 阶段即插件 | 清洗、去重、评审都是同一个东西；CLI 子命令只是组合不同 |
| P5 | 产量声明唯一 | 总量在 `plan.count`，比例在 `weight`，策略 `count` 仅作份额覆盖 |
| P6 | 可溯源、尽量可复现 | seed 控制全部本地随机源；id 确定性保证断点可续 |
| P7 | 状态即库 | 检查点是 DuckDB 状态库本身；输出是投影 |

## DuckDB 后端与断点

一个 `.duckdb` 文件承载 10 张表：

```
samples        输出投影（id 主键，幂等）
events         事件审计日志（committed / dropped / planned …）
pending        批式屏障的磁盘背压缓冲
embeddings     embedding 内容寻址缓存（跨 run 复用，省钱）
fingerprints   SHA256 去重状态          ← 保留
minhash_sigs   MinHash 签名（LSH 索引是视图，threshold 改动兼容） ← 保留
scores         (样本 × 端点) 评审缓存    ← 保留
dropped        drop 终态（resume 跳过依据） ← 保留
planned        Plan 产物（id 主键）      ← 保留
kv             manifest（config_hash / seed / version / num_perm）
```

- **原子性**：效果与标记同事务提交，崩溃窗口内不留中间态；
- **`cache_cleanup` 只清瞬时表**（`events` / `pending`）；终态集合、去重状态、scores、embeddings 全部保留——否则 resume 会重复生成已 drop 的样本或让重复漏网；
- **resume 不变式**：重跑既不产生重复样本，也不在已终态 id 上重复花钱（`--resume` 后 LLM 调用数应为 0）。

详细协议见 [docs/checkpoint-design.md](docs/checkpoint-design.md)。

## 扩展

**新增策略**（只需实现 `plan / execute`；重试、并发、熔断、断点由骨架承担）：

```python
from corpuslab.core.registry import register_strategy
from corpuslab.strategies.base import PlanExecuteStrategy

@register_strategy("my_strategy")
class MyStrategy(PlanExecuteStrategy):
    async def _plan(self, materials, budget, ctx): ...   # 产出确定性 id 的 TaskSpec
    async def _execute_one(self, spec, ctx): ...          # 返回 Sample 或 None
```

**新增治理阶段**：

```python
from corpuslab.core.registry import register_stage

@register_stage("my_filter", scheduling="streaming")
class MyFilter:
    async def apply_stream(self, stream, ctx): ...        # 流式

@register_stage("my_batch", scheduling="batch")
class MyBatch:
    async def apply_batch(self, samples, ctx) -> list: ...  # 批式（async）
```

约束：插件不得自建 HTTP / 线程池 / 数据库连接——基础设施一律经 `ctx`（`ctx.chat / ctx.embed / ctx.store`）。

## 测试

```bash
python -m pytest tests/ -q
```

测试全部走假 LLM / 假 embedding（`corpuslab/testing.py`），零真实 API 调用；
含断点幂等、跨策略去重、策略契约（id 唯一性与 seed 确定性）等关键测试。
