"""Pure unified-diff parsing and changed-line -> symbol range intersection.

Phase 3F uses this module to answer a deliberately narrow question: *which
symbols did a diff actually change?*  It is line-range intersection over the
diff's own content lines, not semantic analysis.  Nothing here imports the
graph models, touches Git, or reads the filesystem: the module is a pure
function over text.

What this module can and cannot tell you
----------------------------------------
A unified diff hunk carries three kinds of content line: ``+`` (added), ``-``
(removed) and `` `` (context).  Git prints up to three context lines around
each change, so the *header* range of a hunk is wider than the edit.  This
module therefore parses the content lines and reports, per hunk:

* ``added_lines`` — line numbers **added** in the new file (1-indexed);
* ``removed_lines`` — line numbers **removed** from the old file (1-indexed).

Symbol attribution uses only those lines.  Consequences, all of which callers
must report:

* An edit inside one function matches only that function.  Context lines are
  never attributed to a neighbouring function.
* A hunk whose added lines straddle two functions matches both.
* A decorator line matches the decorated function only when the symbol span
  includes the decorator (extractor-dependent).
* Module symbols (``kind == "module"``) span the whole file and would match
  every hunk, so they are **excluded** from symbol-level attribution.  When a
  hunk touches only module-level code and no real symbol can be identified,
  the file is reported at ``file_level_only`` — never guessed.
* A hunk that overlaps no symbol is *not* an error.  Callers decide whether
  partial coverage is enough for symbol-level precision.

Deletion-only hunks
-------------------
A pure removal has no added lines to intersect, and the removed lines no
longer exist in the new file the graph was built from.  Attribution is
therefore conservative: a removal is only attributed to the symbol that
*strictly contains* the deletion point (``start < anchor <= end - 1``) and
that is taller than the number of removed lines.  Removing a whole function
therefore anchors on the last surviving line of its neighbour and is reported
at file level instead of being blamed on that neighbour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Hunk",
    "FileDiff",
    "SymbolMatch",
    "parse_unified_diff",
    "find_file_diff",
    "candidate_diffs",
    "match_symbols",
    "SYMBOL_LEVEL",
    "FILE_LEVEL",
    "MODULE_KIND",
]


# Precision labels shared with ``impact_service``.  Declared here (rather than
# imported) so this module stays free of app-internal dependencies.
SYMBOL_LEVEL = "symbol_level"
FILE_LEVEL = "file_level_only"

# ``SymbolKind.MODULE.value`` lower-cased; compared via ``_kind_value`` so that
# both the enum and the plain string form work.
MODULE_KIND = "module"


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_length>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_length>\d+))? @@"
)

_DIFF_HEADER = "diff --git "
_OLD_MARKER = "--- "
_NEW_MARKER = "+++ "
_DEV_NULL = "/dev/null"
_NO_NEWLINE = "\\ No newline at end of file"


def _kind_value(kind: Any) -> str:
    """Normalise ``SymbolKind`` (enum or string) to its lower-case value."""
    value = getattr(kind, "value", kind)
    return str(value or "").lower()


@dataclass(frozen=True)
class Hunk:
    """One ``@@`` block, with the content lines Git printed for it.

    ``old_start``/``new_start`` describe the header range, which *includes
    context lines*.  ``added_lines`` and ``removed_lines`` describe only the
    lines that actually changed; they are empty when a caller constructed the
    hunk without body text, in which case matching falls back to the header
    range and says so.
    """

    old_start: int
    old_length: int
    new_start: int
    new_length: int
    added_lines: Tuple[int, ...] = ()
    removed_lines: Tuple[int, ...] = ()
    #: New-file position (the line that follows) of each removed block.  One
    #: entry per removed line; repeated for contiguous removals.
    removal_anchors: Tuple[int, ...] = ()

    @property
    def has_content(self) -> bool:
        """True when the hunk body was parsed (content lines are known)."""
        return bool(self.added_lines or self.removed_lines)

    @property
    def is_deletion_only(self) -> bool:
        return self.new_length == 0

    @property
    def is_pure_removal(self) -> bool:
        """A removal with no inserted line (body parsed)."""
        return bool(self.removed_lines) and not self.added_lines

    @property
    def new_range(self) -> Tuple[int, int]:
        """Inclusive ``(start, end)`` header range in the new file.

        This range **includes context lines**.  It is only used when the hunk
        body was not parsed; attribution normally uses :attr:`added_lines`.
        """
        if self.new_length == 0:
            anchor = self.new_start if self.new_start >= 1 else 1
            return (anchor, anchor)
        start = max(1, self.new_start)
        return (start, self.new_start + self.new_length - 1)

    @property
    def added_range(self) -> Optional[Tuple[int, int]]:
        if not self.added_lines:
            return None
        return (min(self.added_lines), max(self.added_lines))

    @property
    def deletion_anchor(self) -> int:
        """New-file position where the first removed line used to sit.

        The value is the line number of the line that *follows* the removal in
        the new file, so ``deletion_anchor - 1`` is the last surviving line
        before it.  Falls back to ``new_start`` for hunks without body text.
        """
        if self.removal_anchors:
            return self.removal_anchors[0]
        return self.new_start if self.new_start >= 1 else 1

    def to_dict(self) -> dict:
        start, end = self.new_range
        return {
            "old_start": self.old_start,
            "old_length": self.old_length,
            "new_start": self.new_start,
            "new_length": self.new_length,
            "new_line_start": start,
            "new_line_end": end,
            "deletion_only": self.is_deletion_only,
            "added_lines": list(self.added_lines),
            "removed_lines": list(self.removed_lines),
        }


@dataclass(frozen=True)
class FileDiff:
    """The hunks Git reported for one file.

    ``path`` is the new path (``None`` for a deletion), ``old_path`` the path
    before a rename (``None`` when unchanged or newly added).
    """

    path: Optional[str]
    old_path: Optional[str] = None
    hunks: Tuple[Hunk, ...] = ()
    is_binary: bool = False
    is_rename: bool = False
    parse_errors: Tuple[str, ...] = ()

    @property
    def has_hunks(self) -> bool:
        return bool(self.hunks)

    @property
    def is_rename_only(self) -> bool:
        """A rename with no content change (no hunks)."""
        return self.is_rename and not self.hunks

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "is_binary": self.is_binary,
            "is_rename": self.is_rename,
            "hunk_count": len(self.hunks),
            "hunks": [hunk.to_dict() for hunk in self.hunks],
            "parse_errors": list(self.parse_errors),
        }


@dataclass(frozen=True)
class SymbolMatch:
    """Symbols changed by the hunks, the precision claim, and what was missed."""

    symbols: Tuple[Any, ...] = ()
    uncovered_ranges: Tuple[Tuple[int, int], ...] = ()
    hunk_count: int = 0
    precision: str = SYMBOL_LEVEL
    reason: Optional[str] = None
    uncertain_hunk_count: int = 0


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


def _unquote_git_path(value: str) -> str:
    """Undo Git's C-style quoting for paths with unusual characters.

    Git only quotes when it must (``core.quotePath``); an unquoted path is
    returned unchanged.  The escapes handled here are the ones Git emits for
    filenames (``\\t``, ``\\n``, ``\\\\``, ``\\"`` and octal bytes).
    """
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return value
    body = value[1:-1]
    out: List[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1]
        simple = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "\\": "\\",
            '"': '"',
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "v": "\v",
        }
        if nxt in simple:
            out.append(simple[nxt])
            index += 2
            continue
        octal = body[index + 1 : index + 4]
        if len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(chr(int(octal, 8)))
            index += 4
            continue
        out.append(nxt)
        index += 2
    return "".join(out)


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _path_from_marker(line: str, marker: str) -> Optional[str]:
    """Extract the path from a ``---``/``+++`` line (``None`` for /dev/null)."""
    raw = line[len(marker) :]
    # A timestamp may follow a tab when the diff was produced by plain `diff`.
    raw = raw.split("\t", 1)[0].strip()
    raw = _unquote_git_path(raw)
    if not raw or raw == _DEV_NULL:
        return None
    return _strip_prefix(_strip_prefix(raw, "a/"), "b/")


def _paths_from_diff_header(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort ``(old, new)`` from ``diff --git a/X b/Y``.

    Used only when a block has no ``---``/``+++`` pair (rename-only and binary
    blocks).  Splits on the last `` b/`` so spaces in paths survive.
    """
    rest = line[len(_DIFF_HEADER) :].strip()
    if rest.startswith('"'):
        # Two quoted tokens; find the closing quote of the first.
        end = rest.rfind('" b/')
        if end == -1:
            return (None, None)
        old = _unquote_git_path(rest[: end + 1])
        new = _unquote_git_path(rest[end + 2 :])
        return (_strip_prefix(old, "a/"), _strip_prefix(new, "b/"))
    split_at = rest.rfind(" b/")
    if split_at == -1:
        return (None, None)
    old = rest[:split_at]
    new = rest[split_at + 1 :]
    return (_strip_prefix(old, "a/"), _strip_prefix(new, "b/"))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _new_block(path: Optional[str], old_path: Optional[str]) -> Dict[str, Any]:
    return {
        "path": path,
        "old_path": old_path,
        "hunks": [],
        "is_binary": False,
        "is_rename": False,
        "errors": [],
    }


def parse_unified_diff(text: Optional[str]) -> Tuple[FileDiff, ...]:
    """Parse unified diff text into one :class:`FileDiff` per file block.

    Hunk *content* lines are read so that added and removed lines can be told
    apart from context.  The parser never raises: malformed input yields a
    ``FileDiff`` with ``parse_errors`` populated so callers can fall back to
    file-level precision instead of guessing.
    """
    if not text:
        return ()

    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    # Pending hunk state: the header plus the counters needed to map content
    # lines to old/new line numbers.
    pending: Optional[Dict[str, Any]] = None

    def flush_hunk() -> None:
        nonlocal pending
        if pending is None or current is None:
            pending = None
            return
        current["hunks"].append(
            Hunk(
                old_start=pending["old_start"],
                old_length=pending["old_length"],
                new_start=pending["new_start"],
                new_length=pending["new_length"],
                added_lines=tuple(sorted(pending["added"])),
                removed_lines=tuple(sorted(pending["removed"])),
                removal_anchors=tuple(pending["anchors"]),
            )
        )
        pending = None

    for line in text.splitlines():
        if pending is not None:
            # Inside a hunk body: classify the content line.
            if line.startswith("+"):
                pending["added"].append(pending["new_line"])
                pending["new_line"] += 1
                pending["new_remaining"] -= 1
            elif line.startswith("-"):
                pending["removed"].append(pending["old_line"])
                # The removal sits at the current position in the *new* file:
                # the line counter is not advanced, so it names the line that
                # follows the removed block.
                pending["anchors"].append(pending["new_line"])
                pending["old_line"] += 1
                pending["old_remaining"] -= 1
            elif line.startswith(" ") or line == "":
                pending["new_line"] += 1
                pending["old_line"] += 1
                pending["new_remaining"] -= 1
                pending["old_remaining"] -= 1
            elif line.strip() == _NO_NEWLINE.strip():
                pass
            else:
                # Malformed body: stop attributing lines for this hunk but keep
                # whatever was already parsed.
                if pending["old_remaining"] > 0 or pending["new_remaining"] > 0:
                    current["errors"].append(
                        "hunk body ended before the header range was consumed"
                    )
                flush_hunk()
                # Fall through to the block-level handling below.
                pending = None

            if pending is not None and (
                pending["old_remaining"] <= 0 and pending["new_remaining"] <= 0
            ):
                flush_hunk()
                continue
            if pending is not None:
                continue

        if line.startswith(_DIFF_HEADER):
            flush_hunk()
            if current is not None:
                blocks.append(current)
            old, new = _paths_from_diff_header(line)
            current = _new_block(new, old)
            continue

        if current is None:
            # Hunk lines before any `diff --git` header: tolerate a bare
            # single-file diff (some tools omit the header).
            if line.startswith("@@ "):
                current = _new_block(None, None)
            else:
                continue

        if line.startswith(_OLD_MARKER):
            marker_path = _path_from_marker(line, _OLD_MARKER)
            if marker_path is not None:
                current["old_path"] = marker_path
            continue
        if line.startswith(_NEW_MARKER):
            marker_path = _path_from_marker(line, _NEW_MARKER)
            if marker_path is not None:
                current["path"] = marker_path
            continue
        if line.startswith("rename from "):
            current["is_rename"] = True
            if not current["old_path"]:
                current["old_path"] = _unquote_git_path(line[len("rename from ") :])
            continue
        if line.startswith("rename to "):
            current["is_rename"] = True
            if not current["path"]:
                current["path"] = _unquote_git_path(line[len("rename to ") :])
            continue
        if line.startswith("copy from "):
            current["is_rename"] = True
            if not current["old_path"]:
                current["old_path"] = _unquote_git_path(line[len("copy from ") :])
            continue
        if line.startswith("copy to "):
            current["is_rename"] = True
            if not current["path"]:
                current["path"] = _unquote_git_path(line[len("copy to ") :])
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current["is_binary"] = True
            continue
        if line.startswith("@@ "):
            match = _HUNK_HEADER_RE.match(line)
            if match is None:
                current["errors"].append(f"unparsable hunk header: {line[:80]}")
                continue
            old_start = int(match.group("old_start"))
            old_length = int(match.group("old_length") or 1)
            new_start = int(match.group("new_start"))
            new_length = int(match.group("new_length") or 1)
            pending = {
                "old_start": old_start,
                "old_length": old_length,
                "new_start": new_start,
                "new_length": new_length,
                "old_line": old_start if old_length else old_start + 1,
                "new_line": new_start if new_length else new_start + 1,
                "old_remaining": old_length,
                "new_remaining": new_length,
                "added": [],
                "removed": [],
                "anchors": [],
            }
            if old_length == 0 and new_length == 0:
                flush_hunk()
            continue

    flush_hunk()
    if current is not None:
        blocks.append(current)

    results: List[FileDiff] = []
    for block in blocks:
        if block["path"] is None and block["old_path"] is None:
            block["errors"].append("file block has no recognizable path")
        results.append(
            FileDiff(
                path=block["path"],
                old_path=block["old_path"],
                hunks=tuple(block["hunks"]),
                is_binary=block["is_binary"],
                is_rename=block["is_rename"],
                parse_errors=tuple(block["errors"]),
            )
        )
    return tuple(results)


def candidate_diffs(
    diffs: Sequence[FileDiff],
    *,
    path: str,
    previous_path: Optional[str] = None,
) -> List[FileDiff]:
    """Every block that could describe ``path`` (or its rename source)."""
    return [
        diff
        for diff in diffs
        if path in (diff.path, diff.old_path)
        or (previous_path is not None and previous_path in (diff.path, diff.old_path))
    ]


def find_file_diff(
    diffs: Sequence[FileDiff],
    *,
    path: str,
    previous_path: Optional[str] = None,
) -> Optional[FileDiff]:
    """Pick the single block that belongs to ``path``.

    Returns ``None`` when no block matches *and* when several do.  Several
    blocks for one path means the diff text concatenated hunks from different
    baselines (for example index and working tree); their line numbers are not
    comparable, so the caller must fall back to file-level precision rather
    than merging them.
    """
    candidates = candidate_diffs(
        diffs, path=path, previous_path=previous_path
    )
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Intersection
# ---------------------------------------------------------------------------


def _span(symbol: Any) -> Optional[Tuple[int, int]]:
    start = getattr(symbol, "start_line", None)
    end = getattr(symbol, "end_line", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if start < 1 or end < start:
        return None
    return (start, end)


def _symbol_key(symbol: Any) -> Tuple[int, int, str, str]:
    span = _span(symbol) or (0, 0)
    return (
        span[0],
        span[1],
        str(getattr(symbol, "qualified_name", "") or ""),
        str(getattr(symbol, "id", "") or ""),
    )


def _is_module(symbol: Any) -> bool:
    return _kind_value(getattr(symbol, "kind", "")) == MODULE_KIND


def _covers(symbol: Any, line: int) -> bool:
    span = _span(symbol)
    if span is None:
        return False
    start, end = span
    return start <= line <= end


def _removal_is_inside(symbol: Any, anchor: int) -> bool:
    """True when a removal at ``anchor`` is unambiguously inside ``symbol``.

    ``anchor`` is the new-file line that *follows* the removed block, so the
    removal sits between ``anchor - 1`` and ``anchor``.  Both neighbouring
    lines must belong to the symbol: if only one does, the removal is at the
    symbol's boundary and could equally be a deleted neighbour, which this
    module refuses to guess.
    """
    return _covers(symbol, anchor - 1) and _covers(symbol, anchor)


def match_symbols(
    hunks: Iterable[Hunk],
    symbols: Iterable[Any],
) -> SymbolMatch:
    """Return the symbols a diff actually changed.

    ``symbols`` may be any objects exposing integer ``start_line``/``end_line``
    and a ``kind`` (``SymbolNode`` satisfies this).  Module symbols are
    excluded from symbol-level attribution because they span the whole file.

    Added lines are matched directly.  A hunk with no added lines is a pure
    removal and is matched only when the deletion point lies strictly inside a
    symbol that is taller than the removal.  A hunk without parsed content
    falls back to its header range and is counted in ``uncertain_hunk_count``.
    """
    hunk_list = list(hunks)
    spans: List[Tuple[Any, int, int]] = []
    module_spans: List[Tuple[Any, int, int]] = []
    for symbol in symbols:
        span = _span(symbol)
        if span is None:
            continue
        if _is_module(symbol):
            module_spans.append((symbol, span[0], span[1]))
        else:
            spans.append((symbol, span[0], span[1]))

    matched: Dict[str, Any] = {}
    uncovered: List[Tuple[int, int]] = []
    module_only_touch = False
    uncertain = 0
    removal_unattributed = 0

    for hunk in hunk_list:
        touched = False

        if hunk.added_lines:
            for line in hunk.added_lines:
                for symbol, symbol_start, symbol_end in spans:
                    if symbol_start <= line <= symbol_end:
                        touched = True
                        matched.setdefault(
                            str(getattr(symbol, "id", None) or id(symbol)), symbol
                        )
            if not touched:
                # Added lines that hit only module-level code (or empty space).
                hits_module = any(
                    symbol_start <= line <= symbol_end
                    for symbol, symbol_start, symbol_end in module_spans
                    for line in hunk.added_lines
                )
                if hits_module:
                    module_only_touch = True
                else:
                    uncovered.append(
                        (
                            min(hunk.added_lines),
                            max(hunk.added_lines),
                        )
                    )
            continue

        if hunk.removed_lines:
            removed = len(hunk.removed_lines)
            anchors = hunk.removal_anchors or (hunk.deletion_anchor,)
            best: Optional[Any] = None
            best_height = 0
            for anchor in anchors:
                for symbol, symbol_start, symbol_end in spans:
                    if not _removal_is_inside(symbol, anchor):
                        continue
                    height = symbol_end - symbol_start + 1
                    if height <= removed:
                        # Removing at least as many lines as the symbol spans
                        # cannot be an edit *inside* that symbol.
                        continue
                    if height > best_height:
                        best, best_height = symbol, height
            if best is None:
                # Could be a wholly deleted function, module-level code, or a
                # boundary edit: do not guess.
                removal_unattributed += 1
                anchor = anchors[0]
                uncovered.append((max(1, anchor - 1), max(1, anchor - 1)))
            else:
                matched.setdefault(
                    str(getattr(best, "id", None) or id(best)), best
                )
            continue

        # No content lines were parsed (synthetic hunk): fall back to the
        # header range and flag the uncertainty.
        uncertain += 1
        start, end = hunk.new_range
        hit = False
        for symbol, symbol_start, symbol_end in spans:
            if symbol_start > end or symbol_end < start:
                continue
            hit = True
            matched.setdefault(str(getattr(symbol, "id", None) or id(symbol)), symbol)
        if not hit:
            uncovered.append((start, end))

    symbols_out = tuple(sorted(matched.values(), key=_symbol_key))
    hunk_count = len(hunk_list)

    if symbols_out:
        return SymbolMatch(
            symbols=symbols_out,
            uncovered_ranges=tuple(uncovered),
            hunk_count=hunk_count,
            precision=SYMBOL_LEVEL,
            reason=None,
            uncertain_hunk_count=uncertain,
        )

    if module_only_touch:
        reason = "the change is module-level only; no function or class symbol was identified"
    elif removal_unattributed:
        reason = (
            "a removed block could not be attributed to a surviving symbol "
            "(a deleted function or module-level code)"
        )
    elif not spans and module_spans:
        reason = "the file exposes no function or class symbols"
    else:
        reason = f"none of {hunk_count} hunk(s) changed a line inside a symbol span"

    return SymbolMatch(
        symbols=(),
        uncovered_ranges=tuple(uncovered),
        hunk_count=hunk_count,
        precision=FILE_LEVEL,
        reason=reason,
        uncertain_hunk_count=uncertain,
    )
