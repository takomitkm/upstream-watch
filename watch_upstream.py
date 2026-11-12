#!/usr/bin/env python3
"""upstream-watch —— 盯上游仓库指定路径的内容变化，变化则开/评论 GitHub issue。

v1.2 新增: ①name/repo 解耦（可同仓库多 ref、自定义 label）；
           ②release notes 进报警；
           ③webhook 通知（URL 走环境变量/Secret，best-effort）；
           ④冲突预演报告嵌入（由 conflict_preview.py 事先生成）；
           ⑤.gitmodules 子模块监视（钉住 commit 落后上游即报警）；
           ⑥报警成功写 marker 文件（供 action 触发冒烟测试）。

manifest 结构（全部字段可选除 upstreams[].repo|name 与 ref）:
{
  "issue": {"label": "upstream-watch", "title_prefix": "[upstream-watch] 上游有变化: "},
  "notify": [{"type": "webhook", "env": "UW_WEBHOOK_URL", "format": "slack|json"}],
  "discover": {"gitmodules": true},
  "upstreams": [
    {"label": "自定义显示名(默认=repo)", "repo": "owner/repo",   // 旧写法 "name": "owner/repo" 仍兼容
     "ref": "main", "release": true,
     "conflict_branch": "分支名", "conflict_repo": "分支所在仓库(默认=消费仓)",
     "reason": "…", "paths": ["a/b.cc", "submodule-dir"]}
  ]
}

设计要点:
- 只用 Python 标准库；HTTP 一律走 curl 子进程；git 只用于只读（ls-files 读 index）。
- state.json 记录 blob sha / tip / release / 子模块 pin；首跑只建基线、不报警。
- 同一时刻最多一个带标签的开放 issue，有则评论、无则新建。
- state 在报警成功之后才写盘：宁可下次重复提醒，不可静默丢失。

用法:
  python3 watch_upstream.py --vendor vendor.json --state state.json --repo OWNER/REPO
      [--baseline] [--dry-run]
      [--workspace DIR]               # 消费仓 checkout（读 .gitmodules）
      [--conflict-report PATH]        # conflict_preview.py 的产物（有则嵌入报警）
      [--alerted-marker PATH]         # 报警成功后写该文件（action 触发冒烟用）
环境变量: GITHUB_TOKEN；notify 里各 env（如 UW_WEBHOOK_URL）

退出码: 0 = 正常，非 0 = API/网络错误（CI 里直接红）。
"""
import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote

API = "https://api.github.com"
ABSENT = "ABSENT"
PATCH_MAX_LINES = 40
RELEASE_NOTES_MAX_CHARS = 1200

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


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

def fetch_release(repo, token):
    """最新 release（GitHub 语义：非 draft/prerelease）。没有 release 返回 None。
    notes 只进报警不进 state（state 只存 tag/url/published，避免膨胀）。"""
    try:
        r = gh_get(f"{API}/repos/{repo}/releases/latest", token)
    except RuntimeError as exc:
        if "404" in str(exc):
            return None
        raise
    return {"tag": r.get("tag_name", ""),
            "url": r.get("html_url", ""),
            "published": (r.get("published_at") or "")[:10],
            "notes": (r.get("body") or "")[:RELEASE_NOTES_MAX_CHARS]}


def rel_key(rel):
    """用于新旧比较的子集（不含 notes）。"""
    return None if rel is None else {k: rel.get(k) for k in ("tag", "url", "published")}


def default_branch(repo, token):
    try:
        return gh_get(f"{API}/repos/{repo}", token).get("default_branch") or "main"
    except RuntimeError:
        return "main"


