---
type: architecture
status: pilot
version: "0.1-pilot"
created: 2026-08-11
updated: 2026-08-11
decision_state: validating
title: 知识库 2.0 双核一桥架构方案
source:
  - D:/photo/知识库/知识库双核一桥架构方案与现状全景对比.md
---

# 知识库 2.0：双核一桥架构方案

> 本文是知识库 2.0 的 V0.1 试运行架构与验证基线。  
> 它只描述新系统，不承担旧知识库审计、目录映射或迁移记录。  
> 当前只冻结事实源、双核所有权、不可变发布和单一 commit point；目录、Schema、CLI、检索与工作流细节必须经过真实样本验证后再冻结。

## 一、架构裁决

知识库 2.0 采用“双核一桥 + 活知识工作台 + 独立投影编译器”：

- **AI Organizer** 是默认整理者，接收人的自然语言、文件和修改，自动判断归属、生成结构并调用系统能力；它不是事实所有者。
- **Living Knowledge Workspace** 管尚在生长的知识，回答“目前在想什么、有哪些假设、冲突和综合”；它允许持续修改，不是第三个正式发布内核。
- **Context OS** 管跨会话工作，回答“为什么做、现在怎样、下一步是什么”。
- **Artifact KB** 管正式产物与证据，回答“留下了什么、哪个修订可用、以后怎样复用”。
- **Publication Bridge** 是唯一正式发布边界，内部的 Release Authority 负责“什么能够以及如何正式留下”。
- **Projection Compiler** 独立构建 HOME、Registry、SQLite、Capsule 和搜索；它与发布事务共享合同和安全策略，但不是发布 commit point。

一句话原则：

> 人只提供内容、意图和纠正；AI 负责整理，Release Authority 负责确定性校验与发布，事实只有一个归属，读取可以有多个视图。

### 1.1 架构地图

~~~mermaid
flowchart LR
    U["人<br/>提供内容、意图与纠正"]
    S["Durable Capture Spool<br/>先保存、安全预检、默认不索引"]
    O["AI Organizer<br/>归类、整理、生成元数据与候选"]
    L["Living Knowledge Workspace<br/>捕获、问题、假设、冲突、综合"]
    C["Context OS<br/>目标、状态、决策、证据、候选产物"]
    W["外部工作空间<br/>仓库、文稿、媒体工程、工作簿"]
    B["Release Authority<br/>校验、revision、Receipt、原子提交"]
    A["Artifact KB<br/>不可变 Artifact / Evidence Revision"]
    P["Publication Receipt<br/>验收、证据、哈希"]
    D["Projection Compiler<br/>独立编译"]
    G["Derived Read Models<br/>HOME、Capsule、Registry、SQLite、Search"]
    Y["Shared Policy<br/>Security、Publish、Index、Export"]
    V["外部 Secret Vault"]

    U --> S
    S --> O
    O --> L
    O --> C
    C --> W
    L -->|"正式化候选"| B
    C -->|"expected output"| B
    W -->|"candidate"| B
    B -->|"released revision"| A
    B -->|"commit marker"| P
    L --> D
    C --> D
    A --> D
    P --> D
    D --> G
    G --> U
    Y -. "same policy digest" .-> B
    Y -. "same policy digest" .-> D
    U -. "直接修改；形成 human override" .-> L
    U -. "直接修改；形成 human override" .-> C
    V -. "secret_ref" .-> C
    V -. "secret_ref" .-> A
~~~

### 1.2 复杂度归属

| 边界 | 拥有的复杂度 | 对调用者隐藏什么 |
|---|---|---|
| Durable Capture Spool | 输入持久化、安全预检、幂等重放 | AI 或工具故障时输入不丢失 |
| AI Organizer | 输入理解、自动归类、语义元数据生成、不确定性路由、纠错反馈 | 人不必选择目录、填写字段或操作 CLI |
| Living Knowledge Workspace | 可持续修改的笔记、问题、假设、冲突和综合 | 不必把尚未成熟的知识伪装成正式 Artifact |
| Context OS | 工作生命周期、当前态、决策、验证、交接 | 不必从全量日志重放当前状态 |
| Artifact KB | 稳定身份、不可变修订、权威范围、适用性 | 不必猜“哪份正式修订可用” |
| Release Authority | 哈希、revision、幂等、Receipt、发布事务与恢复 | AI 不必自行维护提交一致性 |
| Shared Policy | Security、Publish、Index 与 Export 的版本化判断 | Release 与 Projection 不各自解释一套规则 |
| Projection Compiler | Registry、SQLite、HOME、Capsule、Search 的同批构建 | 搜索和展示变化不进入发布核心 |
| Derived Read Models | 无事实所有权，只负责读取性能与视图 | 人和 Agent 不必扫描全库 |

最痛的复杂度中心仍是“从内容到可信结果”的晋升，但必须拆清两类复杂度：AI Organizer 负责可修正的语义判断，Release Authority 负责确定性的发布不变量，Projection Compiler 负责可失败、可重建的读取投影。统一 `kb` CLI 可以编排三者，但发布模块不得依赖搜索、SQLite、HOME 或其他 generated 内容。

### 1.3 选定的架构视角

- 边界与所有权：每条事实必须只有一个拥有者。
- AI-first 交互：Schema 是机器合同，不是人的表单。
- 接口稳定性：正式冻结的只有 URI、Receipt、发布结果和必要 Schema；实现细节先验证。
- 数据语义：运行态、发布事务、修订状态不能共用一个 status。
- 错误与恢复：发布成功、投影失败、候选变化必须有不同结果。
- 安全策略：任何索引晚于安全分类；语义不确定时进入低权威区或隔离区。
- 人工覆盖：人的直接修改必须稳定保留，AI 不得在后续整理中静默覆盖。
- 可验证性：语义生成允许不确定，结构和发布不变量必须能由确定性代码检查。

## 二、不可破坏的系统原则

1. Markdown、YAML、原始文件、Artifact Payload 和外部工作空间是事实源。
2. SQLite、Registry、HOME、Capsule、搜索和关系图都是可删除重建的投影。
3. 用户不填写结构化字段；用户界面只接收内容、意图和直接修改。
4. AI Organizer 自动创建和维护 Garden、Context 与 Artifact candidate；Schema 复杂度不得泄漏给用户。
5. AI 不确定时不得强迫用户填表：默认进入 Garden、`reference`、candidate 或安全隔离区。
6. 人的直接修改高于 AI 生成结果，必须形成可追踪、不可静默覆盖的 human override。
7. Context 拥有运行态；Living Knowledge Workspace 拥有持续演化的知识；Artifact 拥有 released revision；Release Authority 只拥有发布事务与 Receipt。
8. Projection Compiler 只拥有生成算法，不拥有 Context、Garden、Artifact 或 Receipt 的事实。
9. 只有正式对象需要稳定 ID；ID 不随标题、路径或文件名变化。
10. released revision 永不原地修改；任何发布语义变化产生新 revision。
11. 当前态与历史分离；当前状态不依赖重放完整 Events 才能获得。
12. 发布 source 只证明来源登记与完整性，不自动证明内容正确。
13. `canonical` 只用于明确声明单一当前答案的受治理范围；探索性知识默认允许多个 `reference` 并存。
14. Collection 默认只暴露 Manifest，不允许 Agent 默认扫描内部 raw 文件。
15. 真实秘密的长期事实只存在外部 Vault；Capture Spool 只允许在安全预检前受保护暂存，安全分类先于 SQLite、全文检索、向量处理或导出。
16. Release Authority 或 Projection Compiler 故障时，普通文件仍可由人直接阅读；投影失败不撤销已提交发布。
17. Agent 日常操作不得直接写 released、generated 或 SQLite。
18. 新旧知识库禁止双写；旧内容只在被真实复用时单向晋升。
19. 只治理高价值对象；普通捕获、活知识和短任务不进入正式发布流程。
20. 用户的明确纠错可以沉淀为有作用域的整理规则；单次含糊修改不得被 AI 擅自推广成全局政策。
21. 所有输入在 AI 分类前先进入受保护的 Durable Capture Spool；安全预检通过后才进入 Garden、Context、Artifact 或普通索引。

### 2.1 用户零表单合同

用户默认只做三件事：

1. 通过自然语言、文件、网页、图片或其他原始输入提供内容。
2. 阅读、使用和继续发展知识。
3. 发现结果不合理时，直接修改正文、标题、关系或通过自然语言纠正 AI。

用户不需要：

- 选择物理目录或对象类型。
- 创建 ID、Context 骨架、revision 或 Receipt。
- 填写 title、summary、tags、authority、applicability、validity、security 或 acceptance 表单。
- 手工调用发布、构建、索引或回链命令。

AI Organizer 负责完成这些结构化工作，并调用 `kb` façade 的稳定机器接口。JSON Schema、YAML、CLI 参数和 Policy 是 AI 与系统之间的合同，不是人的交互界面。

### 2.2 AI 不确定性路由

AI 的语义判断允许不确定，但不确定性必须有安全默认：

| 不确定项 | 默认处理 |
|---|---|
| 不确定是否成熟 | 写入 Living Knowledge Workspace，不创建正式 Artifact |
| 不确定是否值得长期治理 | 保留为 Garden note 或 candidate |
| 不确定是否权威 | 使用 `reference`，不得自动声称 `canonical` |
| 不确定适用范围或有效期 | 省略可选字段或标记待复核，不制造伪精确值 |
| 不确定是否包含秘密 | 进入隔离区，禁止索引与导出 |
| 不确定是否需要 Context | 默认不创建；只有跨会话推进、验证、交接或正式产出需要时才创建 |

普通不确定性不能转化为用户表单。只有安全风险、不可逆操作或事实冲突无法安全降级时，系统才暂停自动发布；原始输入仍必须完整保留。

### 2.3 Human Override 与纠错反馈

- 用户对 AI 生成的可编辑内容直接修改，是知识修改，不是表单填写。
- 系统必须将外部修改识别为 human override，并记录修改范围与时间。
- 后续 AI 整理可以更新机械字段，例如 digest、revision、generated_at；不得静默覆盖被人修改的语义字段。
- 对 released Artifact 的纠正不得原地修改历史 revision；系统必须把纠正转成下一 revision candidate，再由 Release Authority 发布。
- 若内容变化确实要求改动 human override，AI 必须生成可审计 diff，并保留原值与理由。
- 用户明确表达“以后这类内容都这样处理”时，AI 可以生成有作用域的整理规则；规则必须可定位、可撤销、可被后续反例修正。
- 同类错误重复出现，视为整理策略缺陷，而不是要求用户反复修改单个文件。

### 2.4 Living Knowledge Workspace

Living Knowledge Workspace 保存尚在形成、仍会频繁修改的知识，例如：

- 随手捕获与未整理输入。
- 问题、假设、观察和暂定结论。
- 相互支持或冲突的观点。
- 跨来源综合、概念笔记和正在演进的方法。

它默认不要求稳定 ID、Receipt、revision 或唯一 canonical，可以由 AI 持续重组。满足以下任一条件时，AI 才将其提升为正式 Artifact candidate：

- 被多个 Context 或成果稳定复用。
- 需要交付、稳定引用或精确复现。
- 重建成本高，需要保存证据和版本。
- 涉及安全、审计、运行规则或不可逆决策。

Living Knowledge Workspace 不是无人管理的垃圾区：AI 负责归类、合并重复、提出关系和形成 synthesis；未达到发布门槛只代表“仍在生长”，不代表“无价值”。

