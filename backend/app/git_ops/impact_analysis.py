"""Change impact propagation over a :class:`CodeGraph` (Phase 3D / 3D.1).

The service answers one question: *given a Git changeset, which graph nodes
depend on the changed nodes, and through which evidence?*

Edge orientation
----------------
Every edge in the graph points from the **dependent** to the **dependency**:

========================================  ==============================
Edge kind                                 Orientation
========================================  ==============================
``IMPORTS``                               ``file A -> file B`` (A imports B)
``CALLS``                                 ``symbol X -> symbol Y`` (X calls Y)
``DEFINES``                               ``file A -> symbol S`` (A declares S)
``CONTAINS`` (repository)                 ``repository -> file A``
``CONTAINS`` (lexical)                    ``file/symbol -> symbol``
========================================  ==============================

Impact therefore propagates only along **incoming dependency edges**
(``IMPORTS`` and ``CALLS``).  If ``B`` changes, every ``A`` with
``A --IMPORTS--> B`` is impacted, and the files that import ``A`` are
transitively impacted.  ``CONTAINS`` and ``DEFINES`` are structural, not
dependency edges: they are used only to expand a changed file into the symbols
it declares.  They never propagate impact away from a change, so a file that
``B`` imports is **not** reported as impacted.

Resolution honesty
------------------
An edge whose ``resolution`` is ``UNRESOLVED`` or ``AMBIGUOUS`` is never
reported as a confirmed impact.  It is reported in its own bucket
(``unresolved_impacts`` / ``ambiguous_impacts``) and its endpoint is excluded
from ``impacted_nodes``.

Depth semantics
---------------
``max_depth`` counts hops from the changed node.  ``1`` returns direct
dependents only, ``N`` bounds the search to ``N`` hops, and ``-1`` means
unlimited (cycles are still terminated by path/visited tracking).  ``0``
returns no impacts at all.

Maximum-result semantics
------------------------
``max_results`` is a hard cap on the number of impact **records** returned
across ``direct_impacts``, ``transitive_impacts``, ``unresolved_impacts`` and
``ambiguous_impacts`` combined.  Records are ordered by
``(depth, edge_id, path)`` *before* the cap is applied, so truncation is
deterministic and always keeps the shallowest impacts.  When the cap is hit,
``traversal_metadata["truncated"]`` is ``True`` and a warning is added; the
cap is never applied silently.

Determinism
-----------
Changed node IDs, frontiers, records, paths and impacted nodes are sorted with
stable keys, so analysing the same graph and changeset twice (or a graph whose
edge list was built in a different order) produces byte-identical
:meth:`ImpactAnalysisResult.to_json` output.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.code_graph.models import (
    CodeGraph,
    EdgeKind,
    NodeKind,
    ResolutionKind,
)
from app.code_graph.traversal import _build_index
from app.git_ops.mapping_service import EvidenceConfidence as MappingConfidence
from app.git_ops.mapping_service import MappingResult
from app.git_ops.models.changeset import ChangeStatus, Changeset


#: Edge kinds that carry a dependency (\"source depends on target\").
DEPENDENCY_EDGE_KINDS = frozenset({EdgeKind.IMPORTS, EdgeKind.CALLS})

#: Structural edge kinds.  Documented for orientation checks; never propagated.
STRUCTURAL_EDGE_KINDS = frozenset({EdgeKind.CONTAINS, EdgeKind.DEFINES})

_DEFAULT_MAX_DEPTH = -1
_DEFAULT_MAX_RESULTS = 1000

_CONFIG_KEYS = frozenset(
    {"max_depth", "max_results", "kinds", "exclusions", "changed_symbols"}
)


class ImpactClassification(str, Enum):
    """Why a node appears in an impact report."""

    DIRECT = "direct"
    TRANSITIVE = "transitive"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    UNMAPPED = "unmapped"


@dataclass(frozen=True)
class EdgeImpact:
    """Impact recorded through one graph edge.

    ``path`` runs from the changed node to the impacted node, so ``path[0]`` is
    the changed node and ``path[-1]`` is the impacted node.  ``depth`` is
    ``len(path) - 1``.
    """

    edge: Any
    depth: int
    classification: ImpactClassification
    path: Tuple[str, ...]


@dataclass
class ImpactAnalysisResult:
    """Structured result of propagating change impact through a CodeGraph."""

    changed_nodes: Dict[str, Dict[str, Any]]
    impacted_nodes: Dict[str, Dict[str, Any]]
    direct_impacts: List[Dict[str, Any]]
    transitive_impacts: List[Dict[str, Any]]
    unresolved_impacts: List[Dict[str, Any]]
    ambiguous_impacts: List[Dict[str, Any]]
    unmapped_changes: List[Dict[str, Any]]
    dependency_paths: Dict[str, List[List[str]]]
    traversal_metadata: Dict[str, Any]
    analysis_warnings: List[str]
    analysis_summary: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "changed_nodes": self.changed_nodes,
            "impacted_nodes": self.impacted_nodes,
            "direct_impacts": self.direct_impacts,
            "transitive_impacts": self.transitive_impacts,
            "unresolved_impacts": self.unresolved_impacts,
            "ambiguous_impacts": self.ambiguous_impacts,
            "unmapped_changes": self.unmapped_changes,
            "dependency_paths": self.dependency_paths,
            "traversal_metadata": self.traversal_metadata,
            "analysis_warnings": self.analysis_warnings,
            "analysis_summary": self.analysis_summary,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _normalize_path(path: str) -> str:
    """Lenient repository-path normalization (``\\`` -> ``/``, drop ``.``)."""
    normalized = unicodedata.normalize("NFC", str(path)).replace("\\", "/")
    return "/".join(part for part in normalized.split("/") if part not in ("", "."))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


class ImpactAnalysisService:
    """Propagates change impact through a CodeGraph using bounded BFS."""

    def __init__(
        self,
        graph: CodeGraph,
        mapping_result: MappingResult,
        traversal_config: Optional[Dict[str, Any]] = None,
    ):
        if not isinstance(graph, CodeGraph):
            raise TypeError("graph must be a CodeGraph instance.")
        if not isinstance(mapping_result, MappingResult):
            raise TypeError("mapping_result must be a MappingResult instance.")

        self.graph = graph
        self.mapping_result = mapping_result

        # Adjacency indexes are built once; the graph is never mutated.
        (
            self._known_ids,
            self._outgoing,
            self._incoming,
            self._symbols_by_id,
        ) = _build_index(graph)
        self._files_by_id = {file.id: file for file in graph.files}
        self._nodes_by_id: Dict[str, Any] = {
            **self._symbols_by_id,
            **self._files_by_id,
        }
        self._files_by_path = {
            file.relative_path: file for file in graph.files
        }
        self._symbols_by_path: Dict[str, List[Any]] = {}
        for symbol in graph.symbols:
            self._symbols_by_path.setdefault(symbol.relative_path, []).append(symbol)
        for symbols in self._symbols_by_path.values():
            symbols.sort(key=lambda s: (s.qualified_name, s.id))

        self._config = self._validate_config(traversal_config or {})

        # Per-run state (reset at the start of every analyze()).
        self._deleted_node_ids: Set[str] = set()
        self._unresolved_symbol_refs: List[str] = []
        self._dangling_edge_ids: Set[str] = set()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        unknown = set(config) - _CONFIG_KEYS
        if unknown:
            raise ValueError(
                "Unknown traversal_config key(s): "
                + ", ".join(sorted(unknown))
                + ". Supported keys: "
                + ", ".join(sorted(_CONFIG_KEYS))
                + "."
            )

        max_depth = config.get("max_depth", _DEFAULT_MAX_DEPTH)
        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise ValueError(f"max_depth must be an integer; got {max_depth!r}.")
        if max_depth < -1:
            raise ValueError(f"max_depth must be >= -1; got {max_depth}.")

        max_results = config.get("max_results", _DEFAULT_MAX_RESULTS)
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError(f"max_results must be an integer; got {max_results!r}.")
        if max_results < 1:
            raise ValueError(f"max_results must be >= 1; got {max_results}.")

        kinds = config.get("kinds")
        if kinds is None:
            dependency_kinds = DEPENDENCY_EDGE_KINDS
        else:
            if isinstance(kinds, (str, EdgeKind)):
                kinds = [kinds]
            try:
                coerced = frozenset(
                    kind if isinstance(kind, EdgeKind) else EdgeKind(kind)
                    for kind in kinds
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"kinds must contain only EdgeKind values; got {kinds!r}."
                ) from error
            invalid = coerced - DEPENDENCY_EDGE_KINDS
            if invalid:
                raise ValueError(
                    "kinds may only restrict dependency edges "
                    f"({', '.join(sorted(k.value for k in DEPENDENCY_EDGE_KINDS))}); "
                    "structural edges "
                    f"({', '.join(sorted(k.value for k in invalid))}) "
                    "never propagate impact."
                )
            dependency_kinds = coerced

        exclusions = config.get("exclusions")
        if exclusions is None:
            exclusion_set: frozenset[str] = frozenset()
        else:
            if isinstance(exclusions, str):
                raise ValueError(
                    "exclusions must be a collection of node IDs, not a string."
                )
            try:
                exclusion_set = frozenset(str(node_id) for node_id in exclusions)
            except TypeError as error:
                raise ValueError(
                    f"exclusions must be an iterable of node IDs; got {exclusions!r}."
                ) from error

        changed_symbols = config.get("changed_symbols")
        if changed_symbols is None:
            symbol_refs: Tuple[str, ...] = ()
        else:
            if isinstance(changed_symbols, str):
                changed_symbols = [changed_symbols]
            try:
                symbol_refs = tuple(str(ref) for ref in changed_symbols)
            except TypeError as error:
                raise ValueError(
                    "changed_symbols must be a collection of symbol references; "
                    f"got {changed_symbols!r}."
                ) from error

        return {
            "max_depth": max_depth,
            "max_results": max_results,
            "kinds": kinds,
            "exclusions": exclusion_set,
            "changed_symbols": symbol_refs,
            "dependency_kinds": dependency_kinds,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, changeset: Changeset) -> ImpactAnalysisResult:
        """Run full impact analysis for ``changeset``."""
        if not isinstance(changeset, Changeset):
            raise TypeError("changeset must be a Changeset instance.")

        self._deleted_node_ids = set()
        self._unresolved_symbol_refs = []
        self._dangling_edge_ids = set()

        changed_node_ids = self._identify_changed_nodes(changeset)
        unmapped = self._resolve_unmapped(changeset)

        if not changed_node_ids:
            return self._empty_result(changeset, changed_node_ids, unmapped)

        # ---- Phase 2: propagate along incoming dependency edges ----
        raw_impacts: List[EdgeImpact] = []
        for changed_id in changed_node_ids:
            if changed_id in self._deleted_node_ids:
                # A deleted file has no live dependency edges to follow.
                continue
            raw_impacts.extend(self._collect_impacts(changed_id))

        discovered = len(raw_impacts)
        impacts = self._dedupe(raw_impacts)

        truncated = False
        max_results = self._config["max_results"]
        if len(impacts) > max_results:
            impacts = impacts[:max_results]
            truncated = True

        resolved: List[EdgeImpact] = []
        unresolved: List[EdgeImpact] = []
        ambiguous: List[EdgeImpact] = []
        for impact in impacts:
            resolution = impact.edge.resolution
            if resolution == ResolutionKind.UNRESOLVED:
                unresolved.append(impact)
            elif resolution == ResolutionKind.AMBIGUOUS:
                ambiguous.append(impact)
            else:
                resolved.append(impact)

        # A component reachable at several depths is classified DIRECT if any
        # path is one hop long; transitive records for that component are then
        # reported as direct evidence rather than duplicated.
        min_depth: Dict[str, int] = {}
        for impact in resolved:
            node_id = impact.path[-1]
            current = min_depth.get(node_id)
            if current is None or impact.depth < current:
                min_depth[node_id] = impact.depth

        direct_impacts = [
            impact
            for impact in resolved
            if min_depth[impact.path[-1]] == 1
        ]
        transitive_impacts = [
            impact
            for impact in resolved
            if min_depth[impact.path[-1]] > 1
        ]

        dependency_paths: Dict[str, List[List[str]]] = {}
        for impact in resolved:
            node_id = impact.path[-1]
            paths = dependency_paths.setdefault(node_id, [])
            path = list(impact.path)
            if path not in paths:
                paths.append(path)
        for paths in dependency_paths.values():
            paths.sort(key=lambda p: (len(p), p))
        dependency_paths = {
            node_id: dependency_paths[node_id] for node_id in sorted(dependency_paths)
        }

        changed_nodes = {
            node_id: self._node_record(node_id) for node_id in changed_node_ids
        }
        impacted_nodes = {
            node_id: self._node_record(node_id) for node_id in sorted(min_depth)
        }

        direct_records = self._classify_by_type(
            direct_impacts, ImpactClassification.DIRECT
        )
        transitive_records = self._classify_by_type(
            transitive_impacts, ImpactClassification.TRANSITIVE
        )
        unresolved_records = self._classify_by_type(
            unresolved, ImpactClassification.UNRESOLVED
        )
        ambiguous_records = self._classify_by_type(
            ambiguous, ImpactClassification.AMBIGUOUS
        )

        warnings = self._build_warnings(
            changeset=changeset,
            changed_node_ids=changed_node_ids,
            unresolved=unresolved,
            ambiguous=ambiguous,
            unmapped=unmapped,
            truncated=truncated,
        )

        summary = self._build_summary(
            changed=len(changed_node_ids),
            impacted=len(impacted_nodes),
            direct=len(direct_records),
            transitive=len(transitive_records),
            unresolved=len(unresolved_records),
            ambiguous=len(ambiguous_records),
            unmapped=len(unmapped),
            truncated=truncated,
        )

        return ImpactAnalysisResult(
            changed_nodes=changed_nodes,
            impacted_nodes=impacted_nodes,
            direct_impacts=direct_records,
            transitive_impacts=transitive_records,
            unresolved_impacts=unresolved_records,
            ambiguous_impacts=ambiguous_records,
            unmapped_changes=unmapped,
            dependency_paths=dependency_paths,
            traversal_metadata=self._metadata(
                changeset=changeset,
                changed_node_count=len(changed_node_ids),
                discovered=discovered,
                returned=len(impacts),
                truncated=truncated,
            ),
            analysis_warnings=warnings,
            analysis_summary=summary,
        )

    # ------------------------------------------------------------------
    # Phase 1: changed nodes
    # ------------------------------------------------------------------

    def _identify_changed_nodes(self, changeset: Changeset) -> List[str]:
        """Map changeset units (and configured symbols) to graph node IDs."""
        ordered: List[str] = []
        seen: Set[str] = set()

        def add(node_id: Optional[str]) -> None:
            if node_id and node_id in self._known_ids and node_id not in seen:
                seen.add(node_id)
                ordered.append(node_id)

        for reference in self._config["changed_symbols"]:
            node_id = self._resolve_symbol_ref(reference)
            if node_id is None:
                self._unresolved_symbol_refs.append(reference)
            else:
                add(node_id)

        for unit in changeset.units:
            path = _normalize_path(unit.path)
            deleted = unit.is_deleted or unit.status == ChangeStatus.DELETED
            file_node = self._files_by_path.get(path)

            if file_node is not None:
                add(file_node.id)
                for symbol in self._symbols_by_path.get(path, []):
                    add(symbol.id)
                    if deleted:
                        self._deleted_node_ids.add(symbol.id)
                if deleted:
                    self._deleted_node_ids.add(file_node.id)

            if unit.is_renamed and unit.previous_path:
                previous = _normalize_path(unit.previous_path)
                previous_file = self._files_by_path.get(previous)
                if previous_file is not None:
                    add(previous_file.id)
                    for symbol in self._symbols_by_path.get(previous, []):
                        add(symbol.id)

        return sorted(ordered)

    def _resolve_symbol_ref(self, reference: str) -> Optional[str]:
        """Resolve a symbol ID, ``path::qualified_name``, or bare qualified name."""
        if reference in self._symbols_by_id:
            return reference
        if reference in self._files_by_path:
            return None  # a file reference is not a symbol reference

        if "::" in reference:
            raw_path, _, name = reference.partition("::")
            path = _normalize_path(raw_path)
            candidates = [
                symbol
                for symbol in self._symbols_by_path.get(path, [])
                if symbol.qualified_name == name or symbol.name == name
            ]
        else:
            candidates = sorted(
                (
                    symbol
                    for symbol in self.graph.symbols
                    if symbol.qualified_name == reference or symbol.name == reference
                ),
                key=lambda symbol: (symbol.relative_path, symbol.qualified_name, symbol.id),
            )

        if len(candidates) == 1:
            return candidates[0].id
        return None

    # ------------------------------------------------------------------
    # Phase 2: propagation
    # ------------------------------------------------------------------

    def _collect_impacts(self, changed_id: str) -> List[EdgeImpact]:
        """Bounded reverse BFS over incoming dependency edges.

        Only **resolved** edges are expanded: an unresolved or ambiguous edge
        is reported in its own bucket but never used to reach further nodes,
        because that would present an unconfirmed link as transitive evidence.
        Every shortest path is recorded; each node is expanded at most once,
        so dense graphs stay bounded and cycles terminate.
        """
        dependency_kinds = self._config["dependency_kinds"]
        max_depth = self._config["max_depth"]
        exclusions = self._config["exclusions"]

        if not dependency_kinds or max_depth == 0:
            return []

        impacts: List[EdgeImpact] = []
        frontier: List[Tuple[str, Tuple[str, ...]]] = [(changed_id, (changed_id,))]
        confirmed_depth: Dict[str, int] = {changed_id: 0}
        expanded: Set[str] = {changed_id}

        while frontier:
            next_frontier: List[Tuple[str, Tuple[str, ...]]] = []
            for current, path in frontier:
                depth = len(path) - 1
                if max_depth != -1 and depth >= max_depth:
                    continue
                for edge in self._incoming.get(current, []):
                    if edge.kind not in dependency_kinds:
                        continue
                    impacted = edge.source_id
                    if impacted not in self._known_ids:
                        if edge.id:
                            self._dangling_edge_ids.add(edge.id)
                        continue
                    if impacted in exclusions:
                        continue
                    if impacted in path:
                        # Cycle: never re-enter a node already on this path.
                        continue

                    new_depth = depth + 1
                    resolved = edge.resolution == ResolutionKind.RESOLVED

                    if resolved:
                        previous = confirmed_depth.get(impacted)
                        if previous is not None and previous < new_depth:
                            continue
                        if previous is None:
                            confirmed_depth[impacted] = new_depth

                    classification = (
                        ImpactClassification.DIRECT
                        if new_depth == 1
                        else ImpactClassification.TRANSITIVE
                    )
                    impacts.append(
                        EdgeImpact(
                            edge=edge,
                            depth=new_depth,
                            classification=classification,
                            path=path + (impacted,),
                        )
                    )

                    if resolved and impacted not in expanded:
                        expanded.add(impacted)
                        next_frontier.append((impacted, path + (impacted,)))
            frontier = next_frontier

        return impacts

    @staticmethod
    def _dedupe(impacts: Sequence[EdgeImpact]) -> List[EdgeImpact]:
        """Remove identical (edge, path) pairs and order deterministically."""
        unique: Dict[Tuple[str, Tuple[str, ...]], EdgeImpact] = {}
        for impact in impacts:
            key = (impact.edge.id or "", impact.path)
            if key not in unique:
                unique[key] = impact
        return sorted(
            unique.values(),
            key=lambda impact: (
                impact.depth,
                impact.edge.id or "",
                impact.path,
            ),
        )

    # ------------------------------------------------------------------
    # Phase 3: reporting
    # ------------------------------------------------------------------

    def _node_record(self, node_id: str) -> Dict[str, Any]:
        node = self._nodes_by_id.get(node_id)
        if node is None:
            return {"id": node_id, "name": None, "kind": "unknown", "repository_id": None}
        name = getattr(node, "name", None) or getattr(node, "relative_path", None)
        return {
            "id": node_id,
            "name": name,
            "kind": self._node_kind(node),
            "repository_id": node.repository_id,
            "relative_path": getattr(node, "relative_path", None),
        }

    @staticmethod
    def _node_kind(node: Any) -> str:
        kind = getattr(node, "node_kind", None)
        if isinstance(kind, NodeKind):
            return kind.value
        return str(kind)

    def _mapping_confidence(self, node_id: str) -> str:
        """Mapping confidence for a node, without inventing certainty.

        Symbol nodes use ``MappingResult.symbol_confidence`` verbatim.  File
        nodes are ``exact`` only when the mapping recorded symbols for them,
        otherwise ``file_level_only``.  Unrecorded symbols are ``approximate``
        because their file mapped but no per-symbol evidence was stored.
        """
        recorded = self.mapping_result.symbol_confidence.get(node_id)
        if recorded is not None:
            return str(_enum_value(recorded))

        file_node = self._files_by_id.get(node_id)
        if file_node is not None:
            mapped = self.mapping_result.file_to_symbols.get(file_node.relative_path)
            if mapped is None:
                mapped = self.mapping_result.file_to_symbols.get(node_id)
            return (
                MappingConfidence.EXACT.value
                if mapped
                else MappingConfidence.FILE_LEVEL_ONLY.value
            )

        return MappingConfidence.APPROXIMATE.value

    def _impact_record(
        self, impact: EdgeImpact, classification: ImpactClassification
    ) -> Dict[str, Any]:
        edge = impact.edge
        node_id = impact.path[-1]
        record = {
            "node_id": node_id,
            "classification": classification.value,
            "depth": impact.depth,
            "edge_id": edge.id,
            "edge_kind": edge.kind.value,
            "edge_source_id": edge.source_id,
            "edge_target_id": edge.target_id,
            "resolution": edge.resolution.value,
            "evidence_confidence": _enum_value(edge.confidence),
            "mapping_confidence": self._mapping_confidence(node_id),
            "path": list(impact.path),
            "provenance": {
                "provenance_kind": edge.provenance_kind.value,
                "discovery_method": edge.discovery_method.value,
                "source_file": edge.source_file,
                "start_line": edge.start_line,
                "end_line": edge.end_line,
                "start_column": edge.start_column,
                "end_column": edge.end_column,
                "source_hash": edge.source_hash,
            },
        }
        record.update(
            {
                f"node_{key}": value
                for key, value in self._node_record(node_id).items()
                if key != "id"
            }
        )
        return record

    def _classify_by_type(
        self,
        impacts: Iterable[EdgeImpact],
        classification: ImpactClassification,
    ) -> List[Dict[str, Any]]:
        return [self._impact_record(impact, classification) for impact in impacts]

    def _resolve_unmapped(self, changeset: Changeset) -> List[Dict[str, Any]]:
        """Report changeset units that are not confirmed graph nodes.

        ``reason`` is one of ``deleted`` (deleted file, reported for
        visibility even when the graph still contains it), ``not_in_graph``
        (no file node for the path) or ``previous_path_not_in_graph`` (rename
        source unknown).
        """
        records: List[Dict[str, Any]] = []
        for unit in changeset.units:
            path = _normalize_path(unit.path)
            status = str(_enum_value(unit.status))
            file_node = self._files_by_path.get(path)
            deleted = unit.is_deleted or unit.status == ChangeStatus.DELETED

            if deleted:
                records.append(
                    {
                        "path": path,
                        "status": status,
                        "reason": "deleted",
                        "mapped": file_node is not None,
                    }
                )
                continue

            if file_node is None:
                records.append(
                    {
                        "path": path,
                        "status": status,
                        "reason": "not_in_graph",
                        "mapped": False,
                    }
                )
                continue

            if unit.is_renamed and unit.previous_path:
                previous = _normalize_path(unit.previous_path)
                if previous not in self._files_by_path:
                    records.append(
                        {
                            "path": path,
                            "status": status,
                            "reason": "previous_path_not_in_graph",
                            "mapped": True,
                            "previous_path": previous,
                        }
                    )

        records.sort(key=lambda record: (record["path"], record["reason"]))
        return records

    def _build_warnings(
        self,
        *,
        changeset: Changeset,
        changed_node_ids: Sequence[str],
        unresolved: Sequence[EdgeImpact],
        ambiguous: Sequence[EdgeImpact],
        unmapped: Sequence[Dict[str, Any]],
        truncated: bool,
    ) -> List[str]:
        warnings: List[str] = []

        if not changeset.units and not self._config["changed_symbols"]:
            warnings.append("Changeset is empty; no impact analysis was performed.")

        if self._unresolved_symbol_refs:
            warnings.append(
                f"{len(self._unresolved_symbol_refs)} changed symbol reference(s) "
                "could not be resolved to exactly one graph symbol and were "
                "ignored: " + ", ".join(sorted(self._unresolved_symbol_refs))
            )

        not_in_graph = sorted(
            record["path"]
            for record in unmapped
            if record["reason"] == "not_in_graph"
        )
        if not_in_graph:
            warnings.append(
                f"{len(not_in_graph)} changed file(s) are not present in the graph "
                "and were not analysed: " + ", ".join(not_in_graph)
            )

        deleted = sorted(
            record["path"] for record in unmapped if record["reason"] == "deleted"
        )
        if deleted:
            warnings.append(
                f"{len(deleted)} deleted file(s) are reported for visibility but "
                "were not traversed: " + ", ".join(deleted)
            )

        previous_missing = sorted(
            record["path"]
            for record in unmapped
            if record["reason"] == "previous_path_not_in_graph"
        )
        if previous_missing:
            warnings.append(
                f"{len(previous_missing)} renamed file(s) have a previous path "
                "that is not in the graph: " + ", ".join(previous_missing)
            )

        if not self.mapping_result.fully_mapped:
            warnings.append(
                "File-to-symbol mapping is not exhaustive "
                "(MappingResult.fully_mapped=False); symbol-level impacts may be "
                "under-reported."
            )

        if unresolved:
            warnings.append(
                f"{len(unresolved)} unresolved relationship edge(s) were excluded "
                "from confirmed impacts."
            )
        if ambiguous:
            warnings.append(
                f"{len(ambiguous)} ambiguous relationship edge(s) were excluded "
                "from confirmed impacts."
            )
        if self._dangling_edge_ids:
            warnings.append(
                f"{len(self._dangling_edge_ids)} dependency edge(s) referenced "
                "nodes absent from the graph and were skipped."
            )
        if truncated:
            warnings.append(
                "Impact records were truncated at max_results="
                f"{self._config['max_results']}; the shallowest impacts were kept."
            )
        if changed_node_ids and not any(
            node_id not in self._deleted_node_ids for node_id in changed_node_ids
        ):
            warnings.append(
                "All changed nodes are deleted; impact propagation was skipped."
            )

        return warnings

    def _build_summary(
        self,
        *,
        changed: int,
        impacted: int,
        direct: int,
        transitive: int,
        unresolved: int,
        ambiguous: int,
        unmapped: int,
        truncated: bool,
    ) -> Dict[str, Any]:
        return {
            "changed_node_count": changed,
            "impacted_node_count": impacted,
            "direct_impact_count": direct,
            "transitive_impact_count": transitive,
            "unresolved_impact_count": unresolved,
            "ambiguous_impact_count": ambiguous,
            "unmapped_change_count": unmapped,
            "confirmed_impact_count": direct + transitive,
            "truncated": truncated,
            "mapping_fully_mapped": self.mapping_result.fully_mapped,
            "navigation_only": True,
            "runtime_impact_verified": False,
        }

    def _metadata(
        self,
        *,
        changeset: Changeset,
        changed_node_count: int,
        discovered: int,
        returned: int,
        truncated: bool,
    ) -> Dict[str, Any]:
        return {
            "direction": "incoming",
            "dependency_edge_kinds": sorted(
                kind.value for kind in self._config["dependency_kinds"]
            ),
            "max_depth": self._config["max_depth"],
            "max_results": self._config["max_results"],
            "kinds": (
                None
                if self._config["kinds"] is None
                else sorted(
                    _enum_value(kind) for kind in self._config["kinds"]
                )
            ),
            "exclusions": sorted(self._config["exclusions"]),
            "changed_symbols": list(self._config["changed_symbols"]),
            "total_changed_files": len(changeset.units),
            "total_changed_nodes": changed_node_count,
            "total_impacts_discovered": discovered,
            "total_impacts_returned": returned,
            "truncated": truncated,
        }

    def _empty_result(
        self,
        changeset: Changeset,
        changed_node_ids: Sequence[str],
        unmapped: Sequence[Dict[str, Any]],
    ) -> ImpactAnalysisResult:
        warnings = self._build_warnings(
            changeset=changeset,
            changed_node_ids=changed_node_ids,
            unresolved=[],
            ambiguous=[],
            unmapped=unmapped,
            truncated=False,
        )
        return ImpactAnalysisResult(
            changed_nodes={},
            impacted_nodes={},
            direct_impacts=[],
            transitive_impacts=[],
            unresolved_impacts=[],
            ambiguous_impacts=[],
            unmapped_changes=list(unmapped),
            dependency_paths={},
            traversal_metadata=self._metadata(
                changeset=changeset,
                changed_node_count=0,
                discovered=0,
                returned=0,
                truncated=False,
            ),
            analysis_warnings=warnings,
            analysis_summary=self._build_summary(
                changed=0,
                impacted=0,
                direct=0,
                transitive=0,
                unresolved=0,
                ambiguous=0,
                unmapped=len(unmapped),
                truncated=False,
            ),
        )


def create_impact_analysis_service(
    graph: CodeGraph,
    mapping_result: MappingResult,
    traversal_config: Optional[Dict[str, Any]] = None,
) -> ImpactAnalysisService:
    """Factory function to create an ImpactAnalysisService instance."""
    return ImpactAnalysisService(graph, mapping_result, traversal_config)
