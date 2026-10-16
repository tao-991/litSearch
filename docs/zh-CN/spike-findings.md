> **Archived Chinese copy (earlier draft).** The English file in the repository is authoritative; this copy is not kept in sync.

# Phase 0 实测结论（spike findings）

实测日期：2026-10-16。脚本在 `spikes/`（`uv run --python 3.12 spikes/run_all.py [openalex] [s2] [arxiv]`），原始响应在 `spikes/samples/`（已 gitignore）。

测试设置：3 条共用查询（见下），每源每条取 20 篇；除 Google Scholar 外，跨源 `asyncio.gather` 并发，同源串行（Google Scholar 单独低频串行跑，见 §4）；重试为 429/5xx/网络错误指数退避（2/4/8s，优先 `Retry-After`）最多 3 次，连续 3 次放弃后熔断。

- q1 `developer trust acceptance of AI-generated code changes`
- q2 `coding agent pull request review acceptance`
- q3 `LLM explaining code changes to support code review`

arXiv 无法用自然语言长句，三条查询手工转成布尔语法：`all:"AI-generated code" AND (all:trust OR all:acceptance)` 等。

> 样本只有每源每条 20 篇（共 60 篇上下），占比数字仅作粗略参考。

## 0. 总览：假设判定

| # | SPEC 假设 | 判定 | 要点 |
|---|---|---|---|
| 1 | OpenAlex API key 可选 | **需调整** | 无 key 每 IP 每天共享 $0.10 额度（约 100 次搜索），`mailto` 不提高额度；`cursor` 翻页无 key 直接 401。实际应视为需要 |
| 2 | OpenAlex 摘要需还原 | 成立 | `abstract_inverted_index` → 按位置排序拼接即可 |
| 3 | OpenAlex 有引用/被引 | 成立 | `referenced_works` + `filter=openalex:` 批量取；`filter=cites:` 取被引 |
| 4 | S2 未认证可用但易 429 | **需调整** | 实测约 85% 的请求 429，只能当"尽力而为"的源 |
| 5 | S2 references/citations 可用 | **未验证** | 两次运行都被 429 挡住，未拿到一次成功响应 |
| 6 | arXiv 间隔不少于 3 秒 | 成立 | 按 3s 间隔共 12 个请求（两次运行）全部 200，无 429；未测低于 3s（刻意不违反规则） |
| 7 | arXiv 不提供引用关系 | 成立 | API 无此功能，未发请求 |
| 8 | 同一个查询在各源可直接复用 | **需调整** | OpenAlex 默认 `search` 是全文检索；arXiv 要布尔语法；S2 `bulk` 是布尔语法 |
| 9 | 每源并发度 1 足够 | 需继续观察 | OpenAlex、arXiv 串行很快；S2 的瓶颈是共享额度，与并发度无关 |
| 10 | `scholarly` 在目标机器上可用 | 成立（有限制） | 3 条查询 × 20 篇全部成功，未被拦；但只给搜索片段、无 DOI、`eprint_url` 全部无效，详见 §4 |
| 11 | 模型 ID | **未做** | 本次范围外，Phase 1 前核实 |

## 1. OpenAlex

**端点与认证**
- 搜索：`GET https://api.openalex.org/works?search=...&per-page=20`，`mailto=` 可带。
- 计费方式已变为**额度制**，响应头 `x-ratelimit-*` 与 `meta.cost_usd` 可见：
  - 无 key：每天 1000 credits（$0.10），按网络出口 IP 共享；`mailto` **不**提高额度（`meta.notice` 明说）。
  - 免费 key：$1/天（10 倍）。申请入口 `openalex.org/settings/api`；响应还提示可 `POST /api-keys` 以邮箱自动申请（本次**未使用**，需用户同意再做）。
  - 本次实测各请求消耗：搜索（`search=`）10 credits；`filter=` 列表 1 credit；按 DOI 单条查询 0；错误响应（400）0。`per-page=50` 的消耗**未测**。
  - 额度重置时间：`x-ratelimit-reset` 约 72500 秒（约 20 小时，以响应头为准）。
  - 估算：无 key 约 100 次搜索/天；有 key 约 1000 次搜索/天。Phase 1 的几十条查询 × 多页会很快用完无 key 额度。
- 14 个请求串行（间隔 0.2s）用时 12s，单次延迟约 0.3–2s，无 429。

