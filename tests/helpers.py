"""Shared test helpers: import path, fixture homes and CLI runner."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import make_fixtures  # noqa: E402
from codex_goals import cli  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "codex_goals.py")


def have_zstd():
    try:
        from compression import zstd  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import zstandard  # noqa: F401
        return True
    except ImportError:
        return shutil.which("zstd") is not None


class FixtureCase(unittest.TestCase):
    scenario = "linux"
    legacy_goals = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codex_goals_test_")
        self.home, self.project = make_fixtures.build(self.tmp, self.scenario, legacy_goals=self.legacy_goals)
        # Never let the developer's real Codex leak into a test.
        self._env = dict(os.environ)
        for k in ("CODEX_HOME", "CODEX_SQLITE_HOME", "CLAUDE_PLUGIN_OPTION_SESSION_NOTICE", "CODEX_GOALS_NOTICE"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(args))
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()