def fetch_upstream(repo, ref, paths, want_release, token):
    tip = gh_get(f"{API}/repos/{repo}/commits/{ref}", token)
    tip_sha = tip["sha"]
    tree = gh_get(f"{API}/repos/{repo}/git/trees/{tip_sha}?recursive=1", token)
    if tree.get("truncated"):
        entries = None
    else:
        entries = {e["path"]: e["sha"] for e in tree.get("tree", [])
                   if e["type"] in ("blob", "commit")}
    result = {}
    for p in paths:
        if entries is not None:
            result[p] = entries.get(p, ABSENT)
        else:
            try:
                c = gh_get(f"{API}/repos/{repo}/contents/{quote(p, safe='/')}?ref={quote(ref, safe='')}", token)
                result[p] = c["sha"]
            except RuntimeError as exc:
                if "404" in str(exc):
                    result[p] = ABSENT
                else:
                    raise
    out = {"tip": tip_sha, "paths": result}
    if want_release:
        out["release"] = fetch_release(repo, token)
    return out


def fetch_compare(repo, old_tip, new_tip, token):
    try:
        c = gh_get(f"{API}/repos/{repo}/compare/{old_tip}...{new_tip}", token)
    except RuntimeError as exc:
        print(f"  compare {old_tip[:7]}...{new_tip[:7]} 不可用（{str(exc)[:80]}），报警降级为无 patch")
        return None, {}
    files = c.get("files") or []
    if len(files) >= 300:
        print("  compare 文件数达上限，patch 可能不全")
    return c.get("html_url"), {f["filename"]: f for f in files}


def commits_touching(repo, ref, path, token, limit=3):
    try:
        lst = gh_get(f"{API}/repos/{repo}/commits?sha={quote(ref, safe='')}"
                     f"&path={quote(path, safe='/')}&per_page={limit}", token)
    except RuntimeError:
        return []
    out = []
    for c in lst:
        msg = (c.get("commit", {}).get("message") or "").splitlines()[0][:80]
        out.append({"sha": c["sha"][:10], "msg": msg, "url": c["html_url"]})
    return out


# ---------------------------------------------------------------- 子模块发现

def parse_gitmodules(workspace):
    """解析消费仓 .gitmodules → [{path, url, branch}]。没有就返回 []。"""
    gm = os.path.join(workspace or ".", ".gitmodules")
    if not os.path.exists(gm):
        return []
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(gm, encoding="utf-8")
    out = []
    for sec in cp.sections():
        if not cp.has_option(sec, "path") or not cp.has_option(sec, "url"):
            continue
        out.append({"path": cp.get(sec, "path").strip(),
                    "url": cp.get(sec, "url").strip(),
                    "branch": (cp.get(sec, "branch", fallback="").strip() or "")})
    return out


