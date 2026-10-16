> **Archived Chinese copy (earlier draft).** The English file in the repository is authoritative; this copy is not kept in sync.

# Lit Search — 项目规格（SPEC v0.2）

> 给 Claude Code 的实现说明。本版取代 v0.1：第一版只做"深度检索 + 漏斗筛选 + 摘要总结与链接"，定时监控、问答、综述后续再补。已定的决策直接照做；标注"待核实"的外部事实，先在 Phase 0 用官方文档或真实请求确认，再写代码。

## 0. 工作方式（给 Claude Code）

1. 分阶段做（§11）。每个阶段开始前先写简短计划让我确认；结束后跑该阶段验收清单，再提交。
2. 外部 API 的字段名、限额、模型 ID 不要凭记忆写，查官方文档或用真实请求验证，结论记在 `docs/spike-findings.md`。
3. 不引入本文未列出的重型依赖（向量库、Celery、Redis、Postgres 等）。需要加依赖先说明理由。
4. 单元测试不访问真实网络；真实网络只用于 `spikes/` 和手动验收。
5. 所有可调参数走环境变量或命令行参数，维护 `.env.example`；不硬编码模型 ID、邮箱、密钥。
6. 不做的事见 §1.2。

## 1. 背景与目标

### 1.1 目标

痛点：在对话里让 AI 做文献检索，会被限流、结果不完整，搜两三轮就停了，覆盖面看不出来。

第一版要做到：给定"项目描述 + 关键词 + 可选的种子论文"，**自动做多轮、多源、可追溯的深度检索**，按"人读文献的漏斗"逐级筛选，输出一份排好序的报告——每篇论文有基于摘要的总结、与项目的相关性说明、链接；同时记录用了哪些查询、各数据源是否成功、每一轮新增多少，让覆盖程度**可见、可复查**。

### 1.2 非目标（第一版）

- 定时监控、网页界面、跨论文问答、综述撰写、PDF 全文精读（见 §7，后续阶段）。
- 向量库 / 全文切块 RAG。
- 绕过付费墙；爬取出版商网站；任何灰色获取渠道。

### 1.3 规模假设

单次检索：几十条查询，候选论文几百到一两千篇，进入模型打分的为去重后的候选，最终报告几十篇。单用户，命令行运行。

## 2. 阅读漏斗（模拟人读文献）

| 级别 | 读什么 | 做什么 | 第一版 |
|---|---|---|---|
| L0 | 元数据 | 检索召回 | ✅ |
| L1 | 标题 + 摘要 | 相关性打分、3 句总结、主题标签（批量，便宜模型） | ✅ |
| L2 | 摘要 + 引言 + 结论 | 对 L1 通过的前 K 篇略读，需要 PDF | 后续（Phase 2） |
| L3 | 全文 | 用户手动挑选的少数几篇精读 | 后续 |

原则：**越往下越贵，所以只让通过上一级的论文进入下一级。** 第一版只做到 L1，但数据结构要为 L2/L3 留好位置（论文状态、PDF 链接字段）。

## 3. 检索策略（决定"深度"的部分）

### 3.1 输入

- `project.md`：自由文本，描述研究问题、方法、数据、关心的角度、已知相关工作。
- 可选 `keywords`、`seeds`（DOI / arXiv ID / 标题列表）、日期范围、领域过滤。
- 可选 `known.txt`：用户已知相关的论文（用于评估召回，见 §11）。

### 3.2 查询生成

由模型根据输入生成 N 条多样查询（同义词、方法名、问题的不同表述、相邻领域术语），存入数据库；同时导出 `queries.yaml`，用户可编辑后重跑。每条查询记录它在各数据源带来的命中数与"有效命中数"（得分 ≥ 阈值）。

### 3.3 多源、分页召回

每条查询在每个数据源分页取到上限（`--per-query`，默认 50），不只取首页。各源并发度 1，串行请求。

### 3.4 合并去重

规则同下 §5.2。去重后的论文才进入打分，避免同一篇重复花钱。

