"""Unit tests for the unified-diff parser and changed-line symbol matching.

Defect coverage: D1 (content lines, context exclusion, module symbols),
D6 (ambiguous multi-block diffs) and D7 (deletion anchoring).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.code_graph.models import SymbolKind
from app.git_ops.diff_hunks import (
    FILE_LEVEL,
    SYMBOL_LEVEL,
    Hunk,
    candidate_diffs,
    find_file_diff,
    match_symbols,
    parse_unified_diff,
)


@dataclass
class FakeSymbol:
    """Minimal ``SymbolNode`` stand-in: only the attributes used here."""

    id: str
    qualified_name: str
    start_line: int
    end_line: int
    kind: SymbolKind = SymbolKind.FUNCTION


# Line layout every diff below refers to:
#   1 LIMIT = 10   4 def alpha():   5 a=1   6 b=2   7 return a+b
#  10 def helper():  11 c=3   12 return c
MODULE = FakeSymbol("m", "m", 1, 40, SymbolKind.MODULE)
ALPHA = FakeSymbol("a", "m.alpha", 4, 7, SymbolKind.FUNCTION)
HELPER = FakeSymbol("h", "m.helper", 10, 12, SymbolKind.FUNCTION)


def diff(*body: str, header: str = "@@ -4,4 +4,4 @@") -> str:
    """Wrap hunk body lines in a one-file unified diff."""
    lines = ["diff --git a/m.py b/m.py", "--- a/m.py", "+++ b/m.py", header]
    lines.extend(body)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Parsing: added/removed vs context
# ---------------------------------------------------------------------------


def test_parser_separates_added_removed_and_context_lines():
    parsed = parse_unified_diff(
        diff(
            " def alpha():",
            "     a = 1",
            "-    b = 2",
            "+    b = 99",
            "     return a + b",
            header="@@ -4,5 +4,5 @@",
        )
    )
    hunk = parsed[0].hunks[0]
    # b = 2 is line 6 in this file; the surrounding lines are context.
    assert hunk.removed_lines == (6,)
    assert hunk.added_lines == (6,)
    # The header range includes the context lines; the changed-line fields do not.
    assert hunk.new_range == (4, 8)


def test_parser_tracks_lines_across_multiple_hunks():
    text = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
        "@@ -20,3 +20,4 @@\n"
        " twenty\n"
        "+inserted\n"
        " twentyone\n"
        " twentytwo\n"
    )
    hunks = parse_unified_diff(text)[0].hunks
    assert len(hunks) == 2
    assert hunks[0].added_lines == (2,)
    assert hunks[1].added_lines == (21,)


def test_parser_handles_omitted_lengths_and_deletions():
    text = diff("-gone", header="@@ -7 +7,0 @@").splitlines()
    parsed = parse_unified_diff("\n".join(text))
    hunk = parsed[0].hunks[0]
    assert hunk.old_start == 7 and hunk.old_length == 1
    assert hunk.new_length == 0
    assert hunk.removed_lines == (7,)
    assert hunk.added_lines == ()
    assert hunk.is_deletion_only


def test_parser_reports_malformed_and_empty_input():
    assert parse_unified_diff(None) == ()
    assert parse_unified_diff("") == ()

    parsed = parse_unified_diff("diff --git a/m.py b/m.py\n@@ nonsense @@\n")
    assert parsed[0].hunks == ()
    assert parsed[0].parse_errors
    assert "unparsable hunk header" in parsed[0].parse_errors[0]


def test_parser_marks_rename_only_block_without_hunks():
    parsed = parse_unified_diff(
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    block = parsed[0]
    assert block.path == "new.py"
    assert block.old_path == "old.py"
    assert block.is_rename is True
    assert block.hunks == ()


# ---------------------------------------------------------------------------
# Matching: only genuinely changed lines are attributed
# ---------------------------------------------------------------------------


def test_edit_inside_one_function_does_not_mark_a_neighbour():
    """D1: the ±3 context window must not attribute the edit to a neighbour."""
    parsed = parse_unified_diff(
        diff(
            " ",
            " def alpha():",
            "     a = 1",
            "-    b = 2",
            "+    b = 99",
            "     return a + b",
            " ",
            header="@@ -3,7 +3,7 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    assert [symbol.qualified_name for symbol in match.symbols] == ["m.alpha"]
    assert match.precision == SYMBOL_LEVEL
    assert match.reason is None
    assert HELPER not in match.symbols


def test_change_spanning_two_functions_matches_both():
    parsed = parse_unified_diff(
        diff(
            "     a = 1",
            "     b = 2",
            "+    x = 0",
            "     return a + b",
            " ",
            " ",
            " def helper():",
            "+    y = 1",
            "     c = 3",
            "     return c",
            header="@@ -5,8 +5,10 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    names = [symbol.qualified_name for symbol in match.symbols]
    assert names == ["m.alpha", "m.helper"]
    assert match.precision == SYMBOL_LEVEL


def test_module_level_change_is_reported_at_file_level():
    """D1: module symbols span the file, so they must not claim the edit."""
    parsed = parse_unified_diff(
        diff(
            "-LIMIT = 10",
            "+LIMIT = 20",
            header="@@ -1,1 +1,1 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    assert match.symbols == ()
    assert match.precision == FILE_LEVEL
    assert "module-level" in (match.reason or "")


def test_import_change_is_reported_at_file_level():
    parsed = parse_unified_diff(
        diff(
            " import os",
            "+import sys",
            header="@@ -1,1 +1,2 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA])
    assert match.precision == FILE_LEVEL
    assert match.symbols == ()


def test_multiple_hunks_all_attributed():
    text = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -5,1 +5,1 @@\n"
        "-    b = 2\n"
        "+    b = 3\n"
        "@@ -11,1 +11,1 @@\n"
        "-    c = 3\n"
        "+    c = 4\n"
    )
    parsed = parse_unified_diff(text)
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    names = [symbol.qualified_name for symbol in match.symbols]
    assert names == ["m.alpha", "m.helper"]
    assert match.hunk_count == 2
    assert match.uncovered_ranges == ()


def test_added_lines_outside_every_symbol_are_recorded_as_uncovered():
    parsed = parse_unified_diff(
        diff(
            " # section banner",
            "+# another banner",
            header="@@ -8,1 +8,2 @@",
        )
    )
    # Only the function symbols are offered, so line 9 is covered by nothing.
    match = match_symbols(parsed[0].hunks, [ALPHA, HELPER])
    assert match.precision == FILE_LEVEL
    assert match.uncovered_ranges


def test_module_level_addition_is_classified_as_module_only():
    parsed = parse_unified_diff(
        diff(
            " # section banner",
            "+# another banner",
            header="@@ -8,1 +8,2 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    assert match.precision == FILE_LEVEL
    assert "module-level" in (match.reason or "")


def test_empty_and_malformed_hunks_never_raise():
    assert match_symbols([], [MODULE]).precision == FILE_LEVEL
    assert match_symbols([], []).symbols == ()

    malformed = parse_unified_diff("diff --git a/m.py b/m.py\n@@ bad @@\n")
    match = match_symbols(malformed[0].hunks, [MODULE, ALPHA])
    assert match.symbols == ()
    assert match.precision == FILE_LEVEL


def test_hunk_without_content_falls_back_to_header_range_and_says_so():
    # Handler-built hunks carry no content lines; the fallback is explicit.
    synthetic = Hunk(old_start=4, old_length=4, new_start=4, new_length=4)
    match = match_symbols([synthetic], [MODULE, ALPHA, HELPER])
    assert [symbol.qualified_name for symbol in match.symbols] == ["m.alpha"]
    assert match.uncertain_hunk_count == 1


# ---------------------------------------------------------------------------
# Deletion anchoring (D7)
# ---------------------------------------------------------------------------


def test_deletion_inside_a_function_is_attributed_to_it():
    parsed = parse_unified_diff(
        diff(
            "     a = 1",
            "-    b = 2",
            "     return a",
            header="@@ -5,3 +5,2 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA, HELPER])
    assert [symbol.qualified_name for symbol in match.symbols] == ["m.alpha"]


def test_wholly_deleted_function_is_not_blamed_on_its_neighbour():
    """D7: removing a whole function anchors on the neighbour's last line."""
    parsed = parse_unified_diff(
        diff(
            "     return a + b",
            " ",
            " ",
            "-def helper():",
            "-    c = 3",
            "-    return c",
            "-",
            " ",
            " ",
            " def omega():",
            header="@@ -7,8 +7,4 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA])
    assert match.symbols == ()
    assert match.precision == FILE_LEVEL
    assert "removed block" in (match.reason or "")


def test_deletion_at_start_of_file_is_file_level():
    parsed = parse_unified_diff(
        diff(
            "-LIMIT = 10",
            "-",
            "-",
            " def alpha():",
            header="@@ -1,3 +1,0 @@",
        )
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA])
    assert match.precision == FILE_LEVEL


def test_module_symbol_never_appears_in_symbol_level_matches():
    parsed = parse_unified_diff(
        diff("-    b = 2", "+    b = 3", header="@@ -6,1 +6,1 @@")
    )
    match = match_symbols(parsed[0].hunks, [MODULE, ALPHA])
    assert all(symbol.kind is not SymbolKind.MODULE for symbol in match.symbols)


# ---------------------------------------------------------------------------
# Block selection (D6)
# ---------------------------------------------------------------------------


def test_multiple_blocks_for_one_path_are_reported_as_ambiguous():
    text = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -1,1 +1,1 @@\n-a\n+b\n"
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -9,1 +9,1 @@\n-c\n+d\n"
    )
    parsed = parse_unified_diff(text)
    assert len(candidate_diffs(parsed, path="m.py")) == 2
    assert find_file_diff(parsed, path="m.py") is None


def test_single_block_is_returned_and_missing_block_is_none():
    parsed = parse_unified_diff(diff(" a", header="@@ -1,1 +1,1 @@"))
    assert find_file_diff(parsed, path="m.py") is parsed[0]
    assert find_file_diff(parsed, path="other.py") is None


def test_rename_source_is_matched_through_previous_path():
    parsed = parse_unified_diff(
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    assert find_file_diff(parsed, path="new.py") is parsed[0]
    assert find_file_diff(parsed, path="new.py", previous_path="old.py") is parsed[0]
