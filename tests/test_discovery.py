import os
import shutil
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401
from codex_goals import discovery


class SqliteHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.req = os.path.join(self.tmp, "requirements.toml")
        self.patch = mock.patch.object(discovery, "requirements_paths", return_value=[self.req])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_default_is_codex_home(self):
        self.assertEqual(discovery.resolve_sqlite_home(self.home, True, {}), (self.home, "CODEX_HOME"))

    def p(self, name):
        """An absolute path on every OS (on Windows "/x" is drive-relative)."""
        return os.path.join(self.tmp, name)

    def test_env_var(self):
        got = discovery.resolve_sqlite_home(self.home, True, {"CODEX_SQLITE_HOME": self.p("data")})
        self.assertEqual(got, (self.p("data"), "$CODEX_SQLITE_HOME"))

    def test_env_var_ignored_for_foreign_homes(self):
        got = discovery.resolve_sqlite_home(self.home, False, {"CODEX_SQLITE_HOME": self.p("data")})
        self.assertEqual(got[1], "CODEX_HOME")

    def test_config_beats_env(self):
        self.write(os.path.join(self.home, "config.toml"),
                   "model = 'x'\nsqlite_home = '%s'\n[tui]\nsqlite_home = 'nope'\n" % self.p("cfg"))
        got = discovery.resolve_sqlite_home(self.home, True, {"CODEX_SQLITE_HOME": self.p("data")})
        self.assertEqual(got, (self.p("cfg"), "config.toml"))

    def test_config_relative_path(self):
        self.write(os.path.join(self.home, "config.toml"), "sqlite_home = 'db'\n")
        got = discovery.resolve_sqlite_home(self.home, True, {})
        self.assertEqual(got[0], os.path.join(self.home, "db"))

    def test_only_top_level_key_counts(self):
        self.write(os.path.join(self.home, "config.toml"), '[profile]\nsqlite_home = "/nested"\n')
        self.assertEqual(discovery.resolve_sqlite_home(self.home, True, {})[1], "CODEX_HOME")

    def test_requirements_beat_everything(self):
        self.write(os.path.join(self.home, "config.toml"), "sqlite_home = '%s'\n" % self.p("cfg"))
        self.write(self.req, "sqlite_home = '%s'\n" % self.p("policy"))
        got = discovery.resolve_sqlite_home(self.home, True, {"CODEX_SQLITE_HOME": self.p("data")})
        self.assertEqual(got[0], self.p("policy"))
        self.assertTrue(got[1].startswith("requirements.toml"))

    def test_fallback_parser_without_tomllib(self):
        self.write(os.path.join(self.home, "config.toml"), "sqlite_home = '%s' # comment\n" % self.p("cfg"))
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "tomllib":
                raise ImportError
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            got = discovery.resolve_sqlite_home(self.home, True, {})
        self.assertEqual(got[0], self.p("cfg"))


class FindDbsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, *names):
        for n in names:
            open(os.path.join(self.tmp, n), "w").close()

    def test_highest_version_wins(self):
        self.touch("goals_1.sqlite", "state_4.sqlite", "state_5.sqlite", "thread_history_1.sqlite",
                   "logs_2.sqlite", "state_5.sqlite-wal")
        dbs, warnings = discovery.find_dbs(self.tmp)
        self.assertEqual(dbs["state"].version, 5)
        self.assertEqual(sorted(dbs), ["goals", "state", "thread_history"])
        self.assertEqual(warnings, [])

    def test_untested_version_warns(self):
        self.touch("goals_1.sqlite", "goals_2.sqlite")
        dbs, warnings = discovery.find_dbs(self.tmp)
        self.assertEqual(dbs["goals"].version, 2)
        self.assertFalse(dbs["goals"].tested)
        self.assertTrue(any("goals_2" in w for w in warnings))


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_override_and_rejection(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        d = discovery.discover(empty, env={})
        self.assertEqual(d.homes, [])
        self.assertIn("no Codex data", d.skipped[0][1])
        os.makedirs(os.path.join(empty, "sessions"))
        d = discovery.discover(empty, env={})
        self.assertEqual(len(d.homes), 1)
        self.assertEqual(d.homes[0].origin, "--home")

    def test_codex_home_env_first_and_deduplicated(self):
        home = os.path.join(self.tmp, "ch")
        os.makedirs(home)
        open(os.path.join(home, "goals_1.sqlite"), "w").close()
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            d = discovery.discover(None, env={"CODEX_HOME": home})
        self.assertEqual(d.homes[0].origin, "$CODEX_HOME")
        self.assertEqual(d.homes[0].path, home)

    def test_wsl_candidates(self):
        with mock.patch.object(discovery, "is_wsl", return_value=True), \
                mock.patch.object(discovery.os, "name", "posix"), \
                mock.patch.object(discovery.sys, "platform", "linux"), \
                mock.patch.object(discovery.glob, "glob", side_effect=lambda p, **k: [p.replace("*", "ana")]):
            cands = discovery.candidate_homes({})
        paths = [c[0] for c in cands]
        self.assertIn("/mnt/c/Users/ana/.codex", paths)
        self.assertIn("/mnt/c/Users/ana/AppData/Local/OpenAI/CodexAppHome", paths)
        self.assertTrue(all(not c[2] for c in cands if c[0].startswith("/mnt/c")))

    def test_windows_candidates(self):
        env = {"USERPROFILE": r"C:\Users\ana", "LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}
        with mock.patch.object(discovery.os, "name", "nt"), \
                mock.patch.object(discovery.glob, "glob", return_value=[r"\\wsl.localhost\Ubuntu\home\ana\.codex"]):
            cands = discovery.candidate_homes(env)
        origins = [c[1] for c in cands]
        self.assertIn("%USERPROFILE%", origins)
        self.assertIn("Codex app (%LOCALAPPDATA%)", origins)
        self.assertIn("WSL distribution", origins)


if __name__ == "__main__":
    unittest.main()
