"""`kitehut impact` command.

Runs the Phase 3F impact-analysis workflow and displays results.

Exit codes: 0 success · 1 analysis with warnings/degraded detection · 2 usage error · 3 workflow/error
"""

from __future__ import annotations

import sys
from typing import Optional

import click

from app.cli.ui import (
    bold,
    cyan,
    dim,
    print_bullet,
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)
from app.code_graph.discovery import DEFAULT_MAX_FILES
from app.git_ops.impact_service import (
    ImpactWorkflow,
    ImpactWorkflowError,
    ImpactWorkflowOptions,
)


@click.command(name="impact", help="Run impact analysis on the repository.")
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option(
    "--base",
    "base_revision",
    default=None,
    show_default=True,
    help="Compare against this revision instead of working-tree changes.",
)
@click.option(
    "--working-tree/--no-working-tree",
    "working_tree",
    default=True,
    show_default=True,
    help=(
        "Working-tree mode (default). Overridden by --base. "
        "--no-working-tree analyses the index only (staged changes)."
    ),
)
@click.option(
    "--staged-only/--no-staged-only",
    "staged_only",
    default=False,
    show_default=True,
    help="Only consider staged changes (incompatible with base revision).",
)
@click.option(
    "--untracked/--no-untracked",
    "include_untracked",
    default=True,
    show_default=True,
    help="Include untracked files in detection.",
)
@click.option(
    "--depth",
    "max_depth",
    default=-1,
    show_default=True,
    type=click.IntRange(min=-1),
    help="Maximum propagation depth. -1 means unlimited.",
)
@click.option(
    "--kinds",
    default=None,
    show_default=True,
    help="Comma-separated list of dependency edge kinds to propagate (e.g. imports,calls).",
)
@click.option(
    "--exclusions",
    default=None,
    show_default=True,
    help="Comma-separated list of path exclusions.",
)
@click.option(
    "--max-results",
    "max_results",
    default=1000,
    show_default=True,
    type=click.IntRange(min=1),
    help="Hard bound on total changed files considered.",
)
@click.option(
    "--max-ranked",
    "max_ranked",
    default=25,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum number of ranked impacts to display.",
)
@click.option(
    "--graph",
    "graph_source",
    type=click.Choice(["build", "cached"]),
    default="build",
    show_default=True,
    help="Code graph source.",
)
@click.option(
    "--max-files",
    "max_files",
    default=DEFAULT_MAX_FILES,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum number of files to scan for the code graph.",
)
@click.option(
    "--exclude",
    "exclude",
    default=None,
    multiple=True,
    show_default=True,
    help="Additional path exclusion pattern (repeatable).",
)
@click.option(
    "--json/--no-json",
    "as_json",
    default=False,
    show_default=True,
    help="Output result as deterministic JSON (sorted keys, no timestamps).",
)
@click.option(
    "--ranked/--no-ranked",
    "show_ranked",
    default=True,
    show_default=True,
    help="Show ranked impacts in human-readable mode (default: on).",
)
def impact_command(
    path: str,
    base_revision: Optional[str],
    working_tree: bool,
    staged_only: bool,
    include_untracked: bool,
    max_depth: int,
    kinds: Optional[str],
    exclusions: Optional[str],
    max_results: int,
    max_ranked: int,
    graph_source: str,
    max_files: int,
    exclude: tuple,
    as_json: bool,
    show_ranked: bool,
) -> None:
    """Run the impact-analysis workflow and display results."""
    try:
        kinds_tuple: Optional[tuple] = None
        if kinds:
            kinds_tuple = tuple(part.strip() for part in kinds.split(",") if part.strip())

        exclusions_tuple: tuple = ()
        if exclusions:
            exclusions_tuple = tuple(
                part.strip() for part in exclusions.split(",") if part.strip()
            )

        exclude_dirs = tuple(str(pattern) for pattern in exclude) if exclude else ()

        # --- mode resolution -------------------------------------------
        # A named base revision always wins (documented: it disables the
        # working tree rather than erroring).  Otherwise --working-tree
        # selects the working-tree comparison and --no-working-tree falls
        # back to the index, which the workflow calls "staged".
        if base_revision:
            mode = "base"
        elif working_tree:
            mode = "working_tree"
        else:
            mode = "staged"

        options = ImpactWorkflowOptions(
            mode=mode,
            base_revision=base_revision,
            staged_only=staged_only,
            include_untracked=include_untracked,
            max_depth=max_depth,
            max_results=max_results,
            max_ranked=max_ranked,
            kinds=kinds_tuple,
            exclusions=exclusions_tuple,
            changed_symbols=(),
            graph_source=graph_source,
            max_files=max_files,
            exclude_dirs=exclude_dirs,
        )

        workflow = ImpactWorkflow(repository_path=path, options=options)
        report = workflow.run()

        if as_json:
            # stdout must contain valid JSON only; diagnostics go to stderr.
            click.echo(report.to_json())
        else:
            _display_impact_human(report, path=path, max_ranked=max_ranked)

        # 0 = clean analysis, 3 = completed with warnings / degraded / truncated.
        sys.exit(EXIT_WITH_WARNINGS if _has_caveats(report) else EXIT_OK)

    except ImpactWorkflowError as error:
        print_error(str(error))
        sys.exit(EXIT_WORKFLOW_ERROR)
    except Exception as error:  # pragma: no cover - defensive
        print_error(f"Unexpected error: {error}")
        sys.exit(EXIT_WORKFLOW_ERROR)


