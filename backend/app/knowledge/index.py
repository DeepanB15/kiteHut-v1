"""Precomputed lookup tables over a :class:`CodeGraph`.

Built once and shared by the Markdown generator, the HTML visualization, the
context engine and the MCP tools so each of them sees identical relationships.
Only edges present in the graph are used; nothing is inferred.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from app.code_graph.models import (
    CodeGraph,
    EdgeKind,
    FileNode,
    GraphEdge,
    ResolutionKind,
    SymbolKind,
    SymbolNode,
)


@dataclass
class GraphIndex:
    graph: CodeGraph
    files_by_id: Dict[str, FileNode] = field(default_factory=dict)
    files_by_path: Dict[str, FileNode] = field(default_factory=dict)
    symbols_by_id: Dict[str, SymbolNode] = field(default_factory=dict)
    symbols_by_file: Dict[str, List[SymbolNode]] = field(default_factory=lambda: defaultdict(list))
    module_symbol: Dict[str, SymbolNode] = field(default_factory=dict)  # file_id -> MODULE symbol
    children: Dict[str, List[SymbolNode]] = field(default_factory=lambda: defaultdict(list))
    calls_out: Dict[str, List[Tuple[SymbolNode, GraphEdge]]] = field(default_factory=lambda: defaultdict(list))
    calls_in: Dict[str, List[Tuple[SymbolNode, GraphEdge]]] = field(default_factory=lambda: defaultdict(list))
    imports_out: Dict[str, List[FileNode]] = field(default_factory=lambda: defaultdict(list))
    imports_in: Dict[str, List[FileNode]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def from_graph(cls, graph: CodeGraph) -> "GraphIndex":
        idx = cls(graph=graph)
        idx.files_by_id = {f.id: f for f in graph.files}
        idx.files_by_path = {f.relative_path: f for f in graph.files}
        idx.symbols_by_id = {s.id: s for s in graph.symbols}
        for sym in graph.symbols:
            idx.symbols_by_file[sym.file_id].append(sym)
            if sym.kind == SymbolKind.MODULE:
                idx.module_symbol[sym.file_id] = sym
            if sym.parent_id and sym.parent_id in idx.symbols_by_id:
                idx.children[sym.parent_id].append(sym)
        seen_import: Set[Tuple[str, str]] = set()
        seen_call: Set[Tuple[str, str]] = set()
        for edge in graph.edges:
            if edge.resolution != ResolutionKind.RESOLVED:
                continue
            if edge.kind == EdgeKind.CALLS:
                src = idx.symbols_by_id.get(edge.source_id)
                dst = idx.symbols_by_id.get(edge.target_id)
                key = (edge.source_id, edge.target_id)
                if src and dst and key not in seen_call:
                    seen_call.add(key)
                    idx.calls_out[src.id].append((dst, edge))
                    idx.calls_in[dst.id].append((src, edge))
            elif edge.kind == EdgeKind.IMPORTS:
                src_f = idx.files_by_id.get(edge.source_id)
                dst_f = idx.files_by_id.get(edge.target_id)
                key = (edge.source_id, edge.target_id)
                if src_f and dst_f and key not in seen_import and src_f.id != dst_f.id:
                    seen_import.add(key)
                    idx.imports_out[src_f.id].append(dst_f)
                    idx.imports_in[dst_f.id].append(src_f)
        for table in (idx.symbols_by_file, idx.children):
            for items in table.values():
                items.sort(key=lambda s: (s.start_line, s.qualified_name, s.id))
        for table in (idx.calls_out, idx.calls_in):
            for items in table.values():
                items.sort(key=lambda pair: (pair[0].relative_path, pair[0].start_line, pair[0].qualified_name))
        for table in (idx.imports_out, idx.imports_in):
            for items in table.values():
                items.sort(key=lambda f: f.relative_path)
        return idx

    # -- convenience -------------------------------------------------------
    def declared_symbols(self) -> List[SymbolNode]:
        """All non-module symbols in deterministic order."""
        return sorted(
            (s for s in self.graph.symbols if s.kind != SymbolKind.MODULE),
            key=lambda s: (s.relative_path, s.start_line, s.qualified_name, s.id),
        )

    def fan_in(self, file_id: str) -> int:
        return len(self.imports_in.get(file_id, ()))

    def fan_out(self, file_id: str) -> int:
        return len(self.imports_out.get(file_id, ()))

    def incoming_calls(self, symbol_id: str) -> int:
        return len(self.calls_in.get(symbol_id, ()))


def kind_label(kind: SymbolKind) -> str:
    """Short, user-facing symbol kind (async variants collapse to base kind)."""
    return {
        SymbolKind.MODULE: "module",
        SymbolKind.CLASS: "class",
        SymbolKind.FUNCTION: "function",
        SymbolKind.ASYNC_FUNCTION: "async function",
        SymbolKind.METHOD: "method",
        SymbolKind.ASYNC_METHOD: "async method",
    }[kind]
