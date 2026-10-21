"""Command line interface: where · list · brief · export (and notify for the hook)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, brief as brief_mod, discovery, history, store
from .paths import is_within, pretty

EXIT_OK, EXIT_NOT_FOUND, EXIT_READ, EXIT_SCHEMA = 0, 1, 2, 3


def _out(text=""):
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:  # legacy Windows consoles
        enc = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")


def _err(text):
    sys.stderr.write("codex_goals: " + text + "\n")


def _dump(obj):
    _out(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- loading

def load_all(home_override):
    """Discover homes and read every goal. Returns (discovery, goals, problems)."""
    disc = discovery.discover(home_override)
    goals, problems, seen = [], [], set()
    for home in disc.homes:
        try:
            for g in store.read_goals(home):
                if g.thread_id not in seen:
                    seen.add(g.thread_id)
                    goals.append(g)
        except store.SchemaError as exc:
            problems.append(("schema", str(exc)))
        except store.ReadError as exc:
            problems.append(("read", str(exc)))
    return disc, goals, problems


def thread_rows(goals):
    rows = {}
    by_home = {}
    for g in goals:
        by_home.setdefault(id(g.home), (g.home, []))[1].append(g.thread_id)
    for home, ids in by_home.values():
        try:
            rows.update(store.read_threads(home, ids))
        except store.ReadError:
            pass
        for tid in ids:
            if tid not in rows:
                meta = history.session_meta(home, tid)
                if meta:
                    git = meta.get("git") or {}
                    rows[tid] = {"id": tid, "cwd": meta.get("cwd"), "git_sha": git.get("commit_hash"),
                                 "git_branch": git.get("branch"), "rollout_path": meta.get("path")}
    return rows


def short_ids(ids, minimum=8):
    """Shortest prefix (>= minimum) that is unique among ``ids``."""
    out = {}
    for i in ids:
        n = minimum
        while n < len(i) and sum(1 for j in ids if j.startswith(i[:n])) > 1:
            n += 1
        out[i] = i[:n]
    return out


def resolve(goals, prefix):
    """Find goals whose thread_id or goal_id starts with ``prefix``."""
    p = prefix.strip().lower()
    exact = [g for g in goals if g.thread_id.lower() == p or (g.goal_id or "").lower() == p]
    if exact:
        return exact
    return [g for g in goals if g.thread_id.lower().startswith(p)
            or (g.goal_id or "").lower().startswith(p)]


def _problem_exit(problems):
    for kind, msg in problems:
        _err(msg)
    if any(k == "schema" for k, _ in problems):
        return EXIT_SCHEMA
    return EXIT_READ


# ---------------------------------------------------------------- where

def cmd_where(args):
    disc, goals, problems = load_all(args.home)
    data = {"homes": [], "skipped": [{"path": p, "reason": r} for p, r in disc.skipped]}
    for h in disc.homes:
        hg = [g for g in goals if g.home is h]
        data["homes"].append({
            "codex_home": h.path, "origin": h.origin,
            "sqlite_home": h.sqlite_home, "sqlite_origin": h.sqlite_origin,
            "databases": {k: {"file": os.path.basename(d.path), "version": d.version,
                              "tested": d.tested} for k, d in sorted(h.dbs.items())},
            "goals_total": len(hg),
            "goals_unfinished": sum(1 for g in hg if g.status in store.UNFINISHED),
            "session_dirs": h.session_dirs(), "warnings": h.warnings,
        })
    data["problems"] = [m for _, m in problems]
    if args.json:
        _dump(data)
    else:
        if not disc.homes:
            _out("No Codex installation found.")
        for h in data["homes"]:
            _out("CODEX_HOME  %s (origin: %s)" % (pretty(h["codex_home"]), h["origin"]))
            _out("  sqlite    %s (origin: %s)" % (pretty(h["sqlite_home"]), h["sqlite_origin"]))
            dbs = []
            for kind in discovery.DB_KINDS:
                d = h["databases"].get(kind)
                dbs.append("%s_%s %s" % (kind, d["version"], "✓" if d["tested"] else "(untested version)")
                           if d else "%s ✗" % kind)
            _out("  DB        " + "  ".join(dbs))
            _out("  goals     %d total · %d unfinished" % (h["goals_total"], h["goals_unfinished"]))
            for w in h["warnings"]:
                _out("  warning   " + w)
        if data["skipped"]:
            _out("Paths checked without Codex data:")
            for s in data["skipped"]:
                _out("  %s (%s)" % (pretty(s["path"]), s["reason"]))
        for m in data["problems"]:
            _out("Problem: " + m)
    if problems and not goals:
        return _problem_exit(problems)
    return EXIT_OK


# ---------------------------------------------------------------- list

def filter_goals(goals, rows, cwd, show_all, include_complete, statuses):
    out = []
    for g in goals:
        if statuses:
            if g.status not in statuses:
                continue
        elif not include_complete and g.status not in store.UNFINISHED:
            continue
        if not show_all:
            tcwd = (rows.get(g.thread_id) or {}).get("cwd")
            if not tcwd or not is_within(tcwd, cwd):
                continue
        out.append(g)
    out.sort(key=lambda g: g.updated_at_ms or 0, reverse=True)
    return out


def _first_line(text, width):
    line = " ".join((text or "").strip().split())
    return line if len(line) <= width else line[: width - 1] + "…"


def cmd_list(args):
    disc, goals, problems = load_all(args.home)
    if problems and not goals:
        return _problem_exit(problems)
    rows = thread_rows(goals)
    statuses = [s.strip() for s in args.status.split(",")] if args.status else None
    cwd = args.cwd or os.getcwd()
    sel = filter_goals(goals, rows, cwd, args.all, args.include_complete, statuses)
    shorts = short_ids([g.thread_id for g in goals]) if goals else {}
    items = []
    for g in sel:
        r = rows.get(g.thread_id) or {}
        items.append({
            "id": shorts.get(g.thread_id, g.thread_id[:8]), "thread_id": g.thread_id,
            "status": g.status, "updated_at_ms": g.updated_at_ms,
            "updated": brief_mod.ago(g.updated_at_ms), "cwd": r.get("cwd"),
            "branch": r.get("git_branch"), "sha": (r.get("git_sha") or "")[:7] or None,
            "objective": g.objective, "tokens_used": g.tokens_used,
            "codex_home": g.home.path,
        })
    if args.json:
        _dump({"cwd": cwd, "goals": items})
        return EXIT_OK
    if not items:
        scope = "any project" if args.all else pretty(cwd)
        _out("No %sCodex goals for %s." % ("" if args.include_complete or statuses else "unfinished ",
                                            scope))
        if not args.all:
            others = filter_goals(goals, rows, cwd, True, args.include_complete, statuses)
            if others:
                _out("There %s %d in other projects: use --all."
                     % ("is" if len(others) == 1 else "are", len(others)))
        if not disc.homes:
            _out("No Codex installation found; run `codex_goals.py where` to see the paths checked.")
        return EXIT_OK
    header = ["ID", "STATUS", "UPDATED", "BRANCH @ COMMIT"] + (["PROJECT"] if args.all else []) + ["OBJECTIVE"]
    table = []
    for it in items:
        bc = "%s @ %s" % (it["branch"] or "?", it["sha"] or "?") if (it["branch"] or it["sha"]) else "—"
        row = [it["id"], it["status"], it["updated"], bc]
        if args.all:
            row.append(pretty(it["cwd"]) or "?")
        row.append(_first_line(it["objective"], 60))
        table.append(row)
    widths = [max(len(str(r[i])) for r in [header] + table) for i in range(len(header) - 1)]
    for r in [header] + table:
        _out("  ".join(str(c).ljust(w) for c, w in zip(r, widths)) + "  " + str(r[-1]))
    return EXIT_OK


# ---------------------------------------------------------------- brief

def _pick(goals, prefix):
    matches = resolve(goals, prefix)
    if not matches:
        _err("no Codex goal matches '%s' (try `list --all --include-complete`)" % prefix)
        return None, EXIT_NOT_FOUND
    if len(matches) > 1:
        _err("'%s' is ambiguous: %s" % (prefix, ", ".join(g.thread_id[:12] for g in matches[:6])))
        return None, EXIT_NOT_FOUND
    return matches[0], EXIT_OK


def cmd_brief(args):
    disc, goals, problems = load_all(args.home)
    if problems and not goals:
        return _problem_exit(problems)
    goal, code = _pick(goals, args.id)
    if not goal:
        return code
    rows = thread_rows([goal])
    try:
        data = brief_mod.collect(goal, rows, use_git=not args.no_git)
    except store.SchemaError as exc:
        _err(str(exc))
        return EXIT_SCHEMA
    except store.ReadError as exc:
        _err(str(exc))
        return EXIT_READ
    md, sel = brief_mod.build(data, max_tokens=args.max_tokens, full=args.full)
    if args.json:
        sel = dict(sel)
        sel["estimated_tokens"] = brief_mod.estimate_tokens(md)
        _dump(sel)
    else:
        _out(md.rstrip("\n"))
    return EXIT_OK


# ---------------------------------------------------------------- export

def cmd_export(args):
    from . import anonymize

    if not args.anonymize:
        _err("export only produces anonymised copies; pass --anonymize")
        return EXIT_READ
    disc, goals, problems = load_all(args.home)
    if problems and not goals:
        return _problem_exit(problems)
    goal, code = _pick(goals, args.id)
    if not goal:
        return code
    out_dir = args.out or os.path.join(os.getcwd(), "codex-goal-%s-anonymized" % goal.thread_id[:8])
    for h in disc.homes:
        if is_within(os.path.abspath(out_dir), h.path) or is_within(os.path.abspath(out_dir), h.sqlite_home):
            _err("refusing to write inside a Codex folder (%s); choose another --out" % h.path)
            return EXIT_READ
    try:
        report = anonymize.export(goal, out_dir)
    except (store.ReadError, OSError) as exc:
        _err(str(exc))
        return EXIT_READ
    _out("Anonymised copy written to %s" % out_dir)
    for line in report:
        _out("  " + line)
    _out("Review every file before sharing it: anonymisation is automatic and may miss things.")
    return EXIT_OK


# ---------------------------------------------------------------- notify (SessionStart hook)

def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def cmd_notify(args):
    """One-line notice for the SessionStart hook. Never fails, silent when disabled."""
    enabled = args.force or _truthy(os.environ.get("CLAUDE_PLUGIN_OPTION_NOTICE")) \
        or _truthy(os.environ.get("CODEX_GOALS_NOTICE"))
    if not enabled:
        return EXIT_OK
    try:
        cwd = args.cwd
        if not cwd and not sys.stdin.isatty():
            try:
                payload = json.loads(sys.stdin.read() or "{}")
                cwd = payload.get("cwd")
            except ValueError:
                cwd = None
        cwd = cwd or os.getcwd()
        disc, goals, _ = load_all(args.home)
        rows = thread_rows(goals)
        sel = filter_goals(goals, rows, cwd, False, False, None)
        if not sel:
            return EXIT_OK
        by_status = {}
        for g in sel:
            by_status[g.status] = by_status.get(g.status, 0) + 1
        what = ", ".join("%d %s" % (n, s.replace("_", " ")) for s, n in by_status.items())
        first = _first_line(sel[0].objective, 50)
        noun = "goal" if len(sel) == 1 else "goals"
        msg = "Codex %s in this project: %s («%s»). Use /codex-goals to resume." % (noun, what, first)
        _out(json.dumps({"systemMessage": msg,
                         "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                "additionalContext": msg}},
                        ensure_ascii=False))
    except Exception:  # the hook must never break session start
        pass
    return EXIT_OK


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(
        prog="codex_goals.py",
        description="Read-only access to OpenAI Codex /goal goals so Claude Code can resume them.",
        epilog="Exit codes: 0 ok · 1 goal not found · 2 read error · 3 unsupported schema. "
               "IDs can be shortened to any unique prefix, like git.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", metavar="PATH", help="use this CODEX_HOME only")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", metavar="{where,list,brief,export}")
    sub.required = True

    w = sub.add_parser("where", parents=[common], help="show which Codex installations were found")
    w.set_defaults(func=cmd_where)

    l = sub.add_parser("list", parents=[common], help="list unfinished goals of this project")
    l.add_argument("--all", action="store_true", help="goals of every project")
    l.add_argument("--include-complete", action="store_true", help="also completed goals")
    l.add_argument("--status", metavar="S1,S2", help="only these statuses (%s)" % ",".join(store.GOAL_STATUSES))
    l.add_argument("--cwd", metavar="PATH", help="project directory (default: current directory)")
    l.set_defaults(func=cmd_list)

    b = sub.add_parser("brief", parents=[common], help="handoff brief for one goal")
    b.add_argument("id", help="goal/thread id or unique prefix")
    b.add_argument("--max-tokens", type=int, default=8000, metavar="N",
                   help="approximate size limit (default 8000)")
    b.add_argument("--full", action="store_true", help="no trimming (secrets are still masked)")
    b.add_argument("--no-git", action="store_true", help="skip the git section")
    b.set_defaults(func=cmd_brief)

    e = sub.add_parser("export", parents=[common], help="anonymised copy of one goal for test fixtures")
    e.add_argument("id", help="goal/thread id or unique prefix")
    e.add_argument("--anonymize", action="store_true", help="required: replace texts, paths and names")
    e.add_argument("--out", metavar="DIR", help="output directory")
    e.set_defaults(func=cmd_export)

    n = sub.add_parser("notify", parents=[common])
    n.add_argument("--cwd", metavar="PATH")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_notify)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.SchemaError as exc:
        _err(str(exc))
        return EXIT_SCHEMA
    except store.ReadError as exc:
        _err(str(exc))
        return EXIT_READ
    except KeyboardInterrupt:
        return 130
