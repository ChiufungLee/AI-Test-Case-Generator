# 决策记录（Decision Log）

本文件记录项目演进过程中的重要决策。

**记录标准**：一个决策如果未来可能被质疑、被推翻、或被忘记动机，就值得记录。不追求面面俱到，避免流水账。

**维护规则**：条目只追加、不删除；决策被推翻时，将原条目状态改为「已推翻」并注明替代决策编号；修订历史由 git 留痕。

| 编号 | 日期 | 类别 | 决策摘要 | 状态 |
|---|---|---|---|---|
| D-001 | 2026-10-01 | 流程 | 新功能独立分支开发，开发前走计划模式出计划 | 生效中 |
| D-002 | 2026-10-01 | 路线 | 从"AI 功能集合"转向"测试资产闭环"，按五阶段推进，执行并入 API 工作台阶段 | 生效中 |
| D-003 | 2026-10-01 | 架构 | LLM 一律走结构化输出链，不做 function-calling 工具层 | 生效中 |
| D-004 | 2026-10-01 | 范围 | 用例的 AI 修改延后至 1.5 期，数据模型预留 | 生效中 |
| D-005 | 2026-10-01 | 架构 | 测试资产独立建表，Artifact 保持"运行产物溯源"定位 | 生效中 |
| D-006 | 2026-10-01 | 架构 | 用例集版本以整集 JSON 存版本行，不做用例级拆表 | 生效中 |
| D-007 | 2026-10-01 | 设计 | 一个工作流至多发布一个资产，重复发布追加版本 | 生效中 |
| D-008 | 2026-10-01 | 设计 | 用例编辑采用全量替换 + base_version 乐观锁 | 生效中 |
| D-009 | 2026-10-01 | 设计 | 资产 owner 外键用 CASCADE 非空，而非 KnowledgeBase 的 SET NULL | 生效中 |
| D-010 | 2026-10-01 | 设计 | 重复 case_id 在发布与编辑入口校验拒绝（422） | 生效中 |
| D-011 | 2026-10-01 | 命名 | 导航「测试工作台」，术语「测试用例集」，代码 test_asset、路径 /api/test-sets | 生效中 |
| D-012 | 2026-10-01 | 架构 | 测试执行第一版用进程内 httpx，Runner 抽象推迟到 UI 自动化阶段 | 生效中（方向性） |
| D-013 | 2026-10-01 | 设计 | source_workflow_id 加数据库唯一约束，一 workflow 一资产由 DB 强制 | 生效中 |
| D-014 | 2026-10-01 | 设计 | 版本语义分离：parent 恒为当前版本且不对外开放，rollback 来源用 source_version_id | 生效中 |
| D-015 | 2026-10-01 | 设计 | case_id 为不可变业务身份：编辑入口强制显式删除声明，publish/rollback 豁免 | 生效中 |
| D-016 | 2026-10-01 | 设计 | 资产与工作流单向关联，Workflow 永不回指资产 | 生效中 |
| D-017 | 2026-10-01 | 设计 | AI 修改两段式：preview 不落库，diff 确认后 confirm 落 ai_edit 版本 | 生效中 |
| D-018 | 2026-10-01 | 架构 | AI 编辑复用结构化解析链并经模块属性调用；「调用+校验」重试自建循环不嵌套 | 生效中 |
| D-019 | 2026-10-01 | 架构 | API 规格资产化，模型按域分文件（api_test_models 平级新文件） | 生效中 |
| D-020 | 2026-10-01 | 设计 | 测试设计=确定性规则引擎为主，LLM 仅补业务语义维度 | 生效中 |
| D-021 | 2026-10-01 | 架构 | SSE 运行基建泛化为 RunHub 共用；执行断言 v1 仅状态码 | 生效中 |
| D-022 | 2026-10-02 | 架构 | OpenAPI URL 导入与同步：按 (method, path) 匹配刷新快照、保留用例 | 生效中 |
| D-023 | 2026-10-02 | 设计 | 执行历史面向规格可读者开放并展示执行人；执行发起仍 owner-only | 生效中 |
| D-024 | 2026-10-02 | 设计 | 请求体媒体类型快照 + 组合 schema 归一 + 用例可展开编辑 | 生效中 |
| D-025 | 2026-10-02 | 设计 | 登录态前置请求：配置存规格（凭据 owner-only），执行先登录收集 Cookie/Token；每轮执行独立 httpx client | 生效中 |
| D-026 | 2026-10-03 | 命名 | 导航拆分：移除「测试工作台」一级菜单，「测试用例集」（/testbench）与「接口测试」（/api-test）独立成页 | 生效中 |