**默认 `search` 是全文检索**：`meta.x_query` 显示翻译为 `fulltext.search`，q1 命中 22,751 篇。前 8 条标题相关性很好（开发者对 AI 代码的信任、接受），但长尾噪声大。对比 `filter=title_and_abstract.search:<整句>` 只有 9 篇且混入无关论文（多词要求同时命中）。结论：Phase 1 的查询生成应产生**短而精的词组**，并评估是否用 `title_and_abstract.search` 做第二路召回。

**翻页**
- `page=N`：可用，翻 2、3 页与第 1 页无重叠；总量上限 10,000 条（`page=501, per-page=20` → 400 `Maximum results size of 10,000 records is exceeded`）。
- `per-page` 范围 1–200（201 → 400）。
- `cursor=*`：第一页可用，返回 `meta.next_cursor`；**用 next_cursor 翻第二页 → 401 `API key required for cursor paging`**。无 key 只能用 `page`。
- 过滤：`filter=from_publication_date:2024-01-01,type:article|preprint` 可用；`select=` 可裁剪字段。

**字段（单个 work 的有用部分）**：`id`、`doi`（`https://doi.org/...` 形式）、`title`/`display_name`、`publication_year`、`publication_date`、`type`（article/preprint…）、`authorships[].author.display_name`、`primary_location`（含 `landing_page_url`、`pdf_url`、`source.display_name`）、`best_oa_location.pdf_url`、`open_access.oa_url`、`cited_by_count`、`referenced_works`、`ids`、`language`。
- 预印本 DOI 形如 `10.48550/arxiv.<id>`，可由此回推 arXiv ID。
- 按 DOI 查询 `works/doi:10.48550/arxiv.1706.03762` 成功解析到 W2626778328，但返回的 `doi` 字段是另一个 DOI（`10.65215/...`）：**用 DOI 做种子解析可行，但不能假设返回的 DOI 与输入相同，去重要靠规范化后的多键匹配**。

**引用关系**
- 参考文献：work 里的 `referenced_works`（URL 列表）；批量取详情 `filter=openalex:W1|W2|...`（本次 16 个一次取回，1 credit）。
- 被引：`filter=cites:W...`，对 Attention Is All You Need 返回 `meta.count=24,511`，分页取回 20 条成功。新论文（被引 0）返回空是正常的。

**覆盖率（3 条查询 × 20 篇 = 60）**：有摘要 58（97%）、有 DOI 55（92%）、有 `best_oa_location.pdf_url` 43（72%）、有 `open_access.oa_url` 59（98%）。

## 2. Semantic Scholar（Graph API，未认证）

**端点**
- 搜索：`GET https://api.semanticscholar.org/graph/v1/paper/search?query=...&limit=20&offset=0&fields=...`。
- 本次请求的 fields：`title,abstract,authors,year,venue,publicationDate,externalIds,openAccessPdf,citationCount,url,isOpenAccess`，全部字段名有效。
- 批量搜索：`/paper/search/bulk`（返回 `data`、`token`、`total`）；q1 整句作为 query 只命中 1 篇，说明 bulk 的查询语法更严格（布尔），不适合直接放自然语言。
- 按 arXiv ID 解析种子：`/paper/ARXIV:<id>` —— **未验证**（429）。

**限流（本次最重要的结论）**
- 两次运行共 47 个请求（串行，间隔 1s，429 时按 2/4/8s 退避），**40 个是 429，仅 7 个成功**（约 15%）。
- 第一次运行 17 个请求只成功 1 个，断路器打开；第二次 29 个请求成功 5 个；间隔几分钟后的单个探测请求成功。
- 说明 429 与我们的请求速率无关（1 req/s、串行），而是**未认证共享额度被其他用户占满**，间歇性出现。响应里没有 `Retry-After` 或 `x-ratelimit-*` 头可用。
- 突发测试（20 个无间隔请求）：第 1–2 个就 429，断路器随即停止，未继续。
- 因无法用个人邮箱申请 key，key 路径**未验证**。

**翻页**
- `offset`/`limit`：offset=20 取到第二页，无重叠（q1 总共 28 篇，第二页 8 篇，`next` 为空）。
- 上限：`offset + limit` 必须 < 1000（offset=990, limit=20 → 400 `Relevance search offset + limit must be < 1000. Consider '/paper/search/bulk'`）。
- `limit` 最大值（>100）—— 未验证（429）。

