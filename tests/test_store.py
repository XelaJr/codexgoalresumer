import glob
import os
import sqlite3
import unittest
from unittest import mock

import helpers
from codex_goals import discovery, store


def snapshot(home):
    """mtime and size of every database file, to prove nothing was written."""
    out = {}
    for p in glob.glob(os.path.join(home, "*.sqlite*")):
        st = os.stat(p)
        out[p] = (st.st_mtime_ns, st.st_size)
    return out


class ReadOnlyTest(helpers.FixtureCase):
    def test_databases_are_never_modified(self):
        before = snapshot(self.home)
        self.assertTrue(before)
        for args in (["where"], ["list", "--all", "--include-complete"], ["brief", "01a0b10c", "--full"],
                     ["brief", "01a0017a"], ["list", "--json", "--cwd", self.project]):
            code, _, err = self.run_cli(*args, "--home", self.home)
            self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(glob.glob(os.path.join(self.home, "*-journal")))

    def test_write_attempt_fails(self):
        conn = store.open_ro(os.path.join(self.home, "goals_1.sqlite"))
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM thread_goals")
        conn.close()

    def test_locked_database_falls_back_to_copy(self):
        path = os.path.join(self.home, "goals_1.sqlite")
        locker = sqlite3.connect(path, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(store, "BUSY_TIMEOUT_MS", 50), mock.patch.object(store, "RETRIES", 1):
                conn = store.open_ro(path)
                n = conn.execute("SELECT count(*) FROM thread_goals").fetchone()[0]
                conn.close()
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        self.assertGreater(n, 0)

    def test_drvfs_paths_are_copied(self):
        self.assertTrue(store._needs_copy("/mnt/c/Users/ana/.codex/goals_1.sqlite"))
        self.assertTrue(store._needs_copy(r"\\wsl.localhost\Ubuntu\home\ana\.codex\goals_1.sqlite"))
        self.assertFalse(store._needs_copy("/home/ana/.codex/goals_1.sqlite"))

    def test_missing_file(self):
        with self.assertRaises(store.ReadError):
            store.open_ro(os.path.join(self.home, "nope.sqlite"))


class SchemaToleranceTest(helpers.FixtureCase):
    def _rebuild_goals(self, columns_sql, insert_sql):
        path = os.path.join(self.home, "goals_1.sqlite")
        os.remove(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE thread_goals (%s)" % columns_sql)
        conn.execute(insert_sql)
        conn.commit()
        conn.close()

    def test_optional_columns_missing(self):
        self._rebuild_goals("thread_id TEXT, objective TEXT, status TEXT",
                            "INSERT INTO thread_goals VALUES ('01a0b10c-5690-7820-96b9-81487adf618b', 'Do it', 'blocked')")
        code, out, err = self.run_cli("list", "--home", self.home, "--cwd", self.project)
        self.assertEqual(code, 0, err)
        self.assertIn("Do it", out)
        code, out, err = self.run_cli("brief", "01a0b10c", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn("Usage: — tokens", out)

    def test_required_column_missing_is_exit_3(self):
        self._rebuild_goals("thread_id TEXT, status TEXT", "INSERT INTO thread_goals VALUES ('x', 'active')")
        code, _, err = self.run_cli("list", "--home", self.home)
        self.assertEqual(code, 3)
        self.assertIn("objective", err)


class LegacyGoalsTableTest(helpers.FixtureCase):
    legacy_goals = True

    def test_goals_in_state_db(self):
        self.assertFalse(os.path.exists(os.path.join(self.home, "goals_1.sqlite")))
        code, out, err = self.run_cli("list", "--home", self.home, "--cwd", self.project)
        self.assertEqual(code, 0, err)
        self.assertIn("Finish the export feature", out)


class UntestedVersionTest(helpers.FixtureCase):
    def test_goals_2_is_used_with_warning(self):
        os.rename(os.path.join(self.home, "goals_1.sqlite"), os.path.join(self.home, "goals_2.sqlite"))
        code, out, _ = self.run_cli("where", "--home", self.home)
        self.assertEqual(code, 0)
        self.assertIn("goals_2 (untested version)", out)
        code, out, _ = self.run_cli("brief", "01a0b10c", "--home", self.home, "--no-git")
        self.assertEqual(code, 0)
        self.assertIn("Warning: goals_2.sqlite is newer", out)


class DiscoveryOnFixtureTest(helpers.FixtureCase):
    def test_where_json(self):
        code, out, _ = self.run_cli("where", "--json", "--home", self.home)
        import json
        data = json.loads(out)
        h = data["homes"][0]
        self.assertEqual(h["origin"], "--home")
        self.assertEqual(set(h["databases"]), {"goals", "state", "thread_history"})
        self.assertEqual(h["goals_unfinished"], 5 if helpers.os.path.exists(helpers.make_fixtures.ZST_FIXTURE) else 4)

    def test_sqlite_home_from_env_with_codex_home(self):
        # Databases moved elsewhere, pointed to by CODEX_SQLITE_HOME.
        other = os.path.join(self.tmp, "sqlite")
        os.makedirs(other)
        for p in glob.glob(os.path.join(self.home, "*.sqlite")):
            os.rename(p, os.path.join(other, os.path.basename(p)))
        os.environ["CODEX_HOME"] = self.home
        os.environ["CODEX_SQLITE_HOME"] = other
        with mock.patch.object(discovery, "requirements_paths", return_value=[]):
            code, out, _ = self.run_cli("where")
        self.assertEqual(code, 0)
        self.assertIn("$CODEX_SQLITE_HOME", out)
        self.assertIn("5 unfinished" if os.path.exists(helpers.make_fixtures.ZST_FIXTURE) else "4 unfinished", out)


if __name__ == "__main__":
    unittest.main()
