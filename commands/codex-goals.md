---
description: List unfinished OpenAI Codex /goal goals for this project and resume one in Claude Code
argument-hint: "[goal-id | --all]"
---

Arguments: `$ARGUMENTS`

The helper script is read-only on Codex's data. Run it with Python 3.9+
(`python3`; on Windows `python` or `py -3` if `python3` is missing):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/codex_goals.py" <command>
```

Do this:

1. If the arguments contain a goal id (anything that is not an option), skip to
   step 3 with that id.

2. Otherwise run `list` (add `--all` if the arguments contain `--all`).
   - If it shows goals, present them briefly (id, status, age, objective) and
     ask the user which one to resume with AskUserQuestion (one option per goal,
     at most four; mention any others in the question text). If the user picks
     none, stop.
   - If it shows none: run `where` and tell the user which Codex installations
     were found and which paths were checked. If `list` said there are goals in
     other projects, offer `/codex-goals --all`. Then stop.

3. Resume the chosen goal by following the `resume-codex-goal` skill from its
   step 2 (read the brief, check it against the repo, report to the user and wait
   for their decision before changing anything).
