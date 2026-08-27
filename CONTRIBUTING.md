# 参与贡献（Contributing）

本文件是**格式与流程约束**，提交前请逐条对照。破坏性偏差会在评审中被打回。

## 1. 提交范围（白名单）

只提交 `.py` 与 `.md` 文件的改动。

- 例外：`.gitignore`、`pyproject.toml`、`examples/*.yaml` 等需要单独说明原因；
- **禁止入库**：`.env`、密钥、`*.duckdb`、`*.parquet`、运行产物、`__pycache__`、`*.egg-info`（白名单 `.gitignore` 已默认忽略，不要用 `-f` 强制添加）。

## 2. 提交信息（commit message）

- **英文**；首行（subject）≤ 72 字符；
- **Conventional Commits 前缀**：`feat:` `fix:` `docs:` `chore:` `test:` `refactor:`；
- **body 里每条 bullet 必须单行，不跨行**（一行写不下就精简措辞）；
- 一个提交一个主题；不同性质的改动（如 feat 与 fix）分开提交。

```text
feat: internal data format contract with validation

- Sample.validate() enforces C1-C4 at store-read and file-ingest boundaries
- FORMAT_VERSION lands in the manifest; mismatches refuse resume
- docs/data-format.md defines the canonical JSON schema and DuckDB rows
```

## 3. 语言

| 位置 | 语言 |
|------|------|
| 代码注释 / docstring / 日志 / 错误消息 / CLI 输出 | **英文** |
| pyproject 元数据（description 等） | 英文 |
| README / docs 面向用户文档 | 中文 |
| commit message | 英文 |

## 4. 代码格式

- 深导入：`from corpuslab.core.store import Store`，**不在 `__init__` re-export**；
- 公开扩展面以 `__all__` 白名单约束：只动 `corpuslab.core.{registry,contracts,sample}` 与 `corpuslab.strategies.base` 时同步维护其 `__all__`；
- 插件约束（S1/S4）：不得 `import cli` 或具体兄弟插件；不得自建 HTTP / 线程池 / 数据库连接——基础设施一律经 `ctx`；
- 流式/批式是两个协议：流式实现 `apply_stream`，批式实现 `apply_batch`（async），**禁止写"两个入口都要实现"的胖接口**；
- 阶段、渲染器、scorer 保持纯函数（S5）；副作用只在 Client / Source / Sink / Store。

## 5. 测试

```bash
python -m pytest tests/ -q    # 全部通过（当前 64 项）
```

- 测试走假 LLM / 假 embedding（`corpuslab/testing.py`），**零真实 API 调用**；
- 新增策略/阶段必须附带契约测试：plan 的 **id 唯一性** 与 **seed 确定性**（resume 幂等的根基，回归会静默丢数据）；
- 修改内部格式或状态库表结构时，须更新 `docs/data-format.md` 并递增 `FORMAT_VERSION`/`SCHEMA_VERSION`。

## 6. 依赖

- 新依赖一律进 **optional extra**（如 `[project.optional-dependencies].fasttext`），不进硬依赖；
- 新增能力优先以「注册一个新阶段 / source」实现，不动 core。

## 7. 文档同步

配置面、数据格式、状态库、代码结构的任何变更，必须同步更新对应文档：
`docs/config-design.md` / `docs/data-format.md` / `docs/checkpoint-design.md` / `docs/project-structure.md`，并保证交叉引用（README 文档索引）不出现死链。

## 8. 凭证安全

- API 密钥只放 `.env`（已 gitignore），**绝不进配置 YAML 或提交**；
- 示例配置不得包含真实密钥。
