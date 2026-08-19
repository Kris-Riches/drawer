# Drawer

Drawer 是一个本地优先、先捕获后整理的个人知识库原型。它把可变知识、跨会话 Context、不可变发布物和可重建检索投影分开，避免输入丢失、人工修改被覆盖或发布历史被事后改写。

## 快速使用

```powershell
.\kb.ps1 status
$text = Get-Content -Raw -LiteralPath '.\note.md'
$text | .\kb.ps1 publish-text
.\kb.ps1 find '查询词'
.\kb.ps1 show 'artifact://ART-...'
.\kb.ps1 trace 'artifact://ART-...'
```

完整运行与恢复约定见 [PROTOCOL.md](PROTOCOL.md)，架构说明见 [docs/architecture.md](docs/architecture.md)。

## 目录

```text
src/kb2/          Python 源码
tests/            自动化测试
docs/             架构与本地内部文档
ingress/          受保护的原始输入
garden/           可持续修改的活知识
contexts/         跨会话工作状态
governance/       owner、override 与发布候选
released/         不可变发布物
generated/        可删除重建的读取投影
```

运行数据、内部推进文档和验收记录由 `.gitignore` 排除，不应把 GitHub 仓库当作个人知识数据备份。

## License

本项目使用 [MIT License](LICENSE)。
