# codexgoalresumer

Resume unfinished **OpenAI Codex `/goal` goals** in **Claude Code**.

Codex's `/goal` keeps an agent working on an objective for hours, until it is
complete, blocked, paused or out of budget. When a goal stops halfway, this
Claude Code plugin lets you:

1. **see** the unfinished Codex goals of the project you are in,
2. get a **handoff brief** of about two pages: what was asked, Codex's plan, what it
   changed, which commands failed, what its subagents did, why it stopped and what
   has changed in the repo since,
3. have **Claude finish the work** on its own branch, after checking the brief against
   the repo and agreeing the next steps with you.

It works on Linux, macOS and Windows (native and WSL, including Codex and Claude Code
running on different sides). It only **reads** Codex's data and never writes to it.

> Español más abajo: [Resumen en español](#resumen-en-español).

## Install

Requires Claude Code and Python 3.9 or newer (`python3`, or `python`/`py` on Windows).
There is nothing else to install: the helper script only uses Python's standard library.

In Claude Code:

```
/plugin marketplace add XelaJr/codexgoalresumer
/plugin install codexgoalresumer@codexgoalresumer
```

Restart Claude Code if it asks you to.

## Use

Open Claude Code in the project where Codex was working, then either:

- run **`/codex-goals`**: it lists the project's unfinished goals and asks which one to resume;
- run **`/codex-goals <id>`** to go straight to a goal (any unique prefix of the id works);
- run **`/codex-goals --all`** to see goals from every project;
- or just ask: *"resume my Codex goal"*, *"finish what Codex started"*,
  *"retoma el goal de Codex"*.

What happens next:

1. Claude generates the brief and reads it.
2. It checks the brief against the repo: new commits, a different branch, files that
   changed, tests that still fail, work Codex did in a *different* repository.
3. It **reports and stops**, e.g. *"Codex completed 3 of 6 steps. It got blocked on
   step 4 because the integration test needs staging credentials. I propose to mock
   it and continue with steps 5 and 6."*
4. You decide: go ahead, change the plan, or stop.
5. Claude works on a branch called `codex-goal/<id>`, runs the tests and tells you what
   is done and what is still open. It does **not** commit, push or open a PR unless you
   ask.
6. The goal stays open in Codex with its old status. Close it from Codex when you are
   happy with the result.

If the blocker depends on you (credentials, access to a machine, a decision), Claude
asks instead of working around it.

### Optional: notice at session start

Off by default. When enabled, starting Claude Code in a project with unfinished Codex
goals shows one line such as:

```
Codex goal in this project: 1 blocked («complete the plan»). Use /codex-goals to resume.
```

Enable it with the plugin's **Session start notice** option (`/plugin`, select
codexgoalresumer, configure), or with `claude plugin configure codexgoalresumer`.
It adds about 60 ms to session start when enabled and nothing when disabled.

## Command line

The plugin is a thin layer over `scripts/codex_goals.py`, which you can also run
yourself:

```
$ python3 scripts/codex_goals.py where
CODEX_HOME  ~/.codex (origin: default path)
  sqlite    ~/.codex (origin: CODEX_HOME)
  DB        goals_1 ✓  state_5 ✓  thread_history_1 ✓
  goals     14 total · 2 unfinished

$ python3 scripts/codex_goals.py list
ID        STATUS   UPDATED     BRANCH @ COMMIT  OBJECTIVE
01a1583f  blocked  1 day ago   main @ 196e884   complete the plan

$ python3 scripts/codex_goals.py brief 01a1583f
# Codex goal 01a1583f · blocked
Project: ~/src/app · main @ 196e884 · source: ~/.codex
...
```

| Command | What it does | Options |
|---|---|---|
| `where` | Which Codex installations were found, where their databases are, and which paths were checked | `--home PATH`, `--json` |
| `list` | Unfinished goals whose working directory is the current directory or below | `--all`, `--include-complete`, `--status blocked,paused`, `--cwd PATH`, `--home`, `--json` |
| `brief <id>` | The handoff brief (Markdown; about 8 000 tokens at most by default) | `--max-tokens N`, `--full`, `--no-git`, `--home`, `--json` |
| `export <id> --anonymize` | An anonymised copy of one goal, to contribute test fixtures | `--out DIR`, `--home` |

Exit codes: `0` ok, `1` goal not found or ambiguous prefix, `2` read error, `3` unsupported
Codex database schema.

## Where it looks

It never assumes a single path. It checks, in order:

- `$CODEX_HOME`, then `~/.codex` (`%USERPROFILE%\.codex` on Windows);
- the Codex Windows app folder `%LOCALAPPDATA%\OpenAI\CodexAppHome`;
- from WSL, the Windows side (`/mnt/c/Users/*/.codex`, `.../CodexAppHome`);
- from Windows, WSL distributions (`\\wsl.localhost\*\home\*\.codex`).

The databases' folder is resolved the way Codex does it: a system `requirements.toml`
policy, then `sqlite_home` in `config.toml`, then `$CODEX_SQLITE_HOME`, then
`CODEX_HOME`. It picks the highest version of `goals_*.sqlite`, `state_*.sqlite` and
`thread_history_*.sqlite`, and warns if that version is newer than the ones it was tested
with. History is read from `thread_history_*.sqlite`, then the session files
`sessions/**/rollout-*.jsonl`, then `rollout-*.jsonl.zst` (Python 3.14+, the
`zstandard` package or the `zstd` command).