## 三、Context OS：运行态内核

### 3.1 Context 的定义

Context 是一项跨会话工作能够被恢复、推进、验证和关闭的最小运行合同。它可以是一项项目、调研、决策、故障处理、学习计划或 Artifact 修订。一次会话内可以完成、无需交接或正式产出的工作不创建 Context，由 AI 在 Garden 或当前会话中直接处理。

Context 由 AI Organizer 自动创建、更新和关闭；用户不填写 Context Schema，也不需要判断何时立项。

Context 不是资料夹，也不是聊天全文容器。它只拥有：

- 目标、范围、非目标和完成条件
- 当前结论、下一动作、阻塞和等待项
- 工作副本及外部工作空间指针
- 任务、依赖、验收门和交接
- 决策及理由
- 事件、测试、故障和验证证据
- 消费的精确 Artifact revision
- 预期产物与最终处置

Context 不拥有：

- released Artifact 的 canonical 正文
- 长期复用知识的最终权威版本
- 大型来源集合的逐文件正文
- 真实密码、Token、私钥或 Cookie

### 3.2 Context 生命周期

试运行阶段只持久化四种状态：

~~~text
active ↔ waiting
   ↓        ↓
 done / cancelled
~~~

| 状态 | 含义 |
|---|---|
| active | 正在推进 |
| waiting | 当前不推进；具体原因由 `waiting.kind` 表达为 external、blocked 或 paused |
| done | 完成条件与必需产物处置均已满足 |
| cancelled | 明确终止，并记录原因 |

尚未承诺推进的想法留在 Living Knowledge Workspace，不创建 planned Context。只有真实样本证明 blocked、paused 或其他状态具有独立查询和自动化价值时，才把它们升级为一级 lifecycle。

### 3.3 Context 物理结构

~~~text
contexts/
└─ CTX-<ULID>-friendly-name/
   ├─ CONTEXT.md
   ├─ EVENTS/                 # optional
   ├─ DECISIONS.md            # optional
   └─ WORK.md                 # optional
~~~

- CONTEXT.md：frontmatter 保存机器合同，正文同时保存目标、当前结论、下一步、验证、阻塞和交接。
- WORK.md：任务和依赖复杂到影响主文阅读时才拆出。
- DECISIONS.md：存在多项真正改变方向的决策时才拆出。
- EVENTS：事件数量或审计需求超过主文承载能力时才创建，不默认复制完整会话。

正式 Context 最少只有一个 CONTEXT.md。拆分的依据是信息边界和阅读负担，不是固定模板完整度。

### 3.4 Context Schema

权威合同为 JSON Schema Draft 2020-12，由 AI Organizer 生成并写入 CONTEXT.md frontmatter：

~~~yaml
---
schema: context/v0.1
id: CTX-01K2G7F9A72PBWFR7Y99QZP6F5
kind: project
title: 示例项目
summary: 建立并验证一条可复现的本地工作流。
lifecycle: active
created_at: 2026-08-11T10:00:00+08:00

workspace_refs:
  - uri: file:///D:/work/example
    role: working-copy

consumes:
  - artifact://ART-01K2G7F9A6E4M4PZ6P4Q8S2H1@r2

expected_outputs:
  - id: OUT-01
    kind: report
    title: 验证报告
    required: true
    acceptance: report-v1
    candidate_ref: file:///D:/work/example/report.md

security:
  profile: personal-full/v1

automation:
  generated_by: ai-organizer
  read_profile: context-standard/v1
  write_policy: task-authorized/v1
---
~~~

必填字段：

- schema、id、kind、title、summary、lifecycle、created_at
- workspace_refs、consumes、expected_outputs
- security.profile
- automation.read_profile、automation.write_policy

约束：

- Context ID 使用 CTX- 加 26 位 ULID。
- title 为 1–160 个字符，summary 为 1–280 个字符。
- 所有时间使用带时区的 RFC3339。
- consumes 必须引用精确的 @rN；@current 不得写入可复现合同。
- expected_output ID 在当前 Context 内唯一，格式为 OUT- 加短编号或稳定键。
- 未知字段默认拒绝；实验字段只能放入 namespaced extensions。

用户不填写上述字段。AI Organizer 根据输入与现有知识自动生成语义字段；Release Authority 或确定性工具生成 ID、时间、哈希和发布相关字段。AI 无法安全判断的可选字段必须省略、降级或隔离，不能转化为用户表单。

### 3.5 当前状态合同

CONTEXT.md 的当前状态区只表达现在，不写成长日志，必须能在一分钟内回答：

1. 当前结论是什么。
2. 下一步最多三项是什么。
3. 被什么阻塞或正在等待什么。
4. 最近一次真实验证是什么时候、证据在哪里。
5. 另一个人或 Agent 接手时必须知道什么。

知识新鲜度由 verified_at、review_after 和证据表达；文件更新时间不能代替现实验证。

### 3.6 Context 完成合同

`published` 不写入 expected_output，也不在 Context 中维护第二份发布状态。它只由成功 Publication Receipt 推导。

expected_output 可以选择性保存未发布处置：

~~~yaml
resolution:
  outcome: discarded   # discarded | waived
  reason: 不再需要该输出
  decided_at: 2026-08-11T10:30:00+08:00
  decided_by: ai
~~~

- 没有 Receipt 且没有 resolution，表示仍为 pending，不需要显式字段。
- candidate 与 validated 只属于发布事务，不写成 expected_output 的长期状态。
- required output 只有在存在成功 Receipt，或存在带理由的 discarded / waived resolution 时才算已处置。
- published_outputs、to revision 和 published_at 全部从 Receipt 反向生成。
- AI 可以根据用户明确表达的放弃意图或已登记的作用域 Policy 生成 canonical resolution；其他情况下只能提出建议并进入 Needs Attention，不得擅自把 required output 标成 discarded / waived。用户修改或否决后形成 human override，后续不得被静默改回。

## 四、Artifact KB：正式产物与证据内核

### 4.1 Artifact 的定义

Artifact 是值得正式保存、稳定寻址、引用、交付或复用的对象。路径不是身份，文件类型也不是价值判断。仍在形成和频繁修改的知识留在 Living Knowledge Workspace；只有达到晋升条件后，才成为 Artifact candidate。

V0.1 使用六种 kind：

| kind | 用途 | released 的准确含义 |
|---|---|---|
| product | 报告、规格、代码 Release、数据表、演示、媒体交付物 | 满足对应输出的验收合同 |
| knowledge | 已稳定的方法、模型、模板、Runbook、操作手册、经验证结论 | 证据、适用边界和复用说明达到要求；探索性内容仍留在 Garden |
| source | 网页、PDF、原始文档及其捕获信息 | 来源身份与完整性已登记，不代表内容正确 |
| collection | 大型语料、源码快照、数据集、恢复包 | 范围、Manifest、统计和深入读取策略明确 |
| external-ref | 外部仓库、在线系统或原生工作空间指针 | canonical 位置、版本和访问方式已固定 |
| snapshot | 指定时点的冻结副本 | 捕获时点与内容完整性可验证 |

### 4.2 Artifact 身份与 URI

Artifact 使用稳定 ULID：

~~~text
ART-01K2G7F9A6E4M4PZ6P4Q8S2H1
~~~

URI：

~~~text
artifact://ART-<ULID>              # 谱系身份
artifact://ART-<ULID>@r3           # 精确 revision
artifact://ART-<ULID>@current      # 只用于交互阅读
artifact://ART-<ULID>@v1.2.0       # 可选 SemVer 别名
~~~

硬规则：

- Receipt、consumes、derived_from、构建、测试和验收必须保存 @rN。
- @current 只允许浏览和非复现查询，不能持久化到事实合同。
- @v1.2.0 在解析后必须固化为 @rN。
- canonical URI 由 id 与 revision 生成，不由用户或 AI 重复填写。
- supersedes 指向 Artifact 谱系身份；证据和派生关系指向精确 revision。

### 4.3 Revision 与 SemVer

V0.1 采用混合版本策略：

| 机制 | 用途 | 规则 |
|---|---|---|
| 整数 revision | 精确寻址、不可变历史、排序 | 所有 Artifact 强制，从 r1 连续递增 |
| release_version | 消费者兼容性 | 可选，存在时使用严格 SemVer |
| RFC3339 时间 | 发布、捕获、验证和新鲜度 | 不承担版本身份 |
| SHA-256 | 完整性、去重和污染检测 | 每个 released revision 强制 |

SemVer 只用于确实声明了兼容性合同的软件 Release、API、Schema、数据合同或稳定协议。普通文章、报告、PDF、Runbook、会议结论和来源快照默认不生成 release_version。

规则：

- released revision 永不原地修改。
- 正文或发布语义元数据改变，创建同一 Artifact 的下一 revision。
- 只重新验证且内容未变，记录 Verification Event，不增加 revision。
- 用途和 canonical_for 保持相同，沿用 Artifact ID。
- 用途、权威范围发生根本改变，或对象被拆分、合并，创建新 Artifact ID 并建立 supersedes。
- Git commit 是技术历史，不代替 Artifact revision。
- 被撤回的 current 且无替代 revision 时，@current 必须失败关闭，禁止静默回退旧版本。

### 4.4 Artifact Release Schema

只有 Release Authority 成功发布的不可变 revision 使用这份合同：

~~~yaml
schema: artifact-release/v0.1
id: ART-01K2G7F9A6E4M4PZ6P4Q8S2H1
revision: 2

kind: knowledge
subkind: runbook
title: Windows 下本地环境恢复手册
summary: 用于恢复本地开发环境并验证网络、路径和工具状态。
published_at: 2026-08-11T10:00:00+08:00

content:
  entry: README.md
  media_type: text/markdown
  digest: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

authority:
  level: canonical
  canonical_for: operations/local-environment-recovery

produced_by:
  context: context://CTX-01K2G7F9A72PBWFR7Y99QZP6F5
  expected_output: OUT-01
  publication: publication://PUB-01K2G7F9A8H24KQ90Y1ZXV8XGM

security:
  profile: personal-full/v1

provenance:
  - ref: artifact://ART-01K2G7F9B123456789ABCDEFGH@r1
    role: evidence

relations:
  - predicate: derived-from
    target: artifact://ART-01K2G7F9C123456789ABCDEFGH@r1

applicability:
  applies_to:
    - codex
    - windows
  constraints:
    - PowerShell 7

validity:
  observed_at: 2026-08-11T09:00:00+08:00
  verified_at: 2026-08-11T09:30:00+08:00
  review_after: 2026-11-11T00:00:00+08:00
  confidence: high
~~~

所有 release 必填：

- schema、id、revision、kind、subkind、title、summary、published_at
- content、authority、produced_by、security

条件必填：

- authority.level 为 canonical 时必须有 canonical_for，并使用声明单一当前答案的 Authority Profile。
- kind 为 source 或 snapshot 时必须有 capture。
- kind 为 external-ref 时必须有 external。
- kind 为 collection 时必须有 collection。
- release_version 只允许用于有真实兼容性语义的对象。

AI Organizer 默认生成 kind、subkind、title、summary、authority、relations、applicability 和 validity 等语义提案；Release Authority 生成 schema、ID、revision、published_at、digest、produced_by 和 Publication URI，并执行确定性合同。用户不填写任何字段，只在判断不合理时修改内容或纠正 AI；纠正形成 human override。

AI 不得仅凭低置信推断发布 canonical。缺少明确权威证据或 Policy 授权时，默认使用 `reference`。探索性观点可以并存，不要求制造唯一答案。

