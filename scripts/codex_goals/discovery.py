"""Find every Codex installation on this machine without assuming a single path.

A *home* is a ``CODEX_HOME`` directory (``~/.codex`` by default). Its SQLite
databases may live elsewhere (``sqlite_home``); this module resolves that with
the same precedence Codex uses:

    requirements.toml (system policy) -> config.toml -> $CODEX_SQLITE_HOME -> CODEX_HOME
"""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass, field

from .paths import norm

# Versions this tool has been tested against (openai/codex state/src/sqlite.rs).
KNOWN_DB_VERSIONS = {"goals": 1, "state": 5, "thread_history": 1}
DB_KINDS = ("goals", "state", "thread_history")


@dataclass
class DbFile:
    kind: str
    version: int
    path: str

    @property
    def tested(self) -> bool:
        return self.version <= KNOWN_DB_VERSIONS.get(self.kind, 0)

    @property
    def name(self) -> str:
        return "%s_%d" % (self.kind, self.version)


@dataclass
class Home:
    path: str
    origin: str
    sqlite_home: str = ""
    sqlite_origin: str = ""
    dbs: dict = field(default_factory=dict)  # kind -> DbFile
    warnings: list = field(default_factory=list)

    def db(self, kind: str) -> DbFile | None:
        return self.dbs.get(kind)

    def session_dirs(self) -> list:
        return [d for d in (os.path.join(self.path, "sessions"),
                            os.path.join(self.path, "archived_sessions")) if os.path.isdir(d)]


@dataclass
class Discovery:
    homes: list
    skipped: list  # (path, reason)


# ---------------------------------------------------------------- toml helpers

