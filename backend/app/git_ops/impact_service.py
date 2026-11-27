"""Unified, read-only impact-analysis workflow (Phase 3F).

This module is the single orchestration point for KiteHut's impact analysis.
It owns sequencing and reporting; it owns no analysis algorithm.  Every step
delegates to the component built in an earlier phase:

===========  ==========================================================
Step         Delegate
===========  ==========================================================
1. detect    :class:`app.git_ops.change_detection.ChangeDetector`
2. graph     :class:`app.code_graph.builder.CodeGraphBuilder` or
             :func:`app.knowledge.store.load_graph`
3. map       :class:`app.git_ops.mapping_service.FileMapper`
4. symbols   :mod:`app.git_ops.diff_hunks` (pure hunk parsing)
5. propagate :func:`app.git_ops.impact_analysis.create_impact_analysis_service`
6. rank      :func:`app.git_ops.ranking.rank_impacts`
7. report    :class:`ImpactReport` (deterministic serialisation)
===========  ==========================================================

Static impact is not runtime verification
-----------------------------------------
Every report states, at the root and in each ranked record, that this is
navigation evidence only: ``navigation_only: true``,
``static_impact_only: true``, ``runtime_failure_verified: false``.  A dependency
path means "this component could be affected", never "this component fails".

Determinism
-----------
The report contains no timestamps, no absolute paths and no set iteration
order: :meth:`ImpactReport.to_json` sorts keys and every list is built from a
sorted source, so identical inputs and identical graph state produce
byte-identical JSON.  This is why ``Changeset.to_dict()`` (which stamps
``generated_at``) is never embedded; :func:`_changeset_summary` recomputes the
same information from the units.

Scope
-----
``ChangeDetector`` resolves the Git root, which may be an ancestor of the
path the caller asked about.  Changes under the requested subdirectory are
analysed; the rest are reported in ``changes_outside_scope`` rather than
silently discarded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "SCHEMA_VERSION",
    "ImpactWorkflowError",
    "ImpactWorkflowOptions",
    "ImpactReport",
    "ImpactWorkflow",
    "create_impact_workflow",
    "run_impact_analysis",
]

from app.code_graph.builder import CodeGraphBuilder
from app.code_graph.discovery import DEFAULT_MAX_FILES, RepositoryScanner
from app.code_graph.models import (
    CodeGraph,
    EdgeKind,
    SymbolNode,
    normalize_repository_path,
)
from app.git_ops.change_detection import (
    DETECTION_MODES,
    ChangeDetectionError,
    ChangeDetector,
)
from app.git_ops.diff_hunks import (
    candidate_diffs,
    find_file_diff,
    match_symbols,
    parse_unified_diff,
)
from app.git_ops.impact_analysis import (
    DEPENDENCY_EDGE_KINDS,
    ImpactAnalysisResult,
    create_impact_analysis_service,
)
from app.git_ops.mapping_service import FileMapper, MappingResult
from app.git_ops.models.changeset import ChangeStatus, ChangeUnit, Changeset
from app.git_ops.ranking import (
    CALIBRATION_NOTE,
    FACTOR_WEIGHTS,
    RANKING_POLICY_VERSION,
    TIER_BANDS,
    ImpactRankingResult,
    rank_impacts,
)

#: Report contract version.  Bumped only for a breaking shape change.
SCHEMA_VERSION = 1

#: Precision labels for a file's hunk-to-symbol resolution.
PRECISION_SYMBOL_LEVEL = "symbol_level"
PRECISION_FILE_LEVEL = "file_level_only"

#: Where a changed symbol came from.
RESOLUTION_HUNK = "hunk"
RESOLUTION_EXPLICIT = "explicit"
RESOLUTION_ADDED_FILE = "file_expansion"

_MODE_WORKING_TREE = "working_tree"
_MODE_STAGED = "staged"
_MODE_BASE = "base"
_GRAPH_BUILD = "build"
_GRAPH_CACHED = "cached"
_GRAPH_SOURCES = (_GRAPH_BUILD, _GRAPH_CACHED)

#: How many build diagnostics / warning lines a report inlines before summarizing.
_MAX_INLINE_DIAGNOSTICS = 10


class ImpactWorkflowError(Exception):
    """Raised when the workflow cannot produce an impact report."""


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactWorkflowOptions:
    """Validated options for :class:`ImpactWorkflow`.

    ``mode`` selects the change-detection source.  Supplying ``base_revision``
    forces ``mode="base"`` and therefore **excludes working-tree changes**:
    a base comparison reports only what is committed between the revision and
    ``HEAD``.  That behaviour is reported in ``report.workflow`` so a caller is
    never surprised by the absence of dirty files.
    """

    mode: str = _MODE_WORKING_TREE
    base_revision: Optional[str] = None
    staged_only: bool = False
    include_untracked: bool = True
    max_depth: int = -1
    max_results: int = 1000
    max_ranked: int = 25
    max_unconfirmed: Optional[int] = None
    kinds: Optional[Tuple[EdgeKind, ...]] = None
    exclusions: Tuple[str, ...] = ()
    changed_symbols: Tuple[str, ...] = ()
    graph_source: str = _GRAPH_BUILD
    max_files: int = DEFAULT_MAX_FILES
    exclude_dirs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "mode", self._validate_mode(self.mode, self.base_revision)
        )
        if self.base_revision is not None:
            base = self.base_revision.strip()
            if not base:
                raise ImpactWorkflowError(
                    "base_revision was given but is empty; omit it or name a revision."
                )
            object.__setattr__(self, "base_revision", base)
        if self.staged_only and self.mode == _MODE_BASE:
            raise ImpactWorkflowError(
                "staged_only cannot be combined with a base revision: a base "
                "comparison only reports committed changes."
            )
        if self.graph_source not in _GRAPH_SOURCES:
            raise ImpactWorkflowError(
                f"Unknown graph_source {self.graph_source!r}; expected "
                f"{' or '.join(_GRAPH_SOURCES)}."
            )
        self._require_int("max_depth", self.max_depth, minimum=-1)
        self._require_int("max_results", self.max_results, minimum=1)
        self._require_int("max_ranked", self.max_ranked, minimum=1)
        self._require_int("max_files", self.max_files, minimum=1)
        if self.max_unconfirmed is not None:
            self._require_int("max_unconfirmed", self.max_unconfirmed, minimum=0)
        object.__setattr__(self, "kinds", self._validate_kinds(self.kinds))
        object.__setattr__(
            self, "exclusions", self._clean_strings("exclusions", self.exclusions)
        )
        object.__setattr__(
            self,
            "changed_symbols",
            self._clean_strings("changed_symbols", self.changed_symbols),
        )
        object.__setattr__(
            self,
            "exclude_dirs",
            self._clean_strings("exclude_dirs", self.exclude_dirs),
        )

    @staticmethod
    def _validate_mode(mode: Any, base_revision: Optional[str]) -> str:
        if not isinstance(mode, str):
            raise ImpactWorkflowError("mode must be a string.")
        normalized = mode.strip().lower()
        if normalized not in DETECTION_MODES:
            raise ImpactWorkflowError(
                f"Unknown mode {mode!r}; expected one of {', '.join(DETECTION_MODES)}."
            )
        if base_revision:
            if normalized == _MODE_BASE:
                return _MODE_BASE
            if normalized == _MODE_STAGED:
                raise ImpactWorkflowError(
                    "staged_only cannot be combined with a base revision."
                )
            # Documented behaviour: naming a base revision disables the
            # working tree, it does not error.
            return _MODE_BASE
        if normalized == _MODE_BASE:
            raise ImpactWorkflowError(
                "mode='base' requires base_revision to be set."
            )
        return normalized

    @staticmethod
    def _require_int(name: str, value: Any, *, minimum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ImpactWorkflowError(f"{name} must be an integer; got {value!r}.")
        if value < minimum:
            raise ImpactWorkflowError(
                f"{name} must be >= {minimum}; got {value}."
            )

    @staticmethod
    def _validate_kinds(
        kinds: Optional[Iterable[Any]],
    ) -> Optional[Tuple[EdgeKind, ...]]:
        if kinds is None:
            return None
        if isinstance(kinds, (str, EdgeKind)):
            kinds = [kinds]
        if isinstance(kinds, (set, frozenset)):
            kinds = sorted(kinds, key=str)
        coerced: List[EdgeKind] = []
        for kind in kinds:
            try:
                edge_kind = kind if isinstance(kind, EdgeKind) else EdgeKind(str(kind))
            except (TypeError, ValueError) as error:
                raise ImpactWorkflowError(
                    f"Unknown edge kind {kind!r}; expected one of "
                    f"{', '.join(sorted(k.value for k in DEPENDENCY_EDGE_KINDS))}."
                ) from error
            if edge_kind not in DEPENDENCY_EDGE_KINDS:
                raise ImpactWorkflowError(
                    f"Edge kind {edge_kind.value!r} never propagates impact; only "
                    f"{', '.join(sorted(k.value for k in DEPENDENCY_EDGE_KINDS))} "
                    "may be requested."
                )
            coerced.append(edge_kind)
        return tuple(sorted(set(coerced), key=lambda kind: kind.value))

    @staticmethod
    def _clean_strings(name: str, values: Any) -> Tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (set, frozenset)):
            values = sorted(values, key=str)
        try:
            cleaned = [str(value).strip() for value in values]
        except TypeError as error:
            raise ImpactWorkflowError(
                f"{name} must be an iterable of strings; got {values!r}."
            ) from error
        return tuple(value for value in cleaned if value)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively convert to JSON-native types with deterministic sets."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_jsonable(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass
class ImpactReport:
    """Structured Phase 3F report.

    The 13 required sections are carried as fields; :meth:`to_dict` assembles
    them in a fixed order and adds the three static-only flags.
    """

    policy: Dict[str, Any]
    mode: Dict[str, Any]
    workflow: Dict[str, Any]
    repository: Dict[str, Any]
    graph: Dict[str, Any]
    change_detection: Dict[str, Any]
    changeset_summary: Dict[str, Any]
    changed_files: List[Dict[str, Any]]
    changes_outside_scope: List[Dict[str, Any]]
    changed_symbols: List[Dict[str, Any]]
    symbol_mapping: Dict[str, Any]
    mapped_changes: List[Dict[str, Any]]
    unmapped_changes: List[Dict[str, Any]]
    direct_impacts: List[Dict[str, Any]]
    transitive_impacts: List[Dict[str, Any]]
    dependency_paths: Dict[str, Any]
    ranked_impacts: List[Dict[str, Any]]
    unconfirmed_impacts: List[Dict[str, Any]]
    evidence: Dict[str, Any]
    ranking: Dict[str, Any]
    analysis_warnings: List[str]
    truncation: Dict[str, Any]
    summary: Dict[str, Any]
    limitations: List[str]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(
            {
                "schema_version": self.schema_version,
                "policy": self.policy,
                "mode": self.mode,
                "workflow": self.workflow,
                "repository": self.repository,
                "graph": self.graph,
                "change_detection": self.change_detection,
                "changeset_summary": self.changeset_summary,
                "changed_files": self.changed_files,
                "changes_outside_scope": self.changes_outside_scope,
                "changed_symbols": self.changed_symbols,
                "symbol_mapping": self.symbol_mapping,
                "mapped_changes": self.mapped_changes,
                "unmapped_changes": self.unmapped_changes,
                "direct_impacts": self.direct_impacts,
                "transitive_impacts": self.transitive_impacts,
                "dependency_paths": self.dependency_paths,
                "ranked_impacts": self.ranked_impacts,
                "unconfirmed_impacts": self.unconfirmed_impacts,
                "evidence": self.evidence,
                "ranking": self.ranking,
                "analysis_warnings": self.analysis_warnings,
                "truncation": self.truncation,
                "summary": self.summary,
                "limitations": self.limitations,
                "navigation_only": True,
                "static_impact_only": True,
                "runtime_failure_verified": False,
            }
        )

    def to_json(self) -> str:
        """Deterministic JSON: sorted keys, no timestamps, no absolute paths."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    """Repository-relative normalisation, tolerant of Git's quirks."""
    try:
        return normalize_repository_path(path)
    except ValueError:
        return "/".join(
            part for part in str(path).replace("\\", "/").split("/") if part not in ("", ".")
        )


