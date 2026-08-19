<p align="center">
  <img src="assets/drawer-logo.png" alt="Drawer logo" width="260">
</p>

<h1 align="center">Drawer</h1>

<p align="center">
  <em>先收进抽屉，再慢慢想清楚。<br>Put it in the drawer first. Make sense of it later.</em>
</p>

<p align="center">
  <a href="https://github.com/Kris-Riches/drawer/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/Kris-Riches/drawer?style=flat-square&color=111111&label=stars"></a>
  <img alt="Version v0.2" src="https://img.shields.io/badge/version-v0.2-111111?style=flat-square">
  <img alt="Regression: 102 passed, 2 opt-in skipped" src="https://img.shields.io/badge/regression-102%20passed%20%7C%202%20opt--in%20skipped-111111?style=flat-square">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-111111?style=flat-square">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-111111?style=flat-square"></a>
</p>

<p align="center">
  <strong>本地优先 · 先捕获后整理 · 不可变发布 · 可重建搜索</strong><br>
  <strong>Local-first · Capture-first · Immutable releases · Rebuildable search</strong>
</p>

<p align="center">
  <a href="#中文">中文</a> · <a href="#english">English</a>
</p>

---

<a id="中文"></a>

## 中文

一段笔记刚出现时，最重要的往往不是“分到哪个文件夹”，而是**先别丢**。

Drawer 是一个本地优先的个人知识库原型。它先原样保留输入，再让内容进入可以持续整理的活知识区；人的纠正优先于自动整理；真正发布后，正文、版本和回执不可被悄悄改写；搜索索引坏了可以从事实对象重新生成。

```text
输入一段文字
     ↓
先保存原始输入
     ↓
整理为活知识，人可以直接纠正
     ↓
发布为不可变版本，并生成发布回执
     ↓
构建可删除、可重建的搜索投影
```

Drawer 面向 AI 协助的工作方式，但可信边界仍由本地、确定性的 Python 代码负责。AI 可以帮你理解和整理，不能绕过安全检查、覆盖人的修改，或改写已经发布的历史。

### 它解决什么

| 常见问题 | Drawer 的做法 |
|---|---|
| 输入还没整理就丢了 | Capture-first：先持久化，再扫描和整理 |
| 自动整理覆盖了手工修改 | Human override：人的纠正具有明确来源并持续生效 |
| 跨会话项目状态互相打架 | 一个 Context 只有一份 canonical current state |
| “最终版”后来又被改写 | Artifact revision 与 Publication Receipt 不可变 |
| 索引损坏后无法确认事实 | Registry、HOME、Search 都是可重建投影，不是事实源 |
| 找到了结果，却不知道从哪来 | `trace` 返回 Capture → Garden → Candidate → Artifact → Revision → Receipt 来源链 |
| 外部资料夹带命令或敏感信息 | 只接收公开严格 UTF-8 文本；敏感内容 fail closed，外部 Skill 只作为 data-only 资料 |

### 工作原理

1. **Capture** — 先保存原始输入，整理失败也不丢内容。
2. **Garden** — 内容进入可持续修改的活知识区。
3. **Context** — 保存跨会话工作的当前状态、进展与阻塞。
4. **Candidate** — 生成经过 owner 与摘要校验的发布候选。
5. **Release** — 提交不可变 Artifact revision 与唯一 Publication Receipt。
6. **Projection** — 从事实对象生成 Registry、HOME 和 Search；需要时可整套重建。

### 快速开始

当前版本在 Windows + PowerShell 上验证，需要 Python 3.11 或更高版本。Codex 桌面环境也可以使用随附的 Python runtime。

```powershell
git clone https://github.com/Kris-Riches/drawer.git
Set-Location .\drawer
.\kb.ps1 status
```

发布一份公开 UTF-8 文本：

```powershell
$text = Get-Content -Raw -LiteralPath '.\note.md'
$text | .\kb.ps1 publish-text
```

查找并核验结果：

```powershell
.\kb.ps1 find '查询词'
.\kb.ps1 show 'artifact://ART-...'
.\kb.ps1 trace 'artifact://ART-...'
```

`publish-text` 会完成 Capture → Garden → Candidate → Release → Projection 的零表单闭环。命令返回 JSON，方便人和 Agent 使用同一入口。

> **重要：** 如果发布已经提交，但返回 Projection stale，只运行 `.\kb.ps1 build`。不要重复发布同一内容。

### 常用命令

