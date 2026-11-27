"""Explainable impact ranking on top of the Phase 3D impact engine.

Phase 3E adds a *prioritisation* view over an existing
:class:`~app.git_ops.impact_analysis.ImpactAnalysisResult`.  It reuses that
result verbatim: it never re-runs propagation, never re-classifies a node and
never changes Phase 3D semantics.

What this module answers
------------------------
*Which of the potentially affected components should a developer look at
first, and why?*  The answer is a transparent, weighted sum of six factors,
each of which is reported per record so the score can be reproduced by hand.

What this module must never claim
---------------------------------
A static dependency is navigation evidence.  It is **not** proof that the
dependent code fails at runtime.  Every ranked record carries
``confidence_limitations`` and the result carries a global disclaimer.

Factors (weights sum to 1.00)
-----------------------------
====================================  ======  ============================================
Factor                                Weight  Effect (higher raw factor = higher score)
====================================  ======  ============================================
``dependency_distance``               0.25    Shallower hop distance scores higher.
``edge_relationship``                 0.20    ``IMPORTS`` scores above ``CALLS``.
``resolution_status``                 0.25    ``RESOLVED`` > ``AMBIGUOUS`` > ``UNRESOLVED``.
``evidence_confidence``               0.15    Graph edge confidence, ``EXACT`` down to ``LOW``.
``mapping_confidence``                0.10    Mapping confidence, ``EXACT`` down to
                                              ``FILE_LEVEL_ONLY``.
``independent_paths``                 0.05    More independent shortest paths score higher.
====================================  ======  ============================================

Normalised factor values (all in ``[0, 1]``):
  * ``dependency_distance``  ``1 / depth`` (depth 1 -> 1.0, 2 -> 0.5, 3 -> 0.333).
  * ``edge_relationship``    ``IMPORTS`` 1.0, ``CALLS`` 0.85.
  * ``resolution_status``    ``RESOLVED`` 1.0, ``AMBIGUOUS`` 0.5, ``UNRESOLVED`` 0.0.
  * ``evidence_confidence``  ``EXACT`` 1.0, ``HIGH`` 0.85, ``MEDIUM`` 0.6,
                             ``LOW`` 0.3, unknown 0.3.
  * ``mapping_confidence``   ``EXACT`` 1.0, ``APPROXIMATE`` 0.5,
                             ``FILE_LEVEL_ONLY`` 0.25, unknown 0.25.
  * ``independent_paths``    ``min(unique_shortest_paths, 3) / 3``.

``score = 100 * sum(weight_i * value_i)``, rounded to two decimals.  Higher
score means higher priority.  Tiers: ``P0 >= 85``, ``P1 >= 70``, ``P2 >= 55``,
``P3 >= 40``, ``P4 >= 25``, ``P5`` otherwise.

Classification safety
---------------------
``DIRECT`` and ``TRANSITIVE`` records are the only records allowed into the
primary ranked list.  ``UNRESOLVED`` and ``AMBIGUOUS`` records are returned in
a **separate** ``unconfirmed`` list with ``confirmed=False``; they are never
interleaved with confirmed impacts, and no score can promote them.  ``UNMAPPED``
changes originate in ``unmapped_changes`` (they are not impacts at all) and are
not ranked.

Deduplication
-------------
A component reachable through several edges is represented **once** in the
primary list.  Its score comes from its strongest record; every distinct
shortest evidence path is preserved in ``evidence_paths`` and counted by the
``independent_paths`` factor.  Identical path tuples are counted once.

Determinism
-----------
Records are ordered by ``(-score, depth, edge_id, path)`` and every grouping is
a ``sorted`` iteration, so reordering graph edges cannot change
:meth:`ImpactRankingResult.to_json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "FACTOR_WEIGHTS",
    "RANKING_POLICY_VERSION",
    "CALIBRATION_NOTE",
    "PriorityTier",
    "RankedImpact",
    "ImpactRankingResult",
    "rank_impacts",
    "create_ranking_service",
    "summarize_ranking",
]


# ---------------------------------------------------------------------------
# Policy identity
# ---------------------------------------------------------------------------

#: Identity of the v0.2.0 ranking policy.  Reporting only: no weight,
#: threshold or ordering decision depends on this value.
RANKING_POLICY_VERSION = "v0.2.0"

#: Stated plainly wherever a score is published, so a number is never mistaken
#: for a validated prediction.
CALIBRATION_NOTE = (
    "Priority scores are heuristic: the six factor weights and the tier "
    "thresholds are hand-set engineering defaults, not empirically calibrated "
    "against runtime failures or developer triage data. Treat tiers as an "
    "ordering aid, not a probability."
)


# ---------------------------------------------------------------------------
# Weights and tier bands
# ---------------------------------------------------------------------------

#: Factor weights.  Documented in the module docstring; sum == 1.0.
FACTOR_WEIGHTS: Dict[str, float] = {
    "dependency_distance": 0.25,
    "edge_relationship": 0.20,
    "resolution_status": 0.25,
    "evidence_confidence": 0.15,
    "mapping_confidence": 0.10,
    "independent_paths": 0.05,
}

#: ``(lower_bound_inclusive, tier_name)`` ordered from highest to lowest.
TIER_BANDS: Tuple[Tuple[float, str], ...] = (
    (85.0, "P0"),
    (70.0, "P1"),
    (55.0, "P2"),
    (40.0, "P3"),
    (25.0, "P4"),
    (0.0, "P5"),
)

#: Maximum number of independent paths that still earns score.
_PATH_CAP = 3

_EDGE_FACTORS = {"imports": 1.0, "calls": 0.85}
_RESOLUTION_FACTORS = {"resolved": 1.0, "ambiguous": 0.5, "unresolved": 0.0}
_EVIDENCE_FACTORS = {
    "exact": 1.0,
    "high": 0.85,
    "medium": 0.6,
    "low": 0.3,
}
_MAPPING_FACTORS = {"exact": 1.0, "approximate": 0.5, "file_level_only": 0.25}

_CONFIRMED_CLASSIFICATIONS = frozenset({"direct", "transitive"})
_UNCONFIRMED_CLASSIFICATIONS = frozenset({"unresolved", "ambiguous"})

RUNTIME_FAILURE_DISCLAIMER = (
    "Static dependency is navigation evidence only; it does not prove that the "
    "dependent component fails at runtime."
)


class PriorityTier(str, Enum):
    """Priority tier derived from the ranking score (``P0`` highest)."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"