---

## D-001 · 2026-10-01 · 流程：新功能分支独立开发 + 计划模式前置

**决策**：所有新功能在独立分支上开发（分支名沿用现有 `type/scope` 惯例，如 `feat/test-assets`）；写代码之前必须先走计划模式产出实施计划并获得确认。

**原因**：（1）功能开发与 main 隔离，main 始终可用；（2）计划先行把方案分歧暴露在写代码之前，避免返工；（3）仓库历史本就按功能分支合并（如 `docs/update-readme-screenshots`、`refactor/func-to-knowledge-pages`），此条将其固化为硬性流程。

**状态**：生效中

## D-002 · 2026-10-01 · 路线：按五阶段推进测试资产闭环

**决策**：产品演进顺序：① 测试资产化（发布/查看/人工编辑/版本）→ ② API 测试工作台（OpenAPI 导入 + Schema 规则引擎 + 测试设计生成 + 进程内执行）→ ③ Runner 抽象与 UI 自动化（Playwright 等）→ ④ AI 失败分析深化 → ⑤ CI Webhook 接入。执行能力并入第②阶段而非单列；"增加更多质量分析 Agent"暂缓。

**原因**：当前平台链路终止于 CSV 导出（`api/endpoints/workflow_api.py` 的 export 端点），"生成 → 导出 → 人工处理"没有闭环，断点在"用例无法沉淀为资产、资产无法执行"。API 执行本质是 HTTP 调用，成本远低于单列一个"执行阶段"，并入②使平台在第二阶段末即第一次具备真实执行能力。否决的备选：先做更多 AI 分析类功能——分析质量的边际收益低于闭环完整性。

**状态**：生效中

## D-003 · 2026-10-01 · 架构：LLM 一律走结构化输出链，不做 function-calling 工具层

**决策**：所有 LLM 调用（含未来"AI 修改用例"）通过 `_invoke_structured` 的解析链（纯文本 JSON 优先 → 截断抢救 `_salvage_truncated_json` → `with_structured_output` 兜底，失败重试 1 次）；不采用"给 AI 提供 search_test_cases()/update_test_case() 等工具"的 agent 工具调用路线。工具层语义由代码直接调用的确定性服务函数承担，LLM 只负责产出结构化数据。

**原因**：实测（记录于 AGENTS.md）DeepSeek 思考模式下复杂 prompt 自动触发思考，此时强制 `tool_choice` 一律 400；agent 工具路线在本项目的 LLM 栈上不可依赖。

**状态**：生效中

## D-004 · 2026-10-01 · 范围：用例的 AI 修改延后至 1.5 期

**决策**：资产化第一期只交付发布 / 查看 / 人工编辑 / 版本管理 / 版本对比 / 回滚 / 导出；"AI 修改用例"作为 1.5 紧随其后。数据模型预留 `TestCaseSetVersion.source_type = 'ai_edit'` 枚举值。

**原因**：第一期先保证资产化独立可上线、价值自足；AI 修改的确认流程依赖版本 diff 展示，复用第一期建好的 diff 服务函数与前端组件成本显著更低。模型预留避免 1.5 期再做迁移。

**状态**：生效中

## D-005 · 2026-10-01 · 架构：测试资产独立建表，Artifact 保持"运行产物溯源"定位

**决策**：新增 `test_case_sets` / `test_case_set_versions` 表承载资产，Artifact 表结构不动。资产对工作流的溯源用可空弱关联（`source_workflow_id`、`source_artifact_id`，ondelete=SET NULL）。

**原因**：Artifact 被 workflow 作用域锁死——唯一约束含 workflow_id、随 Workflow 级联删除——而资产需要独立生命周期（owner、可见性、被执行/Bug 引用）。工作流删除后资产必须存活，故溯源只能弱关联（删除后溯源字段置空，内容不受影响）。否决的备选：把 Artifact"升级为资产基础数据层"——会破坏其运行溯源语义，且级联删除行为对长期资产是危险的。