**字段**：`paperId`、`title`、`abstract`、`authors`、`year`、`venue`、`publicationDate`、`externalIds`（含 `DOI`、`ArXiv`）、`openAccessPdf.url`、`isOpenAccess`、`citationCount`、`url`。

**覆盖率（q1/q2/q3 各取一次成功响应，共 60 篇，来自两次不同运行）**：有摘要 57（95%）、有 DOI 56（93%）、有 arXiv ID 37（62%）、有 `openAccessPdf.url` 32（53%）。

**引用关系**：`/paper/{id}/references`、`/paper/{id}/citations` —— **未验证**（两次均被 429 挡住）。引用滚雪球在 Phase 1 以 OpenAlex 为主，S2 作补充。

## 3. arXiv API

- 端点：`GET https://export.arxiv.org/api/query`，返回 Atom XML。参数：`search_query`、`start`、`max_results`、`sortBy`（relevance/submittedDate）、`sortOrder`、`id_list`。
- 速率：官方要求间隔不少于 3 秒；本次 12 个请求（两次运行）按 3s 间隔串行，全部 200，无 429。未测更快的间隔。
- 查询语法：`all:`、`AND/OR`、括号、引号短语均可用。自然语言长句不适用，需转换（见开头）。
- 翻页：`start=20&max_results=20` 取到第二页（q1 共 22 篇，第二页 2 篇，无重叠）。上限未测。
- 响应元数据：`opensearch:totalResults`、`startIndex`、`itemsPerPage`。
- 字段：`id`（形如 `http://arxiv.org/abs/2601.21379v1`，**带版本号和 http**，规范化时需去掉）、`title`、`summary`（摘要）、`author/name`、`published`、`updated`、`arxiv:doi`、`arxiv:journal_ref`、`arxiv:comment`、`arxiv:primary_category`、`link[title=pdf]`。
- 按 ID 查询：`id_list=1706.03762` 成功（返回 v7）。
- 日期过滤：`submittedDate:[202401010000 TO 202612312359]` 请求成功，但 `totalResults` 与不加过滤相同（108），结果日期均在 2026 年，**无法据此确认过滤生效**，Phase 1 需单独验证。
- 覆盖率（3 条 = 57 篇）：有摘要 57（100%）、有 PDF 链接 57（100%）、作者提供的 `doi` 仅 4（7%）、`journal_ref` 仅 3（5%）。**预印本的"已发表"状态不能靠 arXiv 判断**，需用 OpenAlex/S2 回配。
- 无引用/被引接口。

## 4. Google Scholar（`scholarly`）

脚本 `spikes/spike_gscholar.py`，**单独运行**，不与其他源并发：3 条同样的查询、每条取 20 篇（每页 10 条，共 6 个页面请求）、请求之间随机停 10–30 秒、遇到拦截立即停止且不重试。全程 131 秒。

**结果**：无验证码、无封禁、无 429，3 条查询共 60 篇全部取到（`blocked: null`）。仅一次测试、单个出口 IP，**不能据此推断长期稳定**。

**安装问题**：`scholarly` 依赖 `bibtexparser` 1.x；装最新版（2.x）会在 import 时报 `No module named 'bibtexparser.bibdatabase'`。需固定 `bibtexparser<2`（脚本头部已固定）。`scholarly` 自身还有一条 `SyntaxWarning`（正则转义），不影响运行。

**返回内容（`PUBLICATION_SEARCH_SNIPPET`，未 fill）**
- `bib`：只有 `title`、`author`、`pub_year`、`venue`、`abstract`。**没有 DOI，没有 arXiv ID。**
- 顶层：`gsrank`、`pub_url`、`eprint_url`、`num_citations`、`citedby_url`、`author_id`、`url_scholarbib` 等。
- **`abstract` 只是搜索结果里的关键词片段**，最长约 190 字符，中间有省略（如 "accepted by developers? We examine whether … Our analysis of"），**不能当摘要用于 L1 打分**。要拿完整摘要必须对每篇再发请求（`fill`），代价和封禁风险都大，不建议。
- `author` 是缩写（如 `A Ghammam`），不是全名。`venue` 常被截断（`Proceedings of the 23rd …`）。
- `pub_year`：4/60 为 `"NA"`，其余有值。
- `pub_url`：58/60 是有效 http 链接（多为出版商页面）；**`eprint_url`：出现的 52 个全是 `javascript:void(0)`，没有一个可用**，不能当 PDF 链接。
- 重要：这些字段与 CLAUDE.md "链接、DOI、作者、年份只能来自数据源字段" 的规则兼容，但质量不足以单独作为主元数据。