# ---------------------------------------------------------------------------
# Factor computation
# ---------------------------------------------------------------------------


def _normalised(value: Any) -> str:
    """Lowercase an enum or string for factor lookup."""
    return str(getattr(value, "value", value)).strip().lower()


def _distance_factor(depth: int) -> float:
    """Shallower hop distance scores higher: ``1 / depth``."""
    if depth <= 0:
        return 0.0
    return round(1.0 / depth, 6)


def _edge_factor(edge_kind: Any) -> float:
    return _EDGE_FACTORS.get(_normalised(edge_kind), 0.5)


def _resolution_factor(resolution: Any) -> float:
    return _RESOLUTION_FACTORS.get(_normalised(resolution), 0.0)


def _evidence_factor(evidence_confidence: Any) -> float:
    return _EVIDENCE_FACTORS.get(_normalised(evidence_confidence), 0.3)


def _mapping_factor(mapping_confidence: Any) -> float:
    return _MAPPING_FACTORS.get(_normalised(mapping_confidence), 0.25)


def _paths_factor(independent_paths: int) -> float:
    return min(max(independent_paths, 0), _PATH_CAP) / _PATH_CAP


def _tier_for(score: float) -> PriorityTier:
    for lower_bound, name in TIER_BANDS:
        if score >= lower_bound:
            return PriorityTier(name)
    return PriorityTier.P5


def _unique_paths(records: Sequence[Mapping[str, Any]], node_id: str) -> Tuple[Tuple[str, ...], ...]:
    """Distinct shortest paths for ``node_id``, as sorted tuples."""
    paths = set()
    for record in records:
        path = record.get("path") or ()
        if path:
            paths.add(tuple(str(step) for step in path))
    return tuple(sorted(paths, key=lambda path: (len(path), path)))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedImpact:
    """One ranked component with its full explanation.

    ``record`` is the original Phase 3D impact record, unmodified.  Every
    ranking value is derived from it and is fully reproducible.
    """

    record: Dict[str, Any]
    node_id: str
    priority_score: float
    priority_tier: PriorityTier
    confirmed: bool
    classification: str
    depth: int
    factor_values: Dict[str, float]
    factor_contributions: Dict[str, float]
    evidence_paths: List[List[str]]
    independent_path_count: int
    explanation: str
    confidence_limitations: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_impact": self.record,
            "node_id": self.node_id,
            "priority_score": self.priority_score,
            "priority_tier": self.priority_tier.value,
            "confirmed": self.confirmed,
            "classification": self.classification,
            "depth": self.depth,
            "factor_values": dict(self.factor_values),
            "factor_contributions": dict(self.factor_contributions),
            "evidence_paths": [list(path) for path in self.evidence_paths],
            "independent_path_count": self.independent_path_count,
            "explanation": self.explanation,
            "confidence_limitations": list(self.confidence_limitations),
        }