| 命令 | 用途 |
|---|---|
| `.\kb.ps1 status` | 查看真实计数、能力和 Projection freshness |
| `... | .\kb.ps1 publish-text` | 发布公开严格 UTF-8 文本的日常入口 |
| `.\kb.ps1 find '<query>'` | 搜索公开 Artifact |
| `.\kb.ps1 show '<artifact-uri>'` | 读取已提交的 Artifact revision |
| `.\kb.ps1 trace '<artifact-uri>'` | 查看发布时冻结的来源链 |
| `.\kb.ps1 build` | 从事实对象重建读取投影 |
| `.\kb.ps1 close-context '<context-uri>'` | 幂等关闭一个 Context |

`ingest`、`organize`、`correct`、`publish`、`explain` 和 `recover` 属于机器或诊断入口。完整合同见 [PROTOCOL.md](PROTOCOL.md)。

### 项目结构

```text
src/kb2/             Python 源码
tests/               自动化测试
docs/architecture.md 核心架构说明
kb.ps1               Windows / PowerShell 稳定入口
kb.yaml              知识库根锚点
PROTOCOL.md           当前运行、安全与恢复合同

ingress/              受保护的原始输入              （本地运行数据）
garden/               可持续修改的活知识            （本地运行数据）
contexts/             跨会话 current state          （本地运行数据）
governance/           owner、override 与发布候选     （本地运行数据）
released/             不可变发布物与回执              （本地运行数据）
generated/            可删除重建的 Registry/Search   （本地运行数据）
```

运行数据、内部推进记录和验收证据默认由 `.gitignore` 排除。**GitHub 仓库存的是程序，不是你的个人知识数据备份。**

### 当前状态与边界

Drawer 当前为 **V0.2 GO**：公开严格 UTF-8 文本的单机闭环、Context 生命周期、六段不可变溯源、中英文 Search、外部不可信资料的 data-only 处理，以及可恢复重建均已通过验收。

最后一次公开前扩大回归共 104 项：**102 passed、2 个 24-way 压力测试按设计 opt-in skipped、0 fail/error**。动态对象计数、build identity 与 freshness 请始终以 `.\kb.ps1 status` 为准。

当前明确支持：

- 单机、本地优先的公开文本知识流；
- 人工纠正优先并保留来源；
- Context 的创建、更新与关闭；
- 不可变发布、幂等重放、只读 `show/trace`；
- 中文连续子串、英文短语与词项搜索；
- Projection stale 检测和从事实对象重建。

当前不承诺：

- 文件、图片、PDF、音频或网页自动摄取；
- GUI、多人协作、多机或分布式一致性；
- SQLite、FTS、向量数据库或复杂 Views；
- 旧知识库自动迁移或双写；
- 断电、介质损坏和极端并发下的完整证明；
- 把 Drawer 当作加密保险箱或秘密管理器。

### 开发与验证

项目核心只使用 Python 标准库。运行核心测试：

```powershell
$env:PYTHONPATH = (Resolve-Path '.\src').Path
python -B -m unittest tests.test_release tests.test_cli_release tests.test_workflow tests.test_bootstrap
```

运行全部测试：

```powershell
$env:PYTHONPATH = (Resolve-Path '.\src').Path
python -B -m unittest discover -s tests -p 'test_*.py'
```

架构与事实边界见 [docs/architecture.md](docs/architecture.md)。

### License

Drawer 使用 [MIT License](LICENSE)。

---

<a id="english"></a>

## English

When a note first appears, the most important question is usually not “Which folder does this belong in?” It is **“Did I save it?”**

Drawer is a local-first personal knowledge base prototype. It preserves the original input before organizing it, keeps living knowledge editable, gives human corrections priority over automation, freezes published revisions and receipts, and rebuilds search projections from facts when needed.

```text
Write something
      ↓
Preserve the original input first
      ↓
Organize it as living knowledge; let humans correct it
      ↓
Publish an immutable revision and receipt
      ↓
Build a disposable, reproducible search projection
```

Drawer is designed for AI-assisted workflows, but its trusted boundary remains local and deterministic. An AI may help interpret and organize your notes; it cannot bypass safety checks, overwrite a human correction, or rewrite a published history.

### What it fixes

| Problem | Drawer’s answer |
|---|---|
| Input disappears before it is organized | Capture first, scan and organize second |
| Automation overwrites a manual edit | Human overrides remain explicit, traceable, and active |
| Cross-session project state contradicts itself | Each Context has one canonical current-state document |
| A “final” document changes after publication | Artifact revisions and Publication Receipts are immutable |
| A broken index becomes a broken source of truth | Registry, HOME, and Search are rebuildable projections |
| A result has no trustworthy origin | `trace` exposes the frozen Capture → Garden → Candidate → Artifact → Revision → Receipt chain |
| External material contains commands or secrets | Only public strict UTF-8 text is accepted; sensitive input fails closed and external Skills stay data-only |

### How it works

