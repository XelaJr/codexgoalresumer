"""Regenerate tests/fixtures/schema/*.sql from an openai/codex checkout.

    python3 tests/make_schema.py <codex checkout> [OUT_DIR]

Applies the SQL migrations in codex-rs/state/{goals_migrations,migrations,
thread_history_migrations} in order and dumps the resulting schema.
"""

import glob
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "fixtures", "schema")

DBS = [  # (kind, migrations dir, stop before this migration prefix)
    ("goals", "goals_migrations", None),
    ("state", "migrations", None),
    ("thread_history", "thread_history_migrations", None),
]


def db_versions(codex):
    """{'goals': 1, 'state': 5, ...} from codex-rs/state/src/sqlite.rs."""
    path = os.path.join(codex, "codex-rs", "state", "src", "sqlite.rs")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    out = {}
    for kind in ("goals", "state", "thread_history"):
        m = re.search(r'"%s_(\d+)\.sqlite"' % kind, text)
        if m:
            out[kind] = int(m.group(1))
    return out


def dump(migrations, header, out_path, stop=None):
    conn = sqlite3.connect(":memory:")
    files = sorted(glob.glob(os.path.join(migrations, "*.sql")))
    if not files:
        raise SystemExit("no migrations in %s" % migrations)
    for f in files:
        if stop and os.path.basename(f) >= stop:
            break
        with open(f, encoding="utf-8") as fh:
            conn.executescript(fh.read())
    rows = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for (sql,) in rows:
            fh.write(sql.strip() + ";\n\n")
    return len(rows)


def generate(codex, out=DEFAULT_OUT, rev="unknown"):
    os.makedirs(out, exist_ok=True)
    versions = db_versions(codex)
    state_dir = os.path.join(codex, "codex-rs", "state")
    written = {}
    for kind, sub, stop in DBS:
        name = "%s_%d" % (kind, versions.get(kind, 0))
        header = ("-- Schema of %s.sqlite produced by applying the migrations in\n"
                  "-- openai/codex@%s codex-rs/state/%s (Apache-2.0).\n"
                  "-- Regenerate with tests/make_schema.py <codex checkout>.\n" % (name, rev, sub))
        target = os.path.join(out, "%s_%d.sql" % (kind, versions.get(kind, 0)))
        written[kind] = (target, dump(os.path.join(state_dir, sub), header, target, stop))
    legacy = os.path.join(out, "state_%d_legacy_goals.sql" % versions.get("state", 0))
    dump(os.path.join(state_dir, "migrations"),
         "-- state DB as of migration 0033 (thread_goals still lived in state_N.sqlite),\n"
         "-- from openai/codex@%s codex-rs/state/migrations (Apache-2.0).\n" % rev, legacy, "0034")
    return versions, written


if __name__ == "__main__":
    codex = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    rev = os.environ.get("CODEX_REV", "unknown")
    versions, written = generate(codex, out, rev)
    print("versions:", versions)
    for kind, (path, n) in written.items():
        print("%s: %s (%d objects)" % (kind, path, n))
