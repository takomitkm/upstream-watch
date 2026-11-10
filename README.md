# upstream-watch

可复用的 GitHub Action：对账「上游仓库里你在乎的文件」的内容指纹（git blob sha），
变了就自动开/评论一个带标签的 issue。零第三方依赖（纯 Python 标准库，HTTP 走 curl）。

诞生背景：给 [rime/librime#1260](https://github.com/rime/librime/pull/1260) 看冲突面——
上游 `9777cb2` 改了 `level_db.cc` 的 `Recover()`，等维护者回复才知道，不如每天自动对账。
机制与具体项目无关：vendor 出去的源码、钉住的补丁落点、任何"上游动了我就得知道"的文件都可以盯。

## 三步接入

1. 在你的仓库放一份监视清单 `.upstream-watch/vendor.json`（格式见下，可抄 [`vendor.example.json`](vendor.example.json)）；
2. 在 `.github/workflows/upstream-watch.yml` 放一个工作流（把 `<owner>` 换成本仓库的 owner）：

```yaml
name: upstream-watch
on:
  schedule:
    - cron: '23 3 * * *'
  workflow_dispatch:
permissions:
  contents: write   # 回写 state
  issues: write     # 开/评论报警 issue
jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - uses: <owner>/upstream-watch@v1
```

3. 手动 dispatch 一次建基线（首跑只记档、不报警）。此后受监视路径一旦变化（含新出现/被删除），
   会开（或评论）带 `upstream-watch` 标签的 issue。

对稳定性有要求可把 `@v1` 钉到具体 commit SHA。

## 清单格式（vendor.json）

```json
{
  "issue": { "label": "upstream-watch", "title_prefix": "[upstream-watch] 上游有变化: " },
  "upstreams": [
    {
      "name": "owner/repo",       // 被监视的上游仓库
      "ref": "main",              // 被监视的分支/-tag
      "reason": "为什么盯它",      // 原样出现在报警 issue 里
      "paths": ["dir/file.cc", "submodule-dir"]   // 文件盯 blob sha，submodule 盯指针
    }
  ]
}
```

## 行为约定

- 首跑建基线；新加的上游/路径首轮静默记档，第二轮起才参与报警。
- 同一时刻最多一个开放 issue：有则评论、无则新建；人工确认后关闭。
- state 在报警成功**之后**才写盘：宁可下次重复提醒，不可静默丢失。
- state 由 action 自动 commit 回来（消息带 `[skip ci]`，不会自触发）。

## 输入（`with:`）

| 输入 | 默认 | 说明 |
|---|---|---|
| `vendor` | `.upstream-watch/vendor.json` | 清单路径 |
| `state` | `.upstream-watch/state.json` | 状态文件路径（自动创建并提交） |
| `repo` | 当前仓库 | issue 开到哪个仓库 |
| `token` | `github.token` | API 读取 / 开 issue / 提交 state |
| `commit-state` | `true` | 是否把刷新后的 state 提交回来 |

## 限制

- 只认 GitHub API：Fossil / tarball / 邮件列表发版的上游盯不了。
- 只盯路径内容与 submodule 指针：不发版通知（用 Watch releases）、不扫 CVE（用 osv-scanner）、
  不解析 diff 语义——报警只说明"该去看了"。
- GitHub 会在仓库 60 天无动静后自动停 cron；上游长期安静时手动 dispatch 续命。

## License

MIT