**状态**：生效中

## D-006 · 2026-10-01 · 架构：用例集版本以整集 JSON 存版本行，不做用例级拆表

**决策**：`test_case_set_versions.content` 存与 Artifact 同构的 `{"test_cases": [...]}` JSON；不把用例拆成行级表。未来执行结果以 `(set_id, version, case_id 字符串)` 引用用例。

**原因**：单集规模上限 40 条（`_dynamic_case_limit` = clamp(需求点数×3, 10, 40)）；本期及可预见的操作全部是全集操作（发布、AI 全量修改、CSV 导出、diff、执行输入），Python 内按 case_id 计算 diff 足够；整集 JSON 与 Artifact 语义对齐，AI 修改天然全量进出。**重新评估的触发条件**：出现按用例维度的数据库查询/索引需求（如跨资产检索用例、执行结果需要外键完整性）——届时从 JSON 解析派生行级表即可，迁移成本不高。

**状态**：生效中

## D-007 · 2026-10-01 · 设计：一个工作流至多发布一个资产，重复发布追加版本

**决策**：`publish_from_workflow` 首次执行创建资产 + v1；同一工作流重新生成用例后再次发布，在已关联资产上追加新版本（`source_type=publish`，记录新的 `source_artifact_id`）。

**原因**：让"发布"语义稳定为"把工作流最新用例同步进资产"，用户无需理解资产与工作流的版本对应关系；版本链自动保留每次同步的溯源。否决的备选：每次发布新建资产——会产生指向同一工作流的多个平行资产，列表噪音大且资产身份不连续。

**状态**：生效中

## D-008 · 2026-10-01 · 设计：用例编辑采用全量替换 + base_version 乐观锁

**决策**：编辑端点接收完整用例集内容 + `base_version`；服务端先 `TestCaseSet.model_validate` 校验，再以条件更新 `UPDATE ... SET current_version = current_version + 1 WHERE id = ? AND current_version = :base_version` 原子推进版本号，rowcount 为 0 即返回 409。不提供用例级 PATCH；并发冲突不做三方合并，前端引导"加载最新版本"。

**原因**：与现有产物编辑校验模式一致（`/approve` 端点对 analysis 的 model_validate + 422 处理）；全量替换原子性好、实现简单，40 条规模下局部更新无收益。条件更新的并发控制思路对齐 `try_claim_workflow` 的状态机乐观锁。

**状态**：生效中

## D-009 · 2026-10-01 · 设计：资产 owner 外键用 CASCADE 非空，而非 KnowledgeBase 的 SET NULL

**决策**：`test_case_sets.owner_user_id` 为非空、`ondelete=CASCADE`（对齐 `workflows.user_id`），而非 `knowledge_bases.owner_user_id` 的可空 SET NULL。

**原因**：资产是纯 DB 数据，删除用户级联删除资产无副作用；知识库用 SET NULL 大概率与其删除牵涉 ChromaDB 集合清理的后台任务有关，资产没有这类外部资源。**已知不一致点**：同为用户资产，知识库与用例集在用户删除时行为不同；若未来产品要求"注销后资产可交接/保留"，需改为 SET NULL 并补管理员交接逻辑。

**状态**：生效中

## D-010 · 2026-10-01 · 设计：重复 case_id 在发布与编辑入口校验拒绝（422）

**决策**：`TestCaseSet` 在 Pydantic 字段校验之外，额外要求集合内 case_id 唯一；发布与内容编辑两个入口都执行该校验，违反返回 422 并明确提示重复的编号。

**原因**：版本 diff 按 case_id 键控、未来执行结果按 case_id 引用，唯一性是这两个机制的前提。生成端不保证唯一（截断抢救场景可能产生重复编号），必须在资产入口把关；拒绝并提示比静默去重更安全——用户应当知道生成了重复用例。

**状态**：生效中

## D-011 · 2026-10-01 · 命名：导航「测试工作台」，术语「测试用例集」，代码 test_asset、路径 /api/test-sets

**决策**：用户可见文案：导航项「测试工作台」、资产称「测试用例集」、溯源称「来自测试任务」；代码与 API：服务模块 `test_asset_service`、路径 `/api/test-sets/...`、页面路由 `/testbench`。

