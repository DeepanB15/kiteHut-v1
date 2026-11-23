"""Evidence Engine adapter: source-backed spans with freshness checks.

Thin, serializable wrapper over :mod:`app.code_graph.evidence` and the
existing deterministic :class:`VerificationEngine`.  The graph only *locates*
code; every span is re-read from the working tree, hashed, and compared with
the graph snapshot.  Spans that no longer match are flagged ``stale`` and never
presented as support.  Nothing here fabricates a path or line number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.code_graph.evidence import (
    EvidenceStatus,
    SourceSpanEvidence,
    collect_graph_evidence,
    verify_claim_with_graph,
)
from app.knowledge.redact import redact_secrets
from app.knowledge.safe_io import resolve_repository_root
from app.knowledge.store import load_graph
from app.verification.models.claim import ClaimType, ExtractedClaim

MAX_SNIPPET_CHARS = 1200


def _span_dict(span: SourceSpanEvidence) -> Dict[str, Any]:
    content = redact_secrets(span.content or "")
    return {
        "path": span.relative_path,
        "start_line": span.line_start,
        "end_line": span.line_end,
        "reference": f"{span.relative_path}:{span.line_start}-{span.line_end}",
        "symbol": span.symbol_name,
        "relationship": span.relationship,
        "content_hash": span.content_hash,
        "file_content_hash": span.file_content_hash,
        "file_exists": span.file_exists,
        "missing": span.missing,
        "stale": span.stale,
        "stale_reason": span.stale_reason,
        "ambiguous": span.ambiguous,
        "snippet": content[:MAX_SNIPPET_CHARS] + ("…" if len(content) > MAX_SNIPPET_CHARS else ""),
    }


@dataclass
class EvidenceReport:
    statement: str
    spans: List[Dict[str, Any]] = field(default_factory=list)
    status: Optional[str] = None          # SUPPORTED | UNSUPPORTED | INCONCLUSIVE (when verified)
    reasons: List[str] = field(default_factory=list)
    stale_count: int = 0
    missing_count: int = 0
    graph_used: bool = False
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "statement": self.statement, "status": self.status, "reasons": self.reasons,
            "graph_used": self.graph_used, "stale_count": self.stale_count,
            "missing_count": self.missing_count, "elapsed_ms": self.elapsed_ms, "spans": self.spans,
        }


def _claim(statement: str, claim_type: Optional[str], target_file: Optional[str],
           target_line: Optional[int], expected_pattern: Optional[str]) -> ExtractedClaim:
    if claim_type:
        ctype = ClaimType(claim_type)
    elif expected_pattern:
        ctype = ClaimType.CODE_CONTAINS_PATTERN
    elif target_file and not target_line:
        ctype = ClaimType.FILE_EXISTS
    else:
        ctype = ClaimType.AST_SYMBOL_EXISTS
    return ExtractedClaim(claim_type=ctype, statement=statement, target_file=target_file,
                          target_line=target_line, expected_pattern=expected_pattern)


def collect_evidence(path, statement: str, *, claim_type: Optional[str] = None, target_file: Optional[str] = None,
                     target_line: Optional[int] = None, expected_pattern: Optional[str] = None,
                     depth: int = 1, verify: bool = False, graph=None) -> EvidenceReport:
    """Collect (and optionally verify) source evidence for ``statement``."""
    started = time.perf_counter()
    root = resolve_repository_root(path)
    g = graph if graph is not None else load_graph(root)
    claim = _claim(statement, claim_type, target_file, target_line, expected_pattern)
    report = EvidenceReport(statement=statement)
    if verify:
        outcome = verify_claim_with_graph(claim, root, graph=g, depth=depth)
        bundle = outcome.bundle
        report.status = outcome.status.value if isinstance(outcome.status, EvidenceStatus) else str(outcome.status)
        report.reasons = list(outcome.reasons)
    else:
        bundle = collect_graph_evidence(claim, root, graph=g, depth=depth)
    report.spans = [_span_dict(s) for s in bundle.spans]
    report.stale_count = len(bundle.stale_spans)
    report.missing_count = sum(1 for s in bundle.spans if s.missing)
    report.graph_used = bool(bundle.diagnostics.get("graph_valid"))
    report.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return report