### 4.5 三层状态语义

不要使用一个 status 同时表达三种概念：

~~~text
发布事务：candidate → validated → published
                    └──────────→ rejected

Revision：released → withdrawn

Artifact 谱系：current → superseded / no-current
~~~

- candidate 与 validated 只存在于工作空间和 Release Authority 事务。
- released revision 不回到 working。
- withdrawn 由新 Receipt 表达，不修改历史 payload。
- current、superseded 和 no-current 由 Projection Compiler 根据 Receipt 与 revision 推导，不由人或 AI 填写。
- 只有启用单一当前答案 Authority Profile 的对象，才对作用域化 canonical_for 执行 current 唯一性；普通 knowledge reference 不受此约束。

### 4.6 Collection Profile

大型资料只治理一个 Collection Artifact：

~~~yaml
collection:
  manifest_entry: items.jsonl
  manifest_digest: sha256:...
  item_count: 214
  total_bytes: 2796552192
  index_policy: metadata-only
  deep_read_policy: on-demand
~~~

Manifest 按 UTF-8 POSIX 相对路径排序，记录 path、size 和 sha256；不把 mtime、盘符或绝对路径计入内容身份。内部 raw 文件只有在被独立引用、修订或复用时，才晋升为单独 Artifact。

### 4.7 外部工作空间与 Snapshot

代码仓库、视频工程、设计源文件和大型数据工程留在原生工作空间。Artifact KB 登记稳定指针：

~~~yaml
external:
  canonical_uri: https://github.com/example/repo
  local_uri: file:///D:/work/example
  branch: main
  commit_sha: 0123456789abcdef0123456789abcdef01234567
  captured_at: 2026-08-11T10:00:00+08:00
  snapshot_ref: artifact://ART-...@r1
  update_policy: release-only
~~~

外部项目版本放 upstream_version 或 commit_sha，不冒充 Artifact 的 release_version。同一对象不能让外部工作区和 Artifact KB 中的复制品同时可编辑。

## 五、Publication Bridge：统一外观与正式发布边界

### 5.1 Bridge façade 与内部责任

Publication Bridge 是对 AI 和工具暴露的统一 `kb` 外观。它不拥有正文或领域事实，而是编排两个具有不同失败语义的深模块：

- **Release Authority**：把 Garden、Context 或外部工作空间中的 candidate 发布为可以长期信任和引用的 Artifact。
- **Projection Compiler**：读取 canonical facts，构建可删除、可重建的读模型。

发布链路：

~~~text
AI semantic proposal + candidate
  → 结构与路径校验
  → 验收与证据校验
  → Shared Policy 发布门禁
  → 身份、revision 与 canonical 校验
  → 原子发布 immutable Artifact revision
  → 生成 Publication Receipt
~~~

发布提交完成后，`kb` façade 可以另行调用 Projection Compiler：

~~~text
canonical Garden / Context / Artifact / Receipt
  → Projection Compiler
  → Registry、Capsule、SQLite、Views、HOME、Search、Health
~~~

责任归属：

- Context Contract 拥有 Context lifecycle、expected output 和完成语义。
- Artifact Contract 拥有 identity、revision、authority、canonical 与 supersedes 语义。
- Shared Policy 拥有 Security Profile、发布、索引和导出规则的版本化解释。
- Release Authority 拥有锁、幂等、revision 分配、Receipt、commit 与中断恢复。
- Projection Compiler 拥有 generation、freshness、SQLite、Search、HOME 与其他读模型算法。
- `kb` façade 只拥有 CLI、JSON envelope、错误码和组件编排，不拥有 canonical facts。

Publication Bridge toolchain 不拥有：

- Context 当前态
- Living Knowledge 正文
- Artifact 正文
- 工作副本
- 真实秘密
- 用户意图或 human override 的语义
- generated 的事实所有权

AI Organizer 可以提出分类、摘要、关系、适用性和发布意图，但不能绕过 Contract、Shared Policy 或 Release Authority 自行声明发布成功。

### 5.2 实现技术

V0.1 试运行实现优先使用 Python 3.11+，当前环境可在 Python 3.14 上开发和验证；语言与具体版本不是当前冻结的不变量。

依赖边界：

- 标准库：sqlite3、argparse、pathlib、hashlib、tempfile、json、datetime、os。
- YAML：ruamel.yaml。
- Schema：jsonschema，使用 Draft 2020-12。
- CLI 使用 argparse，不引入 Typer。
- 不需要数据库服务器、Web 服务或常驻进程。
- V0.1 不打包 PyInstaller 单文件。

当前精简代码布局：

~~~text
src/
└─ kb2/
   ├─ cli.py
   ├─ core.py
   ├─ context.py
   ├─ workflow.py
   ├─ release.py
   ├─ bootstrap.py
   └─ result.py
tests/
~~~

Python 是可替换实现，不是知识合同。长期稳定候选是 URI、Receipt、发布结果 envelope 与必要 Schema；generated 布局、SQLite Schema、搜索算法和内部包结构属于可替换实现，必须在试运行后再决定是否冻结。

### 5.3 根合同与 Schema 规则

根锚点为 kb.yaml：

~~~yaml
schema: kb-root/v0.1
id: KB-01K2G7F9A5JESBZQ2D2V8FE1DG
title: 个人知识库 2.0
default_locale: zh-CN
protocol: PROTOCOL.md
capture_spool: ingress/pending/
override_store: governance/overrides/
home_lens: lenses/home.yaml
security_profiles: bridge/policies/security-profiles.yaml
generated_pointer: generated/CURRENT.json
~~~

V0.1 权威 Schema：

- kb-root/v0.1
- context/v0.1
- artifact-release/v0.1
- collection-profile/v0.1
- publication-receipt/v0.1
- human-override/v0.1

通用规则：

- YAML 按 YAML 1.2 JSON 数据模型解析。
- 拒绝重复 key、merge key 和 YAML alias。
- Schema 默认 additionalProperties: false。
- 实验字段只能进入带命名空间的 extensions。
- 内容路径必须为相对路径，不得含盘符、绝对路径或 ..。
- Release Authority 必须解析真实路径，拒绝通过 symlink、junction 或 reparse point 逃逸 release 根目录。
- JSON Schema 只校验形状；跨文件引用、状态迁移、唯一性和无环关系由对应 Contract、Release Authority 与 Projection Compiler 按所有权校验。
- Schema 是机器合同，不能成为用户界面；正常路径不得要求用户填写 YAML 或字段表单。
- AI 对可编辑事实源的语义写入必须携带 base digest；发现用户或其他 Agent 已修改时，不得使用 last-write-wins。

### 5.4 八个跨域契约

1. **capture**

   用户输入先由 Capture Writer 保存到受保护的 Durable Capture Spool，再执行确定性安全预检和 AI 分类。模型、kb toolchain 或分类失败不得造成输入丢失；Capture Spool 默认不进入 Git、普通索引或导出。

2. **organize**

   AI 根据内容、当前 Context、已有知识和 Policy 生成语义修改；对现有文件的写入必须基于 base digest，冲突时保留现状并生成可审计 patch。

3. **consume**

   Context 通过 artifact://ART-ID@rN 消费精确 revision。@current 只用于非复现阅读。

4. **expected_output**

   Context 声明准备产生什么、是否必需、验收 Profile 和候选位置。

5. **publish**

   AI 提交 candidate 与发布意图；Release Authority 校验后原子生成 Artifact revision 与 Receipt。用户不填写发布字段或命令参数。

6. **correct / override**

   用户直接修改或通过自然语言纠正 AI 判断；系统将纠正持久化为有作用域的 human override，后续 organize、build、模型升级和重新摄取不得静默覆盖。

7. **supersede**

   同一 Artifact 的正常演进增加 revision；另一个 Artifact 接管其用途或权威范围时，建立跨 ID supersedes。

8. **trace**

   任一 Artifact revision 都能追溯到 Publication Receipt、Context、expected_output、来源、证据、候选哈希以及影响该结果的 human override。

### 5.5 Publication Receipt

Receipt 是发布事务的权威提交证据。publish Receipt 与 Artifact manifest、payload 同处一个不可变 release bundle，并在 bundle 晋升时同时可见：

~~~yaml
schema: publication-receipt/v0.1
id: PUB-01K2G7F9A8H24KQ90Y1ZXV8XGM
operation: publish

from: context://CTX-01K2G7F9A72PBWFR7Y99QZP6F5
expected_output: OUT-01
candidate: file:///D:/work/example/report.md

outcome: published
to: artifact://ART-01K2G7F9A6E4M4PZ6P4Q8S2H1@r2

acceptance:
  profile: report-v1
  result: passed

evidence:
  - context://CTX-01K2G7F9A72PBWFR7Y99QZP6F5/events/EV-03

integrity:
  candidate_digest: sha256:...
  payload_digest: sha256:...
  release_digest: sha256:...

publisher_version: 0.1.0
published_at: 2026-08-11T10:00:00+08:00
~~~

约束：

- PUB- 后使用 26 位 ULID。
- 每个 released revision 恰好对应一个 successful publish Receipt。
- Receipt、Artifact manifest 和 payload digest 必须三方一致。
- release_digest 覆盖规范化 release manifest 与 payload manifest，计算时排除自身。
- rejected 只返回结构化 diagnostics，不创建 Publication Receipt，也不创建 released Artifact。
- 同一 ID 的新 revision 不写 supersedes。
- 跨 ID supersede 写入新 Artifact 的 publish Receipt。
- withdrawal 创建独立的不可变 operation bundle，放在目标 Artifact 谱系的 operations/PUB-<ULID>/ 下，不改写历史 release。

### 5.6 AI 与工具使用的 CLI 合同

用户不需要直接调用 CLI。用户只通过自然语言、文件输入和直接修改使用知识库；以下命令是 AI Organizer、自动化和诊断工具的机器接口。

试运行命令：

~~~text
kb ingest [TEXT | PATH]
kb status
kb check [PATH | --all]
kb build
kb find QUERY
kb show REF
kb trace REF
kb explain REF
kb correct REF "自然语言纠正"
kb publish --context CTX --output OUT
~~~

`kb ingest` 负责先保存输入，再交给 AI Organizer 分类。`kb correct` 接受自然语言，不要求用户指定 YAML 字段路径。Context、Artifact、Publication ID、revision、时间和 digest 均由系统生成。

高层 `kb publish` 从 Context expected_output 解析 candidate，并在锁内自行完成 preflight hash 与最终 hash 比较。底层 commit API 可以接收 candidate path 与 expected hash，但它不是用户接口。

公共参数：

~~~text
--root
--json
--dry-run
--no-color
~~~

机器输出统一使用：

~~~json
{
  "schema": "kb2-result/v0.1",
  "ok": true,
  "code": "KB2_OK",
  "message": "operation completed",
  "data": {
    "publication": {
      "committed": true,
      "receipt_ref": "publication://PUB-..."
    },
    "projection": {
      "attempted": true,
      "fresh": true,
      "build_id": "..."
    }
  },
  "diagnostics": [],
  "changed": []
}
~~~

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功；包括发布成功但派生层陈旧的可识别成功结果 |
| 2 | 输入、Schema、Policy 或验收拒绝 |
| 3 | 锁冲突、候选变化或其他可重试冲突 |
| 4 | I/O 或不可预期内部故障 |

--json 模式不得交互提问。错误必须给出稳定 code，例如：

