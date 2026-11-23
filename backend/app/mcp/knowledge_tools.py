"""KiteHut knowledge tools for MCP clients (Claude Code, Codex, others).

Plain functions bound to one repository root: the root is fixed when the
server starts and no argument can widen it.  All tools are read-only and
return JSON-serializable dicts with ``success`` and, on failure, a
human-readable ``error``.  Output sizes are capped.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from app.code_graph.queries import find_symbols
from app.knowledge import (
    KiteHutError,
    check_freshness,
    list_knowledge_pages,
    load_graph,
    load_metadata,
    read_knowledge_page,
)
from app.knowledge.context import retrieve_context
from app.knowledge.evidence import collect_evidence
from app.knowledge.index import GraphIndex, kind_label
from app.knowledge.safe_io import resolve_repository_root

MAX_RESULTS = 50
MAX_PAGE_CHARS = 60_000


def _clamp(value: Optional[int], default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value if value is not None else default)))
    except (TypeError, ValueError):
        return default


def _sym_dict(sym) -> Dict[str, Any]:
    return {
        "name": sym.name, "qualified_name": sym.qualified_name, "kind": kind_label(sym.kind),
        "path": sym.relative_path, "start_line": sym.start_line, "end_line": sym.end_line,
        "reference": f"{sym.relative_path}:{sym.start_line}-{sym.end_line}",
    }


class KnowledgeTools:
    """Read-only tool implementations bound to ``repository_path``."""

    def __init__(self, repository_path: str):
        self.root = resolve_repository_root(repository_path)

    # -- helpers -------------------------------------------------------------
    def _guard(self, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            out = fn()
            out.setdefault("success", True)
        except KiteHutError as exc:
            out = {"success": False, "error": str(exc)}
        except Exception as exc:  # never leak a traceback into an MCP client
            out = {"success": False, "error": f"Unexpected error: {exc.__class__.__name__}: {exc}"}
        out["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return out

    def _freshness_note(self) -> Dict[str, Any]:
        try:
            r = check_freshness(self.root)
            return {"state": r.state, "message": r.message}
        except KiteHutError as exc:
            return {"state": "unknown", "message": str(exc)}

    def _match(self, graph, name: str):
        matches = find_symbols(graph, name)
        exact = [m for m in matches if m.qualified_name == name or m.name == name]
        return exact or matches

    # -- tools ---------------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        def run():
            meta = load_metadata(self.root)
            graph = load_graph(self.root)
            idx = GraphIndex.from_graph(graph)
            top = sorted(graph.files, key=lambda f: (-idx.fan_in(f.id), f.relative_path))[:10]
            return {
                "repository": meta.repository_name, "kitehut_version": meta.kitehut_version,
                "generated_at": meta.generated_at, "generation_status": meta.generation_status,
                "files": meta.file_count, "symbols": meta.symbol_count, "relationships": meta.relationship_count,
                "unresolved_imports": meta.unresolved_import_count, "unresolved_calls": meta.unresolved_call_count,
                "most_imported_modules": [{"path": f.relative_path, "imported_by": idx.fan_in(f.id)} for f in top if idx.fan_in(f.id)],
                "knowledge_pages": len(list_knowledge_pages(self.root)),
                "freshness": self._freshness_note(),
                "language_coverage": "Python only",
            }
        return self._guard(run)

    def search_symbols(self, query: str, limit: Optional[int] = None) -> Dict[str, Any]:
        def run():
            n = _clamp(limit, 20, 1, MAX_RESULTS)
            found = self._match(load_graph(self.root), query)
            ordered = sorted(found, key=lambda s: (s.relative_path, s.start_line))
            return {"query": query, "total": len(ordered), "symbols": [_sym_dict(s) for s in ordered[:n]]}
        return self._guard(run)

    def _relation(self, symbol: str, key: str, depth: Optional[int]) -> Dict[str, Any]:
        def run():
            graph = load_graph(self.root)
            matches = self._match(graph, symbol)
            if not matches:
                return {"success": False, "error": f"No symbol matches '{symbol}'. Try kitehut_search_symbols."}
            if len(matches) > 1 and not any(m.qualified_name == symbol for m in matches):
                return {"success": False, "error": "Ambiguous symbol; use a qualified name.",
                        "candidates": [_sym_dict(m) for m in matches[:10]]}
            sym = next((m for m in matches if m.qualified_name == symbol), matches[0])
            idx = GraphIndex.from_graph(graph)
            table = idx.calls_in if key == "callers" else idx.calls_out
            seen, frontier, related = {sym.id}, [sym.id], []
            for level in range(1, _clamp(depth, 1, 1, 3) + 1):
                nxt = []
                for sid in frontier:
                    for other, _edge in table.get(sid, ()):
                        if other.id not in seen:
                            seen.add(other.id)
                            nxt.append(other.id)
                            related.append({**_sym_dict(other), "depth": level})
                frontier = nxt
            related.sort(key=lambda d: (d["depth"], d["path"], d["start_line"]))
            return {"symbol": _sym_dict(sym), key: related[:MAX_RESULTS], "total": len(related),
                    "note": "Only conservatively resolved calls are listed; unresolved calls are not guessed."}
        return self._guard(run)

    def find_callers(self, symbol: str, depth: Optional[int] = None) -> Dict[str, Any]:
        return self._relation(symbol, "callers", depth)

    def find_callees(self, symbol: str, depth: Optional[int] = None) -> Dict[str, Any]:
        return self._relation(symbol, "callees", depth)

    def find_dependencies(self, path: str, direction: Optional[str] = None) -> Dict[str, Any]:
        """Resolved in-repository imports of ``path`` (``imports``) or its importers (``imported_by``)."""
        def run():
            graph = load_graph(self.root)
            idx = GraphIndex.from_graph(graph)
            rel = (path or "").replace("\\", "/").lstrip("./")
            node = idx.files_by_path.get(rel)
            if node is None:
                return {"success": False, "error": f"File not in graph: {path}. Paths are repository-relative."}
            out = [{"path": f.relative_path} for f in idx.imports_out.get(node.id, ())]
            inc = [{"path": f.relative_path} for f in idx.imports_in.get(node.id, ())]
            want = (direction or "both").lower()
            res: Dict[str, Any] = {"path": rel}
            if want in ("imports", "both"):
                res["imports"] = out[:MAX_RESULTS]
            if want in ("imported_by", "both"):
                res["imported_by"] = inc[:MAX_RESULTS]
            res["note"] = "Resolved in-repository imports only; third-party and unresolved imports are not listed."
            return res
        return self._guard(run)

    def get_context(self, query: str, token_budget: Optional[int] = None, max_items: Optional[int] = None) -> Dict[str, Any]:
        def run():
            b = retrieve_context(self.root, query, token_budget=_clamp(token_budget, 4000, 100, 32000),
                                 max_items=_clamp(max_items, 10, 1, 30))
            return b.to_dict()
        return self._guard(run)

    def get_evidence(self, statement: str, file: Optional[str] = None, line: Optional[int] = None,
                     pattern: Optional[str] = None, verify: Optional[bool] = None) -> Dict[str, Any]:
        def run():
            return collect_evidence(self.root, statement, target_file=file, target_line=line,
                                    expected_pattern=pattern, verify=bool(verify)).to_dict()
        return self._guard(run)

    def read_knowledge(self, page: Optional[str] = None) -> Dict[str, Any]:
        """Read a generated Markdown page (default INDEX.md); lists pages when ``page`` is ``list``."""
        def run():
            if page == "list":
                pages = list_knowledge_pages(self.root)
                return {"pages": pages[:500], "total": len(pages)}
            text = read_knowledge_page(self.root, page or "INDEX.md")
            truncated = len(text) > MAX_PAGE_CHARS
            return {"page": page or "INDEX.md", "truncated": truncated, "content": text[:MAX_PAGE_CHARS]}
        return self._guard(run)

    def check_freshness(self) -> Dict[str, Any]:
        def run():
            r = check_freshness(self.root).to_dict()
            for k in ("added", "removed", "modified"):
                r[k] = r[k][:MAX_RESULTS]
            return r
        return self._guard(run)


#: (mcp tool name, method name, description)
TOOL_SPECS: List[tuple] = [
    ("kitehut_overview", "overview", "Repository overview from .kitehut/: counts, most-imported modules, freshness. Start here."),
    ("kitehut_search_symbols", "search_symbols", "Find classes/functions/methods by name. Returns file path and line range."),
    ("kitehut_find_callers", "find_callers", "Symbols that call the given symbol (resolved relationships only)."),
    ("kitehut_find_callees", "find_callees", "Symbols called by the given symbol (resolved relationships only)."),
    ("kitehut_find_dependencies", "find_dependencies", "In-repository imports of a file and the files that import it."),
    ("kitehut_get_context", "get_context", "Ranked, de-duplicated, token-budgeted source snippets for a question, with path:line references."),
    ("kitehut_get_evidence", "get_evidence", "Source-backed evidence (path, lines, hashes, stale flags) for a statement; verify=true runs the verifier."),
    ("kitehut_read_knowledge", "read_knowledge", "Read a generated knowledge page (INDEX.md, architecture.md, modules/..., symbols/...). Use page='list' to list pages."),
    ("kitehut_check_freshness", "check_freshness", "Check whether .kitehut/ is stale relative to the source tree."),
]