def _read_top_level_key(toml_path: str, key: str) -> str | None:
    """Read a top-level string key from a TOML file (stdlib only, any Python)."""
    try:
        with open(toml_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        import tomllib  # Python 3.11+
        try:
            value = tomllib.loads(raw.decode("utf-8")).get(key)
        except Exception:
            value = None
        return value if isinstance(value, str) else None
    except ImportError:
        pass
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            break  # only top-level keys count
        m = re.match(r'^%s\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\')' % re.escape(key), s)
        if m:
            if m.group(1) is not None:
                return _toml_unescape(m.group(1))
            return m.group(2)
    return None


_TOML_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _toml_unescape(value: str) -> str:
    """Basic-string escapes only; leaves non-ASCII text (``José``) intact."""
    def repl(m):
        esc = m.group(1)
        if esc[0] in "uU":
            return chr(int(esc[1:], 16))
        return _TOML_ESCAPES.get(esc, esc)
    return re.sub(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", repl, value)


def requirements_paths() -> list:
    if os.name == "nt":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return [os.path.join(base, "OpenAI", "Codex", "requirements.toml")]
    return ["/etc/codex/requirements.toml"]


def _expand(path: str, base: str) -> str:
    path = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.normpath(path)


def resolve_sqlite_home(home: str, use_env: bool, env=None) -> tuple:
    """Return ``(sqlite_home, origin)`` for a CODEX_HOME."""
    env = os.environ if env is None else env
    for req in requirements_paths():
        value = _read_top_level_key(req, "sqlite_home")
        if value:
            return _expand(value, home), "requirements.toml (%s)" % req
    value = _read_top_level_key(os.path.join(home, "config.toml"), "sqlite_home")
    if value:
        return _expand(value, home), "config.toml"
    if use_env and env.get("CODEX_SQLITE_HOME"):
        return _expand(env["CODEX_SQLITE_HOME"], home), "$CODEX_SQLITE_HOME"
    return home, "CODEX_HOME"


# ---------------------------------------------------------------- db files

_DB_RE = re.compile(r"^(goals|state|thread_history)_(\d+)\.sqlite$")


def find_dbs(sqlite_home: str) -> tuple:
    """Highest version of each versioned database in ``sqlite_home``."""
    found, warnings = {}, []
    try:
        names = os.listdir(sqlite_home)
    except OSError:
        return found, warnings
    for name in names:
        m = _DB_RE.match(name)
        if not m:
            continue
        kind, version = m.group(1), int(m.group(2))
        cur = found.get(kind)
        if cur is None or version > cur.version:
            found[kind] = DbFile(kind, version, os.path.join(sqlite_home, name))
    for db in found.values():
        if not db.tested:
            warnings.append("%s.sqlite is newer than the tested %s_%d.sqlite; "
                            "reading what is understood, some brief sections may be missing"
                            % (db.name, db.kind, KNOWN_DB_VERSIONS[db.kind]))
    return found, warnings


def looks_like_codex_home(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if glob.glob(os.path.join(glob.escape(path), "goals_*.sqlite")):
        return True
    if glob.glob(os.path.join(glob.escape(path), "state_*.sqlite")):
        return True
    return os.path.isdir(os.path.join(path, "sessions"))


# ---------------------------------------------------------------- candidates

def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def wsl_distros() -> list:
    """Installed WSL distributions, from ``wsl.exe -l -q`` (UTF-16 output)."""
    import subprocess

    try:
        r = subprocess.run(["wsl.exe", "-l", "-q"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    raw = r.stdout
    text = raw.decode("utf-16-le", "ignore") if b"\x00" in raw else raw.decode("utf-8", "ignore")
    return [line.strip().strip("\x00") for line in text.splitlines() if line.strip().strip("\x00")]


def wsl_homes_from_windows() -> list:
    """``.codex`` folders inside WSL distributions, seen from Windows."""
    found = []
    roots = [r"\\wsl.localhost", r"\\wsl$"]
    distros = wsl_distros()
    for root in roots:
        bases = [os.path.join(root, d) for d in distros]
        bases += glob.glob(os.path.join(root, "*"))  # works on some Windows builds
        for base in bases:
            for pattern in (os.path.join(base, "home", "*", ".codex"), os.path.join(base, "root", ".codex")):
                for p in sorted(glob.glob(pattern)):
                    if p not in found:
                        found.append(p)
        if found:
            break  # \\wsl$ is an alias of \\wsl.localhost
    return found


def candidate_homes(env=None) -> list:
    """List of ``(path, origin, from_current_env)`` in priority order."""
    env = os.environ if env is None else env
    out = []
    if env.get("CODEX_HOME"):
        out.append((os.path.expanduser(env["CODEX_HOME"]), "$CODEX_HOME", True))
    out.append((os.path.join(os.path.expanduser("~"), ".codex"), "default path", True))
    if os.name == "nt" or sys.platform == "cygwin":
        if env.get("USERPROFILE"):
            out.append((os.path.join(env["USERPROFILE"], ".codex"), "%USERPROFILE%", True))
        if env.get("LOCALAPPDATA"):
            out.append((os.path.join(env["LOCALAPPDATA"], "OpenAI", "CodexAppHome"),
                        "Codex app (%LOCALAPPDATA%)", True))
        for p in wsl_homes_from_windows():
            out.append((p, "WSL distribution", False))
    elif sys.platform.startswith("linux") and not is_wsl():
        out.append(("/mnt/c/Users/*/.codex", "Windows user (not WSL, skipped)", False))
    elif is_wsl():
        for p in sorted(glob.glob("/mnt/c/Users/*/.codex")):
            out.append((p, "Windows user (from WSL)", False))
        for p in sorted(glob.glob("/mnt/c/Users/*/AppData/Local/OpenAI/CodexAppHome")):
            out.append((p, "Windows Codex app (from WSL)", False))
    return out


def discover(home_override: str | None = None, env=None) -> Discovery:
    env = os.environ if env is None else env
    if home_override:
        cands = [(os.path.expanduser(home_override), "--home", True)]
    else:
        cands = candidate_homes(env)
    homes, skipped, seen = [], [], set()
    for path, origin, from_env in cands:
        key = norm(os.path.realpath(path) if os.path.exists(path) else path)
        if key in seen:
            continue
        seen.add(key)
        if "skipped" in origin:
            skipped.append((path, "not WSL"))
            continue
        if not os.path.isdir(path):
            skipped.append((path, "does not exist"))
            continue
        sqlite_home, sqlite_origin = resolve_sqlite_home(path, use_env=from_env, env=env)
        dbs, warnings = find_dbs(sqlite_home)
        if not dbs and sqlite_home != path:
            # A configured sqlite_home that is empty: fall back to CODEX_HOME.
            fb, fbw = find_dbs(path)
            if fb:
                warnings.append("sqlite_home %s has no databases; using %s" % (sqlite_home, path))
                dbs, warnings, sqlite_home = fb, warnings + fbw, path
        if not dbs and not looks_like_codex_home(path):
            skipped.append((path, "no Codex data (no goals_*.sqlite or sessions/)"))
            continue
        homes.append(Home(path=path, origin=origin, sqlite_home=sqlite_home,
                          sqlite_origin=sqlite_origin, dbs=dbs, warnings=warnings))
    return Discovery(homes=homes, skipped=skipped)