**原因**：延续既定文案约定（用户可见一律「测试任务」、不用「工作流」）；"工作台"为后续执行功能预留同一模块空间，第一期只增加一个导航项，避免导航膨胀。否决的备选：「测试资产中心」（偏内部视角，用户感知弱）、「用例库」（后续还要容纳执行入口，语义偏窄）。

**状态**：生效中

## D-012 · 2026-10-01 · 架构（方向性）：测试执行第一版用进程内 httpx，Runner 抽象推迟

**决策**：第二阶段的执行能力用进程内异步 httpx 执行 API 用例，复用工作流已验证的基建模式（后台 asyncio 任务 + 事件重放 SSE + 状态机乐观锁）；第一版不引入 subprocess / pytest / Docker 等 Runner 抽象。

**原因**：API 测试的执行本质是 HTTP 调用，无需进程隔离；建议方案中的 `subprocess.run(pytest)` 对 API 用例反而引入进程管理与输出解析负担。**重新评估的触发条件**：引入 UI 自动化（Playwright 需要浏览器进程与隔离）或负载测试时，再抽象 Runner 层。

**状态**：生效中（方向性，未实施）

## D-013 · 2026-10-01 · 设计：source_workflow_id 加数据库唯一约束（细化 D-007）

**决策**：`test_case_sets.source_workflow_id` 增加 `UniqueConstraint("source_workflow_id", name="uq_test_case_set_source_workflow")`。"一个 workflow 至多对应一个资产"由数据库强制，而非仅靠应用层先查后插。发布创建资产路径捕获 IntegrityError → 回滚 → 返回 409 并发冲突。

**原因**：应用层"先查后插"存在并发窗口——两个并发发布都可能查不到关联资产而各自插入，产生指向同一工作流的两个平行资产，直接违反 D-007。SQLite 与 MySQL 的唯一约束均允许多个 NULL，工作流删除（SET NULL）后不受影响，也不妨碍未来独立创建的资产。

**状态**：生效中

## D-014 · 2026-10-01 · 设计：版本语义分离，parent 恒为当前版本且不对外开放（细化/修正 D-008）

**决策**：`parent_version_id` 语义固化为"这个版本在谁的基础上产生"——恒等于乐观锁校验通过的 base_version 对应版本行，由服务层内部推导，`save_new_version` 不对外暴露 parent 参数；`rollback` 的内容来源用独立字段 `source_version_id`（自引用 FK，仅 rollback 使用）记录。版本链严格线性：回滚 v5→v2 产生 v6 时，v6.parent=v5、v6.source_version_id=v2，而非 v6.parent=v2。

**原因**：原设计让 rollback 的 parent 指向历史版本，版本关系会分叉成树，"版本演进"语义被破坏；对外开放 parent 参数则可能在并发下制造不受控的链。两个概念分开后，publish / manual_edit / ai_edit / rollback 遵循同一条简单规则：**任何新版本都从当前版本产生**，内容来源另行记录。

**状态**：生效中

## D-015 · 2026-10-01 · 设计：case_id 为不可变业务身份，编辑入口强制显式删除声明（细化 D-010）

**决策**：case_id 是测试用例的业务身份（未来执行结果以 `(set_id, version, case_id)` 引用）。`manual_edit`（及未来 `ai_edit`）保存新版本时，服务端校验：「base 版本有而新内容没有的 case_id ⊆ 请求显式声明的 `deleted_case_ids`」且「声明的删除 ⊆ base 版本 ID」，违反返回 422。`publish`（重新生成后编号本就可能全变）与 `rollback`（恢复历史内容）豁免。前端编辑模式中已有用例的编号只读，新增用例自动取号。

**原因**：若 case_id 可在编辑中被顺手改掉，diff 可读性、历史追踪、未来执行与 Bug 关联全部复杂化。纯"禁止改 ID"在服务端不可判定——改名在数据上等价于"删旧+增新"；引入显式删除声明后，意外重编号（如客户端 bug 整体重排）会被 422 拦截，而改名只能表达为显式的删除+新增，diff 会如实展示。**已知局限**：刻意"删除 TC-001 再新增内容相同的 TC-008"无法与正当的删旧建新区分，该守卫防的是意外篡改而非恶意绕过。