@dataclass
class ImpactRankingResult:
    """Ranked impacts, split into confirmed and unconfirmed buckets.

    ``ranked_impacts`` is the primary list: one entry per confirmed component,
    highest priority first.  ``unconfirmed_impacts`` holds ``UNRESOLVED`` and
    ``AMBIGUOUS`` records, which are deliberately kept out of the primary list.
    """

    ranked_impacts: List[RankedImpact] = field(default_factory=list)
    unconfirmed_impacts: List[RankedImpact] = field(default_factory=list)
    ranking_metadata: Dict[str, Any] = field(default_factory=dict)
    confidence_limitations: List[str] = field(default_factory=list)

    @property
    def records(self) -> List[Dict[str, Any]]:
        """Serialised ranked records (confirmed bucket) for convenience."""
        return [ranked.to_dict() for ranked in self.ranked_impacts]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ranked_impacts": [ranked.to_dict() for ranked in self.ranked_impacts],
            "unconfirmed_impacts": [
                ranked.to_dict() for ranked in self.unconfirmed_impacts
            ],
            "ranking_metadata": self.ranking_metadata,
            "confidence_limitations": self.confidence_limitations,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Core ranking
# ---------------------------------------------------------------------------


def _as_records(bucket: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(record) for record in bucket]


def _score_component(
    records: Sequence[Dict[str, Any]],
    node_id: str,
) -> Tuple[Dict[str, float], Dict[str, float], float, Tuple[Tuple[str, ...], ...]]:
    """Compute factor values, weighted contributions and score for a component.

    The strongest record (deepest priority) wins on every factor except
    ``independent_paths``, which is aggregated across the component so that
    several independent shortest paths raise priority.
    """
    paths = _unique_paths(records, node_id)

    best_depth = min(record.get("depth", 0) or 0 for record in records)
    best_edge = max((_edge_factor(record.get("edge_kind")) for record in records), default=0.5)
    best_resolution = max(
        (_resolution_factor(record.get("resolution")) for record in records), default=0.0
    )
    best_evidence = max(
        (_evidence_factor(record.get("evidence_confidence")) for record in records), default=0.3
    )
    best_mapping = max(
        (_mapping_factor(record.get("mapping_confidence")) for record in records), default=0.25
    )
    paths_value = _paths_factor(len(paths))

    values = {
        "dependency_distance": _distance_factor(best_depth),
        "edge_relationship": best_edge,
        "resolution_status": best_resolution,
        "evidence_confidence": best_evidence,
        "mapping_confidence": best_mapping,
        "independent_paths": paths_value,
    }
    contributions = {
        name: round(FACTOR_WEIGHTS[name] * value * 100.0, 4)
        for name, value in values.items()
    }
    score = round(sum(contributions.values()), 2)
    return values, contributions, score, paths


def _limitations_for(record: Mapping[str, Any], classification: str) -> List[str]:
    limitations: List[str] = []
    resolution = _normalised(record.get("resolution", ""))
    mapping = _normalised(record.get("mapping_confidence", ""))
    evidence = _normalised(record.get("evidence_confidence", ""))

    if resolution == "unresolved":
        limitations.append(
            "Relationship is unresolved: the edge endpoint is not statically "
            "confirmed, so this must not be read as a confirmed impact."
        )
    elif resolution == "ambiguous":
        limitations.append(
            "Relationship is ambiguous: several static candidates match, so the "
            "impact is plausible but not confirmed."
        )

    if mapping == "file_level_only":
        limitations.append(
            "Mapping is file-level only: symbol-level impact may be under-reported."
        )
    elif mapping == "approximate":
        limitations.append(
            "Mapping is approximate: exact symbol correspondence was not established."
        )

    if evidence in {"low", "medium"}:
        limitations.append(
            f"Graph evidence confidence is {evidence}; the edge may be incomplete."
        )

    if classification not in _CONFIRMED_CLASSIFICATIONS:
        limitations.append(
            "This component is reported for visibility only and is not a "
            "confirmed impact."
        )

    limitations.append(RUNTIME_FAILURE_DISCLAIMER)
    return limitations