- KB2_VALIDATION_FAILED
- KB2_POLICY_REJECTED
- KB2_EXPECTED_HASH_MISMATCH
- KB2_LOCK_CONFLICT
- KB2_PUBLISHED_INDEX_STALE
- KB2_INTERNAL_ERROR

V0.1 不提供巨型 kb sync，也不允许调用者依赖内部 SQLite 表。

普通不确定性不通过 CLI 追问用户，而是返回安全、可逆的 route，例如 `garden`、`candidate`、`reference` 或 `restricted-hold`。只有安全风险、不可逆动作或无法安全降级的意图冲突，才允许上层交互请求用户判断。

### 5.7 原子发布

发布必须遵循：

1. 在任何写入前完成 Schema、引用、安全、验收和路径预检。
2. 获取全库单写者锁。
3. 在锁内重新计算 candidate hash，并与 Release Authority 在预检阶段记录的 expected hash 比较。
4. 用 Context、Output、Candidate Hash 计算幂等键。
5. 若相同幂等键已有成功 Receipt，直接返回原结果，不重复发布。
6. 在同一卷的 .kb2/staging 下创建完整 release bundle，其中包含 artifact.yaml、publication.yaml 与 payload。
7. 计算 payload 与 release digest，并再次执行全部不变量检查。
8. 关闭写入句柄并刷新文件内容。
9. 以一次同卷目录晋升将完整 bundle 移到新的不可变 Artifact revision 路径；这是唯一 commit point。
10. 释放发布锁并返回独立 ReleaseResult。

上述十步只属于 Release Authority。Publication Bridge façade 收到 committed ReleaseResult 后，可以再调用 Projection Compiler；Release Authority 不得 import 或依赖 projection、search、sql、HOME 或其他 generated 实现。

中断恢复：

- commit 之前中断：只有 staging，没有 final bundle，视为未发布；Release check 报告并允许安全清理。
- final bundle 存在且内含有效 Receipt 与一致 digest：视为已经 committed；重试必须返回原 Receipt。
- final bundle 存在但 Receipt 缺失或 digest 错误：视为完整性故障，阻断 build，不自动删除。
- Receipt 之后 Projection build 失败：发布仍有效，façade 返回 KB2_PUBLISHED_INDEX_STALE；调用者只需重跑 kb build，不能重试 publish。
- 任何失败都不得覆盖旧 revision、旧 Receipt 或 last-good 派生层。

### 5.8 AI、事实文件与 SQLite 写入边界

“AI 不直接写 SQLite”是运行时权限，不是禁止开发 Projection Compiler 的 SQL。AI 可以在授权范围内维护 Garden、Context 和 candidate，但对既有事实文件的语义修改必须基于 base digest，并服从 human override。

日常路径：

~~~text
用户输入或直接修改
→ Capture Writer 保存原始输入，安全预检后由 AI Organizer 生成语义 patch
→ 对 Garden / Context 执行 base-digest 冲突检查
→ 对正式 candidate 调用 Release Authority
→ Projection Compiler 读取 canonical facts
→ Projection Compiler 执行参数化 SQL 并生成读模型
~~~

SQL 由 bridge/sql 中的 Schema、View、检查文件和 Projection Compiler 的参数化写入逻辑统一管理。

允许：

- AI 在任务授权范围内创建和修改 Garden、Context 与工作副本。
- AI 调用稳定 CLI，并提交可审计的语义 patch。
- 用户直接编辑 Markdown；系统在后续处理时将差异视为 human override。
- AI 通过 kb find、kb show、kb trace 和稳定 View 查询。
- 在“开发 Projection Compiler”这一明确任务中修改 SQL 源文件并运行测试。

禁止：

- AI 使用 last-write-wins 覆盖已发生变化的事实文件。
- AI、重建或模型升级静默覆盖 human override。
- 日常知识操作直接对 generated 数据库执行 INSERT、UPDATE、DELETE。
- 手工修补 SQLite 以制造当前状态。
- 把 SQLite 查询结果反向当作新的 canonical。
- 让 AI 绕过 Release Authority 修改 released revision。

### 5.9 写入权限矩阵

| 动作 | 默认权限 | 事实写入位置 |
|---|---|---|
| capture | 自动允许，必须先持久化再分类 | 受保护的 ingress/pending Capture Spool |
| organize / enrich | 允许；已有内容必须校验 base digest | Garden、Context 或 candidate |
| append_event | Context 授权范围内允许 | Context EVENTS 或 CONTEXT.md |
| update_current_state | 当前任务授权内允许；不得覆盖 human override | Context CONTEXT.md |
| human_correct | 始终允许；自动记录作用域 | 可编辑事实文件，或新 revision candidate + governance/overrides |
| propose_candidate | 允许 | 工作副本或 candidate |
| resolve_without_publish | 仅限用户明确意图或已登记的作用域 Policy；必须有理由 | Context expected_output resolution |
| publish_revision | 只能经 Release Authority | Artifact 新 revision + Receipt |
| withdraw / supersede | 经 Release Authority 并校验 | 新 operation Receipt 与 Registry 投影 |
| ingest_source | 自动捕获；发布前校验 | Garden/source/collection candidate |
| edit_registry | 禁止 | 只能由 Projection Compiler 重建 |
| write_sqlite | 禁止 | Projection Compiler 独占 |

## 六、派生读取层

### 6.1 定位

Projection Compiler 把 Living Knowledge、Context、Artifact、Receipt、human override 和策展规则编译成人与 AI 各自高效的读模型：

- Registry JSONL：最小、可审计、工具无关的对象注册表。
- Capsule：低 Token 的 L1 摘要。
- SQLite：结构化过滤、关系查询和全文搜索。
- Markdown Views：Now、Library、Timeline、Collections 等视图。
- HOME：混合策展后的根入口。
- Needs Attention：低置信、安全隔离、冲突与待复核对象。
- Recent AI Changes：最近的自动整理、依据、diff 和撤销入口。
- Health Report：断链、重复、过期、安全和构建异常。

所有派生物必须：

- 标明 generated 与 build_id。
- 共享同一 source_digest 和 config_digest。
- 可从事实源完整重建。
- 禁止直接编辑。
- 不反向覆盖 Context、Artifact 或 Receipt。
- 不反向覆盖 Living Knowledge 或 human override。
- 在 digest 不一致时明确报告 projection stale。

### 6.2 全量构建策略

试运行稳定阶段只实现全量重建，不实现增量 UPSERT、FTS trigger 或 external-content 表。最小垂直切片先生成 Registry、HOME 与基础 find；SQLite、Capsule 和完整搜索在真实样本通过后接入同一全量构建协议。

每次构建生成：

- build_id：一次构建的唯一身份。
- source_digest：按稳定 URI 和路径排序后，对 Garden、Context、Artifact、Receipt、override、Lens 及 manifest hash 计算的摘要。
- config_digest：Contracts、Projection Compiler、Shared Policy、View 和 Tokenizer 版本的摘要。

流程：

~~~text
1. 扫描 Garden、Context、Artifact、Receipt、override 与 Lens 等 canonical 输入
2. 校验 Schema、安全、路径、引用和关系
3. 计算 source_digest 与 config_digest
4. 在 generated/.staging/<build_id>/ 构建完整 generation
5. 写入 Registry、SQLite、Capsule、Views、HOME、Needs Attention、Recent AI Changes、Health
6. 执行外键、SQLite、FTS、链接和安全检查
7. 再次计算输入 digest，排除构建过程中源文件变化
8. 将 staging generation 晋升为 generated/builds/<build_id>/
9. 原子替换 generated/CURRENT.json
~~~

目录：

~~~text
generated/
├─ CURRENT.json
├─ .staging/
└─ builds/
   └─ <build_id>/
      ├─ build.json
      ├─ registry.jsonl
      ├─ kb.sqlite
      ├─ capsules/
      ├─ views/
      ├─ search/
      ├─ HOME.md
      └─ health/
~~~

根 HOME.md 由当前 generation 的 HOME 整文件替换，并带相同 build_id；CURRENT.json 是 generation 的权威指针。若根 HOME 与 CURRENT 的 build_id 不一致，AI 回退读取 CURRENT 指向的 HOME。generated 默认只保留当前与上一个成功 generation；它们都能重建，不形成长期历史。失败构建不能改变 CURRENT 或 last-good。

全量重建永远保留为恢复 oracle。只有满足任一真实条件，才另立 Context 评估增量：

- 受治理对象超过 2,000。
- 全量构建 p95 连续超过 5 秒。
- 正文 FTS 构建已经明显影响正常使用。

Schema、Tokenizer、安全策略或 canonical 规则改变时，即使未来支持增量，也必须强制全量重建。

### 6.3 两种 stale

必须分开：

1. **Projection freshness**

   表示 Registry、SQLite、HOME 等是否与当前事实源和构建配置一致，由 build_id、source_digest、config_digest 和 built_at 判断。

2. **Knowledge validity**

   表示现实知识是否超过 review_after，或已被 withdrawn、superseded，由 observed_at、verified_at、review_after 和 confidence 判断。

projection stale 时，旧索引只能作为候选路由，不能证明当前状态。knowledge stale 时，可以定位对象，但回答必须提示复核。

### 6.4 SQLite Schema

SQLite 是 Agent 查询加速器，不是知识本体。V0.1 建立：

| 表 | 职责 |
|---|---|
| builds | 构建身份、digest、Schema 与 Tokenizer 版本 |
| projection_state | 当前 generation 与 freshness |
| entities | 所有可寻址对象的统一 URI 基表 |
| garden_items | 活知识路径、整理状态、置信度和来源 |
| contexts | Context 生命周期、摘要、入口与验证时点 |
| expected_outputs | 预期产物及最终处置 |
| artifacts | kind、revision、authority、validity、current 推导 |
| collections | 集合范围、统计与深入读取策略 |
| publications | Receipt、候选、验收、证据与发布结果 |
| relations | 类型化对象关系，统一引用 entities.uri |
| aliases | AI 生成、用户纠正与规范化别名 |
| candidates | pending output 的候选与最近校验投影 |
| overrides | human override 的目标、作用域与生效关系 |
| automation_changes | AI 语义修改的 actor、reason、base digest 与 diff 引用 |
| documents_fts | 安全允许的派生检索词 |

连接必须显式启用 PRAGMA foreign_keys = ON；Schema 版本使用 PRAGMA user_version。

稳定 View：

- active_contexts
- current_artifacts
- agent_catalog
- living_knowledge
- needs_attention
- recent_ai_changes
- stale_items，仅表示 knowledge validity
- publishable_candidates
- security_allowed_documents
- build_status，仅表示 projection freshness

FTS 使用普通 contentful 表，不使用 external-content、contentless 表或同步 trigger：

~~~sql
CREATE VIRTUAL TABLE documents_fts USING fts5(
  entity_uri UNINDEXED,
  title_terms,
  summary_terms,
  alias_terms,
  body_terms,
  build_id UNINDEXED,
  tokenize = 'unicode61'
);
~~~

FTS 只存派生检索词。命中后通过 canonical_path 打开事实正文。

### 6.5 SQLite 构建验收

- 删除整个 generated 后，一次 build 可以恢复全部读模型。
- 相同 source/config 输入产生逻辑等价结果；build_id 与 generated_at 可以不同。
- PRAGMA integrity_check 返回 ok。
- PRAGMA foreign_key_check 无结果。
- FTS5 integrity-check 通过。
- 所有当前派生行属于同一个 build_id。
- 删除、改名、withdraw 或 supersede 后不残留旧 current。
- source/config digest 不一致时，freshness 必须失败。
- index policy 为 none 的对象不能出现在 Registry 正文、Capsule、FTS 或普通搜索结果。
- 失败构建不能改变 last-good。