**状态**：生效中

## D-016 · 2026-10-01 · 设计：资产与工作流单向关联（确认 D-005）

**决策**：资产回指工作流（`source_workflow_id`），Workflow 表永不增加指向资产的字段。数据流向保持：Workflow → Artifact → publish → TestCaseSet。

**原因**：一个工作流可产生多类 Artifact，资产只是其业务出口之一；双向引用会造成删除顺序耦合与循环依赖。资产知道"我从哪个工作流来"已满足溯源需求，工作流无需知道"我绑定了哪个资产"。

**状态**：生效中

## D-017 · 2026-10-01 · 设计：AI 修改用例为两段式预览/确认（细化 D-004）

**决策**：AI 修改拆为 preview 与 confirm 两步。preview：读当前版本 → LLM 结构化输出修改后的完整用例集提案（**不落库**）→ 服务端按基线与提案的编号差集推导删除声明 → 返回提案 + diff。用户在 diff 确认页裁决后，confirm 以 base_version 乐观锁落 `source_type=ai_edit` 新版本；预览到确认之间资产被改动则 409，需重新生成。删除声明始终由服务端推导，不信任前端提交。

**原因**：AI 输出必须经人工裁决才能成为资产版本（与工作流"人工确认"节点同一理念）；整集 JSON 输出配合现成的 diff 组件（含字符级高亮）使裁决成本最低。两步分离避免"先落库再回滚"产生脏版本；服务端推导删除防止前端伪造删除声明绕过不可变校验（D-015）。

**状态**：生效中

## D-018 · 2026-10-01 · 架构：AI 编辑复用结构化解析链，经模块属性调用保持桩兼容（细化 D-003）

**决策**：AI 修改的结构化调用复用 `workflows.nodes` 的解析链（纯文本 JSON → 截断抢救 → with_structured_output 兜底），服务层以 `nodes._invoke_structured` 的**模块属性方式调用**（不做 from-import 早期绑定）；「LLM 调用 + 输出校验」自建 2 次尝试循环，不嵌套 `_call_structured_with_retry`（避免最坏 4 次调用）；提示词归入 `WORKFLOW_PROMPTS`（其定位实为"结构化输出提示词"），模板正文无字面花括号，JSON 入参经占位符的值注入。

**原因**：三级解析链是 DeepSeek 思考模式限制下验证过的唯一可靠路径（思考模式强制 tool_choice 一律 400）；早期绑定会让 conftest 的 `monkeypatch.setattr(workflow_nodes, "_invoke_structured", fake)` 桩失效；输出校验失败（case_id 重复等）属于可重试的生成质量问题，纳入同一重试语义比失败即弃更稳，但嵌套两层重试会让单次请求最多打 4 次 LLM，得不偿失。

**状态**：生效中

## D-019 · 2026-10-01 · 架构：API 规格资产化，模型按域分文件

**决策**：第二阶段引入 `ApiSpec`（OpenAPI 文档资产）+ `ApiEndpoint`（path×method 展平的接口快照，parameters/requestBody 已做局部 $ref 解引用与 2.0 body 参数归一），模型放新文件 `models/api_test_models.py`（与 test_asset_models 平级）；规格复用 owner/visibility 资产语义。后续用例（ApiEndpointCase）与执行（TestRun/TestRunResult）同文件扩展。

**原因**：规格是用例与执行的源头资产，独立成域才能让"导入 → 生成 → 执行"链路自洽；test_asset_models 已被用例集语义占满（注释含 1.5 期预留），混入会破坏域边界；仓库既有约定就是按业务域一文件。

**状态**：生效中

## D-020 · 2026-10-01 · 设计：测试设计=确定性规则引擎为主，LLM 仅补业务语义

**决策**：接口用例生成拆两层——`api_case_engine`（纯函数零 LLM）：从 JSON Schema 确定性生成正常样例（required 最小合法 + enum 首值 + format 启发 + min/max 满足）与异常维度（缺失必填/类型错误/越界/非法枚举/违反 pattern，逐维限量 3 条）；`openapi_business_cases_workflow`（LLM，0.4）：仅补业务语义维度（权限/并发/状态依赖/脏数据），提案经前端确认后落库。规则引擎重生成只替换 rule_engine 用例，manual/ai 保留；同名冲突 422。

