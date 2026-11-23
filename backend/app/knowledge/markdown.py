"""Markdown knowledge generator (``.kitehut/knowledge``).

Pure functions: ``CodeGraph`` in, ``{relative_path: text}`` out.  No file I/O,
no timestamps (those live in ``metadata.json``), so the output is
byte-deterministic for a given graph and options.

Nothing here is invented.  Every statement is a direct fact from the graph:
paths, symbol kinds, line spans, and **resolved** relationships.  Calls and
imports that the extractor could not resolve conservatively are reported as
counts only, never guessed.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set
from urllib.parse import quote

from app.code_graph.models import CodeGraph, SymbolKind, SymbolNode
from app.knowledge.constants import (
    DEFAULT_MAX_MODULE_PAGES,
    DEFAULT_MAX_SYMBOL_PAGES,
    GENERATED_MARKER,
)
from app.knowledge.index import GraphIndex, kind_label

MARKER_LINE = f"<!-- {GENERATED_MARKER} -->"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SLUG = 90


@dataclass
class MarkdownOptions:
    max_module_pages: int = DEFAULT_MAX_MODULE_PAGES
    max_symbol_pages: int = DEFAULT_MAX_SYMBOL_PAGES
    unresolved_import_count: int = 0
    unresolved_call_count: int = 0
    generation_status: str = "complete"


@dataclass
class KnowledgeDocs:
    """Generated documents plus page bookkeeping."""

    pages: Dict[str, str] = field(default_factory=dict)  # relative to knowledge/
    module_pages: int = 0
    symbol_pages: int = 0
    modules_omitted: int = 0
    symbols_omitted: int = 0


# -- small helpers -----------------------------------------------------------


def _slug(text: str) -> str:
    slug = _SLUG_RE.sub("_", text.replace("/", "__")).strip("._-")
    return (slug or "item")[:_MAX_SLUG]


def _code(text: str) -> str:
    """Inline code span that cannot break out of its backticks."""
    return "`" + str(text).replace("`", "'").replace("\n", " ") + "`"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _span(sym: SymbolNode) -> str:
    return f"L{sym.start_line}" if sym.start_line == sym.end_line else f"L{sym.start_line}-L{sym.end_line}"


def _source_link(prefix: str, path: str, start: Optional[int] = None, end: Optional[int] = None) -> str:
    anchor = ""
    if start is not None:
        anchor = f"#L{start}" + (f"-L{end}" if end and end != start else "")
    return f"{prefix}{quote(path)}{anchor}"


class _Names:
    """Collision-free, filesystem-safe page names (case-insensitively unique)."""

    def __init__(self) -> None:
        self._used: Set[str] = set()

    def make(self, base: str) -> str:
        name = _slug(base)
        candidate, n = name, 2
        while f"{candidate}.md".lower() in self._used:
            candidate = f"{name}-{n}"
            n += 1
        self._used.add(f"{candidate}.md".lower())
        return f"{candidate}.md"


def _import_cycles(idx: GraphIndex) -> List[List[str]]:
    """Strongly connected components (size > 1) of the resolved import graph."""
    index_of: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    result: List[List[str]] = []
    counter = 0
    for root in sorted(idx.files_by_id):
        if root in index_of:
            continue
        work = [(root, iter(sorted(f.id for f in idx.imports_out.get(root, ()))))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(f.id for f in idx.imports_out.get(nxt, ())))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(idx.files_by_id[m].relative_path for m in component))
    return sorted(result)


# -- generator ---------------------------------------------------------------


def generate_knowledge(graph: CodeGraph, options: Optional[MarkdownOptions] = None) -> KnowledgeDocs:
    opts = options or MarkdownOptions()
    idx = GraphIndex.from_graph(graph)
    docs = KnowledgeDocs()

    # Choose which modules / symbols get pages (bounded for large repos).
    files = sorted(graph.files, key=lambda f: f.relative_path)
    ranked_files = sorted(
        files,
        key=lambda f: (-idx.fan_in(f.id), -len(idx.symbols_by_file.get(f.id, ())), f.relative_path),
    )
    paged_files = sorted(ranked_files[: max(0, opts.max_module_pages)], key=lambda f: f.relative_path)
    paged_file_ids = {f.id for f in paged_files}
    docs.modules_omitted = len(files) - len(paged_files)

    kind_rank = {SymbolKind.CLASS: 0, SymbolKind.FUNCTION: 1, SymbolKind.ASYNC_FUNCTION: 1}
    declared = idx.declared_symbols()
    candidates = [s for s in declared if s.file_id in paged_file_ids]
    ranked_syms = sorted(
        candidates,
        key=lambda s: (-idx.incoming_calls(s.id), kind_rank.get(s.kind, 2), s.relative_path, s.start_line, s.id),
    )
    paged_syms = ranked_syms[: max(0, opts.max_symbol_pages)]
    paged_sym_ids = {s.id for s in paged_syms}
    docs.symbols_omitted = len(declared) - len(paged_syms)

    # Stable names, assigned in deterministic order.
    mod_names, sym_names = _Names(), _Names()
    module_page: Dict[str, str] = {f.id: mod_names.make(f.relative_path.rsplit(".", 1)[0]) for f in paged_files}
    symbol_page: Dict[str, str] = {
        s.id: sym_names.make(s.qualified_name)
        for s in sorted(paged_syms, key=lambda s: (s.relative_path, s.start_line, s.qualified_name, s.id))
    }

    # ``where`` is the directory the link is written from: "root" (knowledge/),
    # "modules" or "symbols".
    _mod_prefix = {"root": "modules/", "modules": "", "symbols": "../modules/"}
    _sym_prefix = {"root": "symbols/", "modules": "../symbols/", "symbols": ""}

    def mod_link(file_id: str, where: str) -> str:
        f = idx.files_by_id[file_id]
        name = module_page.get(file_id)
        if not name:
            return _code(f.relative_path)
        return f"[{_code(f.relative_path)}]({_mod_prefix[where]}{name})"

    def sym_link(sym: SymbolNode, where: str) -> str:
        name = symbol_page.get(sym.id)
        label = _code(sym.qualified_name)
        if not name:
            return label
        return f"[{label}]({_sym_prefix[where]}{name})"

    # -- module pages --------------------------------------------------------
    for f in paged_files:
        syms = [s for s in idx.symbols_by_file.get(f.id, ()) if s.kind != SymbolKind.MODULE]
        counts = Counter(kind_label(s.kind) for s in syms)
        lines = [
            MARKER_LINE,
            f"# Module {_code(f.relative_path)}",
            "",
            f"Source: [{f.relative_path}]({_source_link('../../../', f.relative_path)}) "
            f"· {f.line_count} lines · {f.byte_size} bytes · sha256 {_code(f.content_hash[:12])}",
            "",
            "## Summary",
            "",
            f"- Symbols declared: {len(syms)}"
            + (" (" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ")" if counts else ""),
        ]
        out_imports = idx.imports_out.get(f.id, [])
        in_imports = idx.imports_in.get(f.id, [])
        lines.append("- Imports (resolved, in-repository): " + (", ".join(mod_link(t.id, "modules") for t in out_imports) or "none"))
        lines.append("- Imported by (resolved): " + (", ".join(mod_link(t.id, "modules") for t in in_imports) or "none"))
        lines += ["", "## Symbols", ""]
        if syms:
            lines += ["| Symbol | Kind | Lines | Calls (resolved) | Called by (resolved) |", "|---|---|---|---|---|"]
            for s in syms:
                name = sym_link(s, "modules") if s.id in symbol_page else _code(s.qualified_name)
                lines.append(
                    f"| {_cell(name)} | {kind_label(s.kind)} | "
                    f"[{_span(s)}]({_source_link('../../../', f.relative_path, s.start_line, s.end_line)}) | "
                    f"{len(idx.calls_out.get(s.id, ()))} | {len(idx.calls_in.get(s.id, ()))} |"
                )
        else:
            lines.append("No classes or functions are declared in this module.")
        lines += ["", _limits_note(), ""]
        docs.pages[f"modules/{module_page[f.id]}"] = "\n".join(lines)
    docs.module_pages = len(paged_files)

    # -- symbol pages --------------------------------------------------------
    for s in paged_syms:
        f = idx.files_by_id[s.file_id]
        lines = [
            MARKER_LINE,
            f"# {_code(s.qualified_name)} ({kind_label(s.kind)})",
            "",
            f"Defined in [{f.relative_path} {_span(s)}]({_source_link('../../../', f.relative_path, s.start_line, s.end_line)}) "
            f"· module {mod_link(f.id, 'symbols')}",
            "",
        ]
        parent = idx.symbols_by_id.get(s.parent_id) if s.parent_id else None
        if parent and parent.kind != SymbolKind.MODULE:
            lines += [f"Parent: {sym_link(parent, 'symbols')}", ""]
        members = idx.children.get(s.id, [])
        if members:
            lines += ["## Members", ""]
            lines += [f"- {sym_link(m, 'symbols')} ({kind_label(m.kind)}, {_span(m)})" for m in members]
            lines.append("")
        calls = idx.calls_out.get(s.id, [])
        callers = idx.calls_in.get(s.id, [])
        lines += ["## Calls (resolved)", ""]
        lines += [f"- {sym_link(t, 'symbols')} at {_code(f'{e.source_file or f.relative_path}:{e.start_line}')}" if e.start_line else f"- {sym_link(t, 'symbols')}" for t, e in calls] or ["None found by the conservative resolver."]
        lines += ["", "## Called by (resolved)", ""]
        lines += [f"- {sym_link(t, 'symbols')}" for t, _ in callers] or ["None found by the conservative resolver."]
        lines += ["", _limits_note(), ""]
        docs.pages[f"symbols/{symbol_page[s.id]}"] = "\n".join(lines)
    docs.symbol_pages = len(paged_syms)

    docs.pages["architecture.md"] = _architecture(idx, opts, docs, mod_link)
    docs.pages["INDEX.md"] = _index(idx, opts, docs, paged_files, paged_syms, mod_link, sym_link)
    return docs


def _limits_note() -> str:
    return (
        "> Generated from static analysis of Python source. Relationships shown are only those the "
        "extractor resolved conservatively; unresolved calls and imports are not guessed."
    )


def _architecture(idx: GraphIndex, opts: MarkdownOptions, docs: KnowledgeDocs, mod_link) -> str:
    g = idx.graph
    lines = [
        MARKER_LINE,
        f"# Architecture: {g.repository.name}",
        "",
        "Structure derived from the Code Graph. Source code remains the source of truth.",
        "",
        "## Overview",
        "",
        f"- Python files: {len(g.files)}",
        f"- Symbols (including modules): {len(g.symbols)}",
        f"- Relationships (graph edges): {len(g.edges)}",
        f"- Unresolved imports: {opts.unresolved_import_count} · unresolved calls: {opts.unresolved_call_count} "
        "(not guessed; see limitations)",
        f"- Build status: {opts.generation_status}",
        "",
        "## Packages",
        "",
    ]
    groups: Dict[str, List] = defaultdict(list)
    for f in g.files:
        top = f.relative_path.split("/", 1)[0] if "/" in f.relative_path else "(repository root)"
        groups[top].append(f)
    if groups:
        lines += ["| Top-level path | Files | Lines | Symbols |", "|---|---|---|---|"]
        for top in sorted(groups):
            members = groups[top]
            nsyms = sum(len([s for s in idx.symbols_by_file.get(m.id, ()) if s.kind != SymbolKind.MODULE]) for m in members)
            lines.append(f"| {_cell(_code(top))} | {len(members)} | {sum(m.line_count for m in members)} | {nsyms} |")
    else:
        lines.append("No Python files were discovered.")

    def top_files(key, title, limit=10):
        ranked = [f for f in sorted(g.files, key=lambda f: (-key(f), f.relative_path)) if key(f) > 0][:limit]
        out = ["", f"## {title}", ""]
        out += [f"- {mod_link(f.id, 'root')} ({key(f)})" for f in ranked] or ["None."]
        return out

    lines += top_files(lambda f: idx.fan_in(f.id), "Most imported modules (resolved, in-repository)")
    lines += top_files(lambda f: idx.fan_out(f.id), "Modules with most in-repository imports")
    cycles = _import_cycles(idx)
    lines += ["", "## Import cycles (resolved imports)", ""]
    if cycles:
        for comp in cycles[:10]:
            lines.append("- " + " ↔ ".join(_code(p) for p in comp))
        if len(cycles) > 10:
            lines.append(f"- … and {len(cycles) - 10} more")
    else:
        lines.append("None detected among resolved imports.")
    mains = [s for s in idx.declared_symbols() if s.name == "main" and s.kind in (SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION)]
    lines += ["", "## Symbols named `main`", ""]
    lines += [f"- {_code(s.qualified_name)} — {_code(s.relative_path + ':' + str(s.start_line))}" for s in mains[:20]] or ["None."]
    lines += [
        "",
        "## Limitations",
        "",
        "- Language coverage: Python only.",
        "- Cross-file call resolution is conservative: calls that cannot be tied to exactly one target are left unresolved.",
        "- Dynamic behavior (reflection, monkey-patching, dynamic imports) is not modeled.",
        "",
    ]
    return "\n".join(lines)


def _index(idx, opts, docs, paged_files, paged_syms, mod_link, sym_link) -> str:
    g = idx.graph
    lines = [
        MARKER_LINE,
        f"# KiteHut knowledge: {g.repository.name}",
        "",
        "Graph to understand. Context to reason. Evidence to trust.",
        "",
        "## At a glance",
        "",
        f"- Files: {len(g.files)} · Symbols: {len(g.symbols)} · Relationships: {len(g.edges)}",
        f"- Source fingerprint: {_code(g.source_digest[:16])}",
        f"- Build status: {opts.generation_status}",
        "- Architecture overview: [architecture.md](architecture.md)",
        "- Interactive graph: [graph.html](../visualization/graph.html)",
        "- Raw graph: [repository.json](../graph/repository.json)",
        "",
        "## Modules",
        "",
    ]
    if paged_files:
        lines += ["| Module | Symbols | Lines | Imports | Imported by |", "|---|---|---|---|---|"]
        for f in paged_files:
            nsyms = len([s for s in idx.symbols_by_file.get(f.id, ()) if s.kind != SymbolKind.MODULE])
            lines.append(f"| {_cell(mod_link(f.id, 'root'))} | {nsyms} | {f.line_count} | {idx.fan_out(f.id)} | {idx.fan_in(f.id)} |")
    else:
        lines.append("No Python files were discovered, so there are no module pages.")
    if docs.modules_omitted:
        lines += ["", f"_{docs.modules_omitted} further module(s) have no page (limit: {opts.max_module_pages}). They are in repository.json._"]
    lines += ["", "## Key symbols", ""]
    top = sorted(paged_syms, key=lambda s: (-idx.incoming_calls(s.id), s.relative_path, s.start_line))[:25]
    lines += [f"- {sym_link(s, 'root')} ({kind_label(s.kind)}) — {_code(s.relative_path + ':' + str(s.start_line))}" for s in top] or ["None."]
    if docs.symbols_omitted:
        lines += ["", f"_{docs.symbols_omitted} further symbol(s) have no page (limit: {opts.max_symbol_pages}); they are in repository.json._"]
    lines += [
        "",
        "## Reading this knowledge",
        "",
        "- Every reference gives a file path and line range; open the source to confirm. Source code is the truth.",
        "- Only resolved relationships are listed. Unresolved calls/imports are counted, not guessed.",
        "- Regenerate after code changes with `kitehut refresh <path>`; check staleness with `kitehut status <path>`.",
        "",
    ]
    return "\n".join(lines)
