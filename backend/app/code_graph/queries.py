"""Phase 11.8 — query helpers over a validated :class:`CodeGraph`.

Queries are model-backed: they never mutate the graph, never infer new
edges and never consult NetworkX.  Traversal is breadth-first with a
caller-supplied ``depth`` bound; each node is visited at most once per
query, cycles terminate, and results are returned in deterministic order
(by depth, then canonical ID).

Every returned relationship carries the source path/span provenance from the
originating edge.  Unknown IDs yield empty results rather than raising, so
callers can distinguish "no such node" from "no edges" by the length of the
returned list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from app.code_graph.models import (
    CodeGraph,
    EdgeKind,
    FileNode,
    GraphEdge,
    ResolutionKind,
    SymbolNode,
)


@dataclass(frozen=True)
class RelationshipHit:
    """A resolved edge between two graph nodes, with provenance."""

    edge: GraphEdge
    source_id: str
    target_id: str
    depth: int

    @property
    def kind(self) -> EdgeKind:
        return self.edge.kind

    def provenance(self) -> Dict[str, Any]:
        return {
            "source_file": self.edge.source_file,
            "start_line": self.edge.start_line,
            "end_line": self.edge.end_line,
            "start_column": self.edge.start_column,
            "end_column": self.edge.end_column,
            "source_hash": self.edge.source_hash,
            "provenance_kind": self.edge.provenance_kind.value,
            "confidence": self.edge.confidence.value,
            "resolution": self.edge.resolution.value,
        }


@dataclass
class QueryResult:
    """Outcome of a traversal query; deterministic ordering by construction."""

    start_id: str
    depth: int
    hits: List[RelationshipHit] = field(default_factory=list)

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.hits)

    def __iter__(self):
        return iter(self.hits)


# Small bounded memo: ``_traverse`` is called once per queried node, and callers such as
# graph-guided retrieval issue thousands of queries against the same graph.  Rebuilding the
# index each time made that quadratic.  The key includes element counts, so adding or
# removing nodes/edges in place invalidates the entry.  The entry holds a strong reference to
# the graph, so ``id(graph)`` cannot be reused while it is cached.
_INDEX_MEMO: "OrderedDict[tuple, tuple]" = OrderedDict()
_INDEX_MEMO_MAX = 4


def _index(graph: CodeGraph):
    key = (id(graph), len(graph.files), len(graph.symbols), len(graph.edges))
    hit = _INDEX_MEMO.get(key)
    if hit is not None and hit[0] is graph:
        _INDEX_MEMO.move_to_end(key)
        return hit[1]
    built = _build_index(graph)
    _INDEX_MEMO[key] = (graph, built)
    while len(_INDEX_MEMO) > _INDEX_MEMO_MAX:
        _INDEX_MEMO.popitem(last=False)
    return built


def _build_index(graph: CodeGraph):
    file_ids = {file.id for file in graph.files}
    symbol_ids = {symbol.id for symbol in graph.symbols}
    known_ids = file_ids | symbol_ids | {graph.repository.repository_id}

    outgoing: Dict[str, List[GraphEdge]] = {}
    incoming: Dict[str, List[GraphEdge]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source_id, []).append(edge)
        incoming.setdefault(edge.target_id, []).append(edge)
    for edges in outgoing.values():
        edges.sort(key=lambda edge: edge.id or "")
    for edges in incoming.values():
        edges.sort(key=lambda edge: edge.id or "")

    symbols_by_id = {symbol.id: symbol for symbol in graph.symbols}
    return known_ids, outgoing, incoming, symbols_by_id


def _traverse(
    graph: CodeGraph,
    start_id: str,
    *,
    depth: int,
    kinds: Optional[set[EdgeKind]],
    direction: str,
) -> QueryResult:
    if depth < 0:
        raise ValueError("depth must be >= 0")

    known_ids, outgoing, incoming, _ = _index(graph)
    result = QueryResult(start_id=start_id, depth=depth)
    if start_id not in known_ids or depth == 0:
        return result

    adjacency = outgoing if direction == "out" else incoming
    visited = {start_id}
    frontier = [start_id]

    for level in range(1, depth + 1):
        next_frontier: List[str] = []
        level_hits: List[RelationshipHit] = []
        for node_id in sorted(frontier):
            for edge in adjacency.get(node_id, []):
                if kinds is not None and edge.kind not in kinds:
                    continue
                other = edge.target_id if direction == "out" else edge.source_id
                level_hits.append(
                    RelationshipHit(
                        edge=edge,
                        source_id=edge.source_id,
                        target_id=edge.target_id,
                        depth=level,
                    )
                )
                if other not in visited:
                    visited.add(other)
                    next_frontier.append(other)
        level_hits.sort(key=lambda hit: (hit.edge.id or "", hit.source_id, hit.target_id))
        result.hits.extend(level_hits)
        if not next_frontier:
            break
        frontier = next_frontier

    return result


def find_symbols(
    graph: CodeGraph,
    name_or_qualified_name: str,
) -> List[SymbolNode]:
    """Return symbols whose ``name`` or ``qualified_name`` matches exactly.

    A non-empty, non-matching substring of either field also matches as a
    partial search when no exact matches are found.  Results are sorted by
    canonical symbol ID for deterministic output.
    """
    needle = (name_or_qualified_name or "").strip()
    if not needle:
        return []

    exact = [
        symbol
        for symbol in graph.symbols
        if symbol.name == needle or symbol.qualified_name == needle
    ]
    if exact:
        return sorted(exact, key=lambda symbol: symbol.id or "")

    needle_lower = needle.lower()
    partial = [
        symbol
        for symbol in graph.symbols
        if needle_lower in symbol.name.lower()
        or needle_lower in symbol.qualified_name.lower()
    ]
    return sorted(partial, key=lambda symbol: symbol.id or "")


def callers(graph: CodeGraph, symbol_id: str, depth: int = 1) -> QueryResult:
    """Return CALLS edges whose target is ``symbol_id`` (incoming, ≤ depth)."""
    return _traverse(graph, symbol_id, depth=depth, kinds={EdgeKind.CALLS}, direction="in")


def callees(graph: CodeGraph, symbol_id: str, depth: int = 1) -> QueryResult:
    """Return CALLS edges whose source is ``symbol_id`` (outgoing, ≤ depth)."""
    return _traverse(graph, symbol_id, depth=depth, kinds={EdgeKind.CALLS}, direction="out")


def imports(graph: CodeGraph, file_id: str, depth: int = 1) -> QueryResult:
    """Return IMPORTS edges whose source is ``file_id`` (outgoing, ≤ depth)."""
    return _traverse(graph, file_id, depth=depth, kinds={EdgeKind.IMPORTS}, direction="out")


def dependents(graph: CodeGraph, file_id: str, depth: int = 1) -> QueryResult:
    """Return IMPORTS edges whose target is ``file_id`` (incoming, ≤ depth)."""
    return _traverse(graph, file_id, depth=depth, kinds={EdgeKind.IMPORTS}, direction="in")


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactEntry:
    """A node affected by a change, reached at ``depth`` over a resolved edge."""

    id: str
    relative_path: str
    qualified_name: Optional[str]
    depth: int
    relationship: str  # "imports" | "calls"
    edge: GraphEdge


def _impact(
    graph: CodeGraph,
    start_id: str,
    *,
    kind: EdgeKind,
    max_depth: int,
    relationship: str,
) -> List[ImpactEntry]:
    """Breadth-first reverse traversal over resolved ``kind`` edges.

    Each node is reported once at its minimum depth; ordering is
    ``(depth, path, qualified name, id)`` and cycles terminate.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")
    known_ids, _, incoming, symbols_by_id = _index(graph)
    files_by_id = {file.id: file for file in graph.files}
    if start_id not in known_ids or max_depth == 0:
        return []

    seen = {start_id}
    frontier = [start_id]
    entries: List[ImpactEntry] = []
    for level in range(1, max_depth + 1):
        next_frontier: List[str] = []
        level_entries: List[ImpactEntry] = []
        for node_id in sorted(frontier):
            for edge in incoming.get(node_id, []):
                if edge.kind != kind or edge.resolution != ResolutionKind.RESOLVED:
                    continue
                other = edge.source_id
                if other in seen:
                    continue
                seen.add(other)
                next_frontier.append(other)
                symbol = symbols_by_id.get(other)
                file_node = files_by_id.get(other)
                if symbol is not None:
                    level_entries.append(
                        ImpactEntry(
                            id=other,
                            relative_path=symbol.relative_path,
                            qualified_name=symbol.qualified_name,
                            depth=level,
                            relationship=relationship,
                            edge=edge,
                        )
                    )
                elif file_node is not None:
                    level_entries.append(
                        ImpactEntry(
                            id=other,
                            relative_path=file_node.relative_path,
                            qualified_name=None,
                            depth=level,
                            relationship=relationship,
                            edge=edge,
                        )
                    )
        level_entries.sort(
            key=lambda item: (
                item.depth,
                item.relative_path,
                item.qualified_name or "",
                item.id,
            )
        )
        entries.extend(level_entries)
        if not next_frontier:
            break
        frontier = next_frontier
    return entries


