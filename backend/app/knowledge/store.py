"""Read side of ``.kitehut/``: load the graph, check freshness, read pages."""

from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional

from app.code_graph.discovery import RepositoryScanner
from app.code_graph.models import CodeGraph, GraphBuildStatus
from app.code_graph.serialization import deserialize_graph_json
from app.knowledge.errors import KiteHutError, KnowledgeNotFoundError
from app.knowledge.metadata import (
    FreshnessReport,
    KnowledgeMetadata,
    diff_file_hashes,
    read_metadata,
)
from app.knowledge.safe_io import OutputLayout, resolve_repository_root

MAX_PAGE_BYTES = 200_000


def current_file_hashes(root: Path, exclude_dirs=(), max_files: int = 10_000):
    """Scan (read-only) and return ``(hashes, source_digest, scan_ms)``."""
    started = time.perf_counter()
    result = RepositoryScanner(root, exclude_dirs=exclude_dirs, max_files=max_files).scan()
    elapsed = (time.perf_counter() - started) * 1000
    if result.status == GraphBuildStatus.FAILED or result.graph is None:
        msgs = "; ".join(e.message for e in result.errors) or "scan failed"
        raise KiteHutError(f"Could not scan repository: {msgs}")
    hashes = {f.relative_path: f.content_hash for f in result.graph.files}
    return hashes, result.graph.source_digest, elapsed


def check_freshness(path) -> FreshnessReport:
    """Compare stored metadata with the current working tree (read-only)."""
    root = resolve_repository_root(path)
    layout = OutputLayout(root)
    try:
        meta = read_metadata(layout)
    except KnowledgeNotFoundError as exc:
        state = "not_analyzed" if not layout.metadata_file.exists() else "invalid"
        return FreshnessReport(state=state, message=str(exc))
    if not layout.graph_file.is_file() or layout.graph_file.is_symlink():
        return FreshnessReport(
            state="invalid",
            message="metadata.json exists but graph/repository.json is missing. Run 'kitehut refresh <path>'.",
            generated_at=meta.generated_at,
            generation_status=meta.generation_status,
        )
    hashes, digest, _ = current_file_hashes(root, meta.exclude_dirs, meta.max_files)
    added, removed, modified = diff_file_hashes(meta.file_hashes, hashes)
    from app.knowledge.constants import KITEHUT_VERSION

    mismatch = meta.kitehut_version != KITEHUT_VERSION
    if digest == meta.source_digest and not (added or removed or modified):
        return FreshnessReport(
            state="fresh", message="Knowledge is up to date with the source tree.",
            generated_at=meta.generated_at, generation_status=meta.generation_status,
            kitehut_version_mismatch=mismatch,
        )
    return FreshnessReport(
        state="stale",
        message=(f"Source changed since generation: {len(added)} added, {len(removed)} removed, "
                 f"{len(modified)} modified. Run 'kitehut refresh <path>'."),
        generated_at=meta.generated_at, generation_status=meta.generation_status,
        added=added, removed=removed, modified=modified, kitehut_version_mismatch=mismatch,
    )


# Parsed graphs, keyed by (root, mtime_ns, size) of repository.json.  A refresh rewrites the
# file, which changes the key, so a cached graph is never served after regeneration.  Bounded.
_GRAPH_CACHE: "OrderedDict[tuple, CodeGraph]" = OrderedDict()
_GRAPH_CACHE_MAX = 3


def load_graph(path) -> CodeGraph:
    """Load and validate ``.kitehut/graph/repository.json`` (cached per file version)."""
    root = resolve_repository_root(path)
    layout = OutputLayout(root)
    gf = layout.graph_file
    if gf.is_symlink() or not gf.is_file():
        raise KnowledgeNotFoundError(
            "No graph found at .kitehut/graph/repository.json. Run 'kitehut analyze <path>' first."
        )
    try:
        st = gf.stat()
        cache_key = (str(root), st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _GRAPH_CACHE:
        _GRAPH_CACHE.move_to_end(cache_key)
        return _GRAPH_CACHE[cache_key]
    try:
        text = gf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KnowledgeNotFoundError(f"Graph file is unreadable: {exc}") from exc
    graph, validation = deserialize_graph_json(text)
    if graph is None:
        detail = "; ".join(d.message for d in validation.errors[:2])
        raise KnowledgeNotFoundError(
            f"Stored graph is invalid or from an incompatible version ({detail}). "
            "Run 'kitehut refresh <path>' to regenerate it."
        )
    # root_path is runtime-only (never serialized); re-attach the checkout we loaded from
    # so evidence code can confirm the graph belongs to this repository.
    graph.repository.root_path = str(root)
    if cache_key is not None:
        _GRAPH_CACHE[cache_key] = graph
        while len(_GRAPH_CACHE) > _GRAPH_CACHE_MAX:
            _GRAPH_CACHE.popitem(last=False)
    return graph


def load_metadata(path) -> KnowledgeMetadata:
    return read_metadata(OutputLayout(resolve_repository_root(path)))


def list_knowledge_pages(path) -> List[str]:
    """Relative paths (to ``knowledge/``) of all generated Markdown pages."""
    layout = OutputLayout(resolve_repository_root(path))
    base = layout.knowledge_dir
    if base.is_symlink() or not base.is_dir():
        return []
    pages = []
    for p in sorted(base.rglob("*.md")):
        if p.is_symlink() or not p.is_file():
            continue
        pages.append(p.relative_to(base).as_posix())
    return pages


def read_knowledge_page(path, relative: str = "INDEX.md") -> str:
    """Read one generated page, refusing traversal, symlinks and non-Markdown."""
    layout = OutputLayout(resolve_repository_root(path))
    rel = (relative or "").replace("\\", "/").strip()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or rel.startswith("/") or ".." in parts or not parts[-1].endswith(".md"):
        raise KiteHutError("Page must be a relative .md path inside .kitehut/knowledge (no '..').")
    target = layout.knowledge_dir.joinpath(*parts)
    cur = layout.base
    for part in ("knowledge", *parts):
        cur = cur / part
        if cur.is_symlink():
            raise KiteHutError("Refusing to read through a symbolic link.")
    base_real = layout.knowledge_dir.resolve()
    if not target.is_file() or base_real not in target.resolve().parents:
        raise KnowledgeNotFoundError(f"Knowledge page not found: {rel}")
    if target.stat().st_size > MAX_PAGE_BYTES:
        raise KiteHutError(f"Page is larger than {MAX_PAGE_BYTES} bytes; refusing to load it.")
    return target.read_text(encoding="utf-8")
