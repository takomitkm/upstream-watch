#!/usr/bin/env python3
"""upstream-watch —— 盯上游仓库指定路径的内容变化，变化则开/评论 GitHub issue。

设计要点:
- 只用 Python 标准库；HTTP 一律走 curl 子进程（本机 urllib TLS 不稳，curl 本地/CI 都有）。
- state.json 记录每个被盯路径的 blob sha 与上游 tip；首跑（无 state）只建基线、不报警。
- 报警去重：同一时刻最多一个带标签的开放 issue，有则评论、无则新建。
- state 在报警成功之后才写盘：宁可下次重复提醒，不可静默丢失。

用法:
  python3 watch_upstream.py --vendor vendor.json --state state.json --repo OWNER/REPO
      [--baseline]   # 只记录基线，不报警不评论（建账）
      [--dry-run]    # 什么都不写、不发；把本应发出的 issue 标题/正文打印到 stdout
环境变量: GITHUB_TOKEN（公开仓库读取可不带，匿名限速 60 次/小时）

退出码: 0 = 正常（含"有变化已报警"），非 0 = API/网络错误（CI 里直接红）。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

API = "https://api.github.com"
ABSENT = "ABSENT"  # 路径在上游树里不存在的标记值


# ---------------------------------------------------------------- GitHub API

def _curl_json(args, data=None):
    """GET/POST api.github.com，返回 (http_code, 解析后的 json)。仅允许 API 白名单域。"""
    url = args[0]
    if not url.startswith(API + "/"):
        raise RuntimeError(f"拒绝非白名单 API 地址: {url}")
    cmd = ["curl", "-sS", "--max-time", "60",
           "-H", "Accept: application/vnd.github+json",
           "-H", "X-GitHub-Api-Version: 2022-11-28",
           "-H", "User-Agent: upstream-watch"]
    if data is not None:
        cmd += ["-X", "POST",
                "-H", "Content-Type: application/json",
                "--data-binary", json.dumps(data, ensure_ascii=False)]
    cmd += ["-w", "\n%{http_code}"] + args
    for attempt in range(4):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        body, _, code = proc.stdout.rpartition("\n")
        code = code.strip()
        if proc.returncode == 0 and code == "200":
            return 200, json.loads(body)
        retryable = proc.returncode != 0 or code in ("403", "429", "5")
        if retryable and attempt < 3:
            time.sleep([2, 5, 10][attempt])
            continue
        raise RuntimeError(f"{args[0]} -> HTTP {code or 'ERR'}: {(body or proc.stderr)[:200]}")
    raise RuntimeError("unreachable")


def gh_get(url, token):
    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    return _curl_json([url] + headers)[1]


def gh_post(url, token, payload):
    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    return _curl_json([url] + headers, data=payload)[1]


# ---------------------------------------------------------------- 探测

def fetch_upstream(name, ref, paths, token):
    """返回 {"tip": 上游 tip sha, "paths": {路径: blob sha 或 ABSENT}}。"""
    tip = gh_get(f"{API}/repos/{name}/commits/{ref}", token)
    tip_sha = tip["sha"]
    tree = gh_get(f"{API}/repos/{name}/git/trees/{tip_sha}?recursive=1", token)
    if tree.get("truncated"):
        # 树过大被截断：退化为逐路径查 contents（多花几次调用，保正确）
        entries = None
    else:
        # type=blob 普通文件；type=commit 是 submodule（盯指针）
        entries = {e["path"]: e["sha"] for e in tree.get("tree", [])
                   if e["type"] in ("blob", "commit")}
    result = {}
    for p in paths:
        if entries is not None:
            result[p] = entries.get(p, ABSENT)
        else:
            try:
                c = gh_get(f"{API}/repos/{name}/contents/{quote(p, safe='/')}?ref={quote(ref, safe='')}", token)
                result[p] = c["sha"]
            except RuntimeError as exc:
                if "404" in str(exc):
                    result[p] = ABSENT
                else:
                    raise
    return {"tip": tip_sha, "paths": result}


def commits_touching(name, ref, path, token, limit=3):
    """最近触碰该路径的提交（用于 issue 里的跳转链接）。失败容忍：返回空表。"""
    try:
        lst = gh_get(f"{API}/repos/{name}/commits?sha={quote(ref, safe='')}"
                     f"&path={quote(path, safe='/')}&per_page={limit}", token)
    except RuntimeError:
        return []
    out = []
    for c in lst:
        msg = (c.get("commit", {}).get("message") or "").splitlines()[0][:80]
        out.append({"sha": c["sha"][:10], "msg": msg, "url": c["html_url"]})
    return out


# ---------------------------------------------------------------- 报警

def sha_disp(v, other):
    if v == ABSENT:
        return "（此前不存在）" if other != ABSENT else "（不存在）"
    if other == ABSENT:
        return "（已被删除）"
    return f"`{v[:10]}` → `{other[:10]}`"


def build_alert(vendor, changes):
    """changes = {上游名: {ref, reason, old_tip, new_tip, items:[{path, old, new, commits}]}}
    返回 (title, body)。"""
    names = "、".join(changes)
    title = vendor["issue"]["title_prefix"] + names
    lines = []
    for uname, u in changes.items():
        lines.append(f"## {uname}（{u['ref']}，tip `{u['old_tip'][:10]}` → `{u['new_tip'][:10]}`）")
        lines.append("")
        lines.append(f"监视理由：{u['reason']}")
        lines.append("")
        lines.append("| 路径 | blob sha 变化 | 最近提交 |")
        lines.append("|---|---|---|")
        for it in u["items"]:
            cell = sha_disp(it["old"], it["new"]) if it["old"] != ABSENT else \
                ("（已被删除）" if it["new"] == ABSENT else "新出现")
            if it["old"] != ABSENT and it["new"] != ABSENT:
                cell = f"`{it['old'][:10]}` → `{it['new'][:10]}`"
            links = " · ".join(f"[{c['sha']}]({c['url']}) {c['msg']}" for c in it["commits"]) or "—"
            lines.append(f"| `{it['path']}` | {cell} | {links} |")
        lines.append("")
    lines.append("---")
    lines.append("由 `.upstream-watch/` 每日自动监视生成。含义只有一条：**该去看了**，")
    lines.append("是否真的影响 PR / fork 需人工判断。确认处理完后关闭本 issue；")
    lines.append("之后再变化会新建 issue 或在本 issue 下追加评论。")
    return title, "\n".join(lines)


def find_open_issue(repo, label, token):
    lst = gh_get(f"{API}/repos/{repo}/issues?state=open&labels={quote(label)}&per_page=1", token)
    return lst[0] if lst else None


def deliver_alert(repo, vendor, title, body, token):
    """有开放 issue 则评论，否则新建。返回 issue 编号。"""
    label = vendor["issue"]["label"]
    existing = find_open_issue(repo, label, token)
    if existing:
        gh_post(f"{API}/repos/{repo}/issues/{existing['number']}/comments", token,
                {"body": f"## {title}\n\n{body}"})
        return existing["number"], "commented"
    created = gh_post(f"{API}/repos/{repo}/issues", token,
                      {"title": title, "body": body, "labels": [label]})
    return created["number"], "created"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--repo", required=True, help="接收报警的仓库，如 takomitkm/librime")
    ap.add_argument("--baseline", action="store_true", help="只建基线，不报警")
    ap.add_argument("--dry-run", action="store_true", help="不写盘不发包，报警打印到 stdout")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    with open(args.vendor, encoding="utf-8") as f:
        vendor = json.load(f)

    old_state = None
    if os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as f:
            old_state = json.load(f)
    first_run = not (old_state and old_state.get("upstreams"))

    new_state = {"_说明": "由 watch_upstream.py 自动维护", "upstreams": {}}
    changes = {}
    for u in vendor["upstreams"]:
        cur = fetch_upstream(u["name"], u["ref"], u["paths"], token)
        new_state["upstreams"][u["name"]] = {"ref": u["ref"], "tip": cur["tip"], "paths": cur["paths"]}
        prev = (old_state or {}).get("upstreams", {}).get(u["name"])
        if prev is None:
            continue  # 新加进清单的上游：本轮静默记档
        items = []
        for p in u["paths"]:
            old, new = prev.get("paths", {}).get(p), cur["paths"][p]
            if old is None or old == new:
                continue  # 新加的路径：静默记档；未变：跳过
            items.append({"path": p, "old": old, "new": new,
                          "commits": commits_touching(u["name"], u["ref"], p, token)})
        if items:
            changes[u["name"]] = {"ref": u["ref"], "reason": u.get("reason", ""),
                                  "old_tip": prev.get("tip", "?"), "new_tip": cur["tip"], "items": items}

    if changes and not args.baseline:
        title, body = build_alert(vendor, changes)
        if args.dry_run:
            print(f"=== DRY RUN: 以下内容本应发出 ===\nTITLE: {title}\n\n{body}")
        else:
            num, how = deliver_alert(args.repo, vendor, title, body, token)
            print(f"alert {how}: issue #{num} ({', '.join(changes)})")

    if not args.dry_run:
        with open(args.state, "w", encoding="utf-8", newline="\n") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"state written: {len(new_state['upstreams'])} upstreams, "
              f"first_run={first_run}, changes={len(changes)}")
    else:
        print(f"dry-run done: first_run={first_run}, changes={len(changes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
