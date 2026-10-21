"""Turn a Codex thread's history into a flat list of normalised events.

Sources, in order of preference:

1. ``thread_history_N.sqlite`` → ``thread_items`` (app-server ``ThreadItem`` JSON).
2. ``sessions/YYYY/MM/DD/rollout-*.jsonl`` (``RolloutLine`` JSON per line).
3. The same file compressed as ``rollout-*.jsonl.zst``.

``update_plan`` calls only exist in the rollout file, so it is always scanned
for plan updates and session metadata when present, even if (1) is used.

Every event is a dict with a ``kind`` key:

    user        text
    agent       text
    command     command, exit_code, status, output
    file_change changes=[(path, kind)], status
    plan        steps=[(step, status)], explanation
    goal_tool   name, arguments, followup (next agent message)
    subagent    tool, prompt, receivers, status
    tool_error  name, text
    error       text
    compaction
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import subprocess

from .paths import to_local


class HistoryResult:
    def __init__(self):
        self.events = []
        self.sources = []   # human-readable list of what was read
        self.warnings = []
        self.meta = {}      # from session_meta: forked_from_id, parent_thread_id, git...


# ---------------------------------------------------------------- zstd

def _zstd_reader(path: str):
    """Return a text stream over a .zst file, or raise RuntimeError explaining why not."""
    try:
        from compression import zstd as czstd  # Python 3.14+
        return io.TextIOWrapper(czstd.open(path, "rb"), encoding="utf-8", errors="replace")
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore
        fh = open(path, "rb")
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        return io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if exe:
        out = subprocess.run([exe, "-dc", path], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=False).stdout
        return io.StringIO(out.decode("utf-8", "replace"))
    raise RuntimeError("cannot read %s: needs Python 3.14+, the 'zstandard' package "
                       "or the 'zstd' command" % os.path.basename(path))


def open_rollout(path: str):
    if path.endswith(".zst"):
        return _zstd_reader(path)
    return open(path, "r", encoding="utf-8", errors="replace")


def find_rollout(home, thread_id: str, recorded: str | None) -> str | None:
    cands = []
    if recorded:
        local = to_local(recorded)
        cands += [local, local + ".zst"] if not local.endswith(".zst") else [local, local[:-4]]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    for d in home.session_dirs():
        for pattern in ("**/rollout-*%s.jsonl" % thread_id, "**/rollout-*%s.jsonl.zst" % thread_id):
            hits = glob.glob(os.path.join(glob.escape(d), pattern), recursive=True)
            if hits:
                return sorted(hits)[-1]
    return None


# ---------------------------------------------------------------- helpers

def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            t = c.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def _patch_paths(patch: str) -> list:
    out = []
    for m in re.finditer(r"^\*\*\* (Add|Update|Delete) File: (.+)$", patch or "", re.M):
        out.append((m.group(2).strip(), m.group(1).lower()))
    return out


def _kind_name(kind) -> str:
    if isinstance(kind, dict):
        return str(kind.get("type") or "update").lower()
    return str(kind or "update").lower()


def _cmd_str(command) -> str:
    if isinstance(command, list):
        # ["bash", "-lc", "real command"] -> "real command"
        if len(command) >= 3 and command[1] in ("-lc", "-c", "/c", "-Command"):
            return str(command[2])
        return " ".join(str(c) for c in command)
    return str(command or "")


def _plan_from_args(args) -> dict | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict) or not isinstance(args.get("plan"), list):
        return None
    steps = []
    for s in args["plan"]:
        if isinstance(s, dict):
            steps.append((str(s.get("step", "")), str(s.get("status", "pending"))))
    return {"kind": "plan", "steps": steps, "explanation": args.get("explanation") or ""}


# ---------------------------------------------------------------- items

def _get(item, *keys):
    for k in keys:
        if item.get(k) is not None:
            return item[k]
    return None


def _changes(raw) -> list:
    """FileChange changes: a list (app-server) or a {path: change} map (core)."""
    if isinstance(raw, dict):
        return [(k, _kind_name(v)) for k, v in raw.items()]
    return [(c.get("path", ""), _kind_name(c.get("kind"))) for c in raw or [] if isinstance(c, dict)]


def events_from_item(item: dict) -> list:
    """Normalise a ThreadItem (app-server, camelCase) or TurnItem (core, snake_case)."""
    t = str(item.get("type") or "")
    t = t[:1].lower() + t[1:]
    if t == "userMessage":
        text = _text_of_content(item.get("content"))
        return [{"kind": "user", "text": text}] if text.strip() else []
    if t == "agentMessage":
        text = item.get("text")
        if not isinstance(text, str):
            text = _text_of_content(item.get("content"))
        return [{"kind": "agent", "text": text or ""}]
    if t == "commandExecution":
        return [{"kind": "command", "command": _cmd_str(item.get("command")),
                 "exit_code": _get(item, "exitCode", "exit_code"),
                 "status": str(item.get("status") or "").lower() or None,
                 "output": _get(item, "aggregatedOutput", "aggregated_output") or ""}]
    if t == "fileChange":
        status = item.get("status")
        return [{"kind": "file_change", "changes": _changes(item.get("changes")),
                 "status": str(status).lower() if status else "completed"}]
    if t == "collabAgentToolCall":
        return [{"kind": "subagent", "tool": item.get("tool"), "prompt": item.get("prompt") or "",
                 "receivers": _get(item, "receiverThreadIds", "receiver_thread_ids") or [],
                 "status": item.get("status")}]
    if t in ("mcpToolCall", "dynamicToolCall"):
        tool = item.get("tool") or ""
        name = "%s.%s" % (item.get("server") or item.get("namespace") or "", tool)
        ev = []
        if "goal" in tool.lower():
            ev.append({"kind": "goal_tool", "name": name, "arguments": item.get("arguments"),
                       "followup": ""})
        err = item.get("error")
        if err or item.get("status") == "failed" or item.get("success") is False:
            msg = err.get("message") if isinstance(err, dict) else (err or "failed")
            ev.append({"kind": "tool_error", "name": name.strip("."), "text": str(msg)})
        return ev
    if t == "plan":
        return [{"kind": "proposed_plan", "text": item.get("text") or ""}]
    if t == "contextCompaction":
        return [{"kind": "compaction"}]
    return []


def load_thread_items(rows) -> list:
    events = []
    for r in rows:
        try:
            item = json.loads(r["item_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            events.extend(events_from_item(item))
    return events


# ---------------------------------------------------------------- rollout jsonl

# Events that only the rollout file records (never in thread_items).
ROLLOUT_ONLY = ("plan", "goal_tool", "error")


def parse_rollout(stream) -> tuple:
    """Return ``(events, meta)`` from a rollout stream.

    Newer rollouts record ``event_msg/item_completed`` with full TurnItems; older
    ones only have legacy events (``exec_command_end``, ``agent_message``...) and
    response items. Item-based events win when both exist, to avoid duplicates.
    """
    events, meta = [], {}
    pending_goal = None  # goal status change waiting for the agent message that explains it

    def add(ev, src):
        nonlocal pending_goal
        ev["src"] = src
        events.append(ev)
        if ev["kind"] == "goal_tool":
            pending_goal = ev
        elif ev["kind"] == "agent" and pending_goal is not None and ev["text"].strip():
            if not pending_goal.get("followup"):
                pending_goal["followup"] = ev["text"]
            pending_goal = None

    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        typ, p = rec.get("type"), rec.get("payload")
        if not isinstance(p, dict):
            continue
        if typ == "session_meta":
            if not meta:
                meta = {"id": p.get("id"), "cwd": p.get("cwd"),
                        "forked_from_id": p.get("forked_from_id"),
                        "parent_thread_id": p.get("parent_thread_id"),
                        "git": p.get("git") or {}, "timestamp": p.get("timestamp")}
            continue
        if typ == "response_item":
            pt = p.get("type")
            if pt in ("function_call", "custom_tool_call"):
                name = p.get("name") or ""
                args = p.get("arguments") or p.get("input")
                if name == "update_plan":
                    ev = _plan_from_args(args)
                    if ev:
                        add(ev, "both")
                elif name in ("update_goal", "create_goal"):
                    add({"kind": "goal_tool", "name": name, "arguments": args, "followup": ""}, "both")
            elif pt == "message":
                text = _text_of_content(p.get("content"))
                if p.get("role") == "user" and text.strip() and not text.lstrip().startswith("<"):
                    add({"kind": "user", "text": text}, "response")
                elif p.get("role") == "assistant" and text.strip():
                    add({"kind": "agent", "text": text}, "response")
            continue
        if typ == "compacted":
            add({"kind": "compaction"}, "legacy")
            continue
        if typ != "event_msg":
            continue
        et = p.get("type")
        if et == "item_completed" and isinstance(p.get("item"), dict):
            for ev in events_from_item(p["item"]):
                add(ev, "item")
        elif et == "plan_update":
            ev = _plan_from_args(p)
            if ev and not (events and events[-1].get("kind") == "plan"
                           and events[-1]["steps"] == ev["steps"]):
                add(ev, "both")
        elif et == "thread_goal_updated":
            goal = p.get("goal") or {}
            add({"kind": "goal_tool", "name": "thread_goal_updated",
                 "arguments": json.dumps({"status": goal.get("status")}), "followup": ""}, "both")
        elif et == "user_message":
            add({"kind": "user", "text": p.get("message") or ""}, "legacy")
        elif et == "agent_message":
            add({"kind": "agent", "text": p.get("message") or ""}, "legacy")
        elif et == "exec_command_end":
            add({"kind": "command", "command": _cmd_str(p.get("command")),
                 "exit_code": p.get("exit_code"), "status": p.get("status"),
                 "output": p.get("aggregated_output")
                 or (p.get("stdout") or "") + (p.get("stderr") or "")}, "legacy")
        elif et == "patch_apply_end":
            ch = p.get("changes") or {}
            status = p.get("status") or ("completed" if p.get("success") else "failed")
            add({"kind": "file_change", "changes": _changes(ch), "status": status}, "legacy")
        elif et in ("error", "stream_error"):
            add({"kind": "error", "text": p.get("message") or ""}, "both")
        elif et == "turn_aborted":
            add({"kind": "error", "text": "turn aborted: %s" % (p.get("reason") or "")}, "both")
        elif et == "context_compacted":
            add({"kind": "compaction"}, "legacy")

    has_items = any(e["src"] == "item" for e in events)
    has_legacy_msgs = any(e["src"] == "legacy" and e["kind"] in ("user", "agent") for e in events)
    out = []
    for e in events:
        src = e.pop("src")
        if src == "both":
            out.append(e)
        elif has_items:
            if src == "item":
                out.append(e)
        elif src == "legacy":
            out.append(e)
        elif src == "response" and not has_legacy_msgs:
            out.append(e)
    return out, meta


# ---------------------------------------------------------------- public

def session_meta(home, thread_id: str) -> dict | None:
    """``session_meta`` of a thread's rollout (fallback when ``threads`` is missing)."""
    path = find_rollout(home, thread_id, None)
    if not path:
        return None
    try:
        with open_rollout(path) as fh:
            for i, line in enumerate(fh):
                if i > 20:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    p = rec.get("payload") or {}
                    return {"cwd": p.get("cwd"), "git": p.get("git") or {}, "path": path}
    except (OSError, RuntimeError):
        return None
    return None


def load_history(home, thread_id: str, thread_row: dict | None) -> HistoryResult:
    from . import store

    res = HistoryResult()
    rows = None
    try:
        rows = store.read_thread_items(home, thread_id)
    except store.ReadError as exc:
        res.warnings.append(str(exc))
    if rows:
        res.events = load_thread_items(rows)
        res.sources.append("%s.thread_items (%d items)" % (home.db("thread_history").name, len(rows)))
    rollout = find_rollout(home, thread_id, (thread_row or {}).get("rollout_path"))
    if rollout:
        try:
            with open_rollout(rollout) as fh:
                ev, meta = parse_rollout(fh)
            res.meta = meta
            if rows:
                # Plans, goal status changes and errors only exist in the rollout.
                res.events.extend(e for e in ev if e["kind"] in ROLLOUT_ONLY)
                res.sources.append(os.path.basename(rollout) + " (plans, goal updates)")
            else:
                res.events = ev
                res.sources.append(os.path.basename(rollout))
        except (OSError, RuntimeError) as exc:
            res.warnings.append(str(exc))
    elif not rows:
        res.warnings.append("no history found for thread %s (no thread_items rows, no rollout file)"
                            % thread_id)
    return res