def impacted_files(graph: CodeGraph, file_id: str, max_depth: int = 3) -> List[ImpactEntry]:
    """Files that (transitively) import ``file_id`` — the change-impact set."""
    return _impact(
        graph, file_id, kind=EdgeKind.IMPORTS, max_depth=max_depth,
        relationship="imports",
    )


def impacted_symbols(graph: CodeGraph, symbol_id: str, max_depth: int = 3) -> List[ImpactEntry]:
    """Symbols that (transitively) call ``symbol_id`` — its caller impact set."""
    return _impact(
        graph, symbol_id, kind=EdgeKind.CALLS, max_depth=max_depth,
        relationship="calls",
    )


# ---------------------------------------------------------------------------
# Circular imports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportCycle:
    """A strongly connected set of files connected by resolved IMPORTS edges."""

    members: List[str]
    edges: List[GraphEdge]

    def describe(self) -> str:
        """One-line, source-backed explanation of the cycle."""
        where = " -> ".join([*self.members, self.members[0]])
        locations = ", ".join(
            f"{edge.source_file}:{edge.start_line}" for edge in self.edges
        )
        return f"Import cycle: {where} (imports at {locations})"


def _strongly_connected(adjacency: Dict[str, List[str]]) -> List[List[str]]:
    """Iterative Tarjan SCC; components are deterministic for sorted input."""
    index_of: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: set[str] = set()
    stack: List[str] = []
    result: List[List[str]] = []
    counter = 0

    for start in sorted(adjacency):
        if start in index_of:
            continue
        work = [(start, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = counter
                lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = adjacency.get(node, [])
            if child_index < len(children):
                child = children[child_index]
                work[-1] = (node, child_index + 1)
                if child not in index_of:
                    work.append((child, 0))
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
                continue
            # All children processed: close the component rooted here.
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))
    return result