# Exit codes (click uses 2 for usage errors on its own).
EXIT_OK = 0
EXIT_WORKFLOW_ERROR = 1
EXIT_WITH_WARNINGS = 3


def _is_truncated(report) -> bool:
    """True when the workflow reports that its results were cut short."""
    truncation = getattr(report, "truncation", {}) or {}
    if not isinstance(truncation, dict):
        return False
    return bool(
        truncation.get("analysis_truncated")
        or truncation.get("ranking_truncated")
    )


def _has_caveats(report) -> bool:
    """True when analysis completed with warnings, degraded detection or truncation."""
    change_detection = getattr(report, "change_detection", {}) or {}
    if change_detection.get("degraded", False) or change_detection.get("errors"):
        return True
    if _is_truncated(report):
        return True
    return False


def _display_impact_human(report, *, path: str, max_ranked: int) -> None:
    """Display the impact report in human-readable format."""
    repo = getattr(report, "repository", {}) or {}
    print_header(f"Impact Analysis: {repo.get('name') or path}")
    print_bullet(f"Repository ID: {repo.get('repository_id', 'N/A')}")
    print_bullet(f"Revision: {repo.get('revision', 'N/A')}")
    print_bullet(f"Graph source: {repo.get('graph_source', 'N/A')}")
    print_bullet(f"Files: {repo.get('file_count', 0)}")
    print_bullet(f"Symbols: {repo.get('symbol_count', 0)}")
    print_bullet(f"Relationships (edges): {repo.get('relationship_count', 0)}")

    modes = getattr(report, "mode", {}) or {}
    print_header("Detection")
    print_bullet(f"Mode: {modes.get('detection', 'N/A')}")
    if modes.get("base_revision"):
        print_bullet(f"Base revision: {modes.get('base_revision')}")
    print_bullet(f"Staged-only (effective): {bool(modes.get('staged_only', False))}")
    print_bullet(f"Include untracked: {bool(modes.get('include_untracked', False))}")
    scope = modes.get("scope")
    print_bullet(f"Scope: {scope if scope else 'whole repository'}")
    outside_count = modes.get("changes_outside_scope", 0)
    if outside_count:
        print_bullet(f"Changes outside scope: {outside_count}")

    change_detection = getattr(report, "change_detection", {}) or {}
    print_bullet(f"Change units: {change_detection.get('unit_count', 0)}")
    detection_warnings = change_detection.get("warnings") or []
    for message in detection_warnings[:5]:
        print_warning(message)
    if change_detection.get("degraded", False):
        print_warning(
            "Detection was degraded: at least one file diff could not be read, "
            "so this changeset may be incomplete."
        )
    for message in (change_detection.get("errors") or [])[:5]:
        print_bullet(f"Detection error: {message}")

    summary = getattr(report, "summary", {}) or {}
    print_header("Detected Changes")
    print_bullet(f"Changed files: {summary.get('changed_file_count', 0)}")
    print_bullet(f"Changed symbols: {summary.get('changed_symbol_count', 0)}")
    print_bullet(f"Deleted changes: {summary.get('deleted_change_count', 0)}")

    print_header("Mapping")
    symbol_mapping = getattr(report, "symbol_mapping", {}) or {}
    print_bullet(f"Mapped changes: {len(getattr(report, 'mapped_changes', []) or [])}")
    print_bullet(f"Unmapped changes: {summary.get('unmapped_change_count', 0)}")
    print_bullet(f"Symbol-level files: {symbol_mapping.get('symbol_level_file_count', 0)}")
    print_bullet(f"File-level-only files: {symbol_mapping.get('file_level_only_file_count', 0)}")

    print_header("Impacts")
    print_bullet(f"Direct impacts: {summary.get('direct_impact_count', 0)}")
    print_bullet(f"Transitive impacts: {summary.get('transitive_impact_count', 0)}")
    print_bullet(f"Confirmed impacts: {summary.get('confirmed_impact_count', 0)}")
    print_bullet(f"Unresolved impacts: {summary.get('unresolved_impact_count', 0)}")
    print_bullet(f"Ambiguous impacts: {summary.get('ambiguous_impact_count', 0)}")

    ranked_impacts = getattr(report, "ranked_impacts", []) or []
    if ranked_impacts:
        shown = min(len(ranked_impacts), max_ranked)
        print_header(f"Ranked Impacts (top {shown} of {len(ranked_impacts)})")
        for index, record in enumerate(ranked_impacts[:shown], start=1):
            tier = record.get("priority_tier", "N/A")
            score = record.get("priority_score")
            label = record.get("qualified_name") or record.get("name") or "N/A"
            location = record.get("path", "N/A")
            suffix = f" score={score}" if score is not None else ""
            print_bullet(f"{index}. [{tier}] {label} ({location}){suffix}")

    unconfirmed = getattr(report, "unconfirmed_impacts", []) or []
    if unconfirmed:
        print_header(f"Unconfirmed Impacts ({len(unconfirmed)})")
        for record in unconfirmed[:max_ranked]:
            label = record.get("qualified_name") or record.get("name") or "N/A"
            location = record.get("path", "N/A")
            print_bullet(f"{label} ({location})")

    if _is_truncated(report):
        truncation = getattr(report, "truncation", {}) or {}
        print_header("Truncation")
        print_warning(
            "Results were truncated: impacts discovered "
            f"{truncation.get('impacts_discovered', 0)}, returned "
            f"{truncation.get('impacts_returned', 0)}; components found "
            f"{truncation.get('components_found', 0)}, returned "
            f"{truncation.get('components_returned', 0)}."
        )

    analysis_warnings = getattr(report, "analysis_warnings", []) or []
    if analysis_warnings:
        print_header(f"Warnings ({len(analysis_warnings)})")
        for message in analysis_warnings[:10]:
            print_warning(message)

    limitations = getattr(report, "limitations", []) or []
    if limitations:
        print_header("Limitations")
        for message in limitations[:10]:
            print_bullet(message)

    print_header("Analysis Type")
    print_info("Static impact analysis: navigation evidence only.")
    print_bullet("navigation_only: true")
    print_bullet("static_impact_only: true")
    print_bullet("runtime_failure_verified: false")
    print(dim("These results are not verified runtime failures."))