### 6.6 中文检索

试运行暂用：

> AI 维护且受 human override 约束的 aliases + 相邻汉字二元组 + FTS5 unicode61。

Projection Compiler 保留原始中文，并为索引生成空格分隔的二元组：

~~~text
知识库架构
→ 知识 识库 库架 架构
~~~

查询端调用同一个纯函数：

~~~text
知识库
→ "知识 识库"
~~~

Tokenizer Profile：

~~~yaml
tokenizer_version: zh-bigram-v1
min_han_query_length: 2
body_index_policy: full-only
collection_default: metadata-only
ranking_profile: search-rank-v1
~~~

检索顺序：

1. ID 与 URI 精确匹配。
2. 标题精确匹配。
3. alias 精确匹配。
4. title、summary、alias、body 的 FTS 相关度。
5. current、released、canonical 与 freshness 加权。
6. 打开 canonical 文件确认正文。

规则：

- 索引端和查询端必须使用同一个 tokenizer_version。
- metadata-only 只索引允许公开的标题、摘要和 alias。
- Collection 内部 raw 文件默认不进入全库 FTS。
- 单汉字全文查询拒绝或要求补充条件，禁止退化为无界扫描。
- Tokenizer 变化进入 config_digest，并强制全量重建。

V0.1 不使用 trigram、jieba、ICU、拼音、首字母、第二套全文索引或向量召回。

中文检索验收：

- 至少建立 40 条真实查询金集。
- 精确 ID、标题与 alias 的目标 Top-1 命中率为 100%。
- 综合查询的已知目标 Top-5 命中率至少 90%。
- 综合查询 Top-1 命中率至少 70%。
- 版本、部署、阻塞、决策、故障等二字查询可以命中已知对象。
- index none 和 restricted 正文不得产生 token 泄漏。

若 Top-5 不达标，停止堆叠 alias，另立 Context 评估 jieba 与私人词典；不改变 CLI、SQLite View 或 Agent 读取合同。

### 6.7 HOME

HOME 采用“AI 维护默认入口，人的纠正形成持久约束，Projection Compiler 决定当前显示值”。用户不填写 Lens 表单，可以通过自然语言或直接修改 Lens 来 pin、hide、rename、reorder。

策展事实源只有：

~~~text
lenses/home.yaml
lenses/home-intro.md
~~~

home.yaml 由 AI 默认维护，只允许：

- 固定 pins。
- 排列章节顺序。
- 引用稳定 View。
- 设置每节显示条数。
- 引用 Reading Path。

它不得包含任意 SQL、Context 当前状态副本或 Artifact 正文。

示例：

~~~yaml
version: 1
intro: home-intro.md

sections:
  - kind: view
    title: Now
    source: active_contexts
    limit: 8

  - kind: pins
    title: Start Here
    refs:
      - context://CTX-01K2G7F9A72PBWFR7Y99QZP6F5
      - artifact://ART-01K2G7F9A6E4M4PZ6P4Q8S2H1@current

  - kind: view
    title: Recent Releases
    source: current_artifacts
    limit: 8
~~~

Projection Compiler 整文件生成 HOME.md，顶部必须包含：

~~~yaml
generated: true
build_id: ...
source_digest: ...
generated_at: ...
do_not_edit: true
~~~

禁止在 HOME 内设置“人工区 + generated 区”。用户直接修改 Lens 或 home-intro.md 后形成 human override；后续 AI 策展和 build 不得静默覆盖。

HOME 最多六个主要内容区、约 150 行：

1. 系统与构建状态。
2. Now。
3. 阻塞与下一动作。
4. 最近发布。
5. AI 默认策展与用户固定入口。
6. Collections、Reading Paths 与 PROTOCOL。

明细下沉到 generated/views。Projection Compiler 暂时不可用时，last-good HOME 仍可阅读，并通过 build_id 与 generated_at 明确时点；PROTOCOL 和 canonical 文件始终可以独立恢复系统。

### 6.8 Registry 与 Capsule

Registry JSONL 至少输出：

- uri、entity_type、title、summary
- canonical_path
- lifecycle 或 revision
- authority、security、freshness
- curation_state、confidence、human_override
- read_profile
- source_hash、build_id

Capsule 是 L1 摘要，只包含路由、当前态、权威、新鲜度、关键关系和深入读取条件，不复制长正文或 Evidence。

## 七、AI-first 零表单读写协议

### 7.1 人的入口

人默认只使用：

- 自然语言对话、文件投递和其他原始输入。
- HOME、Now、Living Knowledge、Library、Collections 与 Reading Paths 等阅读入口。
- 对不合理结果的直接修改、自然语言纠正、隐藏、固定或撤销。

人不需要理解目录、Schema、ID、URI、revision、Receipt、candidate hash、Security Profile 或索引策略，也不需要直接调用 CLI。Reading Path 和 HOME Lens 可以由 AI 自动生成；用户的 pin、hide、rename、reorder 和正文修改形成持久约束。

### 7.2 AI 自动摄取与整理流程

~~~text
用户自然表达或放入资料
→ 先保存到受保护的 Durable Capture Spool
→ 确定性安全预检；疑似秘密转为 Vault / 隔离引用
→ AI Organizer 推断 Garden / Context / candidate / Collection 归属
→ 生成标题、摘要、关系、权威、安全与适用性提案
→ 对已有事实执行 base-digest 冲突检查并应用 human override
→ 写入可逆事实源，或调用 Release Authority 正式发布
→ Projection Compiler 更新 Registry、HOME 与 Search
→ 用户只在不合理时直接纠正
~~~

自动处理结果只允许：

- captured：输入已安全保存，尚未归类。
- garden-organized：已进入活知识工作台。
- context-created / context-updated：已进入运行态。
- candidate-created：已形成可逆候选，尚未发布。
- published：已生成不可变 revision 与 Receipt。
- restricted-hold：安全不确定，正文隔离且不索引、不导出。
- needs-review：存在歧义，但不阻断其他工作。

普通不确定性不弹出字段表单。系统选择最可逆、最低权威、最小暴露的结果，并在 Needs Attention 中解释原因。

### 7.3 自动发布门

AI 可以自动完成元数据和发布，但至少同时满足：

1. 存在用户明确的保存或发布意图、Context 预授权 expected_output，或有作用域的自动发布 Policy。
2. 原始输入、candidate 和来源已经持久化。
3. Schema、路径、验收、digest 与安全检查全部通过。
4. 不存在 human override 冲突。
5. 不存在 unresolved duplicate 或 canonical_for 冲突。
6. 当前 Agent 的 write_policy 允许该类发布。

低置信度只能降低自动化程度，不能触发删除、合并、withdraw、supersede、canonical 接管或扩大安全暴露。source、snapshot 和外部抓取内容不得因 AI 推断自动获得 canonical authority。

### 7.4 解释、纠正与覆盖保护

`kb explain REF` 必须能说明：原始意图来自哪里、AI 提出了什么、哪些证据与 Policy 生效、是否命中 human override、为什么进入当前 route / authority / security，以及如何撤销。

`kb correct REF "自然语言纠正"` 把人的纠正编译成事实修改和有作用域的 override；用户不填写字段路径。决策优先级固定为：

~~~text
硬安全不变量
→ 当前用户的明确纠正
→ 对象级 human override
→ 有明确作用域的整理规则
→ Shared Policy
→ AI proposal
→ 安全可逆默认值
~~~

人工修改具有写入优先级，但不自动证明内容正确或获得 canonical authority。模型升级、重新摄取、organize 和 build 都不得覆盖 human override；替代 override 必须保留历史和理由。

若纠正对象是 released Artifact，`kb correct` 只能生成下一 revision candidate，不能原地改写已发布 bundle。

### 7.5 AI 分层读取

| 层级 | 默认读取 | 停止条件 |
|---|---|---|
| L0 Bootstrap | PROTOCOL、build_status、agent_catalog | 已定位少量候选对象 |
| L1 Capsule | 摘要、状态、权威、新鲜度、关系、override、read_profile | 已足够回答或执行 |
| L2 Canonical Body | Garden note、Context CONTEXT.md、Artifact 正文、明确工作文件 | 任务所需事实已齐 |
| L3 Evidence | 原始日志、附件、会话、源码、Collection 内部文件 | 只在求证、复现或审计时进入 |

读取硬规则：

- 不默认扫描未被 Registry、Garden、Context 或 Manifest 链接的目录。
- 不默认读取 done/cancelled Context 的完整 EVENTS。
- 不默认读取历史、withdrawn 或 superseded revision。
- 不默认读取 Collection 内部文件。
- 不读取 Shared Policy 禁止的正文。
- L1 足够时停止下钻；综合与审计任务可以按证据需要扩大候选数，1–3 个只是默认值，不是硬上限。
- projection stale 时先打开 canonical 文件，不把旧 View 声明为 current。

### 7.6 AI 查询流程

~~~text
1. kb status 检查 projection freshness
2. kb find 或 agent_catalog 定位候选
3. 过滤 security、publication、authority、validity 与 override
4. 按 retrieval / synthesis / audit 任务类型选择候选规模
5. 必要时 kb show 打开 canonical body
6. 求证时才 kb trace 到 Receipt、Context 和 Evidence
~~~

SQLite 负责路由，不负责知识权威。最终回答和引用必须落到事实文件与精确 revision；Living Knowledge 必须明确标注其可变、未发布和置信状态。

### 7.7 AI 写入流程

~~~text
运行态或活知识变化
→ 生成带 base digest 的语义 patch
→ 写 Garden / Context / 工作副本

形成正式候选
→ kb check
→ Release Authority publish
→ immutable Artifact revision + Receipt

读取投影更新
→ Projection Compiler build
→ 新 generation + CURRENT
~~~

任何需要直接修 SQLite、Registry 或 HOME 才能完成的知识操作，都说明边界已经被绕过，应停止并修正事实源、Release Authority 或 Projection Compiler 的具体责任。

## 八、安全与信任边界

### 8.1 Secret 规则

真实密码、Token、私钥、Cookie、代理认证和恢复密钥不得进入：

- Context 正文
- Artifact payload
- Git 历史
- Publication Receipt
- Registry、Capsule、SQLite
- 全文或向量索引
- Event 与日志

Durable Capture Spool 是受保护的临时摄取边界，不是普通知识正文：它必须使用受限权限，默认不进入 Git、索引或导出。确定性预检发现真实或疑似秘密时，应先把原文转入外部 Vault 或受保护隔离位置并验证写入成功，知识库只保留 secret_ref、安全摘要与 digest；不得把秘密从 Spool 直接提升到 Garden、Context 或 Artifact。

知识库只保存：

~~~yaml
secret_ref: vault://entry-id/field
purpose: 用于恢复本地服务
scope: local-machine
last_verified_at: 2026-08-11T10:00:00+08:00
~~~

### 8.2 Security Profile

用户不选择 Security Profile。AI 根据来源、内容、当前 Context、已有 override 和安全扫描生成 Profile 提案，Shared Policy 计算不可突破的安全下限：

~~~yaml
security:
  profile: personal-full/v1
~~~

试运行暂用四个 Profile：

