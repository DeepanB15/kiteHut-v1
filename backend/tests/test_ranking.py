"""Executable tests for Phase 3E: Explainable Impact Ranking.

Scenarios required by the Phase 3E brief:

1.  Direct versus transitive ranking.
2.  IMPORTS versus CALLS.
3.  Exact versus file-level mapping.
4.  Resolved versus ambiguous versus unresolved (separate buckets).
5.  Multiple independent shortest paths.
6.  Duplicate paths.
7.  Stable ranking across reordered inputs.
8.  Equal-score tie-breaking.
9.  Empty impact results.
10. Truncated analysis.
11. No unsupported runtime-failure claims.

Plus supporting checks: one record per component in the primary list, factor
contributions summing to the score, and full explainability field coverage.
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
    EvidenceConfidence as GraphConfidence,
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
from app.git_ops.impact_analysis import create_impact_analysis_service
from app.git_ops.mapping_service import (
    EvidenceConfidence as MappingConfidence,
    MappingResult,
)
from app.git_ops.models.changeset import ChangeStatus, ChangeUnit, Changeset
from app.git_ops.ranking import (
    FACTOR_WEIGHTS,
    ImpactRankingResult,
    PriorityTier,
    RankedImpact,
    create_ranking_service,
    rank_impacts,
    summarize_ranking,
)

REPO_ID = "kitehut-test"
SOURCE_DIGEST = "0" * 64
DISCLAIMER = "does not prove that the dependent component fails at runtime"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Graph builders (mirrors the Phase 3D test helpers so fixtures stay identical)
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


def _symbol(path: str, name: str, kind: SymbolKind = SymbolKind.FUNCTION) -> SymbolNode:
    return SymbolNode(
        repository_id=REPO_ID,
        file_id=compute_file_id(REPO_ID, path),
        relative_path=path,
        name=name,
        qualified_name=f"{path}::{name}",
        kind=kind,
        parent_id=None,
        start_line=1,
        end_line=5,
        source_hash=_hash(f"symbol:{path}:{name}"),
    )


def _fid(path: str) -> str:
    return compute_file_id(REPO_ID, path)


def _sid(path: str, name: str, kind: SymbolKind = SymbolKind.FUNCTION) -> str:
    return compute_symbol_id(REPO_ID, path, f"{path}::{name}", kind)


def _import_edge(
    importer: str,
    imported: str,
    *,
    line: int = 1,
    resolution: ResolutionKind = ResolutionKind.RESOLVED,
    confidence: GraphConfidence = GraphConfidence.HIGH,
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
    resolution: ResolutionKind = ResolutionKind.RESOLVED,
    confidence: GraphConfidence = GraphConfidence.HIGH,
) -> GraphEdge:
    return GraphEdge(
        repository_id=REPO_ID,
        source_id=_sid(caller_path, caller_name),
        target_id=_sid(callee_path, callee_name),
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


def _analyze(graph, changeset, mapping=None, config=None):
    return create_impact_analysis_service(
        graph, mapping if mapping is not None else MappingResult(), config
    ).analyze(changeset)


def _by_node(ranking: ImpactRankingResult) -> dict:
    return {ranked.node_id: ranked for ranked in ranking.ranked_impacts}


def _by_node_unconfirmed(ranking: ImpactRankingResult) -> dict:
    return {ranked.node_id: ranked for ranked in ranking.unconfirmed_impacts}


# ---------------------------------------------------------------------------
# 1. Direct versus transitive ranking
# ---------------------------------------------------------------------------


def test_direct_outranks_transitive_at_equal_evidence():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", line=1),  # a is direct
            _import_edge("c.py", "a.py", line=2),  # c is transitive (depth 2)
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))

    ranking = rank_impacts(impact)
    ranked = _by_node(ranking)

    direct = ranked[_fid("a.py")]
    transitive = ranked[_fid("c.py")]

    assert direct.confirmed is True
    assert transitive.confirmed is True
    assert direct.depth == 1
    assert transitive.depth == 2
    assert direct.priority_score > transitive.priority_score
    # Both are confirmed, so they share the primary list and are ordered.
    assert [r.node_id for r in ranking.ranked_impacts] == [
        _fid("a.py"),
        _fid("c.py"),
    ]
    # Distance factor is exactly the documented 1/depth.
    assert direct.factor_values["dependency_distance"] == pytest.approx(1.0)
    assert transitive.factor_values["dependency_distance"] == pytest.approx(0.5)
    # Only the distance factor differs between them.
    for name in FACTOR_WEIGHTS:
        if name == "dependency_distance":
            continue
        assert direct.factor_values[name] == pytest.approx(transitive.factor_values[name])


# ---------------------------------------------------------------------------
# 2. IMPORTS versus CALLS
# ---------------------------------------------------------------------------


def test_imports_edge_outranks_calls_edge():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py"), _file("d.py")],
        symbols=[
            _symbol("a.py", "caller"),
            _symbol("b.py", "callee"),
            _symbol("c.py", "other"),
        ],
        edges=[
            # Both are direct, resolved, depth 1; only the edge kind differs.
            _call_edge("a.py", "caller", "b.py", "callee"),
            _import_edge("c.py", "b.py"),
            _import_edge("d.py", "b.py", line=2),
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    ranking = rank_impacts(impact)
    ranked = _by_node(ranking)

    call_impact = ranked[_sid("a.py", "caller")]
    import_impact = ranked[_fid("c.py")]

    assert call_impact.record["edge_kind"] == "calls"
    assert import_impact.record["edge_kind"] == "imports"
    assert import_impact.factor_values["edge_relationship"] == pytest.approx(1.0)
    assert call_impact.factor_values["edge_relationship"] == pytest.approx(0.85)
    assert import_impact.priority_score > call_impact.priority_score

    # The IMPORTS entries are the two highest-ranked components.
    assert {r.node_id for r in ranking.ranked_impacts[:2]} == {
        _fid("c.py"),
        _fid("d.py"),
    }


# ---------------------------------------------------------------------------
# 3. Exact versus file-level mapping
# ---------------------------------------------------------------------------


def test_exact_mapping_outranks_file_level_only_mapping():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[_import_edge("a.py", "b.py")],
    )
    changeset = _changeset(_modified("b.py"))

    exact_mapping = MappingResult(
        fully_mapped=True,
        file_to_symbols={"a.py": {_sid("a.py", "caller")}},
        symbol_confidence={_fid("a.py"): MappingConfidence.EXACT},
    )
    file_level_mapping = MappingResult(fully_mapped=False)

    exact = _by_node(rank_impacts(_analyze(graph, changeset, mapping=exact_mapping)))[
        _fid("a.py")
    ]
    file_level = _by_node(
        rank_impacts(_analyze(graph, changeset, mapping=file_level_mapping))
    )[_fid("a.py")]

    assert exact.record["mapping_confidence"] == "exact"
    assert file_level.record["mapping_confidence"] == "file_level_only"
    assert exact.factor_values["mapping_confidence"] == pytest.approx(1.0)
    assert file_level.factor_values["mapping_confidence"] == pytest.approx(0.25)
    assert exact.priority_score > file_level.priority_score

    # The reduced certainty is surfaced, not hidden.
    assert any(
        "file-level only" in limitation
        for limitation in file_level.confidence_limitations
    )
    assert not any(
        "file-level only" in limitation for limitation in exact.confidence_limitations
    )


# ---------------------------------------------------------------------------
# 4. Resolved versus ambiguous versus unresolved
# ---------------------------------------------------------------------------


def test_resolved_ambiguous_unresolved_are_separated_and_ordered():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py"), _file("d.py")],
        edges=[
            _import_edge("a.py", "b.py", resolution=ResolutionKind.RESOLVED),
            _import_edge("c.py", "b.py", resolution=ResolutionKind.AMBIGUOUS, line=2),
            _import_edge("d.py", "b.py", resolution=ResolutionKind.UNRESOLVED, line=3),
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    ranking = rank_impacts(impact)

    confirmed_ids = {r.node_id for r in ranking.ranked_impacts}
    unconfirmed_ids = {r.node_id for r in ranking.unconfirmed_impacts}

    # Only the resolved edge is a confirmed impact.
    assert confirmed_ids == {_fid("a.py")}
    # Ambigous and unresolved never enter the primary list ...
    assert unconfirmed_ids == {_fid("c.py"), _fid("d.py")}
    assert _fid("c.py") not in confirmed_ids
    assert _fid("d.py") not in confirmed_ids

    # ... and are flagged as unconfirmed with a resolution-aware explanation.
    for ranked in ranking.unconfirmed_impacts:
        assert ranked.confirmed is False
        assert ranked.priority_tier in set(PriorityTier)
        assert "unconfirmed" in ranked.explanation

    ambiguous = _by_node_unconfirmed(ranking)[_fid("c.py")]
    unresolved = _by_node_unconfirmed(ranking)[_fid("d.py")]
    assert ambiguous.classification == "ambiguous"
    assert unresolved.classification == "unresolved"
    assert ambiguous.factor_values["resolution_status"] == pytest.approx(0.5)
    assert unresolved.factor_values["resolution_status"] == pytest.approx(0.0)
    # Separation is structural, not score-based: an unconfirmed record stays
    # unconfirmed no matter how high its score happens to be.
    assert all(r.confirmed is False for r in ranking.unconfirmed_impacts)
    assert all(r.confirmed is True for r in ranking.ranked_impacts)
    assert not confirmed_ids & unconfirmed_ids


# ---------------------------------------------------------------------------
# 5. Multiple independent shortest paths
# ---------------------------------------------------------------------------


def test_multiple_independent_shortest_paths_raise_priority():
    graph = _graph(
        files=[
            _file("a1.py"),
            _file("a2.py"),
            _file("single.py"),
            _file("b.py"),
            _file("c.py"),
        ],
        edges=[
            _import_edge("a1.py", "b.py", line=1),
            _import_edge("a2.py", "b.py", line=2),
            _import_edge("single.py", "b.py", line=3),
            _import_edge("c.py", "a1.py", line=4),
            _import_edge("c.py", "a2.py", line=5),
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    ranking = rank_impacts(impact)
    ranked = _by_node(ranking)

    multi = ranked[_fid("c.py")]
    single = ranked[_fid("single.py")]

    assert multi.independent_path_count == 2
    assert sorted(multi.evidence_paths) == sorted(
        [
            [_fid("b.py"), _fid("a1.py"), _fid("c.py")],
            [_fid("b.py"), _fid("a2.py"), _fid("c.py")],
        ]
    )
    assert single.independent_path_count == 1
    assert multi.factor_values["independent_paths"] == pytest.approx(2 / 3)
    assert single.factor_values["independent_paths"] == pytest.approx(1 / 3)
    # More independent evidence is visible in the contribution breakdown.
    assert (
        multi.factor_contributions["independent_paths"]
        > single.factor_contributions["independent_paths"]
    )


# ---------------------------------------------------------------------------
# 6. Duplicate paths
# ---------------------------------------------------------------------------


def test_duplicate_paths_are_counted_once():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[
            # Two import statements from the same file to the same file.
            _import_edge("a.py", "b.py", line=1),
            _import_edge("a.py", "b.py", line=2),
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    ranking = rank_impacts(impact)

    # Two evidence records exist in Phase 3D ...
    assert len(impact.direct_impacts) == 2
    # ... but the component is represented once, with one independent path.
    assert len(ranking.ranked_impacts) == 1
    ranked = ranking.ranked_impacts[0]
    assert ranked.node_id == _fid("a.py")
    assert ranked.independent_path_count == 1
    assert ranked.evidence_paths == [[_fid("b.py"), _fid("a.py")]]
    assert ranked.factor_values["independent_paths"] == pytest.approx(1 / 3)
    # No duplicated path in the primary representation.
    assert len({tuple(path) for path in ranked.evidence_paths}) == len(
        ranked.evidence_paths
    )


# ---------------------------------------------------------------------------
# 7. Stable ranking across reordered inputs
# ---------------------------------------------------------------------------


def test_ranking_is_stable_across_reordered_inputs():
    paths = ["a.py", "b.py", "c.py", "d.py"]
    edge_specs = [
        ("a.py", "b.py", 1, ResolutionKind.RESOLVED),
        ("c.py", "b.py", 2, ResolutionKind.RESOLVED),
        ("d.py", "c.py", 3, ResolutionKind.RESOLVED),
        ("d.py", "b.py", 4, ResolutionKind.AMBIGUOUS),
    ]

    forward_edges = [
        _import_edge(importer, imported, line=line, resolution=resolution)
        for importer, imported, line, resolution in edge_specs
    ]
    reversed_edges = [
        _import_edge(importer, imported, line=line, resolution=resolution)
        for importer, imported, line, resolution in reversed(edge_specs)
    ]

    changeset = _changeset(_modified("b.py"))
    forward = rank_impacts(
        _analyze(_graph([_file(p) for p in paths], edges=forward_edges), changeset)
    )
    reversed_ranking = rank_impacts(
        _analyze(_graph([_file(p) for p in paths], edges=reversed_edges), changeset)
    )
    repeated = rank_impacts(
        _analyze(_graph([_file(p) for p in paths], edges=forward_edges), changeset)
    )

    assert forward.to_json() == reversed_ranking.to_json()
    assert forward.to_json() == repeated.to_json()

    # Ordering must be descending score, then an explicit deterministic tie-break.
    order_keys = [
        (-r.priority_score, r.depth, r.record.get("edge_id"), tuple(r.record["path"]))
        for r in forward.ranked_impacts
    ]
    assert order_keys == sorted(order_keys)


# ---------------------------------------------------------------------------
# 8. Equal-score tie-breaking
# ---------------------------------------------------------------------------


def test_equal_score_tie_breaking_is_deterministic():
    # Two symmetric importers of the same changed file: identical factors,
    # so only the explicit tie-break can order them.
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("target.py")],
        edges=[
            _import_edge("a.py", "target.py", line=1),
            _import_edge("b.py", "target.py", line=1),
        ],
    )
    ranking = rank_impacts(
        _analyze(graph, _changeset(_modified("target.py")))
    )

    assert len(ranking.ranked_impacts) == 2
    first, second = ranking.ranked_impacts
    assert first.priority_score == second.priority_score
    assert first.priority_tier is second.priority_tier

    # Tie-break keys are deterministic: depth, then edge_id, then path, then id.
    assert (
        first.depth,
        first.record["edge_id"],
        tuple(first.record["path"]),
        first.node_id,
    ) < (
        second.depth,
        second.record["edge_id"],
        tuple(second.record["path"]),
        second.node_id,
    )

    # Rebuilding the same graph in the opposite edge order keeps the order.
    flipped = _graph(
        files=[_file("a.py"), _file("b.py"), _file("target.py")],
        edges=[
            _import_edge("b.py", "target.py", line=1),
            _import_edge("a.py", "target.py", line=1),
        ],
    )
    flipped_ranking = rank_impacts(_analyze(flipped, _changeset(_modified("target.py"))))
    assert [r.node_id for r in flipped_ranking.ranked_impacts] == [
        r.node_id for r in ranking.ranked_impacts
    ]


# ---------------------------------------------------------------------------
# 9. Empty impact results
# ---------------------------------------------------------------------------


def test_empty_impact_results_produce_empty_ranking():
    graph = _graph(files=[_file("a.py")])
    impact = _analyze(graph, Changeset(repository_root=".", units=[]))

    ranking = rank_impacts(impact)

    assert ranking.ranked_impacts == []
    assert ranking.unconfirmed_impacts == []
    assert ranking.ranking_metadata["confirmed_components_returned"] == 0
    assert ranking.ranking_metadata["confirmed_truncated"] is False
    # The disclaimer survives even when there is nothing to rank.
    assert any(
        DISCLAIMER in limitation for limitation in ranking.confidence_limitations
    )
    assert isinstance(ranking, ImpactRankingResult)

    # A changeset that maps to no graph node is also empty.
    unmapped = _analyze(
        graph, _changeset(_modified("not/in/graph.py"))
    )
    assert rank_impacts(unmapped).ranked_impacts == []


# ---------------------------------------------------------------------------
# 10. Truncated analysis
# ---------------------------------------------------------------------------


def test_truncated_analysis_is_reported_not_silent():
    importers = [f"f{index:02d}.py" for index in range(5)]
    graph = _graph(
        files=[_file("b.py"), *(_file(path) for path in importers)],
        edges=[
            _import_edge(path, "b.py", line=index + 1)
            for index, path in enumerate(importers)
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")), config={"max_results": 2})
    assert impact.traversal_metadata["truncated"] is True

    ranking = rank_impacts(impact, max_ranked=10)

    # Phase 3D truncation is surfaced through ranking metadata.
    assert ranking.ranking_metadata["analysis_truncated"] is True
    assert len(ranking.ranked_impacts) == 2
    assert ranking.ranking_metadata["confirmed_components_found"] == 2

    # Phase 3E truncation of its own list is also explicit.
    capped = rank_impacts(impact, max_ranked=1)
    assert len(capped.ranked_impacts) == 1
    assert capped.ranking_metadata["confirmed_truncated"] is True
    assert capped.ranking_metadata["confirmed_components_found"] == 2

    # Slicing keeps the highest-priority component, not an arbitrary one.
    assert (
        capped.ranked_impacts[0].node_id
        == rank_impacts(impact, max_ranked=10).ranked_impacts[0].node_id
    )


# ---------------------------------------------------------------------------
# 11. No unsupported runtime-failure claims
# ---------------------------------------------------------------------------


def test_ranking_never_claims_runtime_failure():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py"),
            _import_edge("c.py", "b.py", line=2, resolution=ResolutionKind.AMBIGUOUS),
        ],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    ranking = rank_impacts(impact)

    for ranked in [*ranking.ranked_impacts, *ranking.unconfirmed_impacts]:
        assert any(
            DISCLAIMER in limitation for limitation in ranked.confidence_limitations
        ), ranked.node_id
        assert ranked.record["resolution"] in {"resolved", "ambiguous", "unresolved"}
        # The summary flags are the machine-readable form of the same promise.
    assert ranking.ranking_metadata["navigation_only"] is True
    assert ranking.ranking_metadata["runtime_impact_verified"] is False
    assert any(
        DISCLAIMER in limitation for limitation in ranking.confidence_limitations
    )

    # No record is allowed to look like a runtime verdict.
    forbidden = {"runtime_failure", "will_fail", "broken", "failing"}
    payload = ranking.to_json().lower()
    assert not any(token in payload for token in forbidden)


# ---------------------------------------------------------------------------
# Supporting guarantees
# ---------------------------------------------------------------------------


def test_factor_contributions_sum_to_score_and_weights_are_explicit():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py"),
            _import_edge("c.py", "a.py", line=2),
        ],
    )
    ranking = rank_impacts(_analyze(graph, _changeset(_modified("b.py"))))

    assert set(FACTOR_WEIGHTS) == {
        "dependency_distance",
        "edge_relationship",
        "resolution_status",
        "evidence_confidence",
        "mapping_confidence",
        "independent_paths",
    }
    assert sum(FACTOR_WEIGHTS.values()) == pytest.approx(1.0)

    for ranked in ranking.ranked_impacts:
        assert set(ranked.factor_values) == set(FACTOR_WEIGHTS)
        assert set(ranked.factor_contributions) == set(FACTOR_WEIGHTS)
        for name, weight in FACTOR_WEIGHTS.items():
            assert ranked.factor_contributions[name] == pytest.approx(
                weight * ranked.factor_values[name] * 100.0, abs=1e-3
            )
        assert sum(ranked.factor_contributions.values()) == pytest.approx(
            ranked.priority_score, abs=0.01
        )
        assert 0.0 <= ranked.priority_score <= 100.0

    # The weights are published in metadata so a consumer can reproduce a score.
    assert ranking.ranking_metadata["factor_weights"] == FACTOR_WEIGHTS


def test_every_ranked_impact_is_fully_explainable():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        symbols=[_symbol("a.py", "caller"), _symbol("b.py", "callee")],
        edges=[_call_edge("a.py", "caller", "b.py", "callee", line=9)],
    )
    ranking = rank_impacts(_analyze(graph, _changeset(_modified("b.py"))))
    ranked = ranking.ranked_impacts[0]

    assert isinstance(ranked, RankedImpact)
    # Original Phase 3D record is preserved verbatim.
    assert ranked.record["edge_kind"] == "calls"
    assert ranked.record["provenance"]["start_line"] == 9
    assert ranked.record["path"][0] == _sid("b.py", "callee")
    # Every explainability field the brief requires.
    assert isinstance(ranked.priority_score, float)
    assert isinstance(ranked.priority_tier, PriorityTier)
    assert ranked.factor_contributions
    assert ranked.evidence_paths
    assert ranked.explanation
    assert ranked.confidence_limitations
    assert ranked.confirmed is True

    payload = ranked.to_dict()
    for key in (
        "original_impact",
        "priority_score",
        "priority_tier",
        "factor_contributions",
        "evidence_paths",
        "explanation",
        "confidence_limitations",
    ):
        assert key in payload


def test_shared_component_appears_once_in_primary_list():
    graph = _graph(
        files=[_file("a.py"), _file("b.py"), _file("c.py")],
        edges=[
            _import_edge("a.py", "b.py", line=1),
            _import_edge("a.py", "c.py", line=2),
        ],
    )
    ranking = rank_impacts(
        _analyze(graph, _changeset(_modified("b.py"), _modified("c.py")))
    )

    node_ids = [r.node_id for r in ranking.ranked_impacts]
    assert node_ids == [_fid("a.py")]
    assert len(node_ids) == len(set(node_ids))
    # Both independent evidence paths survive on the single entry.
    assert ranking.ranked_impacts[0].independent_path_count == 2


def test_ranking_does_not_mutate_impact_result_and_validates_arguments():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[_import_edge("a.py", "b.py")],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))
    before = impact.to_json()

    rank_impacts(impact)
    rank_impacts(impact)

    assert impact.to_json() == before

    with pytest.raises(TypeError):
        rank_impacts({"direct_impacts": []})
    with pytest.raises(ValueError):
        rank_impacts(impact, max_ranked=0)
    with pytest.raises(TypeError):
        rank_impacts(impact, max_ranked="10")
    with pytest.raises(ValueError):
        rank_impacts(impact, max_unconfirmed=-1)


def test_summary_and_factory_are_available():
    graph = _graph(
        files=[_file("a.py"), _file("b.py")],
        edges=[_import_edge("a.py", "b.py")],
    )
    impact = _analyze(graph, _changeset(_modified("b.py")))

    assert create_ranking_service() is rank_impacts

    summary = summarize_ranking(impact)
    assert summary["top_impacts"][0]["node_id"] == _fid("a.py")
    assert summary["top_impacts"][0]["priority_tier"].startswith("P")
    assert summary["top_impacts"][0]["explanation"]
    assert summary["ranking_metadata"]["navigation_only"] is True
    assert summary["unconfirmed"] == []
