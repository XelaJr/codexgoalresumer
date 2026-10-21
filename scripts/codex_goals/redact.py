"""Mask secrets before any history text leaves this script.

Applied to every string of the brief, including ``--full`` and ``--json``.
Two passes: patterns first, then every value a pattern caught is masked
wherever else it appears (e.g. a password typed in one message and reused in
a later ``ssh`` command).
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_SECRET_NAME = r"[A-Za-z0-9_]*(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_PWD|_PASS|APIKEY|API_KEY)"

# (regex, group holding the secret value; 0 = whole match)
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), 0),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), 0),
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), 0),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), 0),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), 0),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), 0),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), 0),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), 0),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), 0),
    # Authorization: Bearer xxx / Basic xxx
    (re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._~+/=\-]{12,})"), 1),
    # Passwords inside URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s@/]+)@"), 1),
    # auth.json style keys: "access_token": "...", "OPENAI_API_KEY": "..."
    (re.compile(r'(?i)"(?:access_token|refresh_token|id_token|api_key|apikey|client_secret|password|'
                r'secret|%s)"\s*:\s*"([^"]{4,})"' % _SECRET_NAME), 1),
    # NAME_KEY=value / NAME_TOKEN: value / export NAME_SECRET="value"
    (re.compile(r"(?i)\b%s\s*[=:]\s*[\"']?([^\s\"'`,;]{4,})" % _SECRET_NAME), 1),
    # password=..., password: ..., --password ...
    (re.compile(r"(?i)(?:\bpassword|\bpasswd|\bpwd|--password)(?:\s*[=:]\s*|(?<=--password)\s+)[\"']?([^\s\"']{4,})"), 1),
]

# "contraseña Foo123$", "password is Foo123$", "clave: Foo123$": only values that
# contain a digit or a symbol, so ordinary prose ("password reset") is left alone.
_NATURAL = re.compile(
    r"(?i)\b(?:contrase(?:ñ|n|n̄)a|password|passwd|passphrase|clave)"
    r"(?:\s+(?:es|is|del?|de\s+\S+|for\s+\S+))?\s*[:=]?\s+[\"'`]?([^\s\"'`,;]{4,})")


def _natural_ok(value: str) -> bool:
    return bool(re.search(r"[0-9]", value) or re.search(r"[^A-Za-z0-9áéíóúñÁÉÍÓÚÑ.\-]", value))


def find_secrets(text) -> set:
    """Secret values found in ``text`` by the patterns."""
    out = set()
    if not isinstance(text, str) or not text:
        return out
    for rx, grp in _PATTERNS:
        for m in rx.finditer(text):
            out.add(m.group(grp))
    for m in _NATURAL.finditer(text):
        if _natural_ok(m.group(1)):
            out.add(m.group(1))
    return {s for s in out if s and s != MASK}


def _secret_like(s: str) -> bool:
    """Long enough and not a plain word, so masking it everywhere is safe."""
    return len(s) >= 6 and bool(re.search(r"[0-9]", s)) and bool(re.search(r"[A-Za-z]", s)) \
        or len(s) >= 12 and not re.fullmatch(r"[A-Za-z]+", s)


def _variants(secrets) -> list:
    """Secrets plus their form without trailing punctuation, longest first."""
    out = set()
    for s in secrets:
        if _secret_like(s):
            out.add(s)
            trimmed = re.sub(r"[^A-Za-z0-9]+$", "", s)
            if _secret_like(trimmed):
                out.add(trimmed)
    return sorted(out, key=len, reverse=True)


def redact(text, known=None):
    """Return ``text`` with secrets replaced by ``[REDACTED]``. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    secrets = find_secrets(text) | set(known or ())
    for s in _variants(secrets):
        text = text.replace(s, MASK)
    # Values that are not secret-like enough to mask globally are masked in place.
    for rx, grp in _PATTERNS:
        text = rx.sub(lambda m: m.group(0).replace(m.group(grp), MASK)
                      if m.group(grp) != MASK else m.group(0), text)
    def natural(m):
        v = m.group(1)
        return m.group(0).replace(v, MASK) if v != MASK and _natural_ok(v) else m.group(0)
    return _NATURAL.sub(natural, text)


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def collect(obj) -> set:
    found = set()
    for s in _walk_strings(obj):
        found |= find_secrets(s)
    return found


def redact_obj(obj, known=None):
    """Recursively redact every string in a JSON-like structure.

    Secrets found anywhere in ``obj`` are masked everywhere in it.
    """
    if known is None:
        known = collect(obj)
    if isinstance(obj, str):
        return redact(obj, known)
    if isinstance(obj, list):
        return [redact_obj(x, known) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x, known) for x in obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, known) for k, v in obj.items()}
    return obj