**原因**：schema 可推导的用例零成本、零幻觉、可重放；LLM 输出不可复现且对纯 schema 维度毫无优势——这正是 D-002 确定性优先在 API 域的落地。异常预期状态 v1 固定 400（真实服务可能 422，UI 可改）。

**状态**：生效中

## D-021 · 2026-10-01 · 架构：SSE 运行基建泛化为 RunHub 共用；执行断言 v1 仅状态码

**决策**：把 workflow_api 的运行基建（事件缓冲 + 订阅队列重放 + None 哨兵 + start 锁 + 注册表）泛化抽取为 `api/endpoints/run_hub.py` 的 `RunHub`（按 key 注册，同 key 复用语义），workflow_api 与 API 测试执行共用同一实现；API 测试执行器为进程内 httpx.AsyncClient 单例（单事件循环同步段无抢占，无需双检锁）+ 后台 asyncio 任务顺序执行，事件按 run_id key 发布（`publish_key`，执行协程不持有 handle）。断言 v1 仅状态码比对（passed/failed/error 三态，连接类异常归 error），响应快照截断存储。

**原因**：两套发布/订阅逻辑同构，复制会漂移（工作流已验证的晚接入重放/断连不影响执行语义直接继承）；执行不绑 HTTP 请求与工作流同理念。断言 v1 只做状态码——响应体 JSON 断言规则属后续增强，先交付"生成 → 执行 → 三态结果"最小闭环。进程重启后的残留 running 以 409 提示重试（测试执行无 checkpoint，恢复语义属后续）。

**状态**：生效中

## D-022 · 2026-10-02 · 架构：OpenAPI URL 导入与同步

**决策**：`ApiSpec` 增加 `source_url`。`POST /api/api-specs/import-url` 服务端拉取文档（httpx 跟随重定向、2MB 上限、`API_SPEC_IMPORT_TIMEOUT` 超时、仅 http/https；名称缺省取 info.title）；`POST /api/api-specs/{id}/sync`（owner-only）重新拉取并解析，接口快照按 **(method, path) 匹配原行更新**（endpoint id 不变 → 既有用例与关联保留），远端已消失的接口连及其用例删除，同时刷新 content/format/spec_title/spec_version/endpoint_count。

**原因**：被测服务的文档会持续演进，「重导入=新资产」会割裂用例与执行历史和文档的关联；按 (method,path) 匹配让同步成为"刷新快照、保留资产"的低风险操作。拉取放服务端而非浏览器 fetch，避免 CORS 挡住内网文档；与执行阶段允许任意 base_url 的语义一致，不设内网限制。

**状态**：生效中

## D-023 · 2026-10-02 · 设计：执行历史面向规格可读者开放并展示执行人

**决策**：TestRun 历史列表与执行详情从 owner-only 扩展为「执行人本人 **或** 规格可读者（owner/共享）」可见，payload 附 `created_by`/`created_by_username`；发起执行仍 owner-only，占用语义（同规格单人执行）不变。规格被删除后可读者失去访问权，执行人本人仍可回看自己的执行。

**原因**：共享规格是团队协作单元，"谁执行过、结果如何"是协作的基础信息，只展示自己的历史使共享阅读失去意义。逐条结果含请求/响应快照，其可见性与规格可见性对齐（共享读者本就能看到接口定义与用例），比按人裁剪更可预期。

**状态**：生效中

## D-024 · 2026-10-02 · 设计：请求体媒体类型快照 + 组合 schema 归一 + 用例可编辑

**决策**：`ApiEndpoint` 增加 `request_body_media_type`（优先级 json > urlencoded > multipart；Swagger 2.0 `in: formData` 参数聚合为表单请求体，consumes/file 字段推断媒体类型），执行层按媒体类型选择 `json=` / `data=` / `files=`（binary 字段发占位文件）；规则引擎对组合 schema 归一（`_normalize_schema`：allOf 深合并、oneOf/anyOf 取首支）并补 header 参数采样；前端用例行可展开，编辑名称/预期状态与 path/query/headers/body（JSON 编辑区），保存沿用整表替换。存量行迁移回填 application/json。