def import_cycles(graph: CodeGraph) -> List[ImportCycle]:
    """Detect circular module dependencies among **resolved** IMPORTS edges.

    Self-imports are ignored.  Each cycle lists its members (repository-relative
    paths) and every resolved import edge between members, so callers can show
    the exact source locations.  Results are sorted by first member path.
    """
    files_by_id = {file.id: file for file in graph.files}
    adjacency: Dict[str, List[str]] = {file.id: [] for file in graph.files}
    edges_by_source: Dict[str, List[GraphEdge]] = {}
    for edge in graph.edges:
        if (
            edge.kind == EdgeKind.IMPORTS
            and edge.resolution == ResolutionKind.RESOLVED
            and edge.source_id in files_by_id
            and edge.target_id in files_by_id
            and edge.source_id != edge.target_id
        ):
            adjacency.setdefault(edge.source_id, []).append(edge.target_id)
            edges_by_source.setdefault(edge.source_id, []).append(edge)
    for targets in adjacency.values():
        targets.sort()
    for edges in edges_by_source.values():
        edges.sort(key=lambda edge: (edge.source_file, edge.start_line, edge.id or ""))

    cycles: List[ImportCycle] = []
    for component in _strongly_connected(adjacency):
        if len(component) < 2:
            continue
        members = {files_by_id[node].relative_path for node in component}
        member_ids = set(component)
        cycle_edges = sorted(
            (
                edge
                for source, edges in edges_by_source.items()
                if source in member_ids
                for edge in edges
                if edge.target_id in member_ids
            ),
            key=lambda edge: (edge.source_file, edge.start_line, edge.id or ""),
        )
        cycles.append(
            ImportCycle(members=sorted(members), edges=cycle_edges)
        )
    cycles.sort(key=lambda cycle: cycle.members[0])
    return cycles


__all__ = [
    "ImpactEntry",
    "ImportCycle",
    "QueryResult",
    "RelationshipHit",
    "find_symbols",
    "callers",
    "callees",
    "imports",
    "dependents",
    "impacted_files",
    "impacted_symbols",
    "import_cycles",
]
