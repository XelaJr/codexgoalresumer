"""``export --anonymize``: copy one goal into a fixture without personal content.

Structure is preserved (tables, columns, JSON keys, item types, statuses,
exit codes, ids, line counts and text lengths). Free text, paths, commands,
branch names, commit hashes and URLs are replaced by placeholders, using the
same placeholder every time a value repeats so cross-references still match.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

from . import history, store

KEEP_KEYS = {
    "type", "status", "kind", "role", "item_type", "history_mode", "phase", "source",
    "thread_source", "model_provider", "model", "reasoning_effort", "approval_mode",
    "memory_mode", "originator", "cli_version", "name", "tool", "server", "namespace",
    "exit_code", "exitCode", "timestamp", "archived", "detail", "delivery",
}
PATH_KEYS = {"cwd", "path", "rollout_path", "agent_path", "move_path", "movePath", "file",
             "workdir", "savedPath"}
SHA_KEYS = {"git_sha", "commit_hash", "sha"}
BRANCH_KEYS = {"git_branch", "branch"}
URL_KEYS = {"git_origin_url", "repository_url", "url"}
_ID_RE = re.compile(r"(?:^id$|_id$|Id$|Ids$|_ids$)")
_LOREM = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
          "incididunt ut labore et dolore magna aliqua ").split()


class Anonymizer:
    def __init__(self):
        self.map = {}
        self.counts = {}

    def _token(self, kind, value, make):
        key = (kind, value)
        if key not in self.map:
            n = self.counts.get(kind, 0) + 1
            self.counts[kind] = n
            self.map[key] = make(n)
        return self.map[key]

    def text(self, s: str) -> str:
        if not s:
            return s
        lines = []
        for i, line in enumerate(s.split("\n")):
            words, length, j = [], 0, i
            while length < len(line):
                w = _LOREM[j % len(_LOREM)]
                words.append(w)
                length += len(w) + 1
                j += 1
            lines.append(" ".join(words)[: len(line)])
        return "\n".join(lines)

    def path(self, p: str) -> str:
        if not p:
            return p
        def make(n):
            ext = os.path.splitext(p.replace("\\", "/").rstrip("/"))[1]
            return "/anon/path%d%s" % (n, ext if re.match(r"^\.[A-Za-z0-9]{1,8}$", ext or "") else "")
        return self._token("path", p, make)

    def sha(self, s):
        return self._token("sha", s, lambda n: "%040x" % (0xa0000 + n)) if s else s

    def branch(self, s):
        return self._token("branch", s, lambda n: "branch%d" % n) if s else s

    def url(self, s):
        return self._token("url", s, lambda n: "https://example.invalid/repo%d.git" % n) if s else s

    @staticmethod
    def _looks_like_path(s):
        return bool(re.match(r"^(?:/|~/|[A-Za-z]:[\\/]|\\\\)", s or ""))

    def value(self, key, v):
        if isinstance(v, dict):
            return {(self.path(k) if self._looks_like_path(k) else k): self.value(k, x)
                    for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(key, x) for x in v]
        if not isinstance(v, str):
            return v
        if key in KEEP_KEYS or (key and _ID_RE.search(key)):
            return v
        if key in PATH_KEYS or self._looks_like_path(v):
            return self.path(v)
        if key in SHA_KEYS:
            return self.sha(v)
        if key in BRANCH_KEYS:
            return self.branch(v)
        if key in URL_KEYS:
            return self.url(v)
        if key in ("arguments", "input", "item_json", "error_json"):
            try:
                inner = json.loads(v)
            except ValueError:
                return self.text(v)
            if isinstance(inner, (dict, list)):
                return json.dumps(self.value(key + ".json", inner), ensure_ascii=False)
        if key == "command" or key == "command.json":
            return self._token("cmd", v, lambda n: "command-%d" % n)
        return self.text(v)


def _copy_table(src, dst, table, where, params, anon, row_hook=None):
    if table not in store.tables(src):
        return 0
    sql = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    dst.execute(sql)
    cols = [r[1] for r in src.execute("PRAGMA table_info(%s)" % table)]
    n = 0
    for row in src.execute("SELECT * FROM %s WHERE %s" % (table, where), params):
        d = dict(zip(cols, row))
        if row_hook:
            row_hook(d)
        d = {k: anon.value(k, v) for k, v in d.items()}
        dst.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
                    [d[c] for c in cols])
        n += 1
    return n


def export(goal, out_dir) -> list:
    home = goal.home
    anon = Anonymizer()
    target = os.path.join(out_dir, "codex_home")
    os.makedirs(target, exist_ok=True)
    report = []
    ids = [goal.thread_id] + [c["child"] for c in store.read_spawn_children(home, goal.thread_id)]
    marks = ",".join("?" * len(ids))
    plan = [("goals", "thread_goals", "thread_id IN (%s)" % marks),
            ("state", "threads", "id IN (%s)" % marks),
            ("state", "thread_spawn_edges", "parent_thread_id IN (%s)" % marks),
            ("thread_history", "thread_items", "thread_id IN (%s)" % marks),
            ("thread_history", "thread_turns", "thread_id IN (%s)" % marks)]
    rollouts = {}
    conns = {}
    try:
        for kind, table, where in plan:
            db = home.db(kind)
            if not db:
                continue
            if kind not in conns:
                path = os.path.join(target, os.path.basename(db.path))
                if os.path.exists(path):
                    os.remove(path)
                conns[kind] = (store.open_ro(db.path), sqlite3.connect(path))
            src, dst = conns[kind]
            hook = None
            if table == "threads":
                def hook(d):
                    rollouts[d["id"]] = d.get("rollout_path")
            n = _copy_table(src, dst, table, where, ids, anon, hook)
            report.append("%s.%s: %d rows" % (os.path.basename(db.path), table, n))
        for _, (src, dst) in conns.items():
            dst.commit()
            dst.close()
            src.close()
    finally:
        pass
    for tid in ids:
        src = history.find_rollout(home, tid, rollouts.get(tid))
        if not src:
            continue
        name = os.path.basename(src)
        if name.endswith(".zst"):
            name = name[:-4]
        m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})", name)
        sub = os.path.join("sessions", *(m.groups() if m else ("unknown",)))
        os.makedirs(os.path.join(target, sub), exist_ok=True)
        dst = os.path.join(target, sub, name)
        lines = 0
        with history.open_rollout(src) as fin, open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                fout.write(json.dumps(anon.value("", rec), ensure_ascii=False) + "\n")
                lines += 1
        report.append("%s: %d lines" % (os.path.join(sub, name), lines))
    report.append("placeholders: " + ", ".join("%d %s" % (n, k) for k, n in sorted(anon.counts.items())))
    return report
