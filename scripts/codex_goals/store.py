"""Read-only access to Codex's SQLite databases.

Nothing here ever writes to Codex's files. Databases are opened with
``mode=ro``; if SQLite reports a lock (or the file sits on a WSL/DrvFS/9p
mount where locking is unreliable) the database and its ``-wal`` are copied
to a temporary directory and the copy is read instead.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import quote

BUSY_TIMEOUT_MS = 3000
RETRIES = 3

GOAL_STATUSES = ("active", "paused", "blocked", "usage_limited", "budget_limited", "complete")
UNFINISHED = ("active", "paused", "blocked", "usage_limited", "budget_limited")


class SchemaError(Exception):
    """Required tables/columns are missing (exit code 3)."""


class ReadError(Exception):
    """A database could not be read (exit code 2)."""


_tmpdirs = []


def _cleanup():
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def _needs_copy(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return bool(re.match(r"^/mnt/[a-z]/", p) or p.startswith("//wsl") or p.startswith("\\\\wsl"))


def _copy_to_temp(path: str) -> str:
    d = tempfile.mkdtemp(prefix="codex_goals_")
    _tmpdirs.append(d)
    dst = os.path.join(d, os.path.basename(path))
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dst + suffix)
    return dst


def _uri(path: str) -> str:
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p  # file:///C:/x on Windows
    return "file:%s?mode=ro" % quote(p, safe="/:")


def open_ro(path: str) -> sqlite3.Connection:
    """Open ``path`` read-only, retrying on locks and falling back to a copy."""
    if not os.path.isfile(path):
        raise ReadError("database not found: %s" % path)
    last = None
    if not _needs_copy(path):
        for attempt in range(RETRIES):
            try:
                conn = sqlite3.connect(_uri(path), uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0)
                conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
                conn.execute("PRAGMA query_only = ON")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                conn.row_factory = sqlite3.Row
                return conn
            except sqlite3.OperationalError as exc:
                last = exc
                if "locked" not in str(exc) and "busy" not in str(exc):
                    break
                time.sleep(0.2 * (attempt + 1))
    try:
        copy = _copy_to_temp(path)
        conn = sqlite3.connect(_uri(copy), uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.row_factory = sqlite3.Row
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise ReadError("cannot read %s: %s" % (path, last or exc))


def tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % _ident(table))}


def _ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(name)
    return '"%s"' % name


def select(conn, table: str, wanted, where: str = "", params=(), order: str = "") -> list:
    """SELECT the wanted columns that exist; missing ones come back as None."""
    have = columns(conn, table)
    present = [c for c in wanted if c in have]
    if not present:
        return []
    sql = "SELECT %s FROM %s" % (", ".join(_ident(c) for c in present), _ident(table))
    if where:
        sql += " WHERE " + where
    if order:
        sql += " ORDER BY " + order
    out = []
    for row in conn.execute(sql, params):
        d = {c: None for c in wanted}
        d.update(dict(zip(present, row)))
        out.append(d)
    return out


# ---------------------------------------------------------------- goals

GOAL_REQUIRED = ("thread_id", "objective", "status")
GOAL_COLUMNS = GOAL_REQUIRED + ("goal_id", "token_budget", "tokens_used", "time_used_seconds",
                                "created_at_ms", "updated_at_ms", "created_at", "updated_at")


@dataclass
class Goal:
    thread_id: str
    objective: str
    status: str
    goal_id: str | None = None
    token_budget: int | None = None
    tokens_used: int | None = None
    time_used_seconds: int | None = None
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    home: object = None  # discovery.Home

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("home", None)
        return d


def _ms(row, key_ms, key_s):
    if row.get(key_ms) is not None:
        return int(row[key_ms])
    if row.get(key_s) is not None:
        v = int(row[key_s])
        return v if v > 10 ** 11 else v * 1000
    return None


def read_goals(home) -> list:
    """All goals of a home. Uses goals_N.sqlite, or the legacy table in state_N."""
    sources = []
    if home.db("goals"):
        sources.append(home.db("goals").path)
    if home.db("state"):
        sources.append(home.db("state").path)
    for path in sources:
        conn = open_ro(path)
        try:
            if "thread_goals" not in tables(conn):
                continue
            have = columns(conn, "thread_goals")
            missing = [c for c in GOAL_REQUIRED if c not in have]
            if missing:
                raise SchemaError("thread_goals in %s lacks required column(s): %s"
                                  % (path, ", ".join(missing)))
            goals = []
            for r in select(conn, "thread_goals", GOAL_COLUMNS):
                goals.append(Goal(
                    thread_id=r["thread_id"], objective=r["objective"] or "",
                    status=r["status"] or "", goal_id=r["goal_id"],
                    token_budget=r["token_budget"], tokens_used=r["tokens_used"],
                    time_used_seconds=r["time_used_seconds"],
                    created_at_ms=_ms(r, "created_at_ms", "created_at"),
                    updated_at_ms=_ms(r, "updated_at_ms", "updated_at"),
                    home=home))
            return goals
        finally:
            conn.close()
    return []


# ---------------------------------------------------------------- threads

THREAD_COLUMNS = ("id", "cwd", "rollout_path", "git_sha", "git_branch", "git_origin_url",
                  "history_mode", "archived", "title", "first_user_message", "agent_nickname",
                  "agent_role", "agent_path", "model", "reasoning_effort", "cli_version",
                  "created_at", "updated_at", "created_at_ms", "updated_at_ms", "tokens_used")


def read_threads(home, ids) -> dict:
    ids = [i for i in ids if i]
    if not ids or not home.db("state"):
        return {}
    conn = open_ro(home.db("state").path)
    try:
        if "threads" not in tables(conn):
            return {}
        out = {}
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            marks = ",".join("?" * len(part))
            for r in select(conn, "threads", THREAD_COLUMNS, "id IN (%s)" % marks, part):
                out[r["id"]] = r
        return out
    finally:
        conn.close()


def read_spawn_children(home, parent_id: str) -> list:
    """Child thread ids spawned by ``parent_id`` (recursively), with edge status."""
    if not home.db("state"):
        return []
    conn = open_ro(home.db("state").path)
    try:
        if "thread_spawn_edges" not in tables(conn):
            return []
        out, frontier, seen = [], [parent_id], {parent_id}
        while frontier:
            pid = frontier.pop(0)
            for r in select(conn, "thread_spawn_edges",
                            ("parent_thread_id", "child_thread_id", "status"),
                            "parent_thread_id = ?", (pid,)):
                cid = r["child_thread_id"]
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append({"parent": pid, "child": cid, "status": r["status"]})
                    frontier.append(cid)
        return out
    finally:
        conn.close()


def read_thread_items(home, thread_id: str) -> list | None:
    """``thread_items`` rows (item_json strings) in order, or None if unavailable."""
    db = home.db("thread_history")
    if not db:
        return None
    conn = open_ro(db.path)
    try:
        if "thread_items" not in tables(conn):
            return None
        rows = select(conn, "thread_items", ("item_json", "item_type", "rollout_ordinal",
                                             "created_at_ms", "turn_id"),
                      "thread_id = ?", (thread_id,), order="rollout_ordinal")
        return rows or None
    finally:
        conn.close()


def read_turn_errors(home, thread_id: str) -> list:
    db = home.db("thread_history")
    if not db:
        return []
    conn = open_ro(db.path)
    try:
        if "thread_turns" not in tables(conn):
            return []
        return [r for r in select(conn, "thread_turns", ("turn_id", "status", "error_json",
                                                          "rollout_ordinal"),
                                  "thread_id = ? AND error_json IS NOT NULL", (thread_id,),
                                  order="rollout_ordinal")]
    finally:
        conn.close()