Paths recorded on one system are matched on the other: `C:\x`, `c:\X` and `/mnt/c/x`
are the same project, and so are `/private/var/...` and `/var/...` on macOS.

If no goal shows up, run `where`: it prints every path it checked and why it was
skipped. `--home PATH` forces a specific installation.

## Read-only guarantee

- Databases are opened with SQLite's `mode=ro` and `query_only`. If Codex holds a lock
  (or the files sit on a WSL/DrvFS mount), the script copies the database to a temporary
  folder and reads the copy. You do not need to close Codex.
- Nothing is ever written to Codex's folders, not even to mark a goal complete. The test
  suite checks that the databases' modification times do not change.
- The Claude skill is told never to write to Codex's files and to close goals from Codex.

## Privacy

Everything runs locally. The brief is passed to Claude like any file you open in Claude
Code, so it goes to the model you use there. Before that, the script masks secrets in
every part of the brief, including `--full` and `--json`: API keys (OpenAI, Anthropic,
GitHub, AWS, Google, Slack, Hugging Face, npm), Bearer tokens, JWTs, private keys,
passwords in URLs, `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` variables, `auth.json`
fields and passwords written in plain language ("password is ...", "contraseña ...").
A secret found anywhere is masked everywhere it appears. Masking is pattern-based and
cannot catch everything: if your Codex history contains secrets in unusual formats,
review the brief (`codex_goals.py brief <id> --full`) before resuming.

## How it works

```
scripts/codex_goals.py        CLI entry point (where · list · brief · export)
scripts/codex_goals/
  discovery.py                candidate CODEX_HOMEs, sqlite_home, versioned databases
  store.py                    read-only SQLite, retries, temporary copy
  history.py                  thread_items → rollout .jsonl → .jsonl.zst, both rollout formats
  paths.py                    C:\ ↔ /mnt/c, case, \\?\, /private/var
  brief.py                    deterministic extraction, size levels, git context
  redact.py                   secret masking
  anonymize.py                export --anonymize
skills/resume-codex-goal/     the workflow Claude follows
commands/codex-goals.md       /codex-goals
hooks/                        optional SessionStart notice
tests/                        stdlib unittest suite and synthetic fixtures
```

The brief is built deterministically from what Codex recorded (no summarisation by a
model): the objective and the first user messages, status, tokens and time against the
budget, the last `update_plan` with each step's state, changed files, the most recent
failure of each command that is still failing (searches that merely found nothing are
left out), subagents, the last messages, the agent's explanation right after it marked
the goal blocked, and `git log` / `diff --stat` since the commit the goal started from,
for the project and for any other repository Codex edited. An "Omitted" section counts
what was trimmed.

## Contributing test data

Codex changes its storage format from time to time. If the plugin misreads one of your
goals, especially on macOS or Windows, you can share an anonymised copy:

```
python3 scripts/codex_goals.py export <id> --anonymize --out ./my-fixture
```

Texts, paths, commands, branch names, hashes and URLs are replaced by placeholders while
the structure (tables, JSON keys, item types, statuses, exit codes, lengths) is kept.
**Review the files before sharing them**; automatic anonymisation can miss things. Then
open an issue and attach them.

## Development

```
python3 -m unittest discover -s tests -t tests
```

Fixtures are generated at test time from schemas produced by the
[openai/codex](https://github.com/openai/codex) migrations (`tests/fixtures/schema/`,
Apache-2.0). CI runs the suite on Ubuntu, macOS and Windows with Python 3.9 and 3.14.
A weekly job installs the latest Codex release on all three systems, lets Codex itself
create a goal through its app-server (no API key needed, `tests/real_codex_goal.py`) and
checks that `where`, `list` and `brief` find it in the default `CODEX_HOME`.

## Resumen en español

**codexgoalresumer** es un plugin de Claude Code para retomar los `/goal` de Codex que
se quedaron a medias (bloqueados, en pausa o sin presupuesto).

- Instalación: `/plugin marketplace add XelaJr/codexgoalresumer` y después
  `/plugin install codexgoalresumer@codexgoalresumer`. Solo necesita Python 3.9 o superior.
- Uso: abre Claude Code en el proyecto y ejecuta `/codex-goals`, o pide «retoma el goal de
  Codex». Claude lee un resumen de traspaso (qué se pidió, qué hizo Codex, dónde se
  atascó), lo contrasta con el repositorio, te cuenta qué propone y **espera a que
  decidas**. Trabaja en la rama `codex-goal/<id>` y no hace commit, push ni PR si no se lo
  pides.
- Solo lectura: nunca escribe en los ficheros de Codex. El goal sigue abierto en Codex y
  se cierra desde allí.
- Privacidad: todo se ejecuta en local y los secretos (claves, tokens, contraseñas) se
  ocultan antes de pasárselos a Claude.
- Funciona en Linux, macOS, Windows y WSL. Si no aparece ningún goal,
  `python3 scripts/codex_goals.py where` muestra dónde ha buscado.

## License

MIT. See [LICENSE](LICENSE).
