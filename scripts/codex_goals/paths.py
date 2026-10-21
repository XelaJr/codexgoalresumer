"""Path normalisation so a Codex `cwd` can be compared across operating systems.

Codex may have recorded a goal from Windows (``C:\\Users\\me\\proj``), from WSL
(``/mnt/c/Users/me/proj``) or from macOS (``/private/var/...``) while Claude
Code runs on the other side. Every comparison goes through :func:`norm`.
"""

from __future__ import annotations

import os
import re
import sys

_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:/|$)")
_MNT_RE = re.compile(r"^/mnt/([A-Za-z])(?=/|$)")
_WSL_UNC_RE = re.compile(r"^//wsl(?:\.localhost|\$)/[^/]+(?=/|$)", re.IGNORECASE)
_MAC_PRIVATE_RE = re.compile(r"^/private(?=/(?:var|tmp|etc)(?:/|$))")


def norm(path: str | None, platform: str | None = None) -> str:
    """Return a canonical, comparable form of ``path``.

    * ``\\\\?\\`` and ``\\\\?\\UNC\\`` prefixes are removed and backslashes become ``/``.
    * ``C:\\x``, ``c:/X`` and ``/mnt/c/x`` all become ``c:/x``.
    * ``\\\\wsl.localhost\\Distro\\home\\u`` becomes ``/home/u``.
    * macOS ``/private/var`` becomes ``/var``.
    * Trailing slashes are dropped and repeated slashes collapsed.
    * Windows-style paths, and every path on macOS/Windows hosts, are case-folded
      because those file systems are case-insensitive by default.
    """
    if not path:
        return ""
    platform = platform or sys.platform
    p = str(path).strip()
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\") or p.startswith("//?/"):
        p = p[4:]
    p = p.replace("\\", "/")
    unc = p.startswith("//")
    p = re.sub(r"/{2,}", "/", p)
    if unc:
        p = "/" + p
    m = _WSL_UNC_RE.match(p)
    if m:
        p = p[m.end():] or "/"
    windows_style = False
    m = _MNT_RE.match(p)
    if m:
        p = m.group(1).lower() + ":" + (p[m.end():] or "/")
        windows_style = True
    m = _DRIVE_RE.match(p)
    if m:
        p = m.group(1).lower() + ":" + (p[2:] or "/")
        windows_style = True
    p = _MAC_PRIVATE_RE.sub("", p)
    if len(p) > 1 and p.endswith("/") and not re.match(r"^[a-z]:/$", p):
        p = p.rstrip("/")
    if windows_style and p.endswith(":"):
        p += "/"
    if windows_style or unc or platform in ("darwin", "win32", "cygwin"):
        p = p.casefold()
    return p


def is_within(child: str | None, parent: str | None, platform: str | None = None) -> bool:
    """True when ``child`` is ``parent`` or lives below it (after :func:`norm`)."""
    c, p = norm(child, platform), norm(parent, platform)
    if not c or not p:
        return False
    if c == p:
        return True
    prefix = p if p.endswith("/") else p + "/"
    return c.startswith(prefix)


def pretty(path: str | None) -> str:
    """Shorten the user's home directory to ``~`` for display."""
    if not path:
        return ""
    home = os.path.expanduser("~")
    if home and home not in ("~", "/") and is_within(path, home):
        rest = norm(path)[len(norm(home)):]
        return "~" + (rest if rest.startswith("/") else "/" + rest if rest else "")
    return str(path)


def to_local(path: str | None) -> str | None:
    """Best-effort translation of a recorded path into one usable on this host.

    A path recorded on Windows is translated to ``/mnt/<drive>/...`` when running
    under WSL and vice versa, so git and file checks can still find the project.
    """
    if not path:
        return path
    p = str(path)
    if os.path.exists(p):
        return p
    n = p.replace("\\", "/")
    if os.name == "nt":
        m = _MNT_RE.match(n)
        if m:
            cand = m.group(1).upper() + ":" + (n[m.end():] or "/")
            return cand.replace("/", "\\")
        return p
    m = _DRIVE_RE.match(n)
    if m:
        cand = "/mnt/" + m.group(1).lower() + (n[2:] or "/")
        if os.path.exists(cand):
            return cand
    return p