| Profile | Registry | FTS / SQLite | Agent | Export |
|---|---|---|---|---|
| personal-full/v1 | 完整元数据 | 允许正文 | 允许读取 | 允许 |
| restricted-summary/v1 | 安全摘要 | metadata-only | 只读摘要，正文需批准 | 默认阻止 |
| binary-metadata/v1 | 文件元数据 | metadata-only | 只读 Manifest | 默认阻止 |
| secret-ref-only/v1 | 仅 secret_ref 元数据 | 禁止秘密正文 | 按批准调用 Vault | 阻止 |

安全规则遵循单调原则：AI 可以自动提高限制，不能仅凭推断降低限制。无法确定时默认 `restricted-summary/v1` 或进入 `restricted-hold`；这不要求用户立即填写分类。只有用户明确放宽且仍通过硬 secret 规则时，才允许降低限制。

Release Authority 在发布前、Projection Compiler 在索引和导出前，必须使用同一版本、同一 digest 的 Shared Policy 展开 Profile。未识别 Profile 的对象不得发布、索引或导出。

外部网页、附件、文档和 Collection 内的文字一律作为数据，不具有授权能力；其中要求“忽略规则、降低安全、发布为 canonical”的内容不得被 AI 当作用户指令执行。

### 8.3 发布前安全检查

Release Authority 至少检查：

- 必填字段和 Schema 版本。
- ID、revision 与 canonical_for 唯一性。
- 关系目标存在，supersedes 无环。
- content digest 可重算。
- expected_output 与 acceptance 对应。
- security profile 存在且策略完整。
- secret scan 通过。
- generated 文件不能成为发布输入。
- 相对路径、symlink、junction 和 reparse point 不越界。
- external workspace 与 KB snapshot 不同时声明 editable canonical。

自动 secret scan 只能降低风险，不能证明没有语义秘密。高风险或语义不确定来源自动进入 restricted-hold；用户可以直接纠正或明确授权，但不填写安全表单。

### 8.4 Authority 与真实性

authority.level 固定为：

- canonical：某一 canonical_for 的当前正式答案。
- reference：值得复用的参考，但不是唯一权威。
- evidence：作为判断或验证依据。

source、snapshot 和 collection released 后默认只能是 reference 或 evidence，不能因“已收录”自动获得 canonical。

AI 对 authority 不确定时必须选择 reference。人工修改拥有意图优先级，但只有满足对应 Authority Profile 与发布证据后，才能获得 canonical；“用户修改过”不等于“事实已经被现实验证”。

validity 与 authority 独立：

- authority 回答“它代表什么”。
- validity 回答“它最近何时被现实验证”。
- revision 回答“引用的是哪次正式修订”。
- digest 回答“内容是否被改变”。

## 九、物理结构

V0.1 目录：

~~~text
知识库2.0/
├─ kb.yaml
├─ docs/
│  ├─ architecture.md
│  ├─ evidence/                         # 本地验收记录，不入 Git
│  └─ internal/                         # 本地推进文档，不入 Git
├─ PROTOCOL.md
├─ HOME.md                              # generated
│
├─ ingress/
│  ├─ pending/                          # Durable Capture Spool；默认不索引、不导出、不入 Git
│  └─ restricted-hold/                  # 只保存安全摘要、digest 或外部 Vault / 隔离引用
│
├─ garden/
│  ├─ notes/                            # 可持续修改的活知识
│  └─ syntheses/                        # AI 维护的跨来源综合
│
├─ contexts/
│  └─ CTX-<ULID>-friendly-name/
│     ├─ CONTEXT.md
│     ├─ WORK.md                         # optional
│     ├─ DECISIONS.md                    # optional
│     └─ EVENTS/                         # optional
│
├─ artifacts/
│  └─ <kind-dir>/                       # products | knowledge | sources | collections | external-refs | snapshots
│     └─ ART-<ULID>-friendly-name/
│        ├─ r000001/
│        │  ├─ artifact.yaml
│        │  ├─ publication.yaml
│        │  └─ payload/
│        └─ operations/
│           └─ PUB-<ULID>/
│              └─ publication.yaml
│
├─ governance/
│  └─ overrides/
│     └─ OVR-<ULID>.yaml
│
├─ src/
│  └─ kb2/
│     ├─ core.py
│     ├─ context.py
│     ├─ workflow.py
│     ├─ release.py
│     ├─ bootstrap.py
│     ├─ cli.py
│     └─ result.py
├─ tests/
│
├─ lenses/
│  ├─ home.yaml
│  ├─ home-intro.md
│  ├─ reading-paths/
│  └─ saved-queries/
│
├─ .kb2/
│  ├─ lock
│  └─ staging/
│
└─ generated/
   ├─ CURRENT.json
   ├─ .staging/
   └─ builds/
      └─ <build_id>/
         ├─ build.json
         ├─ registry.jsonl
         ├─ kb.sqlite
         ├─ capsules/
         ├─ views/
         ├─ search/
         ├─ HOME.md
         ├─ needs-attention/
         ├─ recent-ai-changes/
         └─ health/
~~~

结构说明：

- artifact.yaml、publication.yaml 与 payload 同处一个不可变 release bundle，获得单一提交边界。
- publication.yaml 的语义所有者是 Release Authority；物理共置不改变所有权。
- Publication Registry 是从 bundle 内 Receipt 生成的 View，不维护第二份权威 Receipt。
- ingress 是受保护的持久摄取队列，不是正式知识；pending 在完成安全检查和路由前不得进入 Git、普通索引或导出，restricted-hold 只保留安全摘要、digest 或外部安全位置引用。
- garden 是通过安全预检后的可编辑事实源，不是 generated。
- governance/overrides 保存用户纠正的持久事实；AI、build 和模型升级不得覆盖或忽略。
- .kb2 只保存 Release Authority 的锁与未提交事务，可在恢复检查后清理。
- generated 整体可删除重建，不能成为 canonical 输入。
- HOME.md 是当前 generation 的根入口，禁止手工编辑。
- PROTOCOL.md 是 AI 默认维护、用户可直接修订的最小启动合同和自动化故障回退入口；用户修订形成 human override。
- lenses 由 AI 默认维护策展规则和阅读顺序，不复制 Artifact 正文；用户修改具有持久优先级。
- 原始附件隶属于 Artifact 或 Collection，不建立无归属的大附件池。

### 9.1 文件事实与目录投影的边界

承载 Garden、Context、Artifact、Receipt、Override 和 Lens 的物理目录是事实容器，不是投影。只有下列内容可重建：

- HOME 与 Markdown Views
- Registry 与 Capsule
- SQLite 与 FTS
- Publication、Timeline、Library 等目录视图
- Health Report 和关系图

因此“目录可重建”只适用于导航目录页和生成视图，不适用于 canonical 文件目录本身。

## 十、方案选择与 Why not

### 10.1 实施形态比较

| 方案 | 边界清晰度 | Agent 可用性 | 实施成本 | 长期风险 | 结论 |
|---|---:|---:|---:|---:|---|
| A. 纯文件 + 人工整理与发布 | 中 | 低 | 低 | 人仍要理解目录、字段和同步规则 | 只可做极短验证 |
| B. AI-first 文件事实源 + kb façade + Release Authority + Projection Compiler | 高 | 高 | 中 | 需要保护 human override 并维护稳定机器合同 | **V0.1 试运行采用** |
| C. SQLite/平台作为知识本体 | 低 | 高 | 中至高 | 人类可读主权、迁移和降级能力下降 | 不采用 |

选择 B 的原因不是“SQLite 更先进”，而是它把复杂度放在正确位置：

- 文件承担长期事实。
- AI Organizer 承担默认整理和语义提案，用户零表单。
- Release Authority 承担正式发布与事务一致性。
- Projection Compiler 承担可抛弃的读模型构建。
- SQLite 承担可抛弃的查询加速。
- 人只面对自然输入、阅读结果和直接纠正；AI 只面对小而稳定的机器入口。

### 10.2 为什么不采用其他语言作为首版

- 不默认 Node：当前任务的核心是文件、Schema、哈希、SQLite 与恢复协议，Python 的标准库边界更小。
- 不默认 Go/Rust：单文件分发是优点，但当前更需要快速形成并验证合同；重写语言不能改善错误的发布语义。
- 不用 PowerShell 承担长期 kb toolchain：适合运维脚本，不适合成为 Schema、事务、幂等和跨版本恢复的主实现。
- 不立即打包 EXE：源码和固定环境更容易检查；分发问题出现后再决定 onedir 或其他语言。

触发语言重评的条件只有：

- 必须在没有 Python 的多台机器运行。
- 单文件分发成为硬验收条件。
- Python 构建或查询在真实规模下无法满足预算。

### 10.3 其他 Why not

- 不把全库做成一本书：书是 Reading Path，不适合承担运行态、版本、集合和跨域复用。
- 不合并 Context 与 Artifact：working、current、released 和历史会失去清晰边界。
- 不完全隔离两核：没有 Receipt 就无法证明产物从何而来、如何验收。
- 不把 Living Knowledge Workspace 立刻升级成第三个正式内核：它先承担可编辑工作面，不另造 ID、Receipt 和发布体系；真实样本证明需要独立生命周期后再重评。
- 不让 AI 直接绕过 Contract 与 Release Authority：模型擅长语义提案，不适合独占哈希、锁、权限、revision 和原子提交。
- 不拆分发布 commit point：Artifact manifest、Receipt 与 payload 仍必须由 Release Authority 在同一 bundle 中提交。
- 不把 Release Authority 与 Projection Compiler 强行视为同一失败域：build 可以失败而发布仍有效；二者保留同一项目和 CLI，不做微服务化。
- 不把 SQLite 作为第三核：它不可人工审阅，且必须允许删除重建。
- 不用搜索替代架构：召回能力不能决定权威、版本、安全和写入责任。
- 不把事件日志当当前态：每次重放历史才能知道现在，会扩大 Agent 上下文。
- 不给所有 raw 文件逐个建对象：Collection Manifest 承担集合级治理。
- 不要求用户手工策展 HOME：AI 生成默认入口，用户只在不合理时纠正。
- 不允许 HOME 自动化覆盖人的 pin、hide、rename 或 reorder：这些修改是持久 human override。
- 不在 HOME 内混写人工与 generated 块：人的策展意图进入 Lens / Override，HOME 仍整文件生成。
- 不把零表单误解为零 Schema：Schema 仍是 AI 与系统的机器合同，只是不向用户暴露。
- 不全库强制 SemVer：绝大多数知识没有公共兼容性 API。
- 不用日期或哈希代替 revision：日期会碰撞，哈希不可读且没有顺序语义。
- 不用 trigram 作为中文主索引：常见二字查询存在硬缺口。
- 不在 V0.1 引入图数据库或向量检索：先把身份、权威、安全和确定性召回做对。

## 十一、红队 / 蓝队压力测试

