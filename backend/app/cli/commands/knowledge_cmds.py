"""KiteHut knowledge commands: analyze, graph, knowledge, visualize, refresh,
context, evidence.  (``status`` lives in status_cmd.py for compatibility.)

Exit codes: 0 success · 1 failure (bad path, build/safety error) · 2 usage
error (click) · 3 ``--strict`` and the result was partial or stale.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from typing import Callable, Tuple

import click

from app.cli.ui import bold, dim, print_bullet, print_error, print_header, print_info, print_success, print_warning
from app.knowledge import (
    GenerationOptions,
    KiteHutError,
    generate,
    refresh,
)
from app.knowledge.constants import (
    ALL_PARTS,
    PART_GRAPH,
    PART_KNOWLEDGE,
    PART_VISUALIZATION,
)
from app.code_graph.discovery import DEFAULT_MAX_FILES

PATH_ARG = click.argument("path", default=".", type=click.Path(file_okay=True, dir_okay=True))


def _handle_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KiteHutError as exc:
            print_error(str(exc))
            sys.exit(1)
    return wrapper


def _scan_options(fn: Callable) -> Callable:
    fn = click.option("--max-files", type=click.IntRange(min=1), default=DEFAULT_MAX_FILES, show_default=True,
                      help="Maximum Python files to analyze.")(fn)
    fn = click.option("--exclude", "exclude", multiple=True, metavar="DIR",
                      help="Extra directory name or repo-relative path to skip (repeatable).")(fn)
    fn = click.option("--strict", is_flag=True, help="Exit with code 3 if the result is partial.")(fn)
    return fn


def _report(result, root_label: str, parts: Tuple[str, ...], strict: bool, verb: str = "Generated") -> None:
    for w in result.warnings:
        print_warning(w)
    if not result.ok:
        for e in result.errors:
            print_error(e)
        sys.exit(1)
    meta = result.metadata
    print_success(f"{verb} {', '.join(p for p in ALL_PARTS if p in parts)} in {bold(str(result.output_dir))}")
    print_bullet(f"Files: {meta.file_count} · Symbols: {meta.symbol_count} · Relationships: {meta.relationship_count}")
    print_bullet(f"Status: {meta.generation_status} · build {result.timings_ms.get('graph_build_ms', 0)} ms · total {result.timings_ms.get('total_ms', 0)} ms")
    if result.written:
        print_bullet(f"Written: {len(result.written)} file(s); unchanged: {len(result.unchanged)}; removed stale pages: {len(result.pruned)}")
    if PART_KNOWLEDGE in parts:
        print_info("Next: read .kitehut/knowledge/INDEX.md")
    if PART_VISUALIZATION in parts:
        print_info("Open .kitehut/visualization/graph.html in a browser (fully offline).")
    if strict and result.status != "complete":
        print_warning("--strict: build was partial.")
        sys.exit(3)


def _run(path: str, parts: Tuple[str, ...], exclude, max_files: int, strict: bool, title: str) -> None:
    opts = GenerationOptions(parts=parts, exclude_dirs=tuple(exclude), max_files=max_files,
                             progress=lambda m: print_info(m + "..."))
    print_header(f"{title} -- {Path(path).name or path}")
    result = generate(path, opts)
    _report(result, path, parts, strict)


@click.command(name="analyze", help="Analyze a repository and generate the full .kitehut/ folder (graph, knowledge, visualization).")
@PATH_ARG
@_scan_options
@_handle_errors
def analyze_command(path, exclude, max_files, strict):
    _run(path, ALL_PARTS, exclude, max_files, strict, "KiteHut Analyze")


@click.command(name="graph", help="Build and write only .kitehut/graph/repository.json (plus metadata).")
@PATH_ARG
@_scan_options
@_handle_errors
def graph_command(path, exclude, max_files, strict):
    _run(path, (PART_GRAPH,), exclude, max_files, strict, "KiteHut Graph")


@click.command(name="knowledge", help="Generate the Markdown knowledge (INDEX.md, architecture, module and symbol pages).")
@PATH_ARG
@_scan_options
@click.option("--max-module-pages", type=click.IntRange(min=0), default=500, show_default=True)
@click.option("--max-symbol-pages", type=click.IntRange(min=0), default=1500, show_default=True)
@_handle_errors
def knowledge_command(path, exclude, max_files, strict, max_module_pages, max_symbol_pages):
    opts = GenerationOptions(parts=(PART_GRAPH, PART_KNOWLEDGE), exclude_dirs=tuple(exclude), max_files=max_files,
                             max_module_pages=max_module_pages, max_symbol_pages=max_symbol_pages,
                             progress=lambda m: print_info(m + "..."))
    print_header(f"KiteHut Knowledge -- {Path(path).name or path}")
    _report(generate(path, opts), path, opts.parts, strict)


@click.command(name="visualize", help="Generate the standalone offline graph.html visualization.")
@PATH_ARG
@_scan_options
@click.option("--max-nodes", type=click.IntRange(min=1), default=2000, show_default=True, help="Maximum nodes drawn.")
@click.option("--open", "open_browser", is_flag=True, help="Open the result in the default browser.")
@_handle_errors
def visualize_command(path, exclude, max_files, strict, max_nodes, open_browser):
    opts = GenerationOptions(parts=(PART_GRAPH, PART_VISUALIZATION), exclude_dirs=tuple(exclude), max_files=max_files,
                             max_viz_nodes=max_nodes, progress=lambda m: print_info(m + "..."))
    print_header(f"KiteHut Visualize -- {Path(path).name or path}")
    result = generate(path, opts)
    _report(result, path, opts.parts, strict)
    if open_browser:
        import webbrowser
        webbrowser.open((result.output_dir / "visualization" / "graph.html").as_uri())


@click.command(name="refresh", help="Regenerate knowledge after code changes (reuses the parts generated before).")
@PATH_ARG
@click.option("--force", is_flag=True, help="Regenerate even if the source is unchanged.")
@click.option("--strict", is_flag=True, help="Exit with code 3 if the result is partial.")
@_handle_errors
def refresh_command(path, force, strict):
    print_header(f"KiteHut Refresh -- {Path(path).name or path}")
    result = refresh(path, GenerationOptions(progress=lambda m: print_info(m + "...")), force=force)
    if result.up_to_date:
        print_success("Already up to date; nothing was rewritten. (Use --force to regenerate.)")
        return
    prev, meta = result.previous, result.metadata
    _report(result, path, tuple(meta.outputs) if meta else (), strict, verb="Refreshed")
    if prev and meta and prev is not meta:
        print_bullet(f"Files {prev.file_count} -> {meta.file_count} · Symbols {prev.symbol_count} -> {meta.symbol_count}")


@click.command(name="context", help="Retrieve ranked, token-budgeted source context for a question (requires prior analyze).")
@click.argument("path", type=click.Path())
@click.argument("query")
@click.option("--budget", type=click.IntRange(min=1), default=4000, show_default=True, help="Token budget (estimated).")
@click.option("--max-items", type=click.IntRange(min=1), default=15, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]), default="markdown", show_default=True)
@_handle_errors
def context_command(path, query, budget, max_items, fmt):
    from app.knowledge.context import retrieve_context
    bundle = retrieve_context(path, query, token_budget=budget, max_items=max_items)
    click.echo(json.dumps(bundle.to_dict(), indent=2) if fmt == "json" else bundle.to_markdown())


@click.command(name="evidence", help="Collect source-backed evidence (path, lines, hashes, freshness) for a statement.")
@click.argument("path", type=click.Path())
@click.argument("statement")
@click.option("--file", "target_file", default=None, help="Repo-relative file the statement is about.")
@click.option("--line", "target_line", type=click.IntRange(min=1), default=None)
@click.option("--pattern", "expected_pattern", default=None, help="Text expected in the source.")
@click.option("--verify", is_flag=True, help="Also run the deterministic verification engine.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@_handle_errors
def evidence_command(path, statement, target_file, target_line, expected_pattern, verify, as_json):
    from app.knowledge.evidence import collect_evidence
    rep = collect_evidence(path, statement, target_file=target_file, target_line=target_line,
                           expected_pattern=expected_pattern, verify=verify)
    if as_json:
        click.echo(json.dumps(rep.to_dict(), indent=2))
        return
    print_header("KiteHut Evidence")
    if rep.status:
        print_info(f"Verification status: {bold(rep.status)}")
        for r in rep.reasons:
            print_bullet(r)
    print_info(f"{len(rep.spans)} source span(s); stale: {rep.stale_count}; missing: {rep.missing_count}; graph used: {rep.graph_used}")
    for s in rep.spans:
        flag = " [STALE]" if s["stale"] else (" [MISSING]" if s["missing"] else "")
        print_bullet(f"{s['reference']} ({s['relationship']}){flag}")


@click.command(name="mcp", help="Run the KiteHut MCP server (stdio) for Claude Code, Codex or any MCP client.")
@click.argument("path", default=".", type=click.Path())
@click.option("--allow-agent-tools", is_flag=True,
              help="ALSO expose tools that can write/delete files, run commands and change git state. Off by default.")
@_handle_errors
def mcp_command(path, allow_agent_tools):
    from app.mcp.server import KiteHutMCPServer
    from app.knowledge.safe_io import resolve_repository_root
    root = resolve_repository_root(path)
    KiteHutMCPServer(repository_path=str(root), allow_agent_tools=allow_agent_tools).run_server("stdio")
