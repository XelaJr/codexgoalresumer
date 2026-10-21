"""Regression tests for the issues found in the v0.1.2 review."""

import json
import os
import sqlite3
import unittest
from unittest import mock

import helpers
from codex_goals import brief, discovery, store

F = helpers.make_fixtures
KEY = "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"


def listing(folder):
    return sorted(os.listdir(folder))


class WalNeverCreatesFilesTest(helpers.FixtureCase):
    def setUp(self):
        super().setUp()
        for name in ("goals_1", "state_5", "thread_history_1"):
            conn = sqlite3.connect(os.path.join(self.home, name + ".sqlite"))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.close()  # a clean close removes -wal and -shm, as when Codex is not running

    def test_reading_wal_databases_leaves_codex_folder_untouched(self):
        before = listing(self.home)
        self.assertFalse([f for f in before if f.endswith(("-wal", "-shm"))])
        for args in (["where"], ["list", "--all"], ["brief", "01a0b10c", "--no-git"]):
            code, _, err = self.run_cli(*args, "--home", self.home)
            self.assertEqual(code, 0, err)
        self.assertEqual(listing(self.home), before)

    def test_copy_is_made_once_per_run(self):
        path = os.path.join(self.home, "state_5.sqlite")
        self.assertEqual(store._copy_to_temp(path), store._copy_to_temp(path))


class SecretsOutsideBriefTest(helpers.FixtureCase):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(os.path.join(self.home, "goals_1.sqlite"))
        conn.execute("UPDATE thread_goals SET objective = ? WHERE thread_id = ?",
                     ("deploy with OPENAI_API_KEY=%s" % KEY, F.ID_BLOCKED))
        conn.commit()
        conn.close()

    def test_list_table_and_json(self):
        for extra in ((), ("--json",)):
            code, out, _ = self.run_cli("list", "--all", "--home", self.home, *extra)
            self.assertEqual(code, 0)
            self.assertNotIn(KEY[:20], out)

    def test_session_notice(self):
        os.environ["CODEX_GOALS_NOTICE"] = "1"
        code, out, _ = self.run_cli("notify", "--home", self.home, "--cwd", self.project)
        self.assertNotIn(KEY[:20], out)


class UnexpectedInputTest(helpers.FixtureCase):
    def test_corrupt_database_is_a_read_error(self):
        with open(os.path.join(self.home, "goals_9.sqlite"), "wb") as fh:
            fh.write(b"not a database at all" * 100)
        code, _, err = self.run_cli("list", "--home", self.home)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)

    def test_renamed_order_column_degrades(self):
        conn = sqlite3.connect(os.path.join(self.home, "thread_history_1.sqlite"))
        conn.execute("DROP INDEX IF EXISTS idx_thread_items_page")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='thread_items' "
                                "AND name NOT LIKE 'sqlite_autoindex%'").fetchall():
            conn.execute('DROP INDEX "%s"' % row[0])
        conn.execute("ALTER TABLE thread_items RENAME COLUMN rollout_ordinal TO ordinal_v2")
        conn.commit()
        conn.close()
        code, out, err = self.run_cli("brief", "01a0b10c", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn("## Where it stopped", out)

    def test_message_with_unexpected_type(self):
        path = os.path.join(self.home, "sessions")
        rollout = [os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs if F.ID_LEGACY in f][0]
        with open(rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                                   "message": [{"type": "text", "text": "hi"}]}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": {"x": 1}}}) + "\n")
        code, out, err = self.run_cli("brief", "01a0017a", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)

    def test_unexpected_exception_is_reported_not_traced(self):
        with mock.patch.object(helpers.cli, "cmd_list", side_effect=RuntimeError("boom")):
            code, _, err = self.run_cli("list", "--home", self.home)
        self.assertEqual(code, 2)
        self.assertIn("unexpected error", err)


class ExportSymlinkTest(helpers.FixtureCase):
    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlink_into_codex_home_is_refused(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.home, link)
        code, _, err = self.run_cli("export", "01a0b10c", "--anonymize", "--out", os.path.join(link, "x"),
                                    "--home", self.home)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.home, "x")))


class RelativePathTest(unittest.TestCase):
    def test_mixed_forms(self):
        self.assertEqual(brief._rel("/var/folders/ab/proj/src/a.py", "/private/var/folders/ab/proj"), "src/a.py")
        self.assertEqual(brief._rel("/mnt/c/proj/src/a.py", "C:\\proj"), "src/a.py")
        self.assertEqual(brief._rel("C:\\Proj\\Src\\A.py", "c:/proj"), "Src/A.py")
        self.assertEqual(brief._rel("/home/ana/proj", "/home/ana/proj"), ".")
        self.assertEqual(brief._rel("/srv/x.py", "/home/ana/proj"), "/srv/x.py")


class TomlFallbackTest(unittest.TestCase):
    def test_unescape_keeps_non_ascii(self):
        self.assertEqual(discovery._toml_unescape("/home/José/db"), "/home/José/db")
        self.assertEqual(discovery._toml_unescape("C:\\\\Users\\\\x"), "C:\\Users\\x")
        self.assertEqual(discovery._toml_unescape("a\\u00e9b"), "aéb")


class ListRelativeCwdTest(helpers.FixtureCase):
    scenario = "linux"

    def setUp(self):
        super().setUp()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        self.home, self.project = F.build(self.tmp, "linux", project=proj)
        self._old = os.getcwd()
        os.chdir(proj)

    def tearDown(self):
        os.chdir(self._old)
        super().tearDown()

    def test_dot(self):
        code, out, _ = self.run_cli("list", "--cwd", ".", "--home", self.home)
        self.assertEqual(code, 0)
        self.assertIn("01a0b10c", out)


if __name__ == "__main__":
    unittest.main()