def _explain(
    *,
    record: Mapping[str, Any],
    node_id: str,
    tier: PriorityTier,
    score: float,
    values: Mapping[str, float],
    contributions: Mapping[str, float],
    paths: Sequence[Sequence[str]],
    confirmed: bool,
) -> str:
    node_name = record.get("node_name") or node_id
    depth = record.get("depth", 0)
    resolution = _normalised(record.get("resolution", "unknown"))
    edge_kind = _normalised(record.get("edge_kind", "unknown"))

    if not confirmed:
        lead = (
            f"{node_name} is an unconfirmed ({resolution}) candidate at depth {depth} "
            f"through a {edge_kind} edge, listed separately from confirmed impacts."
        )
    elif depth == 1:
        lead = (
            f"{node_name} directly depends on a changed node (depth 1) through a "
            f"{edge_kind} edge."
        )
    else:
        lead = (
            f"{node_name} is transitively reachable from a changed node at depth "
            f"{depth} through a {edge_kind} edge."
        )

    parts = [lead, f"Priority {tier.value} with score {score:.2f}/100."]
    parts.append(
        "Factors: "
        + ", ".join(
            f"{name}={values[name]:.3f} x {FACTOR_WEIGHTS[name]:.2f} "
            f"= {contributions[name]:.2f}"
            for name in FACTOR_WEIGHTS
        )
        + "."
    )
    parts.append(
        f"{len(paths)} independent shortest evidence path(s) recorded; "
        f"resolution={resolution}, evidence_confidence="
        f"{_normalised(record.get('evidence_confidence', 'unknown'))}, "
        f"mapping_confidence={_normalised(record.get('mapping_confidence', 'unknown'))}."
    )
    return " ".join(parts)