1. **Capture** — Persist the original input before any interpretation.
2. **Garden** — Maintain the editable, living version of the knowledge.
3. **Context** — Carry the current state of longer-running work across sessions.
4. **Candidate** — Produce an owner-backed, digest-checked release candidate.
5. **Release** — Commit an immutable Artifact revision and exactly one Publication Receipt.
6. **Projection** — Generate Registry, HOME, and Search from facts; delete and rebuild them when necessary.

### Quick start

The current version is verified on Windows with PowerShell and requires Python 3.11 or newer. The bundled Codex Python runtime is also supported.

```powershell
git clone https://github.com/Kris-Riches/drawer.git
Set-Location .\drawer
.\kb.ps1 status
```

Publish a public UTF-8 text file:

```powershell
$text = Get-Content -Raw -LiteralPath '.\note.md'
$text | .\kb.ps1 publish-text
```

Find and verify the result:

```powershell
.\kb.ps1 find 'search terms'
.\kb.ps1 show 'artifact://ART-...'
.\kb.ps1 trace 'artifact://ART-...'
```

`publish-text` runs the zero-form Capture → Garden → Candidate → Release → Projection loop. Commands return JSON so people and agents can share the same interface.

> **Important:** if the release committed but the Projection is stale, run `.\kb.ps1 build`. Do not publish the same content again.

### Everyday commands

| Command | Purpose |
|---|---|
| `.\kb.ps1 status` | Inspect live counts, capabilities, and Projection freshness |
| `... | .\kb.ps1 publish-text` | Publish public strict UTF-8 text |
| `.\kb.ps1 find '<query>'` | Search public Artifacts |
| `.\kb.ps1 show '<artifact-uri>'` | Read a committed Artifact revision |
| `.\kb.ps1 trace '<artifact-uri>'` | Inspect the provenance frozen at release time |
| `.\kb.ps1 build` | Rebuild read projections from facts |
| `.\kb.ps1 close-context '<context-uri>'` | Idempotently close a Context |

`ingest`, `organize`, `correct`, `publish`, `explain`, and `recover` are machine or diagnostic interfaces. See [PROTOCOL.md](PROTOCOL.md) for the complete contract.

### Repository layout

```text
src/kb2/             Python source
tests/               Automated tests
docs/architecture.md Core architecture
kb.ps1               Stable Windows / PowerShell entry point
kb.yaml              Knowledge-base root anchor
PROTOCOL.md           Runtime, safety, and recovery contract

ingress/              Protected raw captures             (local runtime data)
garden/               Editable living knowledge          (local runtime data)
contexts/             Cross-session current state        (local runtime data)
governance/           Owners, overrides, and candidates  (local runtime data)
released/             Immutable releases and receipts    (local runtime data)
generated/            Rebuildable Registry/Search        (local runtime data)
```

Runtime data, internal progress notes, and acceptance evidence are excluded by `.gitignore`. **The GitHub repository contains the program, not a backup of your personal knowledge.**

### Status and scope

Drawer is currently **V0.2 GO**. Its single-machine public-text loop, Context lifecycle, six-stage immutable provenance, Chinese and English Search, data-only treatment of untrusted external material, and recoverable Projection rebuild have passed acceptance.

The last expanded regression before the public release ran 104 tests: **102 passed, two 24-way stress tests remained opt-in skipped, and there were no failures or errors**. Live object counts, build identity, and freshness must always come from `.\kb.ps1 status`.

Supported today:

- local-first, single-machine workflows for public text;
- human corrections with durable provenance and priority;
- Context creation, update, and closure;
- immutable release, idempotent replay, and read-only `show/trace`;
- Chinese substring plus English phrase and term search;
- stale Projection detection and fact-based rebuilds.

Not promised yet:

- automatic ingestion of files, images, PDFs, audio, or webpages;
- a GUI, multi-user collaboration, or distributed consistency;
- SQLite, FTS, vector databases, or complex Views;
- automatic migration or dual-writing of an older knowledge base;
- full guarantees under power loss, media failure, or extreme concurrency;
- use as an encrypted vault or secret manager.

### Development and verification

The core uses only the Python standard library. Run the core tests:

```powershell
$env:PYTHONPATH = (Resolve-Path '.\src').Path
python -B -m unittest tests.test_release tests.test_cli_release tests.test_workflow tests.test_bootstrap
```

Run the complete test suite:

```powershell
$env:PYTHONPATH = (Resolve-Path '.\src').Path
python -B -m unittest discover -s tests -p 'test_*.py'
```

See [docs/architecture.md](docs/architecture.md) for the architecture and source-of-truth boundaries.

### License

Drawer is available under the [MIT License](LICENSE).
