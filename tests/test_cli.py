import json
import os
import subprocess
import sys
import unittest

import helpers
from codex_goals import brief, history

F = helpers.make_fixtures


class ListTest(helpers.FixtureCase):
    def test_default_is_unfinished_in_project(self):
        code, out, _ = self.run_cli("list", "--home", self.home, "--cwd", self.project)
        self.assertEqual(code, 0)
        self.assertIn("01a0b10c", out)
        self.assertIn("01a0ac71", out)  # subdirectory of the project
        self.assertNotIn("01a0d0e0", out)  # complete
        self.assertNotIn("01a007e5", out)  # other project

    def test_all_and_complete_and_status(self):
        code, out, _ = self.run_cli("list", "--all", "--include-complete", "--home", self.home)
        self.assertIn("PROJECT", out)
        self.assertIn("01a0d0e0", out)
        self.assertIn("/srv/other", out)
        code, out, _ = self.run_cli("list", "--all", "--status", "blocked,paused", "--json", "--home", self.home)
        statuses = {g["status"] for g in json.loads(out)["goals"]}
        self.assertEqual(statuses, {"blocked", "paused"})

    def test_empty_project_hints_all(self):
        code, out, _ = self.run_cli("list", "--home", self.home, "--cwd", "/nowhere")
        self.assertEqual(code, 0)
        self.assertIn("No unfinished Codex goals", out)
        self.assertIn("--all", out)


class CrossPlatformTest(unittest.TestCase):
    """Goals recorded on one OS are found from the other side."""

    def test_each_scenario(self):
        for scenario in F.SCENARIOS:
            with self.subTest(scenario=scenario):
                case = helpers.FixtureCase()
                case.scenario = scenario
                case.setUp()
                try:
                    code, out, _ = case.run_cli("list", "--json", "--home", case.home, "--cwd", case.project)
                    ids = {g["thread_id"] for g in json.loads(out)["goals"]}
                    self.assertIn(F.ID_BLOCKED, ids)
                    self.assertIn(F.ID_ACTIVE, ids)
                    self.assertIn(F.ID_LEGACY, ids)  # no threads row: cwd from session_meta
                    self.assertNotIn(F.ID_OTHER, ids)
                finally:
                    case.tearDown()


