"""Check codex_goals against a given openai/codex checkout (weekly CI job).

    python3 tests/compat_check.py <codex checkout> [--rev REV]

1. Reads the database file versions Codex uses (state/src/sqlite.rs) and
   compares them with the versions codex_goals was tested with.
2. Regenerates the schemas from that checkout's migrations.
3. Builds a Codex home with a minimal goal on those schemas and runs
   where / list / brief, plus the whole test suite, against it.

Exits non-zero with a summary if anything breaks.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "codex_goals.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

from codex_goals.discovery import KNOWN_DB_VERSIONS  # noqa: E402
import make_schema  # noqa: E402


def run(args, env):
    r = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codex")
    ap.add_argument("--rev", default=os.environ.get("CODEX_REV", "unknown"))
    a = ap.parse_args()
    problems, notes = [], []

    versions = make_schema.db_versions(a.codex)
    for kind, known in KNOWN_DB_VERSIONS.items():
        got = versions.get(kind)
        if got is None:
            problems.append("could not find %s_N.sqlite in codex-rs/state/src/sqlite.rs" % kind)
        elif got != known:
            problems.append("Codex now uses %s_%d.sqlite (tested: %s_%d.sqlite)" % (kind, got, kind, known))
    notes.append("Codex database versions: %s" % json.dumps(versions))

    tmp = tempfile.mkdtemp(prefix="codex_compat_")
    schema_dir = os.path.join(tmp, "schema")
    make_schema.generate(a.codex, schema_dir, a.rev)
    env = dict(os.environ, CODEX_GOALS_SCHEMA_DIR=schema_dir)
    env.pop("CODEX_HOME", None)

    builder = ("import sys; sys.path.insert(0, %r); import make_fixtures as m; "
               "print(m.build(%r, 'linux', project=%r)[0])"
               % (os.path.join(HERE, "fixtures"), tmp, os.path.join(tmp, "proj")))
    os.makedirs(os.path.join(tmp, "proj"))
    r = subprocess.run([sys.executable, "-c", builder], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        problems.append("building a goal on the new schema failed:\n" + r.stderr[-2000:])
    else:
        home = r.stdout.strip()
        for args, expect in ((["where", "--home", home], "goals"),
                             (["list", "--home", home, "--cwd", os.path.join(tmp, "proj")], "01a0b10c"),
                             (["brief", "01a0b10c", "--home", home, "--no-git"], "## Where it stopped")):
            code, out, err = run(args, env)
            if code != 0 or expect not in out:
                problems.append("`codex_goals.py %s` failed (exit %d):\n%s%s"
                                % (" ".join(args[:2]), code, out[-1500:], err[-1500:]))
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", HERE, "-t", HERE],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    if r.returncode != 0:
        problems.append("test suite on the new schema failed:\n" + r.stderr[-3000:])

    print("\n".join(notes))
    if problems:
        print("\nPROBLEMS (openai/codex@%s):" % a.rev)
        for p in problems:
            print("- " + p)
        return 1
    print("OK: codex_goals works with openai/codex@%s" % a.rev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
