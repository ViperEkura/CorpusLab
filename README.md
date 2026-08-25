# corpuslab

**声明式 LLM 训练数据流水线：原料 → 合成 → 治理 → 评审 → DuckDB 落盘。**

corpuslab 把训练数据的生产变成一条声明式流水线：一份 YAML 编排、一条命令执行，从原始原料产出经过治理与评审的语料——既用于 SFT 指令数据合成，也用于预训练语料的清洗与去重。所有阶段是可组合的插件，**全部状态与输出落在唯一的 DuckDB 后端**：断点续跑、去重状态、embedding 缓存、评审缓存，都在一个 `.duckdb` 文件里。

## 设计定位

```
原料 ──▶ 合成 ──▶ 治理 ──▶ 评审 ──▶ DuckDB 落盘
topics     7 策略    有序阶段链    LLM 裁判    状态库即检查点
seeds      Plan→Execute  流式+批式屏障   本地 scorer   输出是投影
documents                去重/统计门禁
tools
```

- **原料到策略，策略到语料**：概念源（topic_driven / deep_thinking）、样本源（seed_driven / evol_instruct）、文档源（document_qa / instruction_backtranslation）、规格源（tool_call）；预训练场景由文档源与治理链承接；
- **治理即阶段链**：`length → exact_dedup → stats → minhash_dedup`（流式，与生成并发）→ `semantic_dedup → cluster_dedup`（批式屏障），写即生效、不写即关闭；
- **DuckDB 单后端**：事务即原子性；`sample_id` 在 Plan 期确定性派生，resume 幂等——续跑零重复样本、零重复花钱；
- **双通道评审**：LLM-as-Judge 多裁判聚合 + 可选本地 scorer，统一评分协议；
- **配置审计**：死配置删除、同义键收敛、别名迁移提示，`validate` 加载期拦截。

## 快速开始

```bash
pip install -e .                      # httpx / pydantic / pyyaml / datasketch / duckdb
corpuslab validate -c examples/corpuslab.yaml      # 校验配置
corpuslab run -c examples/corpuslab.yaml           # 合成 → 治理 → 评审 → DuckDB
corpuslab run -c examples/corpuslab.yaml --resume  # 断点续跑（已终态样本跳过）
corpuslab clean input.jsonl -o out.duckdb -c corpuslab.yaml   # 对既有语料重跑治理段
corpuslab score input.duckdb -o scored.duckdb -c corpuslab.yaml  # 补评审
CORPUSLAB_FAKE_LLM=1 corpuslab run -c examples/corpuslab.yaml   # 离线冒烟
```

## 核心概念（一句话版）

| 概念 | 是什么 |
|------|--------|
| 配置 | 唯一行为入口；`llm / endpoints / plan / strategies / pipeline / judge / output` |
| 策略 | 原料 × 变异算子；共享 Plan → Execute 骨架，新策略只实现两个方法 |
| 阶段 | 纯函数治理插件，流式与批式是两个协议 |
| 评审 | 维度可自定义；多裁判 `mean/min/max/median` 聚合 + `min_judges / max_disagreement / min_total` 治理 |
| 状态库 | 一个 `.duckdb` 承载 samples/events/pending/embeddings/去重指纹/评分/dropped/planned/manifest 十表 |
| 断点 | 事务原子；`cache_cleanup` 只清瞬时表，终态与去重状态保留 |

## 文档

- [docs/config-design.md](docs/config-design.md) — 配置 Schema、解析/校验规则、alembic 迁移表
- [docs/checkpoint-design.md](docs/checkpoint-design.md) — DuckDB 状态库、崩溃一致性、resume 协议
- [docs/project-structure.md](docs/project-structure.md) — 代码组织、核心契约、并发模型、扩展方式

## 测试与扩展

```bash
python -m pytest tests/ -q      # 46 项，全假 LLM/假 embedding，零网络
```

新增策略 / 阶段只需 `@register_strategy` / `@register_stage` 装饰器注册即可接入；插件不自建基础设施，一律经 `ctx`。