def _rank_bucket(
    records: Sequence[Dict[str, Any]],
    *,
    confirmed: bool,
) -> List[RankedImpact]:
    """Group records by component, score each group and order deterministically."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        node_id = str(record.get("node_id") or "")
        if not node_id:
            continue
        grouped.setdefault(node_id, []).append(record)

    ranked: List[RankedImpact] = []
    for node_id in sorted(grouped):
        group = sorted(
            grouped[node_id],
            key=lambda record: (
                record.get("depth", 0) or 0,
                str(record.get("edge_id") or ""),
                tuple(record.get("path") or ()),
            ),
        )
        values, contributions, score, paths = _score_component(group, node_id)
        representative = group[0]
        tier = _tier_for(score)
        classification = _normalised(representative.get("classification", ""))

        ranked.append(
            RankedImpact(
                record=representative,
                node_id=node_id,
                priority_score=score,
                priority_tier=tier,
                confirmed=confirmed,
                classification=classification,
                depth=int(representative.get("depth", 0) or 0),
                factor_values=values,
                factor_contributions=contributions,
                evidence_paths=[list(path) for path in paths],
                independent_path_count=len(paths),
                explanation=_explain(
                    record=representative,
                    node_id=node_id,
                    tier=tier,
                    score=score,
                    values=values,
                    contributions=contributions,
                    paths=paths,
                    confirmed=confirmed,
                ),
                confidence_limitations=_limitations_for(representative, classification),
            )
        )

    ranked.sort(
        key=lambda ranked_impact: (
            -ranked_impact.priority_score,
            ranked_impact.depth,
            str(ranked_impact.record.get("edge_id") or ""),
            tuple(ranked_impact.record.get("path") or ()),
            ranked_impact.node_id,
        )
    )
    return ranked


def rank_impacts(
    impact_result: Any,
    *,
    max_ranked: int = 50,
    max_unconfirmed: Optional[int] = None,
) -> ImpactRankingResult:
    """Rank the impacts of a Phase 3D ``ImpactAnalysisResult``.

    The analysis result is only read, never modified.  Confirmed impacts
    (``direct_impacts``/``transitive_impacts``) and unconfirmed ones
    (``unresolved_impacts``/``ambiguous_impacts``) are ranked separately so a
    score can never promote an unsupported relationship.

    Parameters
    ----------
    impact_result:
        Result of ``ImpactAnalysisService.analyze()``.
    max_ranked:
        Cap on the confirmed list.  Truncation is reported in
        ``ranking_metadata`` rather than applied silently.
    max_unconfirmed:
        Cap on the unconfirmed list; defaults to ``max_ranked``.
    """
    if not hasattr(impact_result, "direct_impacts") or not hasattr(
        impact_result, "dependency_paths"
    ):
        raise TypeError(
            "impact_result must be an ImpactAnalysisResult "
            "(missing direct_impacts/dependency_paths)."
        )
    if isinstance(max_ranked, bool) or not isinstance(max_ranked, int):
        raise TypeError("max_ranked must be an integer.")
    if max_ranked < 1:
        raise ValueError("max_ranked must be >= 1.")
    if max_unconfirmed is None:
        max_unconfirmed = max_ranked
    if isinstance(max_unconfirmed, bool) or not isinstance(max_unconfirmed, int):
        raise TypeError("max_unconfirmed must be an integer.")
    if max_unconfirmed < 0:
        raise ValueError("max_unconfirmed must be >= 0.")

    confirmed_raw = _as_records(getattr(impact_result, "direct_impacts", [])) + _as_records(
        getattr(impact_result, "transitive_impacts", [])
    )
    unconfirmed_raw = _as_records(
        getattr(impact_result, "unresolved_impacts", [])
    ) + _as_records(getattr(impact_result, "ambiguous_impacts", []))

    # Defensive: a record whose classification is not confirmed must never be
    # ranked as one, even if it arrived in the wrong bucket.
    confirmed_raw = [
        record
        for record in confirmed_raw
        if _normalised(record.get("classification", "")) in _CONFIRMED_CLASSIFICATIONS
    ]
    unconfirmed_raw = [
        record
        for record in unconfirmed_raw
        if _normalised(record.get("classification", "")) in _UNCONFIRMED_CLASSIFICATIONS
    ]

    ranked_all = _rank_bucket(confirmed_raw, confirmed=True)
    unconfirmed_all = _rank_bucket(unconfirmed_raw, confirmed=False)

    confirmed_truncated = len(ranked_all) > max_ranked
    unconfirmed_truncated = len(unconfirmed_all) > max_unconfirmed
    ranked = ranked_all[:max_ranked]
    unconfirmed = unconfirmed_all[:max_unconfirmed]

    tier_distribution = {tier.value: 0 for tier in PriorityTier}
    for ranked_impact in ranked:
        tier_distribution[ranked_impact.priority_tier.value] += 1

    metadata: Dict[str, Any] = {
        "ranking_version": 1,
        "policy_version": RANKING_POLICY_VERSION,
        "calibration_note": CALIBRATION_NOTE,
        "factor_weights": dict(FACTOR_WEIGHTS),
        "tier_bands": [[lower, name] for lower, name in TIER_BANDS],
        "max_ranked": max_ranked,
        "max_unconfirmed": max_unconfirmed,
        "confirmed_components_found": len(ranked_all),
        "confirmed_components_returned": len(ranked),
        "unconfirmed_components_found": len(unconfirmed_all),
        "unconfirmed_components_returned": len(unconfirmed),
        "confirmed_truncated": confirmed_truncated,
        "unconfirmed_truncated": unconfirmed_truncated,
        "tier_distribution": tier_distribution,
        "confirmed_impact_record_count": len(confirmed_raw),
        "unconfirmed_impact_record_count": len(unconfirmed_raw),
        "analysis_truncated": bool(
            getattr(impact_result, "traversal_metadata", {}).get("truncated", False)
        ),
        # UNMAPPED changes are not impacts and are never ranked; the count is
        # surfaced here so a consumer can still see that they exist.
        "unmapped_change_count": len(getattr(impact_result, "unmapped_changes", [])),
        "unmapped_changes_ranked": False,
        "navigation_only": True,
        "runtime_impact_verified": False,
    }

    return ImpactRankingResult(
        ranked_impacts=ranked,
        unconfirmed_impacts=unconfirmed,
        ranking_metadata=metadata,
        confidence_limitations=[
            RUNTIME_FAILURE_DISCLAIMER,
            "Unresolved and ambiguous relationships are ranked separately and are "
            "never presented as confirmed impacts.",
        ],
    )


def create_ranking_service() -> Any:
    """Return the ranking callable, for dependency-injection symmetry."""
    return rank_impacts


def summarize_ranking(impact_result: Any, *, max_ranked: int = 10) -> Dict[str, Any]:
    """Compact ranking view: top components, tiers and metadata."""
    result = rank_impacts(impact_result, max_ranked=max_ranked)
    return {
        "top_impacts": [
            {
                "node_id": ranked.node_id,
                "node_name": ranked.record.get("node_name"),
                "priority_tier": ranked.priority_tier.value,
                "priority_score": ranked.priority_score,
                "depth": ranked.depth,
                "independent_path_count": ranked.independent_path_count,
                "explanation": ranked.explanation,
            }
            for ranked in result.ranked_impacts
        ],
        "unconfirmed": [
            {
                "node_id": ranked.node_id,
                "node_name": ranked.record.get("node_name"),
                "classification": ranked.classification,
                "priority_score": ranked.priority_score,
            }
            for ranked in result.unconfirmed_impacts
        ],
        "ranking_metadata": result.ranking_metadata,
        "confidence_limitations": result.confidence_limitations,
    }