**原因**：此前 form/multipart 接口与 allOf/oneOf 请求体在采样时得到空对象，「正常请求」打到服务端必 422（FastAPI 系服务尤甚）；用例在前端完全不可见不可编辑，使 D-020「服务端实际返回 422 时可在 UI 中调整」落空——用户遇到 422 既看不到发出的请求也无从修正。用例编辑是确定性规则引擎与真实服务业务校验之间差距的必要逃生门。

**状态**：生效中

## D-025 · 2026-10-02 · 设计：登录态前置请求，会话 Cookie/Token 自动携带

**决策**：`ApiSpec` 增加 `auth_config_json`（owner-only 编辑：method/path/body/body_type/token_field）。执行时先发该登录请求：响应 Set-Cookie 自动进入本轮 client 的 cookie jar 供后续用例携带；配置 `token_field` 时从 2xx 响应 JSON 提取 token 生成 `Authorization: Bearer` 头。登录请求不计入用例结果，经 `auth_done` 事件单独下发；失败则本轮直接 failed（error 落 `test_runs.error` 新列），不产生误导性的逐条 401 结果。执行 client 从进程内单例改为**每轮独立**（cookie 隔离，不跨执行/跨用户串会话）；对非 JSON 登录体支持 form 发送。对外 payload 中方法/路径所有人可见，**请求体（凭据）仅 owner 可见**。登录请求的媒体类型以规格中匹配端点的声明优先（表单端点收 JSON 必 422），配置的 body_type 仅在端点未声明/不存在时回退生效，前端在填写路径时按声明自动切换。细化 D-021（client 生命周期变更）。

**原因**：session 认证不在 OpenAPI 文档中，规则引擎与用例编辑都无法表达"先登录"；逐条用例 401 的结果既误导判断也难逐一维护 Cookie。前置登录 + cookie jar 是 Postman/JMeter 验证过的模式，一套机制同时覆盖会话型与 Bearer 型 API。凭据与规格同等信任级别存储，但展示按 owner 裁剪，避免共享读者看到密码。

**状态**：生效中

## D-026 · 2026-10-03 · 命名：导航拆分——测试用例集与接口测试独立为一级菜单

**决策**：移除侧边栏「测试工作台」一级菜单，原页内二级 tab 拆为两个独立页面与一级菜单：「测试用例集」（`/testbench`，active_nav=testbench）与「接口测试」（`/api-test`，active_nav=api_test，替代「API 接口管理」称谓）。页面各自只加载所需脚本（testbench.js / api_workbench.js），页头标题与副标题随页面独立；`/testbench-detail` 归属用例集域不变。

**原因**：「API 接口管理」与「测试用例集」是两类并列的测试资产域，二级 tab 使入口层级深且不可直达/分享；一级菜单独立后 URL 即语义（/api-test 可直接分享），导航高亮由服务端 active_nav 控制无需前端判断。

**状态**：生效中

## D-027 · 2026-10-06 · 设计：响应体字段断言（扩展仅状态码断言）+ 前端批量执行

**决策**：断言从「仅状态码」扩展为两层（细化 D-021）。`ApiEndpointCase` 增加 `assertions_json`（断言规格：`[{"target": "点路径", "op": "eq|exists|type", "expected": 值}]`），`TestRunResult` 增加 `assertions_json`（逐项评估结果，含 expected/actual 紧凑快照与 message）。求值器 `services/api_assertions.py` 为纯函数：eq 做 bool 性一致的值相等（防 `True == 1`）、exists 做点路径可达（支持数组下标段，null 视为存在）、type 做 JSON 类型核对（integer 满足 number）；**求值在执行处对完整响应体进行、先于 4KB 截断快照落库**；响应体非合法 JSON 时全部断言失败并给出明确文案。verdict = 状态码匹配 AND 全部断言通过，failure_reason 组合两部分。规则引擎从响应体 schema 为正常用例派生 exists/type 断言（顶层 required 字段，≤8 条；值断言留给 AI 建议与手工编辑），为此解析层新增 `response_schemas_json`（`{状态码: schema}`，与 `responses_json` 的 `{状态码: 描述}` 并行、不改旧列形状），`endpoint_payload` 与同步链路透传。AI 业务建议提案可选携带 assertions（Pydantic 校验非法即触发重试）。前端：用例编辑器增加响应断言 JSON 编辑区（行内「断言 N」徽标）、执行结果展开逐条 ✓/✗ 断言行、历史详情附断言摘要。同批交付批量执行：接口清单复选框多选/全选（后端 `endpoint_ids` 本就支持列表与缺省全部），`case_done` 事件与结果 payload 增加 `endpoint` 标签（`GET /path`），执行完成后按「实际状态码 × 接口」确定性分组展示失败（error 归「异常」组）。

