#!/usr/bin/env python3
"""conflict_preview —— 冲突预演：对 manifest 里带 conflict_branch 的上游，
用 git merge-tree 预演「补丁分支 ↔ 上游最新 tip」的合并，把结果写成 markdown 报告。

只预报、不落地：merge-tree 是内存中的三方合并，不碰工作树、不产生提交。

manifest 相关字段（每个 upstreams 条目）:
  "conflict_branch": "分支名"                       # 必填才有预演
  "conflict_repo":   "owner/repo"                   # 分支所在仓库，默认=消费仓（--consumer-repo）
  name/repo/ref 同 watch_upstream.py（旧 "name" 写法兼容）

产出（--out）:
  <!-- uw-conflict label="…" -->
  ### 冲突预演 — <label>
  分支 `b`（tip …） ↔ 上游 `ref`（tip …）
  结果: ✅ 可干净合入 / ⚠️ 预期冲突（文件列表）/ ❌ 无法预演（原因）
  <!-- /uw-conflict -->

用法:
  python3 conflict_preview.py --vendor vendor.json --out conflict-report.md \
      --consumer-repo OWNER/REPO [--workspace DIR] [--depth 300]
"""
import argparse
import json
import os
import subprocess
import sys

API = "https://api.github.com"
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def fetch_tip_sha(repo, ref, token):
    """上游 tip（API 取，避免依赖本地 fetch 顺序）。"""
    import time
    url = f"{API}/repos/{repo}/commits/{ref}"
    cmd = ["curl", "-sS", "--max-time", "60",
           "-H", "Accept: application/vnd.github+json",
           "-H", "User-Agent: upstream-watch-conflict"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    cmd += [url]
    for attempt in range(3):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        try:
            data = json.loads(proc.stdout)
            sha = data.get("sha")
            if sha:
                return sha
        except json.JSONDecodeError:
            pass
        time.sleep([2, 5, 10][min(attempt, 2)])
    return None


def git(args, cwd, check=True):
    cmd = ["git", "-c", "protocol.version=2"] + args
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}… 失败: {(proc.stderr or proc.stdout).strip()[:200]}")
    return proc


def auth_url(url, token):
    """私有仓库需要凭据；公有匿名即可。"""
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


def preview_one(entry, workspace, consumer_repo, token, depth):
    """返回 (label, ok, markdown正文)。ok=False 时正文说明原因。"""
    repo = entry.get("repo") or entry.get("name")
    label = entry.get("label") or repo
    ref = entry["ref"]
    branch = entry.get("conflict_branch")
    branch_repo = entry.get("conflict_repo") or consumer_repo

    up_tip = fetch_tip_sha(repo, ref, token)
    if not up_tip:
        return label, False, f"取上游 tip 失败（API 不可达）"

    ns = label.replace("/", "_").replace(" ", "_")
    # 直接在消费仓 checkout 里预演：对象库复用、fetch 少；引用用 refs/uw-* 命名空间隔离
    url_up = auth_url(f"https://github.com/{repo}", token)
    url_br = auth_url(f"https://github.com/{branch_repo}", token)
    git(["fetch", "-q", "--no-tags", "--depth", str(depth),
         url_up, f"+refs/heads/{ref}:refs/uw-{ns}/upstream"], workspace)
    git(["fetch", "-q", "--no-tags", "--depth", str(depth),
         url_br, f"+refs/heads/{branch}:refs/uw-{ns}/branch"], workspace)
    br_tip = git(["rev-parse", f"refs/uw-{ns}/branch"], workspace).stdout.strip()
    proc = git(["merge-tree", "--write-tree",
                f"refs/uw-{ns}/branch", f"refs/uw-{ns}/upstream"], workspace, check=False)
    head = [f"分支 `{branch}`（tip `{br_tip[:10]}`）↔ 上游 `{ref}`（tip `{up_tip[:10]}`）"]
    if proc.returncode == 0:
        body = head + ["", "结果：**可干净合入**（本次上游变化不与该分支冲突）。"]
    elif proc.returncode == 1:
        conflicts = sorted({ln.split(" in ", 1)[1].strip()
                            for ln in (proc.stdout or "").splitlines()
                            if ln.startswith("CONFLICT") and " in " in ln})
        msgs = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("CONFLICT")]
        body = head + ["", "结果：**预期冲突**。", ""]
        if conflicts:
            body += ["| 冲突文件 |", "|---|"] + [f"| `{p}` |" for p in conflicts] + [""]
        if msgs:
            body += ["```", *msgs[:10], "```"]
        body += ["rebase/merge 前先看这些文件；本预演是内存三方合并，未动任何分支。"]
    else:
        body = head + ["", f"结果：**无法预演**（{proc.stderr.strip()[:120] or 'git merge-tree 非零退出'}）。"]
    return label, True, "\n".join(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--consumer-repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--depth", default="300")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    with open(args.vendor, encoding="utf-8") as f:
        vendor = json.load(f)
    entries = [u for u in vendor.get("upstreams", []) if u.get("conflict_branch")]
    if not entries:
        print("conflict_preview: manifest 未配置 conflict_branch，跳过")
        return 0

    sections = []
    for entry in entries:
        label = entry.get("label") or entry.get("repo") or entry.get("name")
        try:
            label, ok, md = preview_one(entry, args.workspace, args.consumer_repo, token, args.depth)
        except Exception as exc:  # noqa: BLE001 预演失败不致命，写进报告
            md = f"结果：**无法预演**（{str(exc)[:160]}）"
        sections.append(f'<!-- uw-conflict label="{label}" -->\n### 冲突预演 — {label}\n{md}\n<!-- /uw-conflict -->')
        print(f"conflict_preview: {label} done")

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sections) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