### 3.5 L1 打分与总结

- 批量送模型（每次约 10–20 篇的标题+摘要，数量可配），要求 JSON 输出：`{id, score 0-10, reason, summary(3 句), tags[]}`。
- **总结只能基于提供的标题和摘要**，报告里明确标注"基于摘要"。
- 阈值宽松：分数在 `maybe` 区间（可配，如 4–5 分）的进"可能相关"桶，不直接丢弃。
- 缓存键：（paper_id, project_hash, prompt_version, model）。

### 3.6 引用滚雪球（snowballing）

取 L1 得分最高的前 M 篇作为种子，拉取其参考文献与被引文献（OpenAlex / Semantic Scholar，端点待核实）作为新候选，回到 3.4–3.5；重复 R 轮（`--rounds`，默认 2）。种子里包含用户提供的 `seeds`。

### 3.7 饱和与终止

每一轮统计"新增且得分 ≥ 阈值的论文数"。满足任一条件即停止：达到轮数上限；新增有效论文数低于 `--saturation`（默认 3）；触达预算上限。报告里画出每轮新增数，让"够不够深"可见。

### 3.8 预算与确认

- `--dry-run`：只做检索与去重，输出预计的查询数、候选数、预计模型调用次数与 token 量级，**不调用模型**。
- 正式运行前显示同样的估算并要求确认（`--yes` 跳过）。
- 上限参数：`--max-scored`（最多打分多少篇）、`--max-llm-calls`。
- 每一步落库，支持 `--resume RUN_ID` 断点续跑。

## 4. 数据源与采集器（字段名与限额均待核实）

统一接口：

```python
class Collector(Protocol):
    name: str
    async def search(self, query: str, *, limit: int, offset: int|cursor, since: date|None) -> Page[PaperMeta]: ...
    async def references(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...   # 可选
    async def citations(self, paper: PaperMeta, limit: int) -> list[PaperMeta]: ...    # 可选
```

每个采集器自带：限速、指数退避重试、断路器、请求头带联系邮箱。`PaperMeta` 至少含 title / abstract / authors / year / date / venue / doi / arxiv_id / 各源 ID / 各类链接 / 开放获取信息 / 来源名。

- **OpenAlex**：`/works` 搜索 + 过滤；引用关系；摘要可能以特殊格式返回，需要还原（待核实）；带联系邮箱；可选 API key。
- **Semantic Scholar**：Graph API 搜索、references、citations；可选 API key（未认证走共享额度，易 429，建议申请免费 key）。
- **arXiv**：官方 API，请求间隔不少于 3 秒（待核实当前规定）。
- **Google Scholar**（`--gs`，默认关）：`scholarly` 放进 `asyncio.to_thread`。只作**补充一轮**：对有效命中数最高的少数查询各取 1–2 页，随机间隔（`GS_MIN_DELAY`/`GS_MAX_DELAY`），支持代理；遇验证码/封禁立即停止 GS，记 `blocked`，**不重试风暴**，其余源照常。GS 日期不可靠，结果用 DOI/标题回配到 OpenAlex，配不上的标记 `gs_only`。引用图谱用 OpenAlex / S2，不依赖 GS。

可参考 RRI（`nhdandz/RRI`，MIT）的 `src/collectors/base.py`（RateLimiter、CircuitBreaker、tenacity 重试），**只借鉴写法，不要 fork**；直接复制代码要保留其版权声明。

## 5. 数据模型与去重

### 5.1 SQLite 表（建议，可微调）

- **runs**：id, project_hash, params(JSON), status, started_at, finished_at。
- **queries**：run_id, text, origin(`llm`/`user`), source, n_hits, n_relevant。
- **papers**：id, title, abstract, authors(JSON), year, date, venue, doi, arxiv_id, 各源 ID, citation_count, links(JSON), oa_pdf_url, state(`candidate`/`scored`/`dismissed`/…)。
- **hits**：run_id, query_id, source, paper_id, rank, round。
- **scores**：paper_id, project_hash, prompt_version, model, score, reason, summary, tags(JSON)。
- **edges**：src_paper_id, dst_paper_id, kind(`cites`)。
- **source_runs**：run_id, source, status(`ok`/`blocked`/`rate_limited`/`error`/`skipped`), n_requests, n_results, error。**"失败可见"的关键，必须进报告。**