class BriefTest(helpers.FixtureCase):
    def brief(self, *extra):
        code, out, err = self.run_cli("brief", "01a0b10c", "--home", self.home, "--no-git", *extra)
        self.assertEqual(code, 0, err)
        return out

    def test_sections(self):
        out = self.brief()
        for heading in ("# Codex goal 01a0b10c · blocked", "## Objective", "## Codex plan (last update_plan",
                        "## What it did", "## Where it stopped", "## Repo changes since the goal started",
                        "## Omitted"):
            self.assertIn(heading, out)

    def test_plan_marks(self):
        out = self.brief()
        self.assertIn("[x] 1. Read the plan", out)
        self.assertIn("[~] 3. Add integration test against staging  ← in progress when it stopped", out)
        self.assertIn("[ ] 4. Update the README", out)
        self.assertIn("(2 of 4 steps completed)", out)

    def test_failed_commands_without_noise(self):
        out = self.brief()
        self.assertIn("`python -m pytest -q tests/test_integration.py` → exit 2", out)
        self.assertNotIn("tests/test_export.py` →", out)  # failed, then passed
        self.assertNotIn("`rg -n export src`", out)  # search with no results
        self.assertIn("1 of them searches that found nothing", out)

    def test_files_relative_and_subagents(self):
        out = self.brief()
        self.assertIn("- src/export.py (add)", out)
        self.assertIn("Subagents: 1", out)
        self.assertIn("task: Check how the staging connection is configured", out)
        self.assertIn("reads DB_PASSWORD from the env", out)

    def test_block_reason(self):
        out = self.brief()
        self.assertIn("Agent message right after it marked the goal blocked:", out)
        self.assertIn("I need a valid DB_PASSWORD or access to the VPN", out)

    def test_secrets_masked_even_full(self):
        for extra in ((), ("--full",), ("--json",)):
            out = self.brief(*extra)
            self.assertNotIn("Stag1ng#Pass", out)
            self.assertNotIn("Stag1ng", out)
            self.assertNotIn(F.FAKE_KEY, out)
            self.assertIn("[REDACTED]", out)

    def test_json(self):
        data = json.loads(self.brief("--json"))
        self.assertEqual(data["goal"]["status"], "blocked")
        self.assertEqual(data["plan"]["steps"][2][1], "in_progress")
        self.assertEqual(data["subagents_total"], 1)
        self.assertIn("estimated_tokens", data)

    def test_size_limit(self):
        big = self.brief("--full")
        small = self.brief("--max-tokens", "300")
        self.assertLess(len(small), len(big))
        self.assertIn("## Omitted", small)

    def test_prefix_resolution_and_exit_codes(self):
        code, _, err = self.run_cli("brief", "01a0", "--home", self.home)
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        code, _, err = self.run_cli("brief", "zzzz", "--home", self.home)
        self.assertEqual(code, 1)
        code, out, _ = self.run_cli("brief", "goal-618b", "--home", self.home, "--no-git")
        self.assertEqual(code, 1)  # goal_id suffixes are shared by fixtures: ambiguous
        code, out, _ = self.run_cli("brief", F.ID_BLOCKED.upper(), "--home", self.home, "--no-git")
        self.assertEqual(code, 0)

    def test_legacy_rollout(self):
        code, out, err = self.run_cli("brief", "01a0017a", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn("Legacy history", out)
        self.assertIn("Refactor half done", out)
        self.assertIn("Commands run: 1 (0 non-zero", out)

    @unittest.skipUnless(helpers.have_zstd(), "no zstd backend available")
    def test_zst_rollout(self):
        code, out, err = self.run_cli("brief", "01a00257", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn(".jsonl.zst", out)
        self.assertIn("Compressed", out)
        self.assertIn("Refactor half done", out)

    def test_zst_without_backend_explains(self):
        with unittest.mock.patch.object(history.shutil, "which", return_value=None), \
                unittest.mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None,
                                                       "zstandard": None}):
            code, out, err = self.run_cli("brief", "01a00257", "--home", self.home, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn("needs Python 3.14+", out)


class GitContextTest(helpers.FixtureCase):
    def setUp(self):
        super().setUp()
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("git not installed")

    def git(self, repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                              env=env, check=True).stdout.strip()

    def test_commits_since_goal_start(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        self.git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("a")
        self.git(repo, "add", "a.txt")
        self.git(repo, "commit", "-q", "-m", "first")
        start = self.git(repo, "rev-parse", "HEAD")
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("b")
        self.git(repo, "add", "b.txt")
        self.git(repo, "commit", "-q", "-m", "second commit")
        self.git(repo, "checkout", "-q", "-b", "feature")
        info = brief.git_context(repo, start, "main")
        self.assertTrue(info["available"])
        self.assertEqual(len(info["commits"]), 1)
        self.assertIn("second commit", info["commits"][0])
        self.assertTrue(info["branch_changed"])
        self.assertIn("1 file changed", info["diff_summary"])
        missing = brief.git_context(repo, "f" * 40, "main")
        self.assertFalse(missing["sha_exists"])

    def test_not_a_repo(self):
        info = brief.git_context(self.tmp, "abc", "main")
        self.assertFalse(info["available"])
        info = brief.git_context("/definitely/not/here", "abc", "main")
        self.assertIn("not found", info["reason"])


class ExportTest(helpers.FixtureCase):
    def test_anonymize_required(self):
        code, _, err = self.run_cli("export", "01a0b10c", "--home", self.home)
        self.assertEqual(code, 2)
        self.assertIn("--anonymize", err)

    def test_never_writes_inside_codex_home(self):
        code, _, err = self.run_cli("export", "01a0b10c", "--anonymize", "--out",
                                    os.path.join(self.home, "x"), "--home", self.home)
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)
        self.assertFalse(os.path.exists(os.path.join(self.home, "x")))

    def test_anonymized_copy_is_usable_and_clean(self):
        out_dir = os.path.join(self.tmp, "export")
        code, out, err = self.run_cli("export", "01a0b10c", "--anonymize", "--out", out_dir, "--home", self.home)
        self.assertEqual(code, 0, err)
        self.assertIn("Review every file", out)
        exported = os.path.join(out_dir, "codex_home")
        blob = b""
        for root, _, files in os.walk(exported):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    blob += fh.read()
        for secret in (b"Stag1ng", b"staging", b"export feature", b"/home/ana", b"pytest", b"VPN"):
            self.assertNotIn(secret, blob, secret)
        # Structure survives: the anonymised home still produces a brief.
        code, out, err = self.run_cli("brief", "01a0b10c", "--home", exported, "--no-git")
        self.assertEqual(code, 0, err)
        self.assertIn("[~] 3.", out)
        self.assertIn("Subagents: 1", out)
        self.assertIn("· blocked", out)


class NotifyTest(helpers.FixtureCase):
    def test_disabled_by_default(self):
        code, out, _ = self.run_cli("notify", "--home", self.home, "--cwd", self.project)
        self.assertEqual((code, out), (0, ""))

    def test_enabled_by_plugin_option(self):
        os.environ["CLAUDE_PLUGIN_OPTION_NOTICE"] = "true"
        code, out, _ = self.run_cli("notify", "--home", self.home, stdin=json.dumps({"cwd": self.project}))
        self.assertEqual(code, 0)
        msg = json.loads(out)
        self.assertIn("/codex-goals", msg["systemMessage"])
        self.assertIn("1 blocked", msg["systemMessage"])
        self.assertEqual(msg["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(len(msg["systemMessage"].splitlines()), 1)

    def test_silent_without_goals(self):
        os.environ["CODEX_GOALS_NOTICE"] = "1"
        code, out, _ = self.run_cli("notify", "--home", self.home, "--cwd", "/nowhere")
        self.assertEqual((code, out), (0, ""))

    def test_never_fails(self):
        os.environ["CODEX_GOALS_NOTICE"] = "1"
        code, out, _ = self.run_cli("notify", "--home", os.path.join(self.tmp, "missing"), "--cwd", "/x")
        self.assertEqual((code, out), (0, ""))


class ScriptTest(unittest.TestCase):
    def test_help_runs_as_a_script(self):
        r = subprocess.run([sys.executable, helpers.SCRIPT, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Exit codes", r.stdout)

    def test_unit_helpers(self):
        self.assertEqual(brief.unwrap("/usr/bin/bash -lc 'pytest -q'"), "pytest -q")
        self.assertEqual(brief.unwrap('bash -c "make test"'), "make test")
        self.assertEqual(brief.unwrap("pwsh -Command 'Get-Item x'"), "Get-Item x")
        self.assertEqual(brief.unwrap("ls"), "ls")
        self.assertEqual(brief.human_duration(4500), "1 h 15 min")
        self.assertEqual(brief.human_tokens(4774012), "4.77M")


if __name__ == "__main__":
    unittest.main()
