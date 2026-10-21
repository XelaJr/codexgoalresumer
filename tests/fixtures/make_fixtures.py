"""Build synthetic Codex installations for the tests.

Everything is generated from the real Codex schemas (``schema/*.sql``, produced
from the openai/codex migrations) plus a small scripted "story" per goal. No
personal data is involved.

    python3 tests/fixtures/make_fixtures.py OUT_DIR [scenario ...]

Scenarios: linux, macos, windows, wsl, shared (see ``SCENARIOS``).
The committed ``rollouts/legacy-zst.jsonl.zst`` is regenerated with
``--write-zst`` (needs the ``zstd`` command).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# CODEX_GOALS_SCHEMA_DIR points at schemas regenerated from another Codex
# version (see tests/make_schema.py and tests/compat_check.py).
SCHEMA = os.environ.get("CODEX_GOALS_SCHEMA_DIR") or os.path.join(HERE, "schema")
ZST_FIXTURE = os.path.join(HERE, "rollouts", "legacy-zst.jsonl.zst")

T0 = 1790000000000  # ms, 2026-09-21

FAKE_KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # not a real key


# ---------------------------------------------------------------- stories

def story_blocked(cwd, sep="/"):
    """A paginated goal that ends blocked waiting for credentials."""
    p = lambda *parts: sep.join(parts)
    return [
        ("user", "Finish the export feature described in docs/PLAN.md"),
        ("user", "Use the staging database, password is Stag1ng#Pass"),
        ("plan", [("Read the plan", "completed"), ("Implement CSV export", "completed"),
                  ("Add integration test against staging", "in_progress"),
                  ("Update the README", "pending")]),
        ("command", "rg -n export src", 1, ""),  # a search that found nothing
        ("command", "python -m pytest -q tests/test_export.py", 1,
         "F\nE   AssertionError: expected 3 rows\n1 failed in 0.12s"),
        ("file", [(p(cwd, "src", "export.py"), "add"), (p(cwd, "tests", "test_export.py"), "add")]),
        ("command", "python -m pytest -q tests/test_export.py", 0, "1 passed"),
        ("command", "python -m pytest -q tests/test_integration.py", 2,
         "ERROR: could not connect to staging: authentication failed\n"
         "export DB_PASSWORD=Stag1ng#Pass"),
        ("file", [(p(cwd, "README.md"), "update")]),
        ("subagent", "child-0001", "Check how the staging connection is configured"),
        ("agent", "The integration test needs staging credentials (OPENAI_API_KEY=%s)." % FAKE_KEY),
        ("goal", "blocked"),
        ("agent", "Blocked: the staging database rejects the credentials. I need a valid "
                  "DB_PASSWORD or access to the VPN to continue."),
    ]


def story_simple(text="Refactor the parser"):
    return [("user", text), ("command", "make test", 0, "ok"),
            ("agent", "Refactor half done, continuing with the lexer.")]


def story_child():
    return [("user", "Check how the staging connection is configured"),
            ("command", "cat config/staging.toml", 0, "host = staging.internal"),
            ("agent", "Staging uses host staging.internal and reads DB_PASSWORD from the env.")]


# ---------------------------------------------------------------- emitters

def v2_items(story, cwd):
    """app-server ThreadItem JSON (camelCase), as stored in thread_items."""
    out = []
    for i, step in enumerate(story):
        kind = step[0]
        iid = "item-%d" % i
        if kind == "user":
            out.append({"type": "userMessage", "id": iid, "clientId": None,
                        "content": [{"type": "text", "text": step[1], "text_elements": []}]})
        elif kind == "agent":
            out.append({"type": "agentMessage", "id": iid, "text": step[1], "phase": None})
        elif kind == "command":
            out.append({"type": "commandExecution", "id": iid, "command": "/bin/bash -lc '%s'" % step[1],
                        "cwd": cwd, "processId": None, "source": "agent",
                        "status": "completed" if step[2] == 0 else "failed", "commandActions": [],
                        "aggregatedOutput": step[3], "exitCode": step[2], "durationMs": 10})
        elif kind == "file":
            out.append({"type": "fileChange", "id": iid, "status": "completed",
                        "changes": [{"path": path, "kind": {"type": k}, "diff": ""} for path, k in step[1]]})
        elif kind == "subagent":
            out.append({"type": "collabAgentToolCall", "id": iid, "tool": "spawnAgent",
                        "status": "completed", "senderThreadId": "x", "receiverThreadIds": [step[1]],
                        "prompt": step[2], "model": None, "reasoningEffort": None, "agentsStates": {}})
    return out


def rollout_lines(story, thread_id, cwd, sha, branch, legacy=False):
    """Rollout JSONL. ``legacy`` uses pre-item events (exec_command_end...)."""
    ts = "2026-09-21T10:00:00.000Z"
    lines = [{"timestamp": ts, "type": "session_meta",
              "payload": {"id": thread_id, "timestamp": ts, "cwd": cwd, "originator": "codex_cli_rs",
                          "cli_version": "0.163.0", "git": {"commit_hash": sha, "branch": branch}}}]
    for i, step in enumerate(story):
        kind = step[0]
        ev = lambda payload: lines.append({"timestamp": ts, "type": "event_msg", "payload": payload})
        item = lambda it: ev({"type": "item_completed", "thread_id": thread_id, "turn_id": "turn-1", "item": it})
        if kind == "plan":
            lines.append({"timestamp": ts, "type": "response_item", "payload": {
                "type": "function_call", "name": "update_plan", "call_id": "call-%d" % i,
                "arguments": json.dumps({"plan": [{"step": s, "status": st} for s, st in step[1]]})}})
        elif kind == "goal":
            lines.append({"timestamp": ts, "type": "response_item", "payload": {
                "type": "function_call", "name": "update_goal", "call_id": "call-%d" % i,
                "arguments": json.dumps({"status": step[1]})}})
        elif kind == "user":
            if legacy:
                ev({"type": "user_message", "message": step[1]})
            else:
                item({"type": "UserMessage", "id": "u%d" % i, "content": [{"type": "text", "text": step[1]}]})
        elif kind == "agent":
            if legacy:
                ev({"type": "agent_message", "message": step[1]})
            else:
                item({"type": "AgentMessage", "id": "a%d" % i, "content": [{"type": "Text", "text": step[1]}]})
        elif kind == "command":
            if legacy:
                ev({"type": "exec_command_end", "call_id": "c%d" % i, "turn_id": "turn-1",
                    "command": ["bash", "-lc", step[1]], "cwd": cwd, "parsed_cmd": [],
                    "aggregated_output": step[3], "exit_code": step[2], "duration": "0s",
                    "status": "completed" if step[2] == 0 else "failed"})
            else:
                item({"type": "CommandExecution", "id": "c%d" % i, "command": ["bash", "-lc", step[1]],
                      "cwd": cwd, "parsed_cmd": [], "source": "agent",
                      "status": "completed" if step[2] == 0 else "failed",
                      "aggregated_output": step[3], "exit_code": step[2]})
        elif kind == "file":
            changes = {path: {"type": k, "content": ""} if k != "update"
                       else {"type": "update", "unified_diff": "", "move_path": None}
                       for path, k in step[1]}
            if legacy:
                ev({"type": "patch_apply_end", "call_id": "p%d" % i, "stdout": "", "stderr": "",
                    "success": True, "changes": changes, "status": "completed"})
            else:
                item({"type": "FileChange", "id": "p%d" % i, "changes": changes, "status": "completed"})
        elif kind == "subagent" and not legacy:
            item({"type": "CollabAgentToolCall", "id": "s%d" % i, "tool": "spawn_agent",
                  "status": "completed", "sender_thread_id": thread_id,
                  "receiver_thread_ids": [step[1]], "receiver_agents": [], "prompt": step[2],
                  "agents_states": {}})
    return lines


# ---------------------------------------------------------------- builders

def schema_name(kind, legacy=False):
    """``goals_1`` etc.: the highest version present in the schema directory."""
    best = None
    for f in os.listdir(SCHEMA):
        m = re.match(r"^%s_(\d+)%s\.sql$" % (kind, "_legacy_goals" if legacy else ""), f)
        if m and (best is None or int(m.group(1)) > best):
            best = int(m.group(1))
    if best is None:
        raise FileNotFoundError("no %s schema in %s" % (kind, SCHEMA))
    return "%s_%d" % (kind, best)


def _db(home, kind, legacy=False):
    name = schema_name(kind)
    conn = sqlite3.connect(os.path.join(home, name + ".sqlite"))
    sql = (schema_name(kind, True) + "_legacy_goals" if legacy else name) + ".sql"
    with open(os.path.join(SCHEMA, sql), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


class HomeBuilder:
    def __init__(self, home, legacy_goals=False):
        self.home = home
        os.makedirs(home, exist_ok=True)
        self.state = _db(home, "state", legacy=legacy_goals)
        self.goals = self.state if legacy_goals else _db(home, "goals")
        self.hist = _db(home, "thread_history")
        self.n = 0

    def rollout_path(self, thread_id, day="21", zst=False):
        d = os.path.join(self.home, "sessions", "2026", "09", day)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "rollout-2026-09-%sT10-00-00-%s.jsonl%s" % (day, thread_id, ".zst" if zst else ""))

    def add(self, thread_id, objective, status, cwd, story, history="paginated", sha="1234567890abcdef1234567890abcdef12345678",
            branch="main", tokens=120000, budget=None, seconds=3600, record_cwd=None, threads_row=True):
        self.n += 1
        rec_cwd = record_cwd or cwd
        if status is not None:
            self.goals.execute(
                "INSERT INTO thread_goals (thread_id, goal_id, objective, status, token_budget, tokens_used, "
                "time_used_seconds, created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (thread_id, "goal-" + thread_id[-4:], objective, status, budget, tokens, seconds,
                 T0 + self.n * 1000, T0 + self.n * 3600 * 1000))
        path = self.rollout_path(thread_id, zst=(history == "zst"))
        if threads_row:
            row = {"id": thread_id, "rollout_path": path, "created_at": T0 // 1000, "updated_at": T0 // 1000,
                   "source": "cli", "model_provider": "openai", "cwd": rec_cwd, "title": objective[:40],
                   "sandbox_policy": "workspace-write", "approval_mode": "on-request", "git_sha": sha,
                   "git_branch": branch, "history_mode": "paginated" if history == "paginated" else "legacy",
                   "first_user_message": story[0][1] if story else ""}
            have = {r[1] for r in self.state.execute("PRAGMA table_info(threads)")}
            cols = [c for c in row if c in have]
            self.state.execute("INSERT INTO threads (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                               [row[c] for c in cols])
        lines = rollout_lines(story, thread_id, rec_cwd, sha, branch, legacy=(history in ("legacy", "zst")))
        if history == "paginated":
            for i, item in enumerate(v2_items(story, rec_cwd)):
                self.hist.execute(
                    "INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_json, "
                    "item_type, updated_at_ordinal) VALUES (?,?,?,?,?,?,?,?)",
                    (thread_id, "turn-1", item["id"], i, T0 + i, json.dumps(item), item["type"], i))
        if history == "zst":
            shutil.copy(ZST_FIXTURE, path)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(json.dumps(line) + "\n")
        return path

    def edge(self, parent, child, status="closed"):
        self.state.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)", (parent, child, status))

    def close(self):
        for c in {id(x): x for x in (self.state, self.goals, self.hist)}.values():
            c.commit()
            c.close()


# Thread ids look like Codex UUIDv7 ids; prefixes are shared on purpose.
ID_BLOCKED = "01a0b10c-5690-7820-96b9-81487adf618b"
ID_ACTIVE = "01a0ac71-1111-7820-96b9-81487adf618b"
ID_LEGACY = "01a0017a-2222-7820-96b9-81487adf618b"
ID_ZST = "01a00257-3333-7820-96b9-81487adf618b"
ID_DONE = "01a0d0e0-4444-7820-96b9-81487adf618b"
ID_OTHER = "01a007e5-5555-7820-96b9-81487adf618b"
ID_CHILD = "child-0001"

SCENARIOS = {
    # name: (project dir as recorded by Codex, separator, project dir as seen by the test)
    "linux": ("/home/ana/proj", "/", "/home/ana/proj"),
    "macos": ("/private/var/folders/ab/proj", "/", "/var/folders/ab/proj"),
    "windows": ("C:\\Users\\Ana\\proj", "\\", "c:/users/ana/proj"),
    "wsl": ("C:\\Users\\Ana\\proj", "\\", "/mnt/c/Users/Ana/proj"),
    "shared": ("/mnt/c/Users/Ana/proj", "/", "C:\\Users\\Ana\\proj"),
}


def build(root, scenario="linux", legacy_goals=False, project=None):
    """Create ``root/codex_home`` for ``scenario`` and return (home, project_as_seen)."""
    recorded, sep, seen = SCENARIOS[scenario]
    if project:
        recorded = seen = project
        sep = os.sep
    home = os.path.join(root, "codex_home")
    b = HomeBuilder(home, legacy_goals=legacy_goals)
    b.add(ID_BLOCKED, "Finish the export feature", "blocked", recorded, story_blocked(recorded, sep))
    b.add(ID_CHILD, "", None, recorded, story_child())
    b.edge(ID_BLOCKED, ID_CHILD)
    b.add(ID_ACTIVE, "Speed up the importer", "active", recorded + sep + "importer", story_simple("Speed it up"))
    b.add(ID_LEGACY, "Old style rollout goal", "paused", recorded, story_simple("Legacy history"),
          history="legacy", threads_row=False)
    if os.path.exists(ZST_FIXTURE):
        b.add(ID_ZST, "Compressed rollout goal", "usage_limited", recorded, story_simple("Compressed"),
              history="zst")
    b.add(ID_DONE, "Already finished", "complete", recorded, story_simple("Done"))
    b.add(ID_OTHER, "Another project goal", "budget_limited", "/srv/other", story_simple("Other"),
          budget=100000)
    b.close()
    return home, seen


def write_zst():
    os.makedirs(os.path.dirname(ZST_FIXTURE), exist_ok=True)
    plain = ZST_FIXTURE[:-4]
    lines = rollout_lines(story_simple("Compressed"), ID_ZST, "/home/ana/proj", "1234567890abcdef1234567890abcdef12345678",
                          "main", legacy=True)
    with open(plain, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    subprocess.run(["zstd", "-q", "-f", "-19", plain, "-o", ZST_FIXTURE], check=True)
    os.remove(plain)


if __name__ == "__main__":
    if "--write-zst" in sys.argv:
        write_zst()
        print("wrote", ZST_FIXTURE)
        sys.exit(0)
    out = sys.argv[1]
    for sc in sys.argv[2:] or list(SCENARIOS):
        home, seen = build(os.path.join(out, sc), sc)
        print(sc, home, "project:", seen)