**与其他源的重叠**：GS 前 20 篇的标题与另三个源已取到的 60 篇样本精确匹配分别为 3/20（q1）、11/20（q2）、4/20（q3）。这只比较了各源的前 20，不代表质量，但说明 GS 排出来的集合与其他源明显不同，作补充召回有价值。

**速度**：每次 `next()` 在同一页内几乎为 0 秒，换页时约 3.6–5.3 秒（真实的页面请求）。

**对 Phase 1 的含义**
- 可以作为"补充一轮"的源，维持 SPEC 的设计：低频、随机间隔、遇阻即停。
- 必须"回配"：用标题去 OpenAlex 查 DOI、摘要和 OA 链接，配不上的标记 `gs_only` 且不进入基于摘要的打分，或仅按标题打分并标注"无摘要"。
- 不依赖它的链接字段中的 `eprint_url`。

## 5. 并发与耗时

- 三源同时跑：OpenAlex 12s、arXiv 15s、S2 65s（含退避等待），总耗时 65s ≈ 最慢的那个源，而不是三者之和。跨源 `asyncio.gather` 有效。
- 同源并发度 1：OpenAlex 和 arXiv 无需更快；S2 的瓶颈是共享额度，加并发只会更糟。
- 同源内并发（如 OpenAlex 并发 5）**未测**（按约定先串行）。
- Google Scholar 不参与并发，按设计保持低频串行。

## 6. 对 SPEC 的建议修改（待你确认后再改 SPEC）

1. **§4 OpenAlex**：把"可选 API key"改为"建议申请免费 key"，并记录额度制（10 credits/搜索，无 key $0.10/天/IP，有 key $1/天）；`mailto` 仅作标识，不影响额度。`--per-query` 的翻页只用 `page`（≤10,000 条）或带 key 的 `cursor`。
2. **§3.8 预算**：`--dry-run` 增加"预计 OpenAlex credits 消耗"，运行前读取 `x-ratelimit-remaining`，不足时提前提示。
3. **§4 Semantic Scholar**：标注为"尽力而为"：未认证约 85% 请求 429；失败写入 `source_runs`；不作为雪球的唯一引用来源，引用关系以 OpenAlex 为主。
4. **§3.2 查询生成**：要求产出短词组；OpenAlex 默认全文检索噪声大，评估增加 `title_and_abstract.search` 路径；每个源有独立的查询语法适配层（arXiv 布尔语法）。
5. **§5.2 去重**：arXiv ID 要去版本号与 `http://arxiv.org/abs/` 前缀；OpenAlex DOI 去掉 `https://doi.org/` 前缀并小写；预印本 DOI `10.48550/arxiv.*` 可回推 arXiv ID。
6. **§4 Google Scholar**：补充说明 `scholarly` 依赖需固定 `bibtexparser<2`；GS 结果只有搜索片段，不得用于 L1 打分，必须回配 OpenAlex 取 DOI 与摘要；忽略 `eprint_url`。
7. **§12**：把 OpenAlex / arXiv / `scholarly` 的"待核实"项更新为已核实，保留 S2 引用端点、模型 ID 为待核实。

## 7. 本次未完成 / 未验证

- Google Scholar：只测了 1 次、3 条查询、单个出口 IP；长期封禁风险、代理、`fill` 取完整摘要（刻意不测）均未验证。
- 模型 ID 核实——按你的决定跳过，Phase 1 前补做。
- S2：`references`/`citations` 端点、`limit` 上限、`ARXIV:` 种子解析、token 翻页、API key 路径（无法申请）。
- OpenAlex：API key 路径、`cursor` 完整翻页、`per-page=50` 的额度消耗、同源并发。
- arXiv：低于 3s 的行为（刻意不测）、翻页深度上限、日期过滤是否生效。
- 本次运行消耗了你当前网络出口 IP 的 OpenAlex 当日额度约 100–150 credits（共 1000）。