### 5.2 去重规则

1. 规范化 DOI 精确匹配；2. arXiv ID（去版本号）精确匹配；3. 标题规范化后相似度 ≥ 0.92，且年份相差 ≤ 1，且至少一个共同作者姓。0.85–0.92 只记日志"疑似重复"，不合并。
合并时保留所有版本和来源，展示用正式发表的元数据优先。需要单元测试覆盖"预印本标题与正式版略有不同"的情形。

## 6. 输出

`out/<run-name>/` 下：

- **`report.md`**（带 frontmatter，Obsidian 可直接用）：
  1. 运行摘要：参数、查询数、各数据源状态与请求数、每轮新增数（饱和情况）、候选总数、通过数。
  2. 按主题标签分组的论文表：分数、标题、作者、年份、venue、**3 句总结（基于摘要）**、相关性理由、**链接**（DOI/出版社页、arXiv、开放获取 PDF、OpenAlex/Semantic Scholar 页）、状态（预印本/已发表）。
  3. "可能相关"折叠区。
  4. 附录：被筛掉论文的简表（标题、分数、一句理由）；全部查询及其有效命中数；数据源日志。
- **`papers.csv`**、可选 **`refs.bib`**、完整的 **`run.json`**。
- `litsearch report RUN_ID` 用缓存重新生成报告，**不再调用模型**。

硬性规则：**所有链接、DOI、作者、年份必须来自数据源返回的字段，不得由模型生成或补全。**

## 7. 后续阶段（第一版不做，但保持接口兼容）

- **Phase 2**：L2 略读与 PDF。PDF 获取顺序：有 arXiv ID 直接下载；否则用 OpenAlex/S2 提供的开放获取链接；都没有则标 `need_manual`，用户手动上传。下载限速、校验文件头与大小。精读基于 Claude 的 PDF 能力（已核实，2026-10）：`document` 块支持 URL / base64 / Files API，每页同时以文字和图片处理；请求体 ≤ 32 MB，页数 ≤ 100（上下文窗口 1M 时可到 600）；每页文字约 1,500–3,000 token，图片另算。开启 `citations` 可得页码与原文片段，但**与结构化 JSON 输出同时用会报 400**，因此分两步：先带引用自由阅读，再不带引用整理成固定字段；无文字层的扫描件不可引用。笔记缓存键含 PDF 的 sha256。
- **Phase 3**：定时监控（按主题增量检索 + digest）、跨论文问答（先看笔记再开原文）、综述生成、发表状态回查、Google Scholar 提醒邮件方案、Obsidian / 网页界面。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 数据源限流或失败 | 串行 + 退避 + 缓存 + 申请 API key；每步落库可 `--resume`；单源失败不使运行失败，写入 `source_runs` 并显示在报告里 |
| Google Scholar 被封 | 只作补充、低频、随机间隔、可选代理；失败不阻塞；备选走提醒邮件（Phase 3） |
| 召回不全 | 多查询 + 多源 + 分页 + 引用滚雪球 + 种子论文；用 `known.txt` 评估召回；报告展示饱和曲线 |
| 仅凭摘要误判 | 宽松阈值 + "可能相关"桶 + 附录可查被筛论文与理由；阈值可调后只重排不重算 |
| 摘要缺失 | 回退到其他源的摘要；仍缺失则仅按标题打分并标注"无摘要" |
| 模型编造 | 链接和元数据只取自数据源；总结限定基于摘要并标注；不让模型生成引用 |
| 成本失控 | `--dry-run`、确认步骤、`--max-scored`、`--max-llm-calls`、结果缓存 |
| 网络出口 | 支持 `HTTPS_PROXY`；日志区分"被封"和"无结果" |

