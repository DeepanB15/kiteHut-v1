"""End-to-end tests for Phase 3D: Change Impact Propagation engine.

Seventeen executable scenarios covering direct/transitive propagation, shared
components, edge orientation, cycles, unresolved/ambiguous relationships,
mapping confidence, unmapped/deleted/renamed files, path deduplication, depth
limits, truncation, determinism and provenance preservation.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

import pytest

from app.code_graph.models import (
    CodeGraph,
    EdgeKind,
    EvidenceConfidence,
    FileNode,
    GraphEdge,
    ProvenanceKind,
    RepositoryNode,
    ResolutionKind,
    SymbolKind,
    SymbolNode,
    compute_file_id,
    compute_symbol_id,
)
from app.git_ops.impact_analysis import (
    DEPENDENCY_EDGE_KINDS,
    STRUCTURAL_EDGE_KINDS,
    ImpactAnalysisService,
    create_impact_analysis_service,
)
from app.git_ops.mapping_service import (
    EvidenceConfidence as MappingConfidence,
    MappingResult,
)
from app.git_ops.models.changeset import ChangeStatus, ChangeUnit, Changeset


REPO_ID = "kitehut-test"
SOURCE_DIGEST = "0" * 64


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


def _file(path: str) -> FileNode:
    return FileNode(
        repository_id=REPO_ID,
        relative_path=path,
        language="python",
        content_hash=_hash(f"content:{path}"),
        line_count=10,
        byte_size=200,
    )


def _symbol(
    path: str,
    name: str,
    kind: SymbolKind = SymbolKind.FUNCTION,
    parent_id=None,
) -> SymbolNode:
    return SymbolNode(
        repository_id=REPO_ID,
        file_id=compute_file_id(REPO_ID, path),
        relative_path=path,
        name=name,
        qualified_name=f"{path}::{name}",
        kind=kind,
        parent_id=parent_id,
        start_line=1,
        end_line=5,
        source_hash=_hash(f"symbol:{path}:{name}"),
    )


def _fid(path: str) -> str:
    return compute_file_id(REPO_ID, path)


def _sid(
    path: str, name: str, kind: SymbolKind = SymbolKind.FUNCTION
) -> str:
    return compute_symbol_id(REPO_ID, path, f"{path}::{name}", kind)


def _import_edge(
    importer: str,
    imported: str,
    *,
    line: int = 1,
    resolution: ResolutionKind = ResolutionKind.RESOLVED,
    confidence: EvidenceConfidence = EvidenceConfidence.HIGH,
) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=_fid(importer),
        target_id=_fid(imported),
        kind=EdgeKind.IMPORTS,
        provenance_kind=ProvenanceKind.IMPORT_STATEMENT,
        source_file=importer,
        source_hash=_hash(f"content:{importer}"),
        start_line=line,
        end_line=line,
        start_column=1,
        end_column=10,
        resolution=resolution,
        confidence=confidence,
    )


def _call_edge(
    caller_path: str,
    caller_name: str,
    callee_path: str,
    callee_name: str,
    *,
    line: int = 7,
    caller_kind: SymbolKind = SymbolKind.FUNCTION,
    callee_kind: SymbolKind = SymbolKind.FUNCTION,
    resolution: ResolutionKind = ResolutionKind.RESOLVED,
    confidence: EvidenceConfidence = EvidenceConfidence.HIGH,
) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=_sid(caller_path, caller_name, caller_kind),
        target_id=_sid(callee_path, callee_name, callee_kind),
        kind=EdgeKind.CALLS,
        provenance_kind=ProvenanceKind.CALL_SITE,
        source_file=caller_path,
        source_hash=_hash(f"content:{caller_path}"),
        start_line=line,
        end_line=line,
        start_column=3,
        end_column=25,
        resolution=resolution,
        confidence=confidence,
    )


def _defines_edge(file_path: str, symbol_name: str) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=_fid(file_path),
        target_id=_sid(file_path, symbol_name),
        kind=EdgeKind.DEFINES,
        provenance_kind=ProvenanceKind.DECLARATION,
        source_file=file_path,
        source_hash=_hash(f"content:{file_path}"),
        start_line=1,
        end_line=5,
    )


def _contains_file_edge(file_path: str) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=REPO_ID,
        target_id=_fid(file_path),
        kind=EdgeKind.CONTAINS,
        provenance_kind=ProvenanceKind.DISCOVERED_PATH,
        source_file=file_path,
        source_hash=_hash(f"content:{file_path}"),
    )


def _contains_symbol_edge(
    parent_path: str,
    parent_name: str,
    child_name: str,
    *,
    parent_kind: SymbolKind = SymbolKind.CLASS,
) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=_sid(parent_path, parent_name, parent_kind),
        target_id=_sid(parent_path, child_name, SymbolKind.METHOD),
        kind=EdgeKind.CONTAINS,
        provenance_kind=ProvenanceKind.DECLARATION,
        source_file=parent_path,
        source_hash=_hash(f"content:{parent_path}"),
        start_line=1,
        end_line=5,
    )


def _graph(files, symbols=(), edges=()) -> CodeGraph:
    return CodeGraph(
        repository=RepositoryNode(
            repository_id=REPO_ID,
            name="kitehut-test",
            source_digest=SOURCE_DIGEST,
        ),
        files=list(files),
        symbols=list(symbols),
        edges=list(edges),
    )


def _modified(path: str) -> ChangeUnit:
    return ChangeUnit(path=path, status=ChangeStatus.MODIFIED)


def _changeset(*units: ChangeUnit) -> Changeset:
    return Changeset(repository_root=".", units=list(units))


def _service(graph, changeset, mapping=None, config=None):
    return create_impact_analysis_service(
        graph, mapping or MappingResult(), config
    ).analyze(changeset)


def _names(result, bucket: str) -> set[str]:
    return {record["node_name"] for record in getattr(result, bucket)}


def _records(result) -> list[dict]:
    return [
        *result.direct_impacts,
        *result.transitive_impacts,
        *result.unresolved_impacts,
        *result.ambiguous_impacts,
    ]


# ---------------------------------------------------------------------------
# 1. One changed file with one direct dependent
# ---------------------------------------------------------------------------


def test_one_changed_file_with_one_direct_dependent():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[_import_edge("a.py", "b.py", line=4)],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    assert list(result.changed_nodes) == [_fid("b.py")]
    assert result.changed_nodes[_fid("b.py")]["name"] == "b.py"
    assert result.changed_nodes[_fid("b.py")]["kind"] == "file"

    assert len(result.direct_impacts) == 1
    record = result.direct_impacts[0]
    assert record["node_id"] == _fid("a.py")
    assert record["node_name"] == "a.py"
    assert record["classification"] == "direct"
    assert record["depth"] == 1
    assert record["edge_kind"] == "imports"
    assert record["edge_source_id"] == _fid("a.py")
    assert record["edge_target_id"] == _fid("b.py")
    assert record["path"] == [_fid("b.py"), _fid("a.py")]
    assert record["provenance"]["source_file"] == "a.py"
    assert record["provenance"]["start_line"] == 4

    assert result.transitive_impacts == []
    assert set(result.impacted_nodes) == {_fid("a.py")}
    assert result.dependency_paths[_fid("a.py")] == [[_fid("b.py"), _fid("a.py")]]
    assert result.analysis_summary["direct_impact_count"] == 1
    assert result.analysis_summary["confirmed_impact_count"] == 1


# ---------------------------------------------------------------------------
# 2. One changed symbol with direct dependents
# ---------------------------------------------------------------------------


def test_one_changed_symbol_with_direct_dependents():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        symbols=[_symbol("a.py", "caller"), _symbol("b.py", "callee")],
        edges=[
            _call_edge("a.py", "caller", "b.py", "callee"),
            _defines_edge("b.py", "callee"),
        ],
    )

    result = _service(
        graph,
        Changeset.empty("."),
        config={"changed_symbols": ["b.py::callee"]},
    )

    assert list(result.changed_nodes) == [_sid("b.py", "callee")]
    assert result.changed_nodes[_sid("b.py", "callee")]["kind"] == "symbol"

    assert len(result.direct_impacts) == 1
    record = result.direct_impacts[0]
    assert record["node_id"] == _sid("a.py", "caller")
    assert record["edge_kind"] == "calls"
    assert record["depth"] == 1
    assert record["path"] == [_sid("b.py", "callee"), _sid("a.py", "caller")]

    # The DEFINES edge into ``callee`` is structural and must not impact the
    # declaring file, nor may the containing file become an impact.
    assert _fid("b.py") not in result.impacted_nodes
    assert set(result.impacted_nodes) == {_sid("a.py", "caller")}


# ---------------------------------------------------------------------------
# 3. Transitive impact
# ---------------------------------------------------------------------------


def test_transitive_impact():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py"),
            _import_edge("c.py", "a.py"),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    assert _names(result, "direct_impacts") == {"a.py"}
    assert _names(result, "transitive_impacts") == {"c.py"}
    assert result.transitive_impacts[0]["depth"] == 2
    assert result.transitive_impacts[0]["classification"] == "transitive"
    assert result.transitive_impacts[0]["path"] == [
        _fid("b.py"),
        _fid("a.py"),
        _fid("c.py"),
    ]
    assert result.dependency_paths[_fid("c.py")] == [
        [_fid("b.py"), _fid("a.py"), _fid("c.py")]
    ]
    assert result.analysis_summary["transitive_impact_count"] == 1


# ---------------------------------------------------------------------------
# 4. Multiple changed files
# ---------------------------------------------------------------------------


def test_multiple_changed_files():
    graph = _graph(
        files=[_file("a.py"), _file("d.py"), _file("b1.py"), _file("b2.py")],
        edges=[
            _import_edge("a.py", "b1.py"),
            _import_edge("d.py", "b2.py"),
        ],
    )

    result = _service(graph, _changeset(_modified("b1.py"), _modified("b2.py")))

    assert set(result.changed_nodes) == {_fid("b1.py"), _fid("b2.py")}
    assert _names(result, "direct_impacts") == {"a.py", "d.py"}
    assert all(record["depth"] == 1 for record in result.direct_impacts)
    assert set(result.impacted_nodes) == {_fid("a.py"), _fid("d.py")}
    assert result.analysis_summary["changed_node_count"] == 2
    assert result.traversal_metadata["total_changed_files"] == 2


# ---------------------------------------------------------------------------
# 5. Shared impacted components
# ---------------------------------------------------------------------------


def test_shared_impacted_components():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", line=1),
            _import_edge("a.py", "c.py", line=2),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py"), _modified("c.py")))

    # One node, two independent evidence edges, two distinct paths.
    assert set(result.impacted_nodes) == {_fid("a.py")}
    assert len(result.direct_impacts) == 2
    assert {record["node_id"] for record in result.direct_impacts} == {_fid("a.py")}
    assert {
        tuple(record["path"]) for record in result.direct_impacts
    } == {
        (_fid("b.py"), _fid("a.py")),
        (_fid("c.py"), _fid("a.py")),
    }
    assert {tuple(path) for path in result.dependency_paths[_fid("a.py")]} == {
        (_fid("b.py"), _fid("a.py")),
        (_fid("c.py"), _fid("a.py")),
    }


# ---------------------------------------------------------------------------
# 6. Incoming versus outgoing dependency direction (and edge orientation)
# ---------------------------------------------------------------------------


def test_incoming_versus_outgoing_direction_and_edge_orientation():
    # b.py imports x.py: b depends on x, so x moving is *not* caused by b.
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("d.py"), _file("x.py")],
        edges=[
            _import_edge("b.py", "x.py"),
            _import_edge("a.py", "b.py", line=2),
            _import_edge("d.py", "b.py", line=3),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    # Incoming dependents are impacted ...
    assert _names(result, "direct_impacts") == {"a.py", "d.py"}
    # ... the outgoing dependency is not.
    assert _fid("x.py") not in result.impacted_nodes
    assert "x.py" not in _names(result, "transitive_impacts")
    assert _names(result, "direct_impacts") == {"a.py", "d.py"}

    # Orientation contract for every edge kind.
    imports = next(e for e in graph.edges if e.kind == EdgeKind.IMPORTS)
    assert imports.source_id == _fid("b.py")  # importer
    assert imports.target_id == _fid("x.py")  # imported

    structural = _graph(
        files=[_file("pkg/service.py")],
        symbols=[
            _symbol("pkg/service.py", "Service", SymbolKind.CLASS),
            _symbol("pkg/service.py", "run", SymbolKind.METHOD),
            _symbol("pkg/service.py", "handle"),
        ],
        edges=[
            _contains_file_edge("pkg/service.py"),
            _defines_edge("pkg/service.py", "Service"),
            _contains_symbol_edge("pkg/service.py", "Service", "run"),
            _call_edge(
                "pkg/service.py",
                "handle",
                "pkg/service.py",
                "run",
                callee_kind=SymbolKind.METHOD,
            ),
        ],
    )
    contains_file = next(
        e for e in structural.edges if e.kind == EdgeKind.CONTAINS
    )
    assert contains_file.source_id == REPO_ID  # repository
    assert contains_file.target_id == _fid("pkg/service.py")

    defines = next(e for e in structural.edges if e.kind == EdgeKind.DEFINES)
    assert defines.source_id == _fid("pkg/service.py")  # declaring file
    assert defines.target_id == _sid("pkg/service.py", "Service")

    lexical = [
        e
        for e in structural.edges
        if e.kind == EdgeKind.CONTAINS and e.source_id != REPO_ID
    ]
    assert len(lexical) == 1
    assert lexical[0].source_id == _sid(
        "pkg/service.py", "Service", SymbolKind.CLASS
    )
    assert lexical[0].target_id == _sid("pkg/service.py", "run", SymbolKind.METHOD)

    calls = next(e for e in structural.edges if e.kind == EdgeKind.CALLS)
    assert calls.source_id == _sid("pkg/service.py", "handle")  # caller
    assert calls.target_id == _sid("pkg/service.py", "run", SymbolKind.METHOD)

    # Containment/membership never propagates as impact.
    structural_result = _service(
        structural,
        Changeset.empty("."),
        config={"changed_symbols": ["pkg/service.py::Service"]},
    )
    assert _sid("pkg/service.py", "run", SymbolKind.METHOD) not in (
        structural_result.impacted_nodes
    )
    assert all(
        record["edge_kind"] in {"imports", "calls"}
        for record in _records(structural_result)
    )
    assert DEPENDENCY_EDGE_KINDS == {EdgeKind.IMPORTS, EdgeKind.CALLS}
    assert STRUCTURAL_EDGE_KINDS == {EdgeKind.CONTAINS, EdgeKind.DEFINES}


# ---------------------------------------------------------------------------
# 7. Cycles
# ---------------------------------------------------------------------------


def test_cycles():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[
            _import_edge("a.py", "b.py", line=1),
            _import_edge("b.py", "a.py", line=2),
        ],
    )

    result = _service(graph, _changeset(_modified("a.py")))

    # b is a direct dependent; the cycle must not make a an impact of itself.
    assert _names(result, "direct_impacts") == {"b.py"}
    assert _fid("a.py") not in result.impacted_nodes
    assert len(_records(result)) == 1
    assert result.transitive_impacts == []
    assert result.traversal_metadata["total_impacts_returned"] == 1
    for record in _records(result):
        assert len(set(record["path"])) == len(record["path"])


# ---------------------------------------------------------------------------
# 8. Unresolved relationships
# ---------------------------------------------------------------------------


def test_unresolved_relationships_are_not_confirmed_impacts():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", resolution=ResolutionKind.UNRESOLVED),
            _import_edge("c.py", "b.py"),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    assert _names(result, "direct_impacts") == {"c.py"}
    assert _names(result, "unresolved_impacts") == {"a.py"}
    unresolved = result.unresolved_impacts[0]
    assert unresolved["classification"] == "unresolved"
    assert unresolved["resolution"] == "unresolved"
    # Never a confirmed impact, never a traversal path.
    assert _fid("a.py") not in result.impacted_nodes
    assert _fid("a.py") not in result.dependency_paths
    assert any(
        "unresolved relationship edge(s) were excluded" in warning
        for warning in result.analysis_warnings
    )
    assert result.analysis_summary["unresolved_impact_count"] == 1
    assert result.analysis_summary["confirmed_impact_count"] == 1


def test_unresolved_edge_does_not_bridge_transitive_impacts():
    # c -> a (resolved) -> b (unresolved). Without the confirmed a -> b link,
    # c must not be reported as a transitive impact of b.
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", resolution=ResolutionKind.UNRESOLVED),
            _import_edge("c.py", "a.py"),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    assert result.direct_impacts == []
    assert result.transitive_impacts == []
    assert result.impacted_nodes == {}
    assert _names(result, "unresolved_impacts") == {"a.py"}


# ---------------------------------------------------------------------------
# 9. Ambiguous relationships
# ---------------------------------------------------------------------------


def test_ambiguous_relationships_are_not_confirmed_impacts():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", resolution=ResolutionKind.AMBIGUOUS),
            _import_edge("c.py", "b.py"),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    assert _names(result, "direct_impacts") == {"c.py"}
    assert _names(result, "ambiguous_impacts") == {"a.py"}
    assert result.ambiguous_impacts[0]["classification"] == "ambiguous"
    assert result.ambiguous_impacts[0]["resolution"] == "ambiguous"
    assert _fid("a.py") not in result.impacted_nodes
    assert any(
        "ambiguous relationship edge(s) were excluded" in warning
        for warning in result.analysis_warnings
    )
    assert result.analysis_summary["ambiguous_impact_count"] == 1


# ---------------------------------------------------------------------------
# 10. File-level-only mapping
# ---------------------------------------------------------------------------


def test_file_level_only_mapping_preserves_confidence():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[_import_edge("a.py", "b.py")],
    )
    changeset = _changeset(_modified("b.py"))

    file_level = MappingResult(fully_mapped=False)
    result = _service(graph, changeset, mapping=file_level)

    assert _names(result, "direct_impacts") == {"a.py"}
    assert result.direct_impacts[0]["mapping_confidence"] == "file_level_only"
    assert result.analysis_summary["mapping_fully_mapped"] is False
    assert any(
        "File-to-symbol mapping is not exhaustive" in warning
        for warning in result.analysis_warnings
    )

    # When the mapper did record symbols, that evidence is preserved verbatim.
    exact = MappingResult(
        fully_mapped=True, file_to_symbols={"a.py": {_sid("a.py", "caller")}}
    )
    exact_result = _service(graph, changeset, mapping=exact)
    assert exact_result.direct_impacts[0]["mapping_confidence"] == "exact"
    assert not any(
        "File-to-symbol mapping is not exhaustive" in warning
        for warning in exact_result.analysis_warnings
    )


# ---------------------------------------------------------------------------
# 11. Unmapped and deleted files
# ---------------------------------------------------------------------------


def test_unmapped_and_deleted_files_remain_visible():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("gone.py")],
        edges=[_import_edge("a.py", "b.py")],
    )
    changeset = _changeset(
        _modified("docs/missing.md"),
        ChangeUnit(path="gone.py", status=ChangeStatus.DELETED, is_deleted=True),
        ChangeUnit(
            path="vanished.py", status=ChangeStatus.DELETED, is_deleted=True
        ),
        _modified("b.py"),
    )

    result = _service(graph, changeset)

    visible = {
        (record["path"], record["reason"], record["mapped"])
        for record in result.unmapped_changes
    }
    assert visible == {
        ("docs/missing.md", "not_in_graph", False),
        ("gone.py", "deleted", True),
        ("vanished.py", "deleted", False),
    }
    assert [record["path"] for record in result.unmapped_changes] == sorted(
        record["path"] for record in result.unmapped_changes
    )

    # The deleted file that the graph still contains is a changed node ...
    assert _fid("gone.py") in result.changed_nodes
    # ... but a deleted node is never traversed for impacts.
    assert _fid("gone.py") not in result.impacted_nodes
    assert _names(result, "direct_impacts") == {"a.py"}
    assert any("not present in the graph" in w for w in result.analysis_warnings)
    assert any("deleted file(s)" in w for w in result.analysis_warnings)


# ---------------------------------------------------------------------------
# 12. Renamed files
# ---------------------------------------------------------------------------


def test_renamed_files_are_tracked_and_old_path_impacts_survive():
    graph = _graph(
        files=[
            _file("consumer.py"),
            _file("old.py"),
            _file("new.py"),
            _file("moved.py"),
        ],
        edges=[_import_edge("consumer.py", "old.py")],
    )
    changeset = _changeset(
        ChangeUnit(
            path="new.py",
            previous_path="old.py",
            status=ChangeStatus.RENAMED,
            is_renamed=True,
        ),
        ChangeUnit(
            path="moved.py",
            previous_path="legacy.py",
            status=ChangeStatus.RENAMED,
            is_renamed=True,
        ),
    )

    result = _service(graph, changeset)

    assert {_fid("new.py"), _fid("old.py")} <= set(result.changed_nodes)
    assert _names(result, "direct_impacts") == {"consumer.py"}
    assert any(
        record["path"] == "moved.py"
        and record["reason"] == "previous_path_not_in_graph"
        and record["previous_path"] == "legacy.py"
        for record in result.unmapped_changes
    )
    # A rename is not an unmapped change when both paths are in the graph.
    assert all(
        record["path"] != "new.py" for record in result.unmapped_changes
    )


# ---------------------------------------------------------------------------
# 13. Duplicate impact paths
# ---------------------------------------------------------------------------


def test_duplicate_impact_paths_are_deduplicated():
    graph = _graph(
        files=[
            _file("a1.py"),
            _file("a2.py"),
            _file("b.py"),
            _file("c.py"),
        ],
        edges=[
            # Two distinct statements importing the same module.
            _import_edge("a1.py", "b.py", line=1),
            _import_edge("a1.py", "b.py", line=2),
            _import_edge("a2.py", "b.py", line=3),
            # c reaches b through two independent intermediate files.
            _import_edge("c.py", "a1.py", line=4),
            _import_edge("c.py", "a2.py", line=5),
        ],
    )

    result = _service(graph, _changeset(_modified("b.py")))

    # Identical paths collapse to one entry ...
    assert result.dependency_paths[_fid("a1.py")] == [
        [_fid("b.py"), _fid("a1.py")]
    ]
    # ... while both edges remain as distinct evidence records.
    assert len(result.direct_impacts) == 3
    assert {record["node_id"] for record in result.direct_impacts} == {
        _fid("a1.py"),
        _fid("a2.py"),
    }

    # Two different shortest paths to c both survive deduplication.
    paths = {tuple(path) for path in result.dependency_paths[_fid("c.py")]}
    assert paths == {
        (_fid("b.py"), _fid("a1.py"), _fid("c.py")),
        (_fid("b.py"), _fid("a2.py"), _fid("c.py")),
    }
    assert len(result.dependency_paths[_fid("c.py")]) == 2
    assert len(result.dependency_paths[_fid("c.py")]) == len(
        {tuple(path) for path in result.dependency_paths[_fid("c.py")]}
    )
    assert len(result.transitive_impacts) == 2
    assert {record["depth"] for record in result.transitive_impacts} == {2}

    # A node reachable at one hop is never re-classified as transitive.
    assert _fid("a1.py") not in {
        record["node_id"] for record in result.transitive_impacts
    }
    assert set(result.impacted_nodes) == {
        _fid("a1.py"),
        _fid("a2.py"),
        _fid("c.py"),
    }


# ---------------------------------------------------------------------------
# 14. Depth limits (and configuration validation)
# ---------------------------------------------------------------------------


def test_depth_limits():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py"), _file("d.py")],
        edges=[
            _import_edge("a.py", "b.py"),
            _import_edge("c.py", "a.py"),
            _import_edge("d.py", "c.py"),
        ],
    )

    def analyse(depth):
        return _service(
            graph, _changeset(_modified("b.py")), config={"max_depth": depth}
        )

    assert analyse(0).impacted_nodes == {}
    assert analyse(1).dependency_paths.keys() == {_fid("a.py")}
    assert set(analyse(2).impacted_nodes) == {_fid("a.py"), _fid("c.py")}
    assert set(analyse(-1).impacted_nodes) == {
        _fid("a.py"),
        _fid("c.py"),
        _fid("d.py"),
    }
    assert analyse(-1).transitive_impacts[-1]["depth"] == 3

    with pytest.raises(ValueError, match="max_depth must be >= -1"):
        analyse(-2)
    with pytest.raises(ValueError, match="max_results must be >= 1"):
        _service(graph, _changeset(), config={"max_results": 0})
    with pytest.raises(ValueError, match="structural edges"):
        _service(graph, _changeset(), config={"kinds": [EdgeKind.CONTAINS]})
    with pytest.raises(ValueError, match="Unknown traversal_config key"):
        _service(graph, _changeset(), config={"depth": 3})
    with pytest.raises(TypeError):
        ImpactAnalysisService(graph, "not-a-mapping-result")


# ---------------------------------------------------------------------------
# 15. Result truncation
# ---------------------------------------------------------------------------


def test_result_truncation_is_explicit_and_keeps_shallowest():
    importers = [f"f{index:02d}.py" for index in range(5)]
    graph = _graph(
        files=[_file("b.py"), *(_file(path) for path in importers)],
        edges=[
            _import_edge(path, "b.py", line=index + 1)
            for index, path in enumerate(importers)
        ],
    )

    result = _service(
        graph, _changeset(_modified("b.py")), config={"max_results": 2}
    )

    assert len(_records(result)) == 2
    assert result.traversal_metadata["truncated"] is True
    assert result.traversal_metadata["total_impacts_discovered"] == 5
    assert result.traversal_metadata["total_impacts_returned"] == 2
    assert result.analysis_summary["truncated"] is True
    assert any(
        "truncated at max_results=2" in warning
        for warning in result.analysis_warnings
    )
    # Only one-hop impacts exist here, so every kept record is depth 1.
    assert {record["depth"] for record in _records(result)} == {1}
    assert len(result.impacted_nodes) == 2


# ---------------------------------------------------------------------------
# 16. Deterministic repeated analysis
# ---------------------------------------------------------------------------


def test_deterministic_repeated_analysis():
    files = [_file(path) for path in ("a.py", "b.py", "c.py", "d.py")]
    edges = [
        _import_edge("a.py", "b.py", line=1),
        _import_edge("c.py", "a.py", line=2),
        _import_edge("d.py", "b.py", line=3),
        _import_edge("a.py", "c.py", line=4),
    ]
    changeset = _changeset(_modified("b.py"), _modified("c.py"))

    forward = _service(_graph(files, edges=edges), changeset)
    repeated = _service(_graph(files, edges=edges), changeset)
    reversed_edges = _service(_graph(files, edges=list(reversed(edges))), changeset)

    assert forward.to_json() == repeated.to_json()
    assert forward.to_json() == reversed_edges.to_json()

    # Ordering keys are explicit and stable.
    ordering = [
        (record["depth"], record["edge_id"], tuple(record["path"]))
        for record in _records(forward)
    ]
    assert ordering == sorted(ordering)


# ---------------------------------------------------------------------------
# 17. Provenance preservation
# ---------------------------------------------------------------------------


def test_provenance_is_preserved_verbatim():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        symbols=[_symbol("a.py", "caller"), _symbol("b.py", "callee")],
        edges=[
            _call_edge(
                "a.py",
                "caller",
                "b.py",
                "callee",
                line=7,
                confidence=EvidenceConfidence.LOW,
            )
        ],
    )
    mapping = MappingResult(
        fully_mapped=True,
        symbol_confidence={_sid("a.py", "caller"): MappingConfidence.APPROXIMATE},
    )

    result = _service(
        graph,
        Changeset.empty("."),
        mapping=mapping,
        config={"changed_symbols": ["b.py::callee"]},
    )

    assert len(result.direct_impacts) == 1
    record = result.direct_impacts[0]
    assert record["provenance"] == {
        "provenance_kind": "call_site",
        "discovery_method": "extracted",
        "source_file": "a.py",
        "start_line": 7,
        "end_line": 7,
        "start_column": 3,
        "end_column": 25,
        "source_hash": _hash("content:a.py"),
    }
    assert record["evidence_confidence"] == "low"
    assert record["resolution"] == "resolved"
    assert record["mapping_confidence"] == "approximate"
    assert record["edge_id"] == graph.edges[0].id