**原因**：D-021 的仅状态码断言把「HTTP 可达」当成了「功能正确」——500 里带预期错误码的响应与 200 的正常响应无法区分，断言明细也是将来 AI 失败分析（已主动延期）的直接输入，先做好这层结构，AI 到场时只剩语义归类一件事。响应 schema 与描述分列存储避免旧数据形状混用；断言 op 只做 eq/exists/type 三个确定性操作符，把 JSONPath/Schema 全量校验排除在 v1 之外，保持「确定性优先」的项目原则。批量执行后端早已就绪（D-012 起即按整规格占用），本次只补 UI 选择粒度；失败分组是执行规模上来后人工看平铺列表的第一痛点，同样不需要 LLM。

**状态**：生效中

## D-028 · 2026-10-06 · 设计（暂缓实施）：测试用例集 → 接口用例关联

**决策**：把「测试设计 → 可执行测试」的关联方向定为**派生**而非行级绑定：`ApiEndpointCase` 增加溯源字段（`test_set_id` + `source_case_id`，均 SET NULL），由用例集用例（TestCaseSetVersion.content JSON 内的条目）经 AI/人工辅助**转换生成**接口用例并保留溯源；执行结果可反查设计用例，为将来「需求点 → 设计用例 → 执行结果」覆盖闭环预留回卷方向。绑定锚点是 `case_id`（D-015 不可变业务身份）而非数据库外键——用例集用例存在版本 JSON 内、没有行级 TestCase 表，行级 FK 方案不成立。版本语义：绑定挂在资产级，执行时按当时版本内容校验 case_id 仍存在（编辑/回滚后用 case_id 的稳定性存活，显式删除的用例其绑定自然失效）。**本轮仅记录设计，不实施**；实施前需另行明确派生交互（在用例集页还是接口测试页发起）与执行入口（按用例集一键执行）。

**原因**：工作流生成的用例是自由文本测试设计（steps/expected_results），不是 API 调用定义，「关联」的本质是一次带语义的转换而非加个外键；先派生后回卷的方向与「设计 → 可执行」的产品叙事一致，且 `source_type` 枚举天然可扩展（如 `from_test_case`）。若先做行级绑定会在不存在的表结构上空转。推迟实施的原因：当前断言与批量执行补齐「可验证、可放量」是更高频的痛点，关联的价值依赖派生交互的打磨，值得独立一轮做。

**状态**：设计中（未实施）

## D-029 · 2026-10-07 · 设计：重新解析——粘贴导入文档的同步等价物 + 同步按钮常显

**决策**：新增 `POST /api/api-specs/{spec_id}/reparse`（owner-only）：不拉取远端、不改写 content，仅用当前解析器重放存量 content 刷新接口快照，与同步共用快照刷新逻辑（`_refresh_endpoint_snapshots`：按 (method, path) 匹配原行更新，端点 id 不变 → 用例与执行历史关联保留）。前端：同步与重新解析两键对粘贴导入与 URL 导入的文档常显（owner），按文档来源互斥置灰——粘贴导入时同步置灰（无来源 URL 不可远程同步，tooltip 引导改用重新解析），URL 导入时重新解析置灰（用同步重新拉取即可）。

**原因**：解析器会持续演进（D-024 媒体类型修复、D-027 响应 schema 快照），但 D-022 的同步依赖 source_url——粘贴导入的文档在解析器升级后永远无法自愈，只能重导入并丢失用例与执行历史关联。实际事故：10-02 02:07 粘贴导入的文档带着「只认 application/json」的旧解析结果，表单类 POST（/api/chat、/login 等）的用例请求体全空，执行必失败。重新解析让存量文档随解析器升级自愈，无需重导入。