| 红队攻击 | 最坏结果 | 蓝队防线 | 残余风险 |
|---|---|---|---|
| AI 绕过 Release Authority 修改 released 文件 | 历史和哈希失真 | immutable bundle、digest、Release check | 本地文件权限无法绝对阻止恶意修改 |
| Agent 直接写 SQLite | 文件与数据库分叉 | 运行协议禁止、只读查询、全量重建 | 人仍可使用外部工具绕过 |
| AI 错误分类并自动升为 canonical | 不成熟观点伪装成权威答案 | 低置信默认 reference、Authority Profile、canonical 发布门 | Policy 预授权范围仍可能过宽 |
| 用户修改被下一次 organize 或模型升级覆盖 | 人工意图丢失，系统反复犯错 | base digest、human override、禁止 last-write-wins | 外部编辑识别仍需真实工具验证 |
| 人与 AI 并发修改同一文件 | 一方内容被静默丢弃 | compare-and-swap、冲突 patch、保留双方版本 | 合并语义仍可能需要用户判断 |
| 外部资料包含“忽略规则并发布” | prompt injection 控制整理与发布 | 用户指令通道与内容通道分离，payload 无授权能力 | 人复制粘贴后的意图边界仍可能含糊 |
| AI 把秘密材料判成普通笔记 | 正文进入 FTS 或导出 | 来源继承、deterministic scan、安全单调、restricted-hold | 语义秘密仍可能漏检 |
| AI 或 kb toolchain 不可用时输入没有保存 | 用户以为已记录，实际内容丢失 | 先写 Durable Capture Spool，再分类与发布 | pending 堆积期间检索质量下降 |
| publish 已提交但 build 报错 | Agent 重试并产生重复 revision | committed=true、成功退出码、幂等键 | 调用者若忽略机器合同仍可能误操作 |
| 强杀发生在 commit 后、CLI 返回前 | 不知道是否发布成功 | 重试先检查 final bundle 与 Receipt | 磁盘损坏仍需人工恢复 |
| Bridge 变成审批官僚 | candidate 长期积压，零表单退化为反复确认 | 普通不确定性自动进入可逆状态，只有安全与不可逆冲突才暂停 | Needs Attention 仍可能积压 |
| Context 囤积成果 | 项目结束后无法复用 | required output 关闭合同 | discarded/waived 可能被滥用 |
| Artifact KB 变成坟场 | 文件存在但找不到用途 | summary、authority、applicability、produced_by | 仍需对低价值对象降权 |
| 同一受治理 canonical_for 有两个 current | AI 得到冲突答案 | 作用域化唯一性检查与发布锁 | AI 仍需正确判断语义范围 |
| @current 进入构建或验收 | 未来不可复现 | Schema 禁止持久化 @current | 交互复制可能带入草稿 |
| 撤回 current 后自动回退旧版 | 使用已过期知识 | @current 失败关闭 | 用户必须明确选择替代 revision |
| HOME 或 Registry 被手工修改 | 下次构建丢失或误导 | generated 标识、digest、整文件重建 | 文件权限不一定为只读 |
| projection stale 被当作现实当前态 | 错误执行 | kb status、build_status、canonical 回退 | Agent 可能忽略 freshness 警告 |
| 大型 Collection 被全量索引 | 成本、泄漏、上下文污染 | metadata-only、Manifest、按需深读 | 深读仍需资源预算 |
| 二元组产生误召回 | Agent 找错知识 | 精确匹配优先、金集、正文确认 | 真实语料可能需要词法分词 |
| 真实秘密进入正文 | Git 和索引长期泄漏 | Vault 引用、发布前扫描、安全 Profile | 语义秘密无法全部自动识别 |
| Garden 变成无人理解的自动垃圾堆 | 内容很多但没有综合与复用 | AI 去重、关系建议、synthesis、按真实复用晋升 | 低价值捕获仍会积累 |
| 同类纠错反复出现 | 用户持续给 AI 擦屁股 | override、纠错规则与回归样本 | 规则作用域过宽可能误伤其他内容 |
| Schema 越来越重 | 机器合同和 AI 判断成本超过产出 | 只治理正式对象、可选字段、extensions 与误拒绝止损线 | 类型边界仍需真实样本校准 |

## 十二、实施顺序

### 阶段 0：冻结原则，不冻结实现

- 本文作为 V0.1 试运行架构。
- 只冻结文件事实源、Context / Artifact 所有权、released 不可变、单一发布 commit point、用户零表单和 human override 不可静默覆盖。
- 目录、Schema 细节、状态数量、CLI、检索算法和读模型规模都是待验证假设。
- 在真实样本跑通前，不迁移旧知识库。

### 阶段 1：零表单捕获与纠错闭环

- 创建最小 kb.yaml、PROTOCOL.md、受保护 ingress/pending、garden 与单文件 CONTEXT.md 约定。
- 实现或模拟“输入先保存，再由 AI 分类”的摄取流程。
- AI 自动生成标题、摘要、关系、路由与安全提案，用户填写结构化字段数为 0。
- 实现 base digest、冲突检测、human override、diff、explain 与 correct；用户修改经过至少三次 organize / build / re-ingest 后仍保持。
- 只生成 Registry JSONL、最小 HOME 和基础 find，不先建设完整 SQLite、Capsule 与中文检索。

### 阶段 2：最小 Release Authority

- 使用 Python 3.11+ 建立统一 kb façade 与独立 release / projection / policy 边界。
- 创建最小 Context、Artifact、Collection、Receipt 与 Override Schema；它们都是机器合同。
- 实现 check、show、trace 与 publish。
- 实现 publish 的单写者锁、同卷 staging、幂等、bundle commit 与 Receipt。
- 加入强杀、候选变化、重复请求、磁盘失败和“publish 成功但 projection 失败”的故障注入测试。
- 增加架构测试，禁止 Release Authority 依赖 projection、search、sql 或 HOME。

### 阶段 3：用五个真实样本证伪架构

1. 一个会产生明确交付物的项目。
2. 一个要求现场验证的运维或故障 Context。
3. 一个需要多次 revision 的稳定 Runbook。
4. 一个大型来源 Collection。
5. 一个包含相互冲突观点、会持续演化的长期研究主题。

每个样本按需要跑通：

~~~text
自然输入
→ 原始内容安全保存
→ AI 自动归类和整理
→ Garden 或 Context
→ candidate
→ Release Authority 发布 + Receipt
→ 最小投影、搜索和 trace
→ 用户纠正
→ 再次整理仍保留 human override
~~~

至少连续使用 2–4 周，记录自动处理率、人工纠正率、绕过率、恢复时间、复用情况和错误 canonical / 安全暴露次数。样本的目标是推翻错误假设，不是证明实现符合本文。

### 阶段 4：冻结公共合同

- 根据真实样本修订并冻结 URI、Receipt、发布结果 envelope 和必要 Schema。
- 只有样本证明具有独立查询价值时，才冻结 lifecycle 细分、Authority Profile、validity、applicability 等语义。
- 对自动归类、human override、低置信路由与安全 Policy 建立纠错回归集。

### 阶段 5：补齐 Projection Compiler

- 实现全量 kb build、CURRENT 与 last-good generation。
- 生成 Registry、Capsule、Views、Needs Attention、Recent AI Changes 和 Health Report。
- 真实查询需要确认后，再生成 SQLite、稳定 View、kb find 与 zh-bigram-v1。
- generated 默认只保留 current 与 previous；失败构建不能改变 last-good。

### 阶段 6：向前运行

- 新输入默认由 AI 自动归类到 Garden、Context、candidate 或受保护来源位置。
- 新成果统一通过 Release Authority 发布，用户不填写发布字段。
- 首批只晋升 20–30 个高价值对象。
- 旧知识只有在被真实调用、修订或复用时才晋升。
- 大型历史资料只创建 Collection Manifest。
- 达到验收指标后，HOME 和 PROTOCOL 才成为默认入口。
- 旧知识库最终可以转为只读 Legacy Collection；删除旧库必须另行明确授权。

## 十三、V0.1 Cut List

V0.1 明确不实现：

- 旧知识库全量迁移。
- 把 LLM 嵌入 Release Authority 或 Projection Compiler；AI Organizer 通过提案与机器合同协作。
- 要求用户填写 YAML、ID、URI、hash、Schema、Security Profile 或发布表单。
- 把 Living Knowledge Workspace 立即扩成第三套 ID、Receipt 与发布体系。
- 在真实样本前实现完整 SQLite、Capsule、Timeline、关系图和所有阅读视图。
- SQLite 增量更新。
- FTS external-content 与同步 trigger。
- 图数据库和向量数据库。
- Web UI、GUI、daemon、watcher。
- 插件与 hook 框架。
- 并发发布和细粒度锁。
- 自动 SemVer 判断或分配。
- jieba、ICU、拼音、trigram 第二索引。
- Collection raw 文件全库全文索引。
- Artifact 自动删除、内容 GC 和内容寻址去重存储；generated 仍按 current + previous 自动轮换。
- PyInstaller onefile。
- 把 Context OS 扩成完整项目管理软件。
- Agent 任意 SQL 和 generated 写入接口。
- AI 仅凭低置信推断执行 canonical、withdraw、supersede、删除、自动合并或降低安全限制。
- AI 从单次含糊修改自动推广全库整理规则。
- 自动修复秘密信息；只能检测并阻断。

这些功能只有被真实指标触发时，才通过新 Context 进入设计，不提前预留复杂插件体系。

## 十四、首版验收

### 14.1 用户与 AI

- 普通捕获、Context 创建、整理和发布中，用户填写的结构化字段中位数与 p95 均为 0。
- 用户只需提供自然内容或意图；AI 自动决定 Garden、Context、candidate、Artifact 或 restricted-hold 路由。
- 100% 原始输入在分类前先持久化；AI、Release Authority 或 Projection Compiler 故障不造成输入丢失。
- 自动整理成功不要求确认弹窗；需要即时人工判断的输入低于 5%，其余歧义自动进入安全可逆状态。
- 用户直接修改经过至少三次 organize、build、re-ingest 或模型升级后仍保持，human override 保持率为 100%。
- 每个 AI 语义修改都能解释来源、理由、Policy、override、diff 与撤销方式。
- 人能从 HOME 在一分钟内进入正在发生的工作、活知识或稳定产物。
- 新 AI 最多读取 PROTOCOL 与一个 Context CONTEXT.md，即可说明 why、now、next。
- AI 默认从少量 Capsule 开始，不扫描全库；综合与审计任务按证据需要扩展候选数。
- 普通任务不读取全局历史 LOG；大型 Collection 默认只读取 Manifest。

### 14.2 Context 与 Artifact

- 每个 active Context 只有一个权威 working-copy 指针。
- 每个 done Context 的 required output，其由 canonical Context resolution 与 successful Receipt 计算出的 effective disposition 均非 pending。
- 每个 released Artifact 有稳定 ID、连续 revision、digest、produced_by 和 Security Profile。
- 每个 released revision 恰好有一个有效 Receipt。
- 精确复现不依赖 @current。
- 启用单一当前答案 Authority Profile 的同一作用域 canonical_for 只有一个 current canonical。
- source released 不会自动成为事实权威。
- Living Knowledge 明确标注为可变、未发布和非复现工作面，不伪装成 released Artifact。

### 14.3 Publication

- staging 校验失败不产生 final bundle。
- commit 前强杀可以安全清理或重试。
- commit 后强杀能够识别 committed，并返回原 Receipt。
- 相同幂等键不会产生第二个 revision。
- candidate hash 变化会阻止发布。
- published 不持久化到 canonical Context，只从 successful Receipt 推导。
- 同一 expected output 不得同时存在 successful Receipt 与 discarded / waived resolution。
- publish committed、Projection build failed 返回成功并明确 projection stale。
- Release Authority 不依赖 Projection Compiler；build 失败不影响 Context 的 effective disposition 计算。
- 路径、symlink、junction 和 reparse point 逃逸被拒绝。
- 跨卷 staging 被拒绝。

### 14.4 Derived Read Models

