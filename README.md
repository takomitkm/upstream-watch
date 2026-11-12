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
  "notify": [ { "type": "webhook", "env": "UW_WEBHOOK_URL", "format": "slack|json" } ],
  "discover": { "gitmodules": true },
  "upstreams": [
    {
      "label": "显示名(默认=repo)",      // state 键；同名不同 ref 靠它区分
      "repo": "owner/repo",             // 被监视的上游仓库（旧写法 "name" 兼容）
      "ref": "main",                    // 被监视的分支/-tag
      "release": true,                  // 可选：盯最新 release，发新版即报警
      "conflict_branch": "my-patches",  // 可选：把该分支与上游 tip 做 merge-tree 冲突预演
      "conflict_repo": "owner/patches", // 可选：分支所在仓库，默认=消费仓
      "reason": "为什么盯它",
      "paths": ["dir/file.cc", "submodule-dir"]   // 文件盯 blob sha，submodule 盯指针
    }
  ]
}
```

## 报警长什么样

- 表格：路径、blob sha 迁移、每文件 **+N −M** 统计、最近触碰提交的链接；
- 每个变化的文件附**截断 patch**（`<details>` 折叠，默认 40 行，点 commit 看全文）；
- 表头有旧 tip → 新 tip 的 **compare 链接**（上游 force-push 等取不到时自动降级为无 patch）；
- 配了 `conflict_branch` 的上游带**冲突预演**：`git merge-tree` 内存三方合并，报"可干净合入 /
  预期冲突（文件列表）/ 无法预演"，只预报不动任何分支；
- 配了 `release: true` 的上游发新版：单独一行告警 + release notes（截断）；
- 配了 `"discover": {"gitmodules": true}` 的：自动发现消费仓 `.gitmodules` 里的子模块，
  钉住的指针落后上游分支（有新提交可用）即报警；认过的 tip 不重复报；
- 同一时刻最多一个开放 issue：有则评论、无则新建；人工确认后关闭。

## 输入（`with:`）

| 输入 | 默认 | 说明 |
|---|---|---|
| `vendor` | `.upstream-watch/vendor.json` | 清单路径 |
| `state` | `.upstream-watch/state.json` | 状态文件路径（自动创建并提交） |
| `repo` | 当前仓库 | issue 开到哪个仓库 |
| `token` | `github.token` | API 读取 / 开 issue / 提交 state |
| `commit-state` | `true` | 是否把刷新后的 state 提交回来 |
| `webhook-url` | 空 | 报警发出后 POST 摘要（`notify` 里 `format: slack` 发 Slack 形态）；从 Secret 传入 |
| `keepalive` | `false` | 分支 tip 超 40 天无提交且本轮 state 没动过时打一个空提交，防 GitHub 停 60 天闲置 cron |
| `smoke-workflow` | 空 | 报警发出后 dispatch 的 workflow 文件名（需 workflow `permissions` 加 `actions: write`） |
| `smoke-ref` | 当前 ref | 冒烟 dispatch 用的 ref |

## 行为约定

- 首跑建基线；新加的上游/路径/子模块首轮静默记档，第二轮起才参与报警。
- state 在报警成功**之后**才写盘：宁可下次重复提醒，不可静默丢失。
- state 由 action 自动 commit 回来（消息带 `[skip ci]`，不会自触发）。
- 冲突预演只在消费仓 checkout 里 fetch + `merge-tree`（`--depth 300`），不 push、不改分支；
  失败（历史不足/私有不可达）在 issue 里如实写"无法预演"。

## 限制

- 源只认 GitHub（API + git fetch）；tarball/Fossil 类上游盯不了。
- 只盯路径内容、submodule 指针与 release：不扫 CVE（osv-scanner）、不解析 diff 语义——
  报警只说明"该去看了"。
- GitHub 会在仓库 60 天无动静后自动停 cron；上游长期安静时开 `keepalive` 或手动 dispatch 续命。

## License

MIT
