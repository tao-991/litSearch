> **Archived Chinese copy (earlier draft).** The English file in the repository is authoritative; this copy is not kept in sync.

# Lit Search — 项目规则（给 Claude Code）

先读 `SPEC.md`，它是需求与设计的唯一依据；与它冲突时先问我，不要自行改方向。

## 技术栈（第一版）
Python 3.12 · Typer(CLI) · httpx(async) · SQLite(sqlite3) · Anthropic SDK · pytest · ruff。第一版不做网页、调度器、Docker。

## 工作流程
- 按 SPEC §11 分阶段做。每阶段开始前先给我一份简短计划；结束后跑验收清单、更新文档、提交。
- 外部 API 的端点、字段、限额、模型 ID 以官方文档或真实请求为准，结论记入 `docs/spike-findings.md`。

## 约定
- 类型标注；`ruff` 与 `pytest` 必须通过；单元测试不访问真实网络。
- 所有可调参数走环境变量或命令行参数，并保持 `.env.example` 最新；不硬编码模型 ID、邮箱、密钥；不提交 `.env`、`data/`、`out/`。
- **报告里的链接、DOI、作者、年份只能来自数据源返回的字段，绝不由模型生成。**
- 单个数据源失败不得使整个运行失败；所有失败写入 `source_runs` 并显示在报告里。
- 每一步落库，支持 `--resume`；LLM 结果一律按（输入哈希, prompt_version, model）缓存。
- 正式运行前必须有 `--dry-run` 估算与确认步骤。

## 不做
- 绕过付费墙、爬取出版商网站、模拟登录。
- 引入 Celery / Redis / Postgres / 向量库 / Web 框架（除非先征得我同意）。
- 对 Google Scholar 高频或并发请求。

## 常用命令（实现后补全）
`litsearch run` · `litsearch report` · `make test` · `make lint`