- 删除 generated 后，一次 build 恢复 Registry、SQLite、Capsule、Views、HOME 和 Health。
- 全批输出共享 build_id、source_digest 和 config_digest。
- 失败 build 保留 last-good。
- projection stale 不参与 Agent 的 current 判断。
- SQLite 外键、完整性和 FTS 检查通过。
- HOME 所有 URI 与 Markdown 链接可解析。
- 删除 generated 不会删除 Garden、Context、Artifact、Receipt、Lens 或 human override。
- Recent AI Changes 与 Needs Attention 能定位自动修改、低置信路由、安全隔离和冲突对象。

### 14.5 中文检索

- 40 条真实中文查询金集建立完成。
- 精确 ID、标题和 alias Top-1 为 100%。
- 综合查询目标 Top-5 至少 90%。
- 综合查询 Top-1 至少 70%。
- 二字关键词能命中已知对象。
- restricted 与 index none 内容没有 token 泄漏。

### 14.6 零表单与治理成本

- 用户人工填写结构化字段数始终为 0；修改 AI 结果不计为填写表单。
- 普通内容自动整理入库率至少 95%，需要用户纠正或即时澄清的比例低于 5%。
- title、summary、alias 等低风险字段的人工纠正率低于 10%；低置信自动 canonical、withdraw、supersede、删除和安全降级次数为 0。
- 已存在 human override 的同类错误复发率低于 2%，错误作用域扩散次数为 0。
- 元数据生成、纠错、发布和构建维护的总成本不超过总工作成本约 10%，且不转化为用户录入劳动。
- 首批 20–30 个正式对象及足够数量的 Garden 样本完成后，再评估 Schema 扩展。

## 十五、架构不变量

以下规则必须由 Domain Contracts、Shared Policy、Release Authority、Projection Compiler 或跨边界契约测试按所有权保护：

1. KB、Context、Artifact 和 Publication ID 全局唯一且格式正确。
2. Artifact revision 从 1 连续递增，不缺号、不覆盖、不重用。
3. released bundle 的 manifest、Receipt 与 payload 不可原地修改。
4. 每个 released revision 恰好对应一个 successful Receipt。
5. Receipt、Artifact 和 payload digest 三方一致。
6. 只有启用单一当前答案 Authority Profile 的作用域化 canonical_for，才在 current canonical 集合中唯一。
7. 同一 ID 内正常演进只增加 revision；supersedes 只用于跨 ID 接管。
8. supersedes 无环。
9. 精确关系引用的 revision 必须存在。
10. @current 不得出现在 Receipt、consumes、验收、构建或复现合同中。
11. current 被撤回且无替代时，@current 查询失败关闭。
12. release 路径规范化后仍位于 release 根目录。
13. 未识别 Security Profile 的对象不得发布或索引；Release 与 Projection 必须使用相同 Policy version / digest。
14. index none 的内容不得进入 Registry 正文、SQLite、FTS 或 Capsule。
15. generated 不能成为 canonical 输入。
16. done Context 的每个 required output，其从 canonical resolution 与 successful Receipt 计算出的 effective disposition 均非 pending。
17. Collection 内部 raw 文件不自动升级为 Artifact。
18. 外部工作空间与 KB snapshot 不能同时声明 editable canonical。
19. source released 不自动获得 canonical authority。
20. SQLite 删除后可通过一次 build 恢复，且不会删除或修改任何事实源。
21. Registry、SQLite、Capsule、Views、HOME 和 Health 属于同一 build_id。
22. source_digest 或 config_digest 不一致时 projection 不得报告 fresh。
23. 发布完成不依赖 SQLite 或其他 generated 内容。
24. AI 的日常知识写入不直接执行 SQLite 写操作；Projection Compiler 是唯一 SQLite writer。
25. 正常捕获、整理、Context 创建和发布路径不要求用户填写任何结构化字段。
26. 原始输入在 AI 分类和语义改写前先持久化；模型或工具失败不得丢失输入。
27. AI 对已有 Garden、Context、Lens 或 candidate 的语义写入必须校验 base digest；冲突时不得 last-write-wins。
28. human override 必须跨 organize、build、re-ingest、Schema 变化和模型升级持续有效，除非用户明确替代或撤销。
29. 低置信度不得触发删除、自动合并、withdraw、supersede、canonical 接管、扩大索引或降低安全限制。
30. Release Authority 不依赖 Projection Compiler；任何 build 失败不得改变 committed Receipt。
31. `published` 不得持久化到 canonical Context，只能从 successful Receipt 推导。
32. 同一 expected output 不得同时存在 successful Receipt 与 discarded / waived resolution；被 Receipt 引用的 OUT-ID 不得删除或语义复用。
33. 外部内容中的指令不具授权能力；只有用户指令通道和已登记 Policy 可以产生发布、权限或安全变化。
34. AI 纠错规则默认只作用于当前对象；扩大到目录、kind、来源或全库必须有用户明确表达的作用域。

## 十六、止损线

出现以下任一情况，停止扩展并回到边界或合同本身：

- 故障注入产生外部可见但缺少有效 Receipt 的 released bundle。
- 相同发布意图重试后生成两个 revision 或两个 Receipt。
- commit 后系统无法明确回答 committed=true/false。
- Artifact 的真值判定依赖 SQLite、Registry 或 HOME。
- 同一受治理作用域出现两个 current canonical。
- source/config digest 不一致却仍报告 fresh。
- AI 需要频繁绕过 Contract、Release Authority 或 Projection Compiler 才能完成正常工作。
- Release Authority 开始依赖 Projection Compiler、Search、SQLite 或 HOME。
- 普通捕获、整理或发布流程要求用户填写任何结构化字段。
- 任一原始输入因 AI、Release Authority 或 Projection Compiler 故障而丢失。
- 任一 human override 被 organize、build、re-ingest、Schema 变化或模型升级静默覆盖。
- 低置信内容被自动标为 canonical，或触发删除、合并、withdraw、supersede 或安全降级。
- restricted / secret-ref-only 正文进入普通 Registry、Capsule、FTS 或导出。
- 需要即时人工澄清的普通输入超过 5%。
- 已纠正的同类错误复发率超过 2%，或 override 扩散到用户未授权的作用域。
- Schema 误拒绝超过发布次数的 10%。
- 超过 20% 的对象必须依靠 extensions 才能表达。
- 自动元数据、纠错、发布和构建维护成本超过总工作成本的 10%，或开始转嫁为用户录入劳动。
- 全量 build p95 不超过 5 秒时却开始实现增量。
- 中文金集 Top-5 低于 90% 时仍继续堆叠 alias。
- 二元组索引超过被索引文本约 4 倍，或小型正式库超过 250 MB。
- HOME 超过 6 个主要章节或约 150 行。
- 五个真实样本未跑通就开始迁移旧库、接入图谱或向量检索。
- 大型 payload 因 revision 复制增长超过约 15%；此时评估内容寻址 blob，不继续目录复制。

## 十七、V0.1 冻结范围与待验证假设

### 17.1 当前冻结的不变量

| 决策 | V0.1 结论 |
|---|---|
| 用户交互 | 正常路径零表单；用户只提供内容、意图和直接纠正 |
| 总体模型 | 双核一桥 + Living Knowledge Workspace + 独立 Projection Compiler |
| AI 角色 | AI 自动捕获、整理、关联、维护，并在门禁与授权范围内发起发布，但不拥有事实或发布 commit |
| Context / Artifact 所有权 | 运行态与正式产物严格分离；Garden 承载可编辑活知识，不伪装成 released Artifact |
| Publication Bridge | 统一 `kb` 外观与编排，不拥有 canonical facts |
| Release Authority | 唯一正式发布写入者，拥有事务、幂等、revision、commit/recovery 与 Receipt |
| Projection Compiler | 只读事实源，拥有全部 generation、freshness 与读模型算法 |
| 事实源 | Markdown、YAML、Garden、Context、Artifact bundle、Receipt、Override、原始文件与外部工作空间 |
| 人工修改 | human override 写入优先、持久、可追踪；不得被 AI 或重建静默覆盖 |
| 不确定性 | 自动退回最可逆、最低权威、最小暴露的状态，不转化为用户表单 |
| Artifact 版本 | 强制整数 revision；released 不可原地修改；SemVer 仅作可选 release_version |
| Artifact 发布 | 同卷 staging + immutable bundle + 单一目录 commit |
| Receipt | 与 Artifact payload 同 bundle 提交，语义归 Release Authority |
| 安全 | 真实秘密只在 Capture Spool 中短暂受保护暂存，长期只存外部 Vault；未知或低置信安全状态只能收紧，不能自动放宽 |
| Collection | Artifact kind + Manifest；内部 raw 默认按需读取 |
| 新旧库关系 | 禁止双写；只按真实复用单向晋升 |

### 17.2 试运行后再决定的实现选择

| 假设 | 当前试运行选择 |
|---|---|
| 工具语言 | Python 3.11+，当前环境优先 Python 3.14 |
| 依赖 | 标准库 + ruamel.yaml + jsonschema |
| ID | KB / CTX / ART / PUB 使用类型前缀 + ULID |
| Schema 载体 | JSON Schema Draft 2020-12；YAML / Markdown frontmatter 由 AI 维护 |
| Context 生命周期 | active / waiting / done / cancelled；细分状态待样本验证 |
| CLI | ingest、status、check、build、find、show、trace、explain、correct、publish |
| SQLite | 只读投影、全量重建、失败保留 last-good |
| 中文检索 | aliases + zh-bigram-v1 + FTS5 unicode61 |
| HOME | AI 默认维护 Lens，用户修改形成 override，Projection Compiler 整文件生成 |
| Security Profile | 四个版本化 Profile；名称与组合方式待真实样本校准 |
| 分层读取 | L0 Bootstrap → L1 Capsule → L2 Body → L3 Evidence；候选数量按任务类型调整 |
| generated 保留 | current + previous；不保存无限 generation 历史 |

试运行选择可以被真实样本推翻。只有改变 17.1 的冻结不变量，才必须先新增明确的 Architecture Decision；修改 17.2 应记录验证证据、兼容影响和回退方式，但不得为了文档一致而拒绝更好的实现。

## 十八、参考标准

- Python sqlite3：https://docs.python.org/3.14/library/sqlite3.html
- Python argparse：https://docs.python.org/3.14/library/argparse.html
- Python os.replace：https://docs.python.org/3.14/library/os.html#os.replace
- JSON Schema Draft 2020-12：https://json-schema.org/draft/2020-12
- SQLite FTS5：https://www.sqlite.org/fts5.html
- SQLite Foreign Keys：https://www.sqlite.org/foreignkeys.html
- Semantic Versioning：https://semver.org/

## 最终架构判断

知识库 2.0 的核心不是目录、数据库或某种“笔记法”，而是七个可长期保持的责任：

~~~text
用户负责表达、使用、纠正与最终意图
AI Organizer 负责捕获、整理、关联与日常维护
Living Knowledge Workspace 负责让尚未成熟的知识持续生长
Context 负责把跨会话的事情做完
Release Authority 负责证明什么能够正式留下
Artifact 负责让正式留下的东西可寻址、可追溯、可复用
Projection Compiler 负责让人和 AI 快速找到并按需下钻
~~~

只要用户零表单、人工纠正优先、AI 不确定时安全退让、事实所有权、发布 commit point 和 release / projection 依赖方向不被破坏，未来可以替换模型、Prompt、目录、SQLite、搜索算法、展示界面甚至工具语言，而不需要重写知识本体或要求用户学习治理结构。