def url_to_repo(url):
    """git URL → owner/repo（支持 https/ssh 形态）。识别不了返回 None。"""
    url = url.strip()
    m = re.match(r"https://[^/]+/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def pinned_sha(workspace, path):
    """从 index 读 submodule 指针（gitlink, mode 160000）。checkout 是否浅克隆无所谓。"""
    proc = subprocess.run(["git", "ls-files", "-s", "--", path], cwd=workspace or ".",
                          capture_output=True, text=True, encoding="utf-8")
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "160000":
            return parts[1]
    return None


def fetch_submodules(manifest, workspace, token):
    """按 .gitmodules 汇总被监视子模块 → [{path, repo, branch, pin}]。"""
    if not (manifest.get("discover") or {}).get("gitmodules"):
        return []
    out = []
    for sm in parse_gitmodules(workspace):
        repo = url_to_repo(sm["url"])
        if not repo:
            print(f"  discover: 跳过无法识别的子模块 URL {sm['url'][:60]}")
            continue
        branch = sm["branch"] or default_branch(repo, token)
        pin = pinned_sha(workspace, sm["path"])
        if not pin:
            print(f"  discover: index 里没有 {sm['path']} 的 gitlink，跳过")
            continue
        out.append({"path": sm["path"], "repo": repo, "branch": branch, "pin": pin})
    return out


# ---------------------------------------------------------------- 报警

def _sha_cell(old, new):
    if old == ABSENT and new == ABSENT:
        return "（不存在）"
    if old == ABSENT:
        return "新出现"
    if new == ABSENT:
        return "（已被删除）"
    return f"`{old[:10]}` → `{new[:10]}`"


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，原文 {len(text)} 字符）"


def build_alert(vendor, changes, sub_changes, conflict_map):
    names = [label for label, _u in changes.items()] + (["子模块"] if sub_changes else [])
    title = vendor["issue"]["title_prefix"] + "、".join(names)
    lines = []
    for u in changes.values():
        lines.append(f"## {u['_label']}（{u['ref']}，tip `{u['old_tip'][:10]}` → `{u['new_tip'][:10]}`）")
        lines.append("")
        if u.get("compare_url"):
            lines.append(f"[查看旧→新完整对比]({u['compare_url']})")
            lines.append("")
        lines.append(f"监视理由：{u['reason']}")
        lines.append("")
        if u["items"]:
            lines.append("| 路径 | blob sha 变化 | ± | 最近提交 |")
            lines.append("|---|---|---|---|")
            for it in u["items"]:
                stat = f"+{it['additions']} −{it['deletions']}" \
                    if it.get("additions") is not None else "—"
                links = " · ".join(f"[{c['sha']}]({c['url']}) {c['msg']}" for c in it["commits"]) or "—"
                lines.append(f"| `{it['path']}` | {_sha_cell(it['old'], it['new'])} | {stat} | {links} |")
            lines.append("")
            for it in u["items"]:
                if it.get("patch"):
                    lines.append(f"<details><summary><code>{it['path']}</code> 的 patch</summary>")
                    lines.append("")
                    lines.append("```diff")
                    lines.append(_truncate_patch(it["patch"]))
                    lines.append("```")
                    lines.append("</details>")
                    lines.append("")
        if u.get("conflict_md"):
            lines.append(u["conflict_md"])
            lines.append("")
        rel = u.get("release")
        if rel:
            old_r, new_r = rel["old"], rel["new"]
            old_tag = old_r["tag"] if old_r else "（无）"
            lines.append(f"**上游发新版：** [{new_r['tag']}]({new_r['url']})"
                         f"（{new_r['published'] or '未知日期'}发布）{old_tag} → {new_r['tag']}")
            if new_r.get("notes"):
                lines.append("")
                lines.append("<details><summary>release notes</summary>")
                lines.append("")
                lines.append(_truncate(new_r["notes"], RELEASE_NOTES_MAX_CHARS))
                lines.append("")
                lines.append("</details>")
            lines.append("")
    if sub_changes:
        lines.append("## 子模块更新（来自消费仓 .gitmodules 自动发现）")
        lines.append("")
        lines.append("| 模块 | 仓库 | 钉在 | 上游 tip | 领先 | 对比 |")
        lines.append("|---|---|---|---|---|---|")
        for s in sub_changes:
            ahead = f"{s['ahead']}" if s.get("ahead") is not None else "—"
            cmp_url = s.get("compare_url") or "—"
            cmp_md = f"[对比]({cmp_url})" if cmp_url != "—" else "—"
            lines.append(f"| `{s['path']}` | {s['repo']} | `{s['pin'][:10]}` | "
                         f"`{s['tip'][:10]}` | {ahead} | {cmp_md} |")
        lines.append("")
        lines.append("含义：子模块指针落后于上游分支（有新提交可用），是否升级需人工决定。")
        lines.append("")
    lines.append("---")
    lines.append("由 `.upstream-watch/` 每日自动监视生成。含义只有一条：**该去看了**，")
    lines.append("是否真的影响 PR / fork 需人工判断。确认处理完后关闭本 issue；")
    lines.append("之后再变化会新建 issue 或在本 issue 下追加评论。")
    return title, "\n".join(lines)


def _truncate_patch(patch):
    lines = patch.splitlines()
    if len(lines) <= PATCH_MAX_LINES:
        return "\n".join(lines)
    return "\n".join(lines[:PATCH_MAX_LINES]) + f"\n…（截断，共 {len(lines)} 行，点 commit 链接看全文）"


def load_conflict_report(path):
    """读 conflict_preview.py 产物，返回 {label: markdown}。"""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return dict(re.findall(
        r'<!-- uw-conflict label="(.*?)" -->\s*(.*?)\s*<!-- /uw-conflict -->',
        text, re.S))


def find_open_issue(repo, label, token):
    lst = gh_get(f"{API}/repos/{repo}/issues?state=open&labels={quote(label)}&per_page=1", token)
    return lst[0] if lst else None


def deliver_alert(repo, vendor, title, body, token):
    label = vendor["issue"]["label"]
    existing = find_open_issue(repo, label, token)
    if existing:
        gh_post(f"{API}/repos/{repo}/issues/{existing['number']}/comments", token,
                {"body": f"## {title}\n\n{body}"})
        return existing["number"], "commented"
    created = gh_post(f"{API}/repos/{repo}/issues", token,
                      {"title": title, "body": body, "labels": [label]})
    return created["number"], "created"


def notify_webhooks(vendor, title, body):
    """best-effort 推 webhook；失败只打日志绝不抛错。URL 来自环境变量（Secret 传入）。"""
    for n in vendor.get("notify", []):
        if n.get("type") != "webhook":
            continue
        env_name = n.get("env", "")
        url = os.environ.get(env_name, "").strip() if env_name else ""
        if not url:
            print(f"  webhook: 环境变量 {env_name} 未设置，跳过")
            continue
        if not (url.startswith("https://") or url.startswith("http://")):
            print(f"  webhook: {env_name} 不是 http(s) URL，跳过")
            continue
        payload = ({"text": f"{title}\n\n{body}"} if n.get("format") == "slack"
                   else {"event": "upstream-watch", "title": title, "body": body})
        cmd = ["curl", "-sS", "--max-time", "15", "-X", "POST",
               "-H", "Content-Type: application/json",
               "--data-binary", json.dumps(payload, ensure_ascii=False),
               "-o", "/dev/null", "-w", "%{http_code}", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
            print(f"  webhook {env_name} -> HTTP {proc.stdout.strip() or 'ERR'}")
        except Exception as exc:  # noqa: BLE001 通知永不致命
            print(f"  webhook {env_name} 失败: {str(exc)[:80]}")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--repo", required=True, help="接收报警的仓库，如 takomitkm/librime")
    ap.add_argument("--baseline", action="store_true", help="只建基线，不报警")
    ap.add_argument("--dry-run", action="store_true", help="不写盘不发包，报警打印到 stdout")
    ap.add_argument("--workspace", default=".", help="消费仓 checkout 根（读 .gitmodules）")
    ap.add_argument("--conflict-report", default=None, help="conflict_preview.py 产物路径")
    ap.add_argument("--alerted-marker", default=None, help="报警成功后写该文件")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    with open(args.vendor, encoding="utf-8") as f:
        vendor = json.load(f)

    # name/repo 解耦：旧 "name" 写法兼容
    for u in vendor.get("upstreams", []):
        u["_repo"] = u.get("repo") or u.get("name")
        u["_label"] = u.get("label") or u["_repo"]
        if not u["_repo"]:
            raise SystemExit(f"upstreams 条目缺 repo/name: {u!r}")

    old_state = None
    if os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as f:
            old_state = json.load(f)
    first_run = not (old_state and old_state.get("upstreams"))

    conflict_map = load_conflict_report(args.conflict_report)

    new_state = {"_说明": "由 watch_upstream.py 自动维护",
                 "upstreams": {}, "submodules": {}}
    changes = {}
    for u in vendor.get("upstreams", []):
        want_release = bool(u.get("release"))
        cur = fetch_upstream(u["_repo"], u["ref"], u["paths"], want_release, token)
        entry = {"ref": u["ref"], "tip": cur["tip"], "paths": cur["paths"]}
        if want_release:
            entry["release"] = rel_key(cur.get("release"))
        new_state["upstreams"][u["_label"]] = entry

        prev = (old_state or {}).get("upstreams", {}).get(u["_label"])
        if prev is None:
            continue  # 新加的上游：静默记档

        items = []
        for p in u["paths"]:
            old, new = prev.get("paths", {}).get(p), cur["paths"][p]
            if old is None or old == new:
                continue
            items.append({"path": p, "old": old, "new": new, "additions": None,
                          "deletions": None, "patch": None,
                          "commits": commits_touching(u["_repo"], u["ref"], p, token)})

        release_change = None
        prev_rel, cur_rel = prev.get("release"), cur.get("release")
        if want_release and prev_rel is not None and rel_key(cur_rel) != prev_rel:
            release_change = {"old": prev_rel, "new": cur_rel or {}}

        if items or release_change:
            compare_url, fmap = None, {}
            if items:
                compare_url, fmap = fetch_compare(u["_repo"], prev.get("tip", ""),
                                                  cur["tip"], token)
            for it in items:
                fobj = fmap.get(it["path"])
                if fobj:
                    it["additions"] = fobj.get("additions")
                    it["deletions"] = fobj.get("deletions")
                    it["patch"] = fobj.get("patch")
            changes[u["_label"]] = {"_label": u["_label"], "ref": u["ref"],
                                    "reason": u.get("reason", ""),
                                    "old_tip": prev.get("tip", "?"),
                                    "new_tip": cur["tip"],
                                    "compare_url": compare_url, "items": items,
                                    "release": release_change,
                                    "conflict_md": conflict_map.get(u["_label"])}

    # 子模块监视（gitmodules 自动发现）
    sub_changes = []
    if (vendor.get("discover") or {}).get("gitmodules"):
        mods = fetch_submodules(vendor, args.workspace, token)
        prev_subs = (old_state or {}).get("submodules", {})
        for m in mods:
            try:
                tip = gh_get(f"{API}/repos/{m['repo']}/commits/{m['branch']}", token)["sha"]
            except RuntimeError as exc:
                print(f"  submodule {m['path']}: 取上游 tip 失败（{str(exc)[:60]}）")
                continue
            prev = prev_subs.get(m["path"])
            seen_tip = (prev or {}).get("seen_tip")
            new_state["submodules"][m["path"]] = {
                "repo": m["repo"], "branch": m["branch"], "pin": m["pin"],
                "seen_tip": tip if (prev is None or (m["pin"] != tip and tip != seen_tip))
                else seen_tip}
            if prev is not None and m["pin"] != tip and tip != seen_tip:
                cmp_url = f"https://github.com/{m['repo']}/compare/{m['pin'][:10]}...{tip[:10]}"
                ahead = None
                try:
                    ahead = gh_get(f"{API}/repos/{m['repo']}/compare/{m['pin']}...{tip}",
                                   token).get("ahead_by")
                except RuntimeError:
                    pass
                sub_changes.append({**m, "tip": tip, "ahead": ahead, "compare_url": cmp_url})
                new_state["submodules"][m["path"]]["seen_tip"] = tip

    if (changes or sub_changes) and not args.baseline:
        title, body = build_alert(vendor, changes, sub_changes, conflict_map)
        if args.dry_run:
            print(f"=== DRY RUN: 以下内容本应发出 ===\nTITLE: {title}\n\n{body}")
        else:
            num, how = deliver_alert(args.repo, vendor, title, body, token)
            print(f"alert {how}: issue #{num} ({', '.join(list(changes) + ['子模块'] if sub_changes else list(changes))})")
            notify_webhooks(vendor, title, body)
            if args.alerted_marker:
                with open(args.alerted_marker, "w", encoding="utf-8") as f:
                    f.write(f"{num}\n")

    if not args.dry_run:
        with open(args.state, "w", encoding="utf-8", newline="\n") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"state written: {len(new_state['upstreams'])} upstreams, "
              f"{len(new_state['submodules'])} submodules, "
              f"first_run={first_run}, changes={len(changes)}+{len(sub_changes)}")
    else:
        print(f"dry-run done: first_run={first_run}, changes={len(changes)}+{len(sub_changes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
