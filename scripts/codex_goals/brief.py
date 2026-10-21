"""Build the handoff brief for one Codex goal.

``collect`` extracts everything deterministically (no summarisation, no
guessing). ``select`` trims it to a size level and counts what was left out.
``render_markdown`` turns the selection into the document Claude reads.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from . import history, store
from .paths import is_within, pretty, to_local
from .redact import redact, redact_obj

STEP_MARK = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}

# Size levels, from generous to tight. ``select`` walks them until the brief fits.
LEVELS = [
    dict(ctx_msgs=3, ctx_chars=1500, files=40, failed=10, fail_lines=12, agent_msgs=3,
         agent_chars=2500, user_chars=1500, subagents=8, sub_chars=500, git_log=20, git_files=25,
         step_chars=300, errors=5),
    dict(ctx_msgs=3, ctx_chars=900, files=30, failed=8, fail_lines=8, agent_msgs=2,
         agent_chars=1500, user_chars=900, subagents=6, sub_chars=300, git_log=15, git_files=15,
         step_chars=200, errors=4),
    dict(ctx_msgs=2, ctx_chars=600, files=20, failed=6, fail_lines=5, agent_msgs=2,
         agent_chars=1000, user_chars=600, subagents=5, sub_chars=200, git_log=10, git_files=10,
         step_chars=160, errors=3),
    dict(ctx_msgs=1, ctx_chars=400, files=15, failed=4, fail_lines=3, agent_msgs=1,
         agent_chars=800, user_chars=400, subagents=4, sub_chars=150, git_log=8, git_files=8,
         step_chars=120, errors=2),
    dict(ctx_msgs=1, ctx_chars=250, files=10, failed=3, fail_lines=2, agent_msgs=1,
         agent_chars=500, user_chars=250, subagents=3, sub_chars=100, git_log=5, git_files=5,
         step_chars=100, errors=1),
]
FULL = {k: None for k in LEVELS[0]}


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _clip(text, limit):
    text = (text or "").strip()
    if limit is None or len(text) <= limit:
        return text, 0
    return text[:limit].rstrip() + " …", len(text) - limit


def _tail_lines(text, n):
    lines = (text or "").rstrip().splitlines()
    if n is None or len(lines) <= n:
        return lines, 0
    return lines[-n:], len(lines) - n


def _take_last(seq, n):
    if n is None or len(seq) <= n:
        return list(seq), 0
    return list(seq[-n:]), len(seq) - n


def _take_first(seq, n):
    if n is None or len(seq) <= n:
        return list(seq), 0
    return list(seq[:n]), len(seq) - n


def human_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min" % m
    return "%d s" % seconds


def human_tokens(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000)
    if n >= 1000:
        return "%dk" % round(n / 1000)
    return str(n)


def ago(ms, now=None) -> str:
    if not ms:
        return "—"
    delta = int((now or time.time()) - ms / 1000)
    if delta < 0:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("min", 60)):
        if delta >= size:
            n = delta // size
            return "%d %s%s ago" % (n, unit, "s" if n > 1 and unit != "min" else "")
    return "just now"


# ---------------------------------------------------------------- git

def _git(cwd, *args, timeout=10):
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").rstrip("\n")


def git_context(cwd, sha, branch) -> dict:
    out = {"available": False}
    local = to_local(cwd)
    if not local or not os.path.isdir(local):
        out["reason"] = "project directory not found on this machine"
        return out
    if _git(local, "rev-parse", "--is-inside-work-tree") != "true":
        out["reason"] = "not a git repository"
        return out
    out["available"] = True
    out["branch"] = _git(local, "rev-parse", "--abbrev-ref", "HEAD")
    out["head"] = _git(local, "rev-parse", "--short", "HEAD")
    out["recorded_branch"] = branch
    out["recorded_sha"] = sha
    out["branch_changed"] = bool(branch and out["branch"] and branch != out["branch"])
    status = _git(local, "status", "--porcelain")
    out["uncommitted"] = len(status.splitlines()) if status else 0
    if not sha:
        out["sha_exists"] = None
        return out
    out["sha_exists"] = _git(local, "cat-file", "-e", sha + "^{commit}") is not None
    if not out["sha_exists"]:
        return out
    log = _git(local, "log", "--oneline", "--no-decorate", sha + "..HEAD")
    out["commits"] = log.splitlines() if log else []
    stat = _git(local, "diff", "--stat", sha)
    lines = stat.splitlines() if stat else []
    out["diff_summary"] = lines[-1].strip() if lines else "no changes"
    out["diff_files"] = [l.strip() for l in lines[:-1]]
    return out


def _git_root(path):
    d = to_local(path)
    if not d:
        return None
    d = d if os.path.isdir(d) else os.path.dirname(d)
    for _ in range(40):
        if not d:
            return None
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def other_repos(cwd, paths, since_ms, limit=3) -> list:
    """Git repos outside the project where Codex edited files during the goal."""
    project_root = _git_root(cwd) if cwd else None
    counts = {}
    for p in paths:
        if not os.path.isabs(to_local(p) or ""):
            continue
        root = _git_root(p)
        if root and root != project_root and not is_within(root, cwd or "\0"):
            counts[root] = counts.get(root, 0) + 1
    out = []
    for root, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if len(out) >= limit:
            break
        head = _git(root, "rev-parse", "--short", "HEAD")
        if not head:
            continue  # not a usable repository (e.g. a stray .git in /tmp)
        info = {"path": root, "files_edited": n,
                "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"), "head": head}
        status = _git(root, "status", "--porcelain")
        info["uncommitted"] = len(status.splitlines()) if status else 0
        if since_ms:
            log = _git(root, "log", "--oneline", "--no-decorate", "--all",
                       "--since=@%d" % (since_ms // 1000))
            info["commits"] = log.splitlines() if log else []
        upstream = _git(root, "rev-list", "--count", "@{upstream}..HEAD")
        info["unpushed"] = int(upstream) if upstream and upstream.isdigit() else None
        out.append(info)
    return out


# ---------------------------------------------------------------- collect

def _thread_summary(events) -> dict:
    cmds = [e for e in events if e["kind"] == "command"]
    files = []
    for e in events:
        if e["kind"] == "file_change":
            files.extend(p for p, _ in e["changes"])
    agents = [e["text"] for e in events if e["kind"] == "agent" and e["text"].strip()]
    return {"commands": len(cmds), "files": sorted(set(files)),
            "last_agent": agents[-1] if agents else ""}


_WRAPPER = re.compile(r"""^\S*(?:bash|sh|zsh|pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+)*-l?c(?:ommand)?\s+(['"])(.*)\1\s*$""", re.S | re.I)
# Exit code 1 from these only means "nothing found / condition false".
_PROBES = {"rg", "grep", "egrep", "fgrep", "test", "[", "[[", "command", "which", "type", "diff",
           "cmp", "find", "ls", "cat", "sed", "head", "tail", "wc", "stat", "file", "pgrep", "git"}


def unwrap(cmd: str) -> str:
    """``/usr/bin/bash -lc 'pytest -q'`` -> ``pytest -q``."""
    m = _WRAPPER.match(cmd or "")
    if not m:
        return cmd or ""
    inner = m.group(2)
    if m.group(1) == "'":
        inner = inner.replace("'\\''", "'")
    return inner


def _failed(e) -> bool:
    if e.get("status") in ("failed", "declined"):
        return True
    code = e.get("exit_code")
    return code is not None and code != 0


def _is_probe(cmd: str, code) -> bool:
    """A search/inspection command whose non-zero exit is not a real failure."""
    if code != 1:
        return False
    first = re.split(r"\s+", cmd.strip(), 1)[0] if cmd.strip() else ""
    first = os.path.basename(first)
    return first in _PROBES


def collect(goal, threads, use_git=True) -> dict:
    """Extract every fact the brief may use. ``threads`` maps id -> state row."""
    home = goal.home
    trow = threads.get(goal.thread_id) or {}
    hist = history.load_history(home, goal.thread_id, trow)
    ev = hist.events
    meta = hist.meta
    cwd = trow.get("cwd") or meta.get("cwd")
    git_meta = meta.get("git") or {}
    sha = trow.get("git_sha") or git_meta.get("commit_hash")
    branch = trow.get("git_branch") or git_meta.get("branch")

    users = [e["text"] for e in ev if e["kind"] == "user" and e["text"].strip()]
    agents = [e["text"] for e in ev if e["kind"] == "agent" and e["text"].strip()]
    plans = [e for e in ev if e["kind"] == "plan"]
    cmds = []
    for e in ev:
        if e["kind"] == "command":
            cmds.append(dict(e, command=unwrap(e["command"])))
    failed = [e for e in cmds if _failed(e)]
    real_failed = [e for e in failed if not _is_probe(e["command"], e.get("exit_code"))]
    # Keep the most recent failure of each distinct command, and drop it if the
    # same command succeeded afterwards.
    last_ok = {}
    for i, e in enumerate(cmds):
        if not _failed(e):
            last_ok[e["command"]] = i
    index = {id(e): i for i, e in enumerate(cmds)}
    seen, failed_unique = set(), []
    for e in reversed(real_failed):
        c = e["command"]
        if c in seen or last_ok.get(c, -1) > index[id(e)]:
            continue
        seen.add(c)
        failed_unique.append(e)
    failed_unique.reverse()

    file_order, file_kinds = [], {}
    edits = 0
    for e in ev:
        if e["kind"] != "file_change" or e.get("status") in ("failed", "declined"):
            continue
        edits += 1
        for path, kind in e["changes"]:
            if path in file_kinds:
                file_order.remove(path)
            file_order.append(path)
            file_kinds.setdefault(path, set()).add(kind)
    files = [{"path": p, "kinds": sorted(file_kinds[p])} for p in reversed(file_order)]

    # Subagents: spawn edges first, then receivers named by collab tool calls.
    children = []
    try:
        children = store.read_spawn_children(home, goal.thread_id)
    except store.ReadError as exc:
        hist.warnings.append(str(exc))
    child_ids = [c["child"] for c in children]
    prompts = {}
    for e in ev:
        if e["kind"] == "subagent":
            for r in e.get("receivers") or []:
                prompts.setdefault(r, e.get("prompt") or "")
                if r not in child_ids:
                    child_ids.append(r)
                    children.append({"parent": goal.thread_id, "child": r, "status": e.get("status")})
    child_rows = store.read_threads(home, child_ids) if child_ids else {}
    subagents = []
    for c in children:
        row = child_rows.get(c["child"]) or {}
        sub_hist = history.load_history(home, c["child"], row) if row or c["child"] else None
        summary = _thread_summary(sub_hist.events) if sub_hist else {}
        subagents.append({
            "thread_id": c["child"], "status": c.get("status"),
            "nickname": row.get("agent_nickname"), "role": row.get("agent_role"),
            "prompt": prompts.get(c["child"]) or row.get("first_user_message") or "",
            "commands": summary.get("commands", 0), "files": summary.get("files", []),
            "last_message": summary.get("last_agent", ""),
        })

    goal_calls = [e for e in ev if e["kind"] == "goal_tool"]
    errors = [e["text"] for e in ev if e["kind"] in ("error", "tool_error") and e.get("text")]
    try:
        for t in store.read_turn_errors(home, goal.thread_id):
            if t.get("error_json"):
                errors.append("turn %s: %s" % (t.get("status") or "", t["error_json"]))
    except store.ReadError:
        pass

    # update_goal only carries a status; the explanation is the agent message
    # that follows the last update_goal(status=blocked) call.
    block_reason = None
    for e in reversed(goal_calls):
        args = e.get("arguments")
        text = args if isinstance(args, str) else str(args or "")
        if "blocked" in text:
            block_reason = e.get("followup") or None
            break

    data = {
        "goal": goal.to_dict(),
        "thread": {
            "id": goal.thread_id, "cwd": cwd, "branch": branch, "sha": sha,
            "title": trow.get("title"), "model": trow.get("model"),
            "cli_version": trow.get("cli_version"), "history_mode": trow.get("history_mode"),
            "forked_from_id": meta.get("forked_from_id"),
            "parent_thread_id": meta.get("parent_thread_id"),
        },
        "source": {"codex_home": home.path, "history": hist.sources,
                   "warnings": list(home.warnings) + hist.warnings},
        "user_messages": users,
        "agent_messages": agents,
        "plan": ({"steps": plans[-1]["steps"], "explanation": plans[-1]["explanation"],
                  "updates": len(plans)} if plans else None),
        "proposed_plan": next((e["text"] for e in reversed(ev) if e["kind"] == "proposed_plan"), None),
        "commands_total": len(cmds),
        "failed_commands": failed_unique,
        "failed_total": len(failed),
        "probe_failures": len(failed) - len(real_failed),
        "files": files,
        "file_edits": edits,
        "subagents": subagents,
        "goal_tool_calls": goal_calls,
        "errors": errors,
        "block_reason": block_reason,
        "compactions": sum(1 for e in ev if e["kind"] == "compaction"),
        "git": git_context(cwd, sha, branch) if use_git else {"available": False,
                                                               "reason": "--no-git"},
        "other_repos": other_repos(cwd, [f["path"] for f in files], goal.created_at_ms)
        if use_git else [],
    }
    return data


# ---------------------------------------------------------------- select

def select(data, lv) -> dict:
    """Apply a size level. Returns the trimmed view plus an ``omitted`` tally."""
    om = {"messages": 0, "output_lines": 0, "chars": 0, "files": 0, "failed_commands": 0,
          "subagents": 0, "commits": 0, "diff_files": 0, "errors": 0}
    users = data["user_messages"]
    ctx, _ = _take_first(users, lv["ctx_msgs"])
    context = []
    for t in ctx:
        c, n = _clip(t, lv["ctx_chars"])
        om["chars"] += n
        context.append(c)
    last_user = None
    if len(users) > len(ctx):
        last_user, n = _clip(users[-1], lv["user_chars"])
        om["chars"] += n
    shown_users = len(ctx) + (1 if last_user else 0)
    om["messages"] += max(0, len(users) - shown_users)

    last_agents_raw, _ = _take_last(data["agent_messages"], lv["agent_msgs"])
    last_agents = []
    for t in last_agents_raw:
        c, n = _clip(t, lv["agent_chars"])
        om["chars"] += n
        last_agents.append(c)
    om["messages"] += max(0, len(data["agent_messages"]) - len(last_agents))

    plan = None
    if data["plan"]:
        steps = []
        for s, st in data["plan"]["steps"]:
            c, n = _clip(s, lv["step_chars"])
            om["chars"] += n
            steps.append((c, st))
        plan = dict(data["plan"], steps=steps)

    files, om["files"] = _take_first(data["files"], lv["files"])
    failed_raw, om["failed_commands"] = _take_last(data["failed_commands"], lv["failed"])
    failed = []
    for e in failed_raw:
        cmd, n = _clip(e["command"], 300 if lv["fail_lines"] is not None else None)
        om["chars"] += n
        lines, dropped = _tail_lines(e.get("output"), lv["fail_lines"])
        om["output_lines"] += dropped
        failed.append({"command": cmd, "exit_code": e.get("exit_code"),
                       "status": e.get("status"), "output": lines})
    subs_raw, om["subagents"] = _take_first(data["subagents"], lv["subagents"])
    subs = []
    for s in subs_raw:
        p, n1 = _clip(s["prompt"], lv["sub_chars"])
        m, n2 = _clip(s["last_message"], lv["sub_chars"])
        om["chars"] += n1 + n2
        subs.append(dict(s, prompt=p, last_message=m))
    errors, om["errors"] = _take_last(data["errors"], lv["errors"])
    errors = [_clip(e, 400 if lv["errors"] is not None else None)[0] for e in errors]
    git = dict(data["git"])
    if git.get("commits") is not None:
        git["commits"], om["commits"] = _take_last(git["commits"], lv["git_log"])
    if git.get("diff_files") is not None:
        git["diff_files"], om["diff_files"] = _take_first(git["diff_files"], lv["git_files"])
    block = data["block_reason"]
    if block:
        block, n = _clip(block, 800 if lv["errors"] is not None else None)
        om["chars"] += n
    return {
        "goal": data["goal"], "thread": data["thread"], "source": data["source"],
        "context": context, "last_user_message": last_user, "last_agent_messages": last_agents,
        "user_message_count": len(users), "agent_message_count": len(data["agent_messages"]),
        "plan": plan, "commands_total": data["commands_total"],
        "failed_total": data["failed_total"], "failed_commands": failed,
        "files": files, "files_total": len(data["files"]), "file_edits": data["file_edits"],
        "subagents": subs, "subagents_total": len(data["subagents"]),
        "goal_tool_calls": len(data["goal_tool_calls"]), "errors": errors,
        "block_reason": block, "compactions": data["compactions"], "git": git,
        "probe_failures": data.get("probe_failures", 0),
        "proposed_plan": (_clip(data.get("proposed_plan"), lv["agent_chars"])[0]
                          if data.get("proposed_plan") else None),
        "other_repos": [dict(r, commits=_take_last(r.get("commits") or [], lv["git_log"])[0])
                        for r in data.get("other_repos") or []],
        "omitted": om,
    }


# ---------------------------------------------------------------- render

def _rel(path, cwd) -> str:
    if cwd and is_within(path, cwd):
        rest = path.replace("\\", "/")[len(cwd.replace("\\", "/").rstrip("/")) + 1:]
        return rest or "."
    return pretty(path)


def _quote(text) -> str:
    return "\n".join("> " + l if l.strip() else ">" for l in (text or "").splitlines())


def render_markdown(sel, now=None) -> str:
    g, t = sel["goal"], sel["thread"]
    L = []
    L.append("# Codex goal %s · %s" % (g["thread_id"][:8], g["status"]))
    proj = pretty(t["cwd"]) or "(unknown)"
    head = "Project: %s" % proj
    if t.get("branch") or t.get("sha"):
        head += " · %s @ %s" % (t.get("branch") or "?", (t.get("sha") or "?")[:7])
    head += " · source: %s" % pretty(sel["source"]["codex_home"])
    L.append(head)
    budget = human_tokens(g.get("token_budget")) if g.get("token_budget") else "—"
    L.append("Usage: %s tokens (budget: %s) · %s · updated %s"
             % (human_tokens(g.get("tokens_used")), budget,
                human_duration(g.get("time_used_seconds")), ago(g.get("updated_at_ms"), now)))
    L.append("Thread: %s%s" % (g["thread_id"], " · history: " + ", ".join(sel["source"]["history"])
                               if sel["source"]["history"] else ""))
    if t.get("forked_from_id"):
        L.append("Forked from thread %s" % t["forked_from_id"])
    for w in sel["source"]["warnings"]:
        L.append("Warning: %s" % w)

    L.append("")
    L.append("## Objective")
    L.append(g["objective"].strip())
    if sel["context"]:
        L.append("")
        L.append("Context (first user messages):")
        for i, c in enumerate(sel["context"], 1):
            L.append("%d." % i)
            L.append(_quote(c))

    L.append("")
    if sel["plan"]:
        p = sel["plan"]
        L.append("## Codex plan (last update_plan, %d update%s)"
                 % (p["updates"], "" if p["updates"] == 1 else "s"))
        if p.get("explanation"):
            L.append("Explanation: %s" % p["explanation"].strip())
        current_marked = False
        for i, (step, st) in enumerate(p["steps"], 1):
            line = "%s %d. %s" % (STEP_MARK.get(st, "[?]"), i, step)
            if st == "in_progress" and not current_marked:
                line += "  ← in progress when it stopped"
                current_marked = True
            L.append(line)
        done = sum(1 for _, st in p["steps"] if st == "completed")
        L.append("(%d of %d steps completed)" % (done, len(p["steps"])))
    elif sel.get("proposed_plan"):
        L.append("## Codex plan (last proposed plan)")
        L.append(_quote(sel["proposed_plan"]))
    else:
        L.append("## Codex plan")
        L.append("No update_plan call recorded. If the objective refers to a plan, it is probably "
                 "a file in the repo (check the context messages).")

    L.append("")
    L.append("## What it did")
    L.append("Commands run: %d (%d non-zero exits, %d of them searches that found nothing) · "
             "file edits: %d across %d files · context compactions: %d"
             % (sel["commands_total"], sel["failed_total"], sel.get("probe_failures", 0),
                sel["file_edits"], sel["files_total"], sel["compactions"]))
    if sel["files"]:
        L.append("Files changed (most recent first; relative to the project unless absolute):")
        for f in sel["files"]:
            kinds = "/".join(k for k in f["kinds"] if k != "update")
            L.append("- %s%s" % (_rel(f["path"], t.get("cwd")), " (%s)" % kinds if kinds else ""))
    if sel["failed_commands"]:
        L.append("")
        L.append("Failed commands (most recent failure of each, oldest first):")
        for c in sel["failed_commands"]:
            code = c["exit_code"] if c["exit_code"] is not None else c["status"]
            L.append("- `%s` → exit %s" % (c["command"].replace("`", "'").replace("\n", " ⏎ "), code))
            for line in c["output"]:
                L.append("    | " + line)
    if sel["subagents"]:
        L.append("")
        L.append("Subagents: %d" % sel["subagents_total"])
        for s in sel["subagents"]:
            who = s.get("nickname") or s["thread_id"][:8]
            if s.get("role"):
                who += " (%s)" % s["role"]
            L.append("- %s [%s] · %d commands · %d files" % (who, s.get("status") or "?",
                                                          s["commands"], len(s["files"])))
            if s["prompt"]:
                L.append("  task: " + s["prompt"].replace("\n", " "))
            if s["last_message"]:
                L.append("  last message: " + s["last_message"].replace("\n", " "))

    L.append("")
    L.append("## Where it stopped")
    L.append("Goal status in Codex: %s" % g["status"])
    if sel["block_reason"]:
        L.append("Agent message right after it marked the goal blocked:")
        L.append(_quote(sel["block_reason"]))
    elif g["status"] == "blocked":
        L.append("Block reason: not recorded explicitly; %s." % (
            "read the last agent message below" if sel["last_agent_messages"]
            else "Codex left no agent message, ask the user"))
    if sel["last_user_message"]:
        L.append("")
        L.append("Last user message:")
        L.append(_quote(sel["last_user_message"]))
    shown_agents = [m for m in sel["last_agent_messages"] if m != sel["block_reason"]]
    if shown_agents:
        L.append("")
        L.append("%s agent message%s (oldest first):" % ("Earlier" if sel["block_reason"] else "Last",
                                                        "s" if len(shown_agents) > 1 else ""))
        for m in shown_agents:
            L.append(_quote(m))
            L.append("")
        L.pop()
    if sel["errors"]:
        L.append("")
        L.append("Errors reported:")
        for e in sel["errors"]:
            L.append("- " + e.replace("\n", " "))

    L.append("")
    L.append("## Repo changes since the goal started")
    gi = sel["git"]
    if not gi.get("available"):
        L.append("Not available: %s." % gi.get("reason", "unknown"))
    else:
        if gi.get("head"):
            L.append("Current branch: %s @ %s · uncommitted changes: %d"
                     % (gi.get("branch"), gi.get("head"), gi.get("uncommitted", 0)))
        else:
            L.append("The repository has no commits yet · uncommitted changes: %d" % gi.get("uncommitted", 0))
        if gi.get("branch_changed"):
            L.append("Warning: Codex worked on branch %s, the repo is now on %s."
                     % (gi.get("recorded_branch"), gi.get("branch")))
        if gi.get("sha_exists") is False:
            L.append("Warning: the starting commit %s no longer exists in this repo."
                     % (gi.get("recorded_sha") or "")[:7])
        elif gi.get("sha_exists"):
            n = len(gi.get("commits") or [])
            L.append("%d new commit%s since %s · %s" % (n, "" if n == 1 else "s",
                                                       gi["recorded_sha"][:7], gi.get("diff_summary")))
            for c in gi.get("commits") or []:
                L.append("- " + c)
            if gi.get("diff_files"):
                L.append("Changed files vs %s:" % gi["recorded_sha"][:7])
                for f in gi["diff_files"]:
                    L.append("    " + f)

    for r in sel.get("other_repos") or []:
        L.append("")
        L.append("Codex also edited %d file%s in %s (%s @ %s · uncommitted: %d%s)"
                 % (r["files_edited"], "" if r["files_edited"] == 1 else "s", pretty(r["path"]),
                    r.get("branch") or "?", r.get("head") or "?", r.get("uncommitted", 0),
                    " · unpushed: %d" % r["unpushed"] if r.get("unpushed") else ""))
        if r.get("commits"):
            L.append("Commits since the goal started (all branches):")
            for c in r["commits"]:
                L.append("- " + c)

    om = sel["omitted"]
    parts = []
    labels = [("messages", "messages"), ("output_lines", "lines of command output"),
              ("chars", "characters of long texts"), ("files", "changed files"),
              ("failed_commands", "failed commands"), ("subagents", "subagents"),
              ("commits", "commits"), ("diff_files", "diff entries"), ("errors", "errors")]
    for key, label in labels:
        if om.get(key):
            parts.append("%d %s" % (om[key], label))
    L.append("")
    L.append("## Omitted")
    if parts:
        L.append(", ".join(parts) + " (use --full for everything).")
    else:
        L.append("Nothing.")
    return redact("\n".join(L)) + "\n"


def build(data, max_tokens=8000, full=False, now=None) -> tuple:
    """Return ``(markdown, selection)`` that fits ``max_tokens`` (unless ``full``)."""
    data = redact_obj(data)
    if full:
        sel = select(data, FULL)
        return render_markdown(sel, now), sel
    sel = md = None
    for lv in LEVELS:
        sel = select(data, lv)
        md = render_markdown(sel, now)
        if estimate_tokens(md) <= max_tokens:
            break
    return md, sel
