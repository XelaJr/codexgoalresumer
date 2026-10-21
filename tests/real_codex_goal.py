"""Create a real goal with the installed Codex, without an API key.

    python3 tests/real_codex_goal.py CODEX_HOME PROJECT_DIR [--status blocked]

Starts ``codex app-server`` (JSON-RPC over stdio) with ``CODEX_HOME`` pointing
at a scratch folder, starts a thread in PROJECT_DIR and sets a goal on it with
``thread/goal/set``. No model call is made. Prints the thread id.

Used by the weekly compatibility job and the platform probe so that
codex_goals is checked against databases written by Codex itself.
"""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codex_home")
    ap.add_argument("project")
    ap.add_argument("--status", default=None, help="also set this status (paused, blocked...)")
    ap.add_argument("--objective", default="Compatibility probe: finish the export feature")
    ap.add_argument("--codex", default=shutil.which("codex") or "codex")
    a = ap.parse_args()

    os.makedirs(a.codex_home, exist_ok=True)
    env = dict(os.environ, CODEX_HOME=os.path.abspath(a.codex_home))
    cmd = [a.codex, "app-server"]
    if os.name == "nt" and not a.codex.lower().endswith(".exe"):
        cmd = ["cmd", "/c"] + cmd  # npm installs a .cmd shim on Windows
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=a.project,
                            text=True, encoding="utf-8", bufsize=1)
    lines = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    next_id = [0]

    def request(method, params, timeout=60):
        next_id[0] += 1
        rid = next_id[0]
        proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                raise SystemExit("codex app-server exited: " + proc.stderr.read()[-2000:])
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise SystemExit("%s failed: %s" % (method, json.dumps(msg["error"])))
                return msg.get("result") or {}
        raise SystemExit("timeout waiting for %s" % method)

    try:
        request("initialize", {"clientInfo": {"name": "codexgoalresumer-probe", "version": "0.1.0"}})
        proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        proc.stdin.flush()
        started = request("thread/start", {"cwd": os.path.abspath(a.project)})
        thread = started.get("thread") or {}
        tid = thread.get("id") or started.get("threadId")
        if not tid:
            raise SystemExit("thread/start returned no thread id: %s" % json.dumps(started)[:500])
        request("thread/goal/set", {"threadId": tid, "objective": a.objective})
        if a.status:
            request("thread/goal/set", {"threadId": tid, "status": a.status})
        print(tid)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