def _dedupe(values: Iterable[str]) -> List[str]:
    """Order-preserving de-duplication (warning lists keep their narrative order)."""
    seen: set[str] = set()
    ordered: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _unit_record(unit: ChangeUnit) -> Dict[str, Any]:
    return {
        "path": _norm(unit.path),
        "previous_path": _norm(unit.previous_path) if unit.previous_path else None,
        "status": _enum_value(unit.status),
        "is_staged": unit.is_staged,
        "is_unstaged": unit.is_unstaged,
        "is_untracked": unit.is_untracked,
        "is_deleted": unit.is_deleted,
        "is_renamed": unit.is_renamed,
        "change_source": _enum_value(unit.change_source),
    }


def _changeset_summary(units: Sequence[ChangeUnit]) -> Dict[str, Any]:
    """Changeset statistics without ``Changeset.to_dict()``'s timestamp."""
    by_status = {status.value: 0 for status in ChangeStatus}
    for unit in units:
        status = str(_enum_value(unit.status))
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "unit_count": len(units),
        "by_status": by_status,
        "staging": {
            "staged": sum(1 for unit in units if unit.is_staged),
            "unstaged": sum(1 for unit in units if unit.is_unstaged),
            "untracked": sum(1 for unit in units if unit.is_untracked),
        },
        "has_changes": bool(units),
    }


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class ImpactWorkflow:
    """Runs the seven-stage impact workflow for one repository checkout."""

    def __init__(
        self,
        repository_path: str | Path = ".",
        options: Optional[ImpactWorkflowOptions] = None,
    ) -> None:
        self.requested_path = Path(repository_path).expanduser().resolve()
        self.options = options or ImpactWorkflowOptions()

    # -- public entry point ---------------------------------------------

    def run(self) -> ImpactReport:
        """Execute every stage and return the assembled report.

        Raises :class:`ImpactWorkflowError` for anything that prevents a
        truthful report: not a Git repository, an unknown base revision, a
        failed graph build, a missing cached graph, or a rejected traversal
        configuration.
        """
        self._require_requested_path()

        # 1. Detect repository changes.
        detector = self._create_detector()
        scope = self._resolve_scope(detector.root)
        (
            changeset,
            outside_scope,
            detection_warnings,
            detection_errors,
            detection_degraded,
        ) = self._detect(detector, scope)

        # 2. Build or load the code graph.
        graph, graph_info, graph_warnings = self._load_graph(detector.root)

        # 3. Map changed files to graph nodes.
        mapping, symbols_by_path, files_by_path = self._build_mapping(graph)

        # 4. Identify changed symbols from diff hunks.
        (
            changed_symbols,
            symbol_refs,
            symbol_mapping,
            symbol_warnings,
        ) = self._resolve_changed_symbols(
            changeset, graph, symbols_by_path, files_by_path
        )

        # 5. Run impact propagation.
        impact = self._propagate(graph, mapping, changeset, symbol_refs)

        # 6. Rank impacts.
        ranking = self._rank(impact)

        # 7. Produce the final structured report.
        return self._build_report(
            scope=scope,
            detection_warnings=detection_warnings,
            detection_errors=detection_errors,
            detection_degraded=detection_degraded,
            changeset=changeset,
            changes_outside_scope=outside_scope,
            graph=graph,
            graph_info=graph_info,
            mapping=mapping,
            files_by_path=files_by_path,
            changed_symbols=changed_symbols,
            symbol_mapping=symbol_mapping,
            impact=impact,
            ranking=ranking,
            warnings=_dedupe(
                [
                    *detection_warnings,
                    *graph_warnings,
                    *symbol_warnings,
                    *impact.analysis_warnings,
                ]
            ),
        )

    # -- stage 1: detection ---------------------------------------------

    def _require_requested_path(self) -> None:
        if not self.requested_path.exists():
            raise ImpactWorkflowError(
                f"Path does not exist: {self.requested_path}"
            )
        if not self.requested_path.is_dir():
            raise ImpactWorkflowError(
                f"Path is not a directory: {self.requested_path}"
            )

    def _create_detector(self) -> ChangeDetector:
        try:
            return ChangeDetector(str(self.requested_path))
        except ChangeDetectionError as error:
            raise ImpactWorkflowError(str(error)) from error

    def _detect(
        self, detector: ChangeDetector, scope: Optional[str]
    ) -> Tuple[
        Changeset,
        List[Dict[str, Any]],
        List[str],
        List[str],
        bool,
    ]:
        """Detect changes, split them by scope, and surface partial failures.

        Returns ``(changeset, outside_scope, warnings, errors, degraded)``.
        ``degraded`` is true when detection produced units but at least one
        file's diff could not be read: the report is useful but incomplete,
        so it must not claim complete detection.
        """
        options = self.options
        try:
            detected = detector.detect(
                mode=options.mode,
                base_revision=options.base_revision,
                staged_only=options.staged_only,
                include_untracked=options.include_untracked,
            )
        except ChangeDetectionError as error:
            raise ImpactWorkflowError(str(error)) from error

        in_scope: List[ChangeUnit] = []
        outside: List[ChangeUnit] = []
        for unit in detected.units:
            path = _norm(unit.path)
            if scope is None or path == scope or path.startswith(scope + "/"):
                in_scope.append(unit)
            else:
                outside.append(unit)

        changeset = Changeset(
            repository_root=detected.repository_root,
            units=sorted(in_scope, key=lambda unit: (unit.path, unit.previous_path or "")),
        )
        outside_records = [
            _unit_record(unit)
            for unit in sorted(outside, key=lambda unit: (unit.path, unit.previous_path or ""))
        ]
        errors = list(detector.errors)
        return (
            changeset,
            outside_records,
            list(detector.warnings),
            errors,
            bool(errors),
        )

    def _resolve_scope(self, root: Path) -> Optional[str]:
        """Subdirectory scope relative to the Git root (``None`` = whole repo)."""
        requested = self.requested_path
        try:
            root = root.resolve()
        except OSError:
            pass
        if requested == root:
            return None
        try:
            relative = requested.relative_to(root)
        except ValueError as error:
            raise ImpactWorkflowError(
                f"{requested} is not inside the repository root {root}."
            ) from error
        text = relative.as_posix()
        if text in ("", "."):
            return None
        return _norm(text)

    # -- stage 2: graph -------------------------------------------------

    def _load_graph(
        self, root: Path
    ) -> Tuple[CodeGraph, Dict[str, Any], List[str]]:
        options = self.options
        if options.graph_source == _GRAPH_CACHED:
            return self._load_cached_graph(root)

        scanner = RepositoryScanner(
            root,
            exclude_dirs=options.exclude_dirs,
            max_files=options.max_files,
        )
        try:
            result = CodeGraphBuilder(root, scanner=scanner).build()
        except Exception as error:  # builder is not expected to raise, be truthful
            raise ImpactWorkflowError(
                f"Code graph build failed unexpectedly: {error}"
            ) from error
        if result.graph is None:
            detail = "; ".join(
                f"{_enum_value(diagnostic.code)}: {diagnostic.message}"
                for diagnostic in result.errors[:3]
            )
            raise ImpactWorkflowError(
                "Code graph build produced no graph"
                + (f" ({detail})" if detail else ".")
            )

        diagnostics = sorted(
            [*result.errors, *result.warnings],
            key=lambda diagnostic: (
                diagnostic.relative_path or "",
                diagnostic.line or 0,
                diagnostic.code,
                diagnostic.message,
            ),
        )
        warnings = [
            f"Graph build {_enum_value(diagnostic.severity)} "
            f"({diagnostic.code}): {diagnostic.message}"
            for diagnostic in diagnostics[:_MAX_INLINE_DIAGNOSTICS]
        ]
        if len(diagnostics) > _MAX_INLINE_DIAGNOSTICS:
            warnings.append(
                f"Graph build reported {len(diagnostics)} diagnostics; only the "
                f"first {_MAX_INLINE_DIAGNOSTICS} are listed."
            )
        info = {
            "source": _GRAPH_BUILD,
            "fresh": True,
            "cache_path": None,
            "build_status": _enum_value(result.status),
            "truncated": result.truncated,
            "file_count": len(result.graph.files),
            "symbol_count": len(result.graph.symbols),
            "edge_count": len(result.graph.edges),
            "diagnostic_count": len(diagnostics),
        }
        return result.graph, info, warnings

    def _load_cached_graph(
        self, root: Path
    ) -> Tuple[CodeGraph, Dict[str, Any], List[str]]:
        # Imported lazily: the knowledge store pulls in serialization helpers
        # that the impact path only needs in cached mode.
        from app.knowledge.store import KnowledgeNotFoundError, load_graph

        cache_path = ".kitehut/graph/repository.json"
        try:
            graph = load_graph(root)
        except KnowledgeNotFoundError as error:
            raise ImpactWorkflowError(str(error)) from error
        info = {
            "source": _GRAPH_CACHED,
            "fresh": None,
            "cache_path": cache_path,
            "build_status": None,
            "truncated": False,
            "file_count": len(graph.files),
            "symbol_count": len(graph.symbols),
            "edge_count": len(graph.edges),
            "diagnostic_count": 0,
        }
        warnings = [
            "The code graph was loaded from "
            f"{cache_path} without verifying it against the working tree; "
            "impacts reflect the graph at generation time. Use the default "
            "fresh build (graph_source='build') for change-accurate results."
        ]
        return graph, info, warnings

    # -- stage 3: mapping -----------------------------------------------

    def _build_mapping(
        self, graph: CodeGraph
    ) -> Tuple[MappingResult, Dict[str, List[SymbolNode]], Dict[str, Any]]:
        symbols_by_path: Dict[str, List[SymbolNode]] = {}
        for symbol in graph.symbols:
            symbols_by_path.setdefault(symbol.relative_path, []).append(symbol)
        for symbols in symbols_by_path.values():
            symbols.sort(
                key=lambda symbol: (
                    symbol.start_line,
                    symbol.end_line,
                    symbol.qualified_name,
                    symbol.id or "",
                )
            )

        mapper = FileMapper()
        for file_node in sorted(graph.files, key=lambda node: node.relative_path):
            symbols = symbols_by_path.get(file_node.relative_path, [])
            symbol_map = self._symbol_map(symbols)
            mapper.register_file(file_node, symbol_map or None)

        mapping = mapper.get_mapping_result()
        # Every graph file was registered above, so the mapping is exhaustive
        # with respect to the graph.  Symbol coverage per file is reported
        # separately (``symbol_mapping``) so a file with no declarations is
        # not mistaken for a mapping failure.
        mapping.fully_mapped = True
        files_by_path = {
            file_node.relative_path: file_node for file_node in graph.files
        }
        return mapping, symbols_by_path, files_by_path

    @staticmethod
    def _symbol_map(symbols: Sequence[SymbolNode]) -> Dict[str, str]:
        """``{qualified_name: symbol_id}``, disambiguated if names collide."""
        symbol_map: Dict[str, str] = {}
        for symbol in symbols:
            key = symbol.qualified_name
            if key in symbol_map:
                key = f"{key}#{symbol.occurrence_ordinal}"
            suffix = 2
            while key in symbol_map:
                key = f"{symbol.qualified_name}#{symbol.occurrence_ordinal}-{suffix}"
                suffix += 1
            symbol_map[key] = symbol.id
        return symbol_map

    # -- stage 4: changed symbols ---------------------------------------

    def _resolve_changed_symbols(
        self,
        changeset: Changeset,
        graph: CodeGraph,
        symbols_by_path: Dict[str, List[SymbolNode]],
        files_by_path: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any], List[str]]:
        records: Dict[str, Dict[str, Any]] = {}
        refs: set[str] = set()
        warnings: List[str] = []
        files_info: List[Dict[str, Any]] = []

        def add(symbol: SymbolNode, resolution: str) -> None:
            if symbol.id is None:
                return
            path = _norm(symbol.relative_path)
            refs.add(f"{path}::{symbol.qualified_name}")
            records[symbol.id] = {
                "node_id": symbol.id,
                "name": symbol.name,
                "qualified_name": symbol.qualified_name,
                "path": path,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "kind": _enum_value(symbol.kind),
                "resolution": resolution,
                "confidence": PRECISION_SYMBOL_LEVEL,
            }

        # Explicit references are accepted as given and resolved here only so
        # they can be reported with their graph identity.  Phase 3D performs
        # the authoritative resolution during propagation.
        for reference in self.options.changed_symbols:
            symbol = self._lookup_symbol(graph, symbols_by_path, reference)
            if symbol is None:
                warnings.append(
                    "Explicit changed symbol reference could not be resolved to "
                    f"exactly one graph symbol and was left to impact analysis: "
                    f"{reference}"
                )
                continue
            add(symbol, RESOLUTION_EXPLICIT)

        for unit in changeset.units:
            path = _norm(unit.path)
            is_deleted = unit.is_deleted or unit.status == ChangeStatus.DELETED
            if is_deleted:
                files_info.append(
                    {
                        "path": path,
                        "precision": PRECISION_FILE_LEVEL,
                        "reason": "deleted file; deleted symbols are never traversed",
                        "hunk_count": 0,
                        "matched_symbol_count": 0,
                        "notes": [],
                    }
                )
                continue
            if path not in files_by_path:
                files_info.append(
                    {
                        "path": path,
                        "precision": PRECISION_FILE_LEVEL,
                        "reason": "file is not present in the code graph",
                        "hunk_count": 0,
                        "matched_symbol_count": 0,
                        "notes": [],
                    }
                )
                continue

            symbols = symbols_by_path.get(path, [])
            if unit.status in (ChangeStatus.ADDED, ChangeStatus.UNTRACKED):
                if symbols:
                    for symbol in symbols:
                        add(symbol, RESOLUTION_ADDED_FILE)
                    files_info.append(
                        {
                            "path": path,
                            "precision": PRECISION_SYMBOL_LEVEL,
                            "reason": None,
                            "hunk_count": 0,
                            "matched_symbol_count": len(symbols),
                            "notes": [
                                "New file: every extracted symbol is changed."
                            ],
                        }
                    )
                else:
                    files_info.append(
                        {
                            "path": path,
                            "precision": PRECISION_FILE_LEVEL,
                            "reason": "no symbols were extracted for this new file",
                            "hunk_count": 0,
                            "matched_symbol_count": 0,
                            "notes": [],
                        }
                    )
                continue

            matched, precision, reason, hunk_count, notes = self._match_unit_hunks(
                unit, path, symbols
            )
            for symbol in matched:
                add(symbol, RESOLUTION_HUNK)
            if precision == PRECISION_FILE_LEVEL:
                warnings.append(
                    f"{path} is reported at file-level only: {reason}."
                )
            files_info.append(
                {
                    "path": path,
                    "precision": precision,
                    "reason": reason,
                    "hunk_count": hunk_count,
                    "matched_symbol_count": len(matched),
                    "notes": notes,
                }
            )

        ordered_records = sorted(
            records.values(),
            key=lambda record: (
                record["path"],
                record["start_line"],
                record["end_line"],
                record["qualified_name"],
                record["node_id"],
            ),
        )
        files_info.sort(key=lambda info: info["path"])
        symbol_level_files = sum(
            1 for info in files_info if info["precision"] == PRECISION_SYMBOL_LEVEL
        )
        mapping_section = {
            "scope": "reporting_and_evidence",
            "method": (
                "Line-range intersection between the lines a diff added (new-"
                "file coordinates) and symbol start/end lines. Context lines "
                "and whole-file module symbols are excluded. A removal is "
                "attributed only when the deletion point lies unambiguously "
                "inside a surviving symbol; otherwise the file is reported at "
                "file level. This is a lexical range test, not semantic "
                "analysis."
            ),
            "semantic_analysis": False,
            "files": files_info,
            "symbol_level_file_count": symbol_level_files,
            "file_level_only_file_count": len(files_info) - symbol_level_files,
        }
        return ordered_records, sorted(refs), mapping_section, warnings

    def _match_unit_hunks(
        self,
        unit: ChangeUnit,
        path: str,
        symbols: Sequence[SymbolNode],
    ) -> Tuple[List[SymbolNode], str, Optional[str], int, List[str]]:
        """Resolve one unit's hunks to symbols, or explain the fallback."""
        if not unit.diff:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "no diff text was available for this file",
                0,
                [],
            )

        diffs = parse_unified_diff(unit.diff)
        candidates = candidate_diffs(
            diffs, path=path, previous_path=unit.previous_path
        )
        if len(candidates) > 1:
            # Several blocks for one path mean the diff text concatenated
            # hunks from different baselines (index and working tree).  Their
            # line numbers are not comparable, so no symbol attribution is
            # claimed.
            return (
                [],
                PRECISION_FILE_LEVEL,
                (
                    "the diff contained "
                    f"{len(candidates)} blocks for this path with "
                    "different baselines, so hunk line numbers are ambiguous"
                ),
                sum(len(diff.hunks) for diff in candidates),
                [],
            )
        block = find_file_diff(
            diffs, path=path, previous_path=unit.previous_path
        )
        if block is None:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "the diff did not contain a block for this path",
                0,
                [],
            )
        if block.parse_errors:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "the diff could not be parsed: " + "; ".join(block.parse_errors[:2]),
                len(block.hunks),
                [],
            )
        if block.is_binary:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "binary file: line ranges are not meaningful",
                0,
                [],
            )
        if not block.hunks:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "the diff contained no hunks (rename or mode change only)",
                0,
                [],
            )
        if not symbols:
            return (
                [],
                PRECISION_FILE_LEVEL,
                "no symbols were extracted for this file",
                len(block.hunks),
                [],
            )

        match = match_symbols(block.hunks, symbols)
        notes: List[str] = []
        if match.uncertain_hunk_count:
            notes.append(
                f"{match.uncertain_hunk_count} hunk(s) had no parsable content "
                "lines; their header range (which includes context) was used, "
                "so attribution for those hunks is approximate."
            )
        if match.uncovered_ranges:
            notes.append(
                f"{len(match.uncovered_ranges)} hunk(s) changed only lines that "
                "no function or class symbol spans (module-level code); the "
                "file node represents those changes."
            )
        if not match.symbols:
            return (
                [],
                PRECISION_FILE_LEVEL,
                match.reason
                or f"none of {len(block.hunks)} hunk(s) changed a line inside a symbol",
                len(block.hunks),
                notes,
            )
        return (
            list(match.symbols),
            match.precision,
            match.reason,
            len(block.hunks),
            notes,
        )

    @staticmethod
    def _lookup_symbol(
        graph: CodeGraph,
        symbols_by_path: Dict[str, List[SymbolNode]],
        reference: str,
    ) -> Optional[SymbolNode]:
        """Resolve a symbol ID or ``path::qualified_name`` reference uniquely."""
        for symbol in graph.symbols:
            if symbol.id == reference:
                return symbol
        if "::" in reference:
            raw_path, _, name = reference.partition("::")
            path = _norm(raw_path)
            candidates = [
                symbol
                for symbol in symbols_by_path.get(path, [])
                if symbol.qualified_name == name or symbol.name == name
            ]
        else:
            candidates = [
                symbol
                for symbol in graph.symbols
                if symbol.qualified_name == reference or symbol.name == reference
            ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    # -- stage 5: propagation -------------------------------------------

    def _propagate(
        self,
        graph: CodeGraph,
        mapping: MappingResult,
        changeset: Changeset,
        symbol_refs: Sequence[str],
    ) -> ImpactAnalysisResult:
        options = self.options
        config: Dict[str, Any] = {
            "max_depth": options.max_depth,
            "max_results": options.max_results,
            "kinds": list(options.kinds) if options.kinds else None,
            "exclusions": list(options.exclusions),
            "changed_symbols": list(symbol_refs),
        }
        try:
            service = create_impact_analysis_service(graph, mapping, config)
            return service.analyze(changeset)
        except (TypeError, ValueError) as error:
            raise ImpactWorkflowError(
                f"Impact analysis rejected the traversal configuration: {error}"
            ) from error

    # -- stage 6: ranking -----------------------------------------------

    def _rank(self, impact: ImpactAnalysisResult) -> ImpactRankingResult:
        options = self.options
        try:
            return rank_impacts(
                impact,
                max_ranked=options.max_ranked,
                max_unconfirmed=options.max_unconfirmed,
            )
        except (TypeError, ValueError) as error:
            raise ImpactWorkflowError(
                f"Impact ranking rejected the configuration: {error}"
            ) from error

    # -- stage 7: report ------------------------------------------------

    def _build_report(
        self,
        *,
        scope: Optional[str],
        detection_warnings: Sequence[str],
        detection_errors: Sequence[str],
        detection_degraded: bool,
        changeset: Changeset,
        changes_outside_scope: Sequence[Dict[str, Any]],
        graph: CodeGraph,
        graph_info: Dict[str, Any],
        mapping: MappingResult,
        files_by_path: Dict[str, Any],
        changed_symbols: Sequence[Dict[str, Any]],
        symbol_mapping: Dict[str, Any],
        impact: ImpactAnalysisResult,
        ranking: ImpactRankingResult,
        warnings: Sequence[str],
    ) -> ImpactReport:
        options = self.options
        units = list(changeset.units)

        changed_files = [
            _unit_record(unit)
            for unit in sorted(units, key=lambda unit: (unit.path, unit.previous_path or ""))
        ]

        mapped_changes: List[Dict[str, Any]] = []
        for unit in units:
            path = _norm(unit.path)
            file_node = files_by_path.get(path)
            if file_node is None:
                continue
            symbol_ids = sorted(mapping.file_to_symbols.get(path, ()))
            mapped_changes.append(
                {
                    "path": path,
                    "node_id": file_node.id,
                    "symbol_ids": symbol_ids,
                    "symbol_count": len(symbol_ids),
                    "mapping_confidence": (
                        "exact" if symbol_ids else "file_level_only"
                    ),
                }
            )
        mapped_changes.sort(key=lambda record: record["path"])

        deleted_change_count = sum(
            1
            for unit in units
            if unit.is_deleted or unit.status == ChangeStatus.DELETED
        )

        ranked_impacts = [record.to_dict() for record in ranking.ranked_impacts]
        unconfirmed_impacts = [
            record.to_dict() for record in ranking.unconfirmed_impacts
        ]
        top_tier = (
            ranking.ranked_impacts[0].priority_tier.value
            if ranking.ranked_impacts
            else None
        )
        file_level_only_impact_count = sum(
            1
            for record in [*ranked_impacts, *unconfirmed_impacts]
            if str(record.get("original_impact", {}).get("mapping_confidence"))
            == "file_level_only"
        )

        traversal = impact.traversal_metadata
        ranking_metadata = ranking.ranking_metadata
        analysis_truncated = bool(traversal.get("truncated", False))
        ranking_truncated = bool(
            ranking_metadata.get("confirmed_truncated")
            or ranking_metadata.get("unconfirmed_truncated")
        )

        summary = {
            "changed_file_count": len(changed_files),
            "changed_file_count_outside_scope": len(changes_outside_scope),
            "changed_symbol_count": len(changed_symbols),
            "deleted_change_count": deleted_change_count,
            "changed_node_count": impact.analysis_summary.get("changed_node_count", 0),
            "impacted_node_count": impact.analysis_summary.get(
                "impacted_node_count", 0
            ),
            "direct_impact_count": len(impact.direct_impacts),
            "transitive_impact_count": len(impact.transitive_impacts),
            "unresolved_impact_count": len(impact.unresolved_impacts),
            "ambiguous_impact_count": len(impact.ambiguous_impacts),
            "confirmed_impact_count": impact.analysis_summary.get(
                "confirmed_impact_count", 0
            ),
            "unmapped_change_count": len(impact.unmapped_changes),
            "ranked_impact_count": len(ranked_impacts),
            "unconfirmed_impact_count": len(unconfirmed_impacts),
            "file_level_only_impact_count": file_level_only_impact_count,
            "symbol_level_file_count": symbol_mapping["symbol_level_file_count"],
            "file_level_only_file_count": symbol_mapping["file_level_only_file_count"],
            "top_priority_tier": top_tier,
        }

        # D8: report the mode detection actually ran in, not only the declared
        # option.  ``mode='staged'`` implies ``staged_only`` even when the flag
        # itself was left at its default.  A base comparison never includes
        # working-tree staging, so ``staged_only`` is meaningless there.
        effective_staged_only = bool(
            not options.base_revision
            and (options.staged_only or options.mode == "staged")
        )

        modes_and_scope = {
            "detection": options.mode,
            "base_revision": options.base_revision,
            "base_overrides_working_tree": bool(options.base_revision),
            "staged_only": effective_staged_only,
            "include_untracked": options.include_untracked,
            "scope": scope,
            "changes_outside_scope": len(changes_outside_scope),
        }

        workflow_section = {
            "mode": options.mode,
            "base_revision": options.base_revision,
            "base_overrides_working_tree": bool(options.base_revision),
            "base_comparison_excludes_working_tree": bool(options.base_revision),
            "staged_only": effective_staged_only,
            "include_untracked": options.include_untracked,
            "scope": scope,
            "graph_source": options.graph_source,
            "max_depth": options.max_depth,
            "max_results": options.max_results,
            "max_ranked": options.max_ranked,
            "max_unconfirmed": ranking_metadata.get("max_unconfirmed"),
            "kinds": (
                [kind.value for kind in options.kinds] if options.kinds else None
            ),
            "exclusions": list(options.exclusions),
            "changed_symbols": list(options.changed_symbols),
            "max_files": options.max_files,
            "exclude_dirs": list(options.exclude_dirs),
            "read_only": True,
        }

        repository = {
            "repository_id": graph.repository.repository_id,
            "name": graph.repository.name,
            "graph_source": graph_info["source"],
            "revision": graph.repository.revision,
            "source_digest": graph.repository.source_digest,
            "file_count": len(graph.files),
            "symbol_count": len(graph.symbols),
            "relationship_count": len(graph.edges),
        }

        limitations = _dedupe(
            [
                *ranking.confidence_limitations,
                (
                    "Diff-to-symbol resolution is line-range intersection over "
                    "added lines, not semantic analysis: a hunk that inserts "
                    "inside two symbols marks both, module-level edits are "
                    "reported at file level, and an unattributable removal is "
                    "never guessed onto a neighbouring symbol."
                ),
                (
                    "Phase 3D conservatively expands every changed file to all of "
                    "its symbols before propagation, so changed_symbols refines "
                    "evidence only; reported impacts are a superset of "
                    "symbol-level precision, never a narrowing."
                ),
                *(
                    [
                        "The graph was loaded from cache; impact accuracy depends "
                        "on the graph having been regenerated after the last change."
                    ]
                    if graph_info["source"] == _GRAPH_CACHED
                    else []
                ),
                *(
                    [
                        f"{len(changes_outside_scope)} change(s) fall outside the "
                        f"requested scope ({scope}) and were not analysed."
                    ]
                    if changes_outside_scope
                    else []
                ),
                *(
                    [
                        f"{deleted_change_count} deleted file(s) are reported for "
                        "visibility but are never traversed."
                    ]
                    if deleted_change_count
                    else []
                ),
                *(
                    [
                        "Change detection was degraded: "
                        f"{len(detection_errors)} file diff(s) could not be "
                        "read, so the changeset may be incomplete."
                    ]
                    if detection_degraded
                    else []
                ),
            ]
        )

        return ImpactReport(
            policy={
                "ranking_version": RANKING_POLICY_VERSION,
                "calibration": CALIBRATION_NOTE,
            },
            mode=modes_and_scope,
            workflow=workflow_section,
            repository=repository,
            graph=graph_info,
            change_detection={
                "unit_count": len(units),
                # A failure that prevents a whole changeset raises
                # ImpactWorkflowError.  Anything here is a partial failure that
                # still produced units: the report is `degraded`, never silent.
                "degraded": detection_degraded,
                "errors": list(detection_errors),
                "warnings": list(detection_warnings),
            },
            changeset_summary=_changeset_summary(units),
            changed_files=changed_files,
            changes_outside_scope=list(changes_outside_scope),
            changed_symbols=list(changed_symbols),
            symbol_mapping=symbol_mapping,
            mapped_changes=mapped_changes,
            unmapped_changes=list(impact.unmapped_changes),
            direct_impacts=list(impact.direct_impacts),
            transitive_impacts=list(impact.transitive_impacts),
            dependency_paths=impact.dependency_paths,
            ranked_impacts=ranked_impacts,
            unconfirmed_impacts=unconfirmed_impacts,
            evidence=self._evidence(impact),
            ranking={
                "factor_weights": dict(FACTOR_WEIGHTS),
                "tier_bands": [[lower, name] for lower, name in TIER_BANDS],
                "tier_distribution": ranking_metadata.get("tier_distribution", {}),
                "metadata": ranking_metadata,
            },
            analysis_warnings=list(warnings),
            truncation={
                "analysis_truncated": analysis_truncated,
                "ranking_truncated": ranking_truncated,
                "max_results": options.max_results,
                "max_ranked": options.max_ranked,
                "max_unconfirmed": ranking_metadata.get("max_unconfirmed"),
                "impacts_discovered": traversal.get("total_impacts_discovered", 0),
                "impacts_returned": traversal.get("total_impacts_returned", 0),
                "components_found": ranking_metadata.get(
                    "confirmed_components_found", 0
                ),
                "components_returned": ranking_metadata.get(
                    "confirmed_components_returned", 0
                ),
                "unconfirmed_components_found": ranking_metadata.get(
                    "unconfirmed_components_found", 0
                ),
                "unconfirmed_components_returned": ranking_metadata.get(
                    "unconfirmed_components_returned", 0
                ),
            },
            summary=summary,
            limitations=limitations,
        )

    @staticmethod
    def _evidence(impact: ImpactAnalysisResult) -> Dict[str, Any]:
        """Provenance for every edge that produced an impact record."""
        records: Dict[str, Dict[str, Any]] = {}
        for bucket in (
            impact.direct_impacts,
            impact.transitive_impacts,
            impact.unresolved_impacts,
            impact.ambiguous_impacts,
        ):
            for record in bucket:
                edge_id = record.get("edge_id")
                if not edge_id or edge_id in records:
                    continue
                provenance = record.get("provenance") or {}
                records[edge_id] = {
                    "edge_id": edge_id,
                    "kind": record.get("edge_kind"),
                    "resolution": record.get("resolution"),
                    "confidence": record.get("evidence_confidence"),
                    "discovery_method": provenance.get("discovery_method"),
                    "source_file": provenance.get("source_file"),
                    "start_line": provenance.get("start_line"),
                    "end_line": provenance.get("end_line"),
                    "start_column": provenance.get("start_column"),
                    "end_column": provenance.get("end_column"),
                    "source_hash": provenance.get("source_hash"),
                }
        ordered = [records[edge_id] for edge_id in sorted(records)]
        return {"edge_records": ordered, "record_count": len(ordered)}


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def create_impact_workflow(
    repository_path: str | Path = ".",
    options: Optional[ImpactWorkflowOptions] = None,
) -> ImpactWorkflow:
    """Factory function to create an :class:`ImpactWorkflow`."""
    return ImpactWorkflow(repository_path, options)


def run_impact_analysis(
    repository_path: str | Path = ".",
    **option_values: Any,
) -> ImpactReport:
    """Run the workflow with keyword options and return the report.

    Convenience wrapper for callers that already have plain values (the CLI and
    MCP layer both use the same options object underneath).
    """
    options = (
        option_values["options"]
        if isinstance(option_values.get("options"), ImpactWorkflowOptions)
        else ImpactWorkflowOptions(**option_values)
    )
    return create_impact_workflow(repository_path, options).run()
