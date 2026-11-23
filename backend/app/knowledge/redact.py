"""Best-effort redaction of obvious hard-coded secrets in served source text.

Applies to text KiteHut *returns* (context, evidence snippets, MCP output). It
never modifies source files. This is a heuristic safety net, not a secret
scanner: unusual secret formats will not be caught.
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"
_ASSIGN = re.compile(
    r"""(?P<lead>(?:api[_-]?key|secret(?:[_-]?key)?|token|password|passwd|private[_-]?key|access[_-]?key|auth)[A-Za-z0-9_]*\s*[:=]\s*)(?P<q>['"])(?P<val>[^'"\n]{6,})(?P=q)""",
    re.IGNORECASE,
)
_KNOWN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _PEM.sub(REDACTED, text)
    text = _ASSIGN.sub(lambda m: f"{m.group('lead')}{m.group('q')}{REDACTED}{m.group('q')}", text)
    return _KNOWN.sub(REDACTED, text)