## 9. 命令行与配置

```
litsearch init                         # 生成 project.md 模板与 .env.example
litsearch run --project project.md [--rounds 2] [--per-query 50] [--sources openalex,s2,arxiv] [--gs]
              [--saturation 3] [--max-scored N] [--max-llm-calls N] [--dry-run] [--yes] [--resume RUN_ID]
litsearch report RUN_ID                # 用缓存重新生成报告，不调用模型
litsearch queries RUN_ID               # 导出/查看查询及其有效命中数
```

`.env.example` 至少包含：

```
ANTHROPIC_API_KEY=
L1_MODEL=claude-haiku-5-5        # 示例，以官方模型列表为准
QUERY_MODEL=claude-sonnet-5-5    # 示例，以官方模型列表为准
CONTACT_EMAIL=                    # 放进 User-Agent 与 OpenAlex 参数
OPENALEX_API_KEY=                 # 可选
S2_API_KEY=                       # 可选
GS_MIN_DELAY=10
GS_MAX_DELAY=30
HTTPS_PROXY=
DATA_DIR=./data
```

## 10. 技术选型与目录

Python 3.12；Typer（命令行）；httpx（异步）；SQLite（标准库 sqlite3 加轻量封装即可）；Anthropic SDK；pytest；ruff。第一版不需要 Web 框架和调度器，也不需要 Docker（后续再加）。

```
lit-search/
  CLAUDE.md  SPEC.md  pyproject.toml  .env.example
  litsearch/
    cli.py  config.py  db.py
    collectors/   base.py  openalex.py  semantic_scholar.py  arxiv.py  gscholar.py
    pipeline/     queries.py  retrieve.py  dedup.py  score.py  snowball.py  report.py
    llm/          client.py  prompts/
  tests/  spikes/  docs/  data/(gitignore)  out/(gitignore)
```

## 11. 阶段与验收

**Phase 0 — 实测（不写应用代码）**，脚本放 `spikes/`，结论写 `docs/spike-findings.md`：
1. OpenAlex：搜索与分页、引用/被引端点、摘要字段的实际格式、是否需要 API key。
2. Semantic Scholar：搜索、references、citations 端点；未认证限额下的实际表现。
3. arXiv API：请求规则与返回字段。
4. 在目标机器上用 `scholarly` 跑 5 个真实查询：是否被限制、返回字段、日期可靠性。
5. 三个源各取 100 篇结果，统计"有摘要"的比例。
6. 核实当前可用的模型 ID。
验收：每个假设标注"成立 / 不成立 / 需调整"，据此更新本 SPEC 再进 Phase 1。

**Phase 1 — 深度检索与漏斗报告（第一版）**，实现 §3–§6、§9。验收：
1. 用一个真实项目描述跑完整流程（含雪球 ≥ 2 轮），得到 `report.md`、`papers.csv`、`run.json`。
2. **召回检查**：提供 `known.txt`（用户已知相关的 5–10 篇），报告显示命中几篇、分别在第几轮出现。
3. **抽查**：随机 20 篇，链接可打开、元数据与数据源一致、总结与摘要一致（人工核对）。
4. 同输入重跑命中缓存，无新增模型调用；`litsearch report` 不调用模型。
5. 故意让某数据源失败（如模拟 429 / GS 封禁）：运行照常完成，报告里该源显示失败。
6. `--dry-run` 不调用模型并给出估算；中途中断后 `--resume` 可继续。
7. 饱和统计与每轮新增数在报告里可见。
8. 单元测试覆盖：去重（含预印本/正式版标题差异）、饱和终止判断、缓存键、报告生成。

**Phase 2 / 3**：见 §7，开工前先更新本文件。

## 12. 事实核查状态

- 已核实（Anthropic 官方文档，2026-10）：§7 中的 PDF 限制与引用约束。
- 待核实（Phase 0 处理）：OpenAlex / Semantic Scholar / arXiv 的端点、字段、限额与规则；`scholarly` 当前可用性；模型 ID。
