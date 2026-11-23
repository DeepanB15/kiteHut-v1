"""Smart Context Engine: graph-guided, budget-aware context assembly.

Candidate discovery reuses the existing graph-guided retrieval
(:func:`app.code_graph.integration.discover_graph_candidates`) and adds lexical
matching over symbol names and paths so natural-language queries work too.
Ranked candidates are de-duplicated (no nested/overlapping spans), checked for
source freshness against the graph's content hashes, and packed into a token
budget.  Every item carries its path and line range.

Honest scope for v0.1.0: this is deterministic lexical + graph-structure
ranking.  It does not use embeddings, and it is not wired into the legacy
vector/hybrid retrieval pipeline (``app.retrieval`` / ``app.rag``).
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.code_graph.integration import discover_graph_candidates
from app.code_graph.models import CodeGraph, SymbolKind, SymbolNode
from app.core.path_guard import PathContainmentError, resolve_repository_path
from app.knowledge.index import GraphIndex, kind_label
from app.knowledge.redact import redact_secrets
from app.knowledge.safe_io import resolve_repository_root
from app.knowledge.store import load_graph

CHARS_PER_TOKEN = 4  # documented heuristic; not a real tokenizer
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = frozenset(
    "a an and are as at be by do does for from how in is it of on or that the this to what where which with why "
    "who when used use uses code file files function functions class classes method methods module modules "
    "show me find get give explain work works".split()
)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (``chars / 4``, rounded up). An estimate, not a count."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


@dataclass
class ContextItem:
    path: str
    start_line: int
    end_line: int
    symbol: Optional[str]
    kind: str
    score: float
    reasons: List[str]
    source_verified: bool
    tokens: int
    truncated: bool
    text: str

    @property
    def reference(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class ContextBundle:
    query: str
    token_budget: int
    tokens_used: int = 0
    items: List[ContextItem] = field(default_factory=list)
    candidates_considered: int = 0
    duplicates_removed: int = 0
    skipped_stale: List[str] = field(default_factory=list)
    skipped_over_budget: int = 0
    retrieval_ms: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for item, src in zip(d["items"], self.items):
            item["reference"] = src.reference
        return d

    def to_markdown(self) -> str:
        lines = [f"# Context for: {self.query}", "",
                 f"{len(self.items)} item(s), ~{self.tokens_used} of {self.token_budget} estimated tokens.", ""]
        for it in self.items:
            lines += [f"## {it.symbol or it.path} ({it.kind}) — `{it.reference}`",
                      f"_score {it.score:.2f}; {', '.join(it.reasons)}_", "", "```python", it.text.rstrip("\n"), "```", ""]
        lines += [f"> {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


def _split_words(text: str) -> List[str]:
    out: List[str] = []
    for chunk in _WORD_RE.findall(text):
        parts = _CAMEL_RE.findall(chunk) or [chunk]
        out.extend(p.lower() for p in parts if len(p) > 1 or p.isdigit())
    return out


def _query_terms(query: str) -> Tuple[List[str], List[str]]:
    """(lowercase word terms without stopwords, original identifier-like tokens)."""
    words = [w for w in _split_words(query) if w not in _STOP]
    idents = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query) if t.lower() not in _STOP and len(t) > 1]
    return list(dict.fromkeys(words)), list(dict.fromkeys(idents))


def _score_symbol(sym: SymbolNode, words: List[str], idents: List[str]) -> Tuple[float, List[str]]:
    score, reasons = 0.0, []
    if sym.name in idents:
        score += 10
        reasons.append("exact name match")
    name_words = set(_split_words(sym.name))
    path_words = set(_split_words(sym.relative_path))
    qual_words = set(_split_words(sym.qualified_name))
    hit_name = [w for w in words if w in name_words]
    hit_qual = [w for w in words if w in qual_words and w not in name_words]
    hit_path = [w for w in words if w in path_words and w not in name_words and w not in qual_words]
    if hit_name:
        score += 3.0 * len(hit_name)
        reasons.append("name words: " + ",".join(hit_name))
    if hit_qual:
        score += 1.5 * len(hit_qual)
        reasons.append("qualified-name words: " + ",".join(hit_qual))
    if hit_path:
        score += 1.0 * len(hit_path)
        reasons.append("path words: " + ",".join(hit_path))
    return score, reasons


def retrieve_context(
    path,
    query: str,
    *,
    token_budget: int = 4000,
    max_items: int = 15,
    depth: int = 1,
    graph: Optional[CodeGraph] = None,
) -> ContextBundle:
    """Retrieve ranked, de-duplicated, freshness-checked source for ``query``."""
    started = time.perf_counter()
    root = resolve_repository_root(path)
    g = graph if graph is not None else load_graph(root)
    idx = GraphIndex.from_graph(g)
    bundle = ContextBundle(query=query, token_budget=max(0, int(token_budget)))
    words, idents = _query_terms(query)
    if not words and not idents:
        bundle.notes.append("Query had no searchable terms.")
        bundle.retrieval_ms = round((time.perf_counter() - started) * 1000, 2)
        return bundle

    scores: Dict[str, float] = {}
    reasons: Dict[str, List[str]] = {}
    symbols = idx.declared_symbols()
    for sym in symbols:
        sc, why = _score_symbol(sym, words, idents)
        if sc > 0:
            scores[sym.id], reasons[sym.id] = sc, why

    # Graph guidance: callers/callees of the strongest direct hits (decayed).
    seeds = sorted(scores, key=lambda i: (-scores[i], i))[:3]
    for seed in seeds:
        for table, label in ((idx.calls_out, "callee of top hit"), (idx.calls_in, "caller of top hit")):
            for other, _edge in table.get(seed, ())[:8]:
                if other.kind == SymbolKind.MODULE:
                    continue
                scores[other.id] = scores.get(other.id, 0.0) + 2.0 / max(1, depth)
                reasons.setdefault(other.id, []).append(label)

    # Reuse the existing graph-guided retrieval for identifier-named seeds.
    by_loc = {(s.relative_path, s.start_line): s for s in symbols}
    for cand in discover_graph_candidates(query, g, depth=depth):
        sym = by_loc.get((cand.get("file_path"), cand.get("start_line")))
        if sym is not None and sym.kind != SymbolKind.MODULE:
            scores[sym.id] = scores.get(sym.id, 0.0) + 1.0 + float(cand.get("graph_score") or 0)
            reasons.setdefault(sym.id, []).append("graph-guided candidate")

    ranked = sorted(
        (idx.symbols_by_id[i] for i in scores),
        key=lambda s: (-scores[s.id], s.relative_path, s.start_line, s.id),
    )
    bundle.candidates_considered = len(ranked)
    if not ranked:
        bundle.notes.append("No symbols matched the query.")

    selected: Dict[str, List[Tuple[int, int]]] = {}
    texts: Dict[str, Optional[List[str]]] = {}
    verified: Dict[str, bool] = {}

    def file_lines(rel: str) -> Optional[List[str]]:
        if rel in texts:
            return texts[rel]
        texts[rel], verified[rel] = None, False
        try:
            full = resolve_repository_path(root, rel)
            raw = Path(full).read_bytes()
        except (PathContainmentError, OSError, ValueError):
            return None
        node = idx.files_by_path.get(rel)
        verified[rel] = bool(node and hashlib.sha256(raw).hexdigest() == node.content_hash)
        texts[rel] = raw.decode("utf-8", errors="replace").splitlines() if verified[rel] else None
        return texts[rel]

    for sym in ranked:
        if len(bundle.items) >= max_items:
            break
        spans = selected.setdefault(sym.relative_path, [])
        if any(not (sym.end_line < a or sym.start_line > b) for a, b in spans):
            bundle.duplicates_removed += 1
            continue
        lines = file_lines(sym.relative_path)
        if lines is None:
            if sym.relative_path not in bundle.skipped_stale:
                bundle.skipped_stale.append(sym.relative_path)
            continue
        snippet = redact_secrets("\n".join(lines[sym.start_line - 1: sym.end_line])) + "\n"
        tokens, truncated = estimate_tokens(snippet), False
        remaining = bundle.token_budget - bundle.tokens_used
        if tokens > remaining:
            if bundle.items or remaining < 20:
                bundle.skipped_over_budget += 1
                continue
            keep = max(1, remaining * CHARS_PER_TOKEN)  # first item only: truncate rather than return nothing
            snippet, tokens, truncated = snippet[:keep].rsplit("\n", 1)[0] + "\n", estimate_tokens(snippet[:keep]), True
        spans.append((sym.start_line, sym.end_line))
        bundle.items.append(ContextItem(
            path=sym.relative_path, start_line=sym.start_line, end_line=sym.end_line, symbol=sym.qualified_name,
            kind=kind_label(sym.kind), score=round(scores[sym.id], 3), reasons=list(dict.fromkeys(reasons[sym.id])),
            source_verified=True, tokens=tokens, truncated=truncated, text=snippet,
        ))
        bundle.tokens_used += tokens

    if bundle.skipped_stale:
        bundle.notes.append("Skipped stale/unreadable files (source changed since analysis): "
                            + ", ".join(sorted(bundle.skipped_stale)) + ". Run 'kitehut refresh'.")
    if bundle.skipped_over_budget:
        bundle.notes.append(f"{bundle.skipped_over_budget} candidate(s) skipped to stay within the token budget.")
    bundle.notes.append("Token counts are estimates (chars/4), not model-specific counts.")
    bundle.retrieval_ms = round((time.perf_counter() - started) * 1000, 2)
    return bundle
