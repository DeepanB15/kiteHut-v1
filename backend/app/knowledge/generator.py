"""Orchestrates generation of the ``.kitehut/`` knowledge folder.

Pipeline: scan + build graph (read-only) → validate → write graph JSON →
Markdown knowledge → HTML visualization → metadata (written last, so a
complete ``metadata.json`` means the run finished).

Nothing is written when the graph build fails.  Source files are never
touched; see :mod:`app.knowledge.safe_io` for the write guarantees.
"""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.code_graph.builder import CodeGraphBuilder
from app.code_graph.discovery import DEFAULT_MAX_FILES, RepositoryScanner
from app.code_graph.models import CodeGraph, GraphBuildStatus
from app.code_graph.serialization import write_graph_file
from app.code_graph.validation import validate_graph
from app.knowledge.constants import (
    ALL_PARTS,
    DEFAULT_MAX_MODULE_PAGES,
    DEFAULT_MAX_SYMBOL_PAGES,
    DEFAULT_MAX_VIZ_NODES,
    KITEHUT_VERSION,
    PART_GRAPH,
    PART_KNOWLEDGE,
    PART_VISUALIZATION,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
)
from app.knowledge.errors import KiteHutError, KnowledgeNotFoundError
from app.knowledge.markdown import MarkdownOptions, generate_knowledge
from app.knowledge.metadata import (
    KnowledgeMetadata,
    metadata_to_json,
    read_metadata,
    utc_now_iso,
)
from app.knowledge.safe_io import (
    OutputLayout,
    atomic_write_text,
    prune_stale_generated,
    resolve_repository_root,
)
from app.knowledge.visualize import VisualizationOptions, render_html

logger = logging.getLogger("kitehut.knowledge")


@dataclass
class GenerationOptions:
    parts: Tuple[str, ...] = ALL_PARTS
    exclude_dirs: Tuple[str, ...] = ()
    max_files: int = DEFAULT_MAX_FILES
    max_module_pages: int = DEFAULT_MAX_MODULE_PAGES
    max_symbol_pages: int = DEFAULT_MAX_SYMBOL_PAGES
    max_viz_nodes: int = DEFAULT_MAX_VIZ_NODES
    progress: Optional[Callable[[str], None]] = None


@dataclass
class GenerationResult:
    ok: bool
    status: str  # complete | partial | failed
    root: Path
    output_dir: Path
    metadata: Optional[KnowledgeMetadata] = None
    previous: Optional[KnowledgeMetadata] = None
    graph: Optional[CodeGraph] = None
    written: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    timings_ms: Dict[str, float] = field(default_factory=dict)
    up_to_date: bool = False  # refresh found nothing to do


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _diag_text(d) -> str:
    where = f" ({d.relative_path}" + (f":{d.line}" if getattr(d, "line", None) else "") + ")" if getattr(d, "relative_path", None) else ""
    return f"{d.code}: {d.message}{where}"


def generate(path, options: Optional[GenerationOptions] = None) -> GenerationResult:
    """Generate the requested ``.kitehut/`` parts for the repository at ``path``."""
    opts = options or GenerationOptions()
    say = opts.progress or (lambda _msg: None)
    parts = tuple(dict.fromkeys(opts.parts))
    unknown = [p for p in parts if p not in ALL_PARTS]
    if unknown or PART_GRAPH not in parts:
        raise KiteHutError(f"Invalid output parts {list(parts)}; the graph is always generated.")

    root = resolve_repository_root(path)
    layout = OutputLayout(root)
    result = GenerationResult(ok=False, status="failed", root=root, output_dir=layout.base)
    total_start = time.perf_counter()

    try:
        result.previous = read_metadata(layout)
    except KnowledgeNotFoundError:
        result.previous = None

    # 1. Build graph (read-only) -------------------------------------------------
    say("Scanning repository and building code graph")
    t = time.perf_counter()
    scanner = RepositoryScanner(root, exclude_dirs=opts.exclude_dirs, max_files=opts.max_files)
    with warnings.catch_warnings():
        # Parsing the *target's* source can emit SyntaxWarnings (e.g. bad escapes); they are not ours to report.
        warnings.simplefilter("ignore")
        build = CodeGraphBuilder(root, scanner=scanner).build()
    result.timings_ms["graph_build_ms"] = _ms(t)
    if build.status == GraphBuildStatus.FAILED or build.graph is None:
        result.errors = [_diag_text(d) for d in build.errors] or ["Graph build failed."]
        result.timings_ms["total_ms"] = _ms(total_start)
        return result
    graph = build.graph
    result.graph = graph
    result.warnings = [_diag_text(d) for d in build.warnings[:20]]
    if build.truncated:
        result.warnings.append(f"File limit reached: only the first {opts.max_files} files were analyzed.")
    if not graph.files:
        result.warnings.append("No Python files were discovered; generated output is empty.")
    partial_errors = [_diag_text(d) for d in build.errors]
    result.warnings += partial_errors[:20]

    validation = validate_graph(graph)
    if not validation.valid:
        result.errors = [d.message for d in validation.errors[:5]] or ["Graph validation failed."]
        result.timings_ms["total_ms"] = _ms(total_start)
        return result

    # 2. Prepare the output folder (refuses symlinks / escapes) -------------------
    layout.prepare()

    # 3. Graph JSON ---------------------------------------------------------------
    say("Writing graph/repository.json")
    t = time.perf_counter()
    layout.safe_target(layout.graph_file)
    ok, diags = write_graph_file(graph, layout.graph_file, validate=True)
    result.timings_ms["write_graph_ms"] = _ms(t)
    if not ok:
        result.errors = [_diag_text(d) for d in diags]
        result.timings_ms["total_ms"] = _ms(total_start)
        return result
    result.written.append(layout.graph_file.relative_to(root).as_posix())

    status = STATUS_COMPLETE if build.status == GraphBuildStatus.COMPLETE and not build.truncated else STATUS_PARTIAL
    md_opts = MarkdownOptions(
        max_module_pages=opts.max_module_pages,
        max_symbol_pages=opts.max_symbol_pages,
        unresolved_import_count=len(build.unresolved_imports),
        unresolved_call_count=len(build.unresolved_calls),
        generation_status=status,
    )

    def record(outcome) -> None:
        rel = outcome.path.relative_to(root).as_posix()
        if outcome.status == "written":
            result.written.append(rel)
        elif outcome.status == "unchanged":
            result.unchanged.append(rel)
        else:
            result.skipped.append(rel)
            result.warnings.append(f"Left existing non-KiteHut file untouched: {rel}")

    # 4. Markdown knowledge -------------------------------------------------------
    if PART_KNOWLEDGE in parts:
        say("Generating Markdown knowledge")
        t = time.perf_counter()
        docs = generate_knowledge(graph, md_opts)
        keep: Dict[str, set] = {"modules": set(), "symbols": set()}
        for rel, text in docs.pages.items():
            record(atomic_write_text(layout, layout.knowledge_dir / rel, text))
            head, _, name = rel.partition("/")
            if name:
                keep[head].add(name)
        for sub, directory in (("modules", layout.modules_dir), ("symbols", layout.symbols_dir)):
            for gone in prune_stale_generated(layout, directory, keep[sub]):
                result.pruned.append(gone.relative_to(root).as_posix())
        if docs.modules_omitted or docs.symbols_omitted:
            result.warnings.append(
                f"Page limits applied: {docs.modules_omitted} module(s) and {docs.symbols_omitted} "
                "symbol(s) have no page (all remain in repository.json)."
            )
        result.timings_ms["knowledge_ms"] = _ms(t)

    # 5. HTML visualization -------------------------------------------------------
    if PART_VISUALIZATION in parts:
        say("Rendering graph.html")
        t = time.perf_counter()
        html = render_html(graph, VisualizationOptions(max_nodes=opts.max_viz_nodes))
        record(atomic_write_text(layout, layout.graph_html, html))
        result.timings_ms["visualization_ms"] = _ms(t)

    # 6. Metadata, written last ----------------------------------------------------
    outputs = set(parts)
    prev = result.previous
    if prev and prev.source_digest == graph.source_digest and prev.kitehut_version == KITEHUT_VERSION:
        outputs |= set(prev.outputs)  # same snapshot: earlier parts are still valid
    result.timings_ms["total_ms"] = _ms(total_start)
    meta = KnowledgeMetadata(
        generated_at=utc_now_iso(),
        repository_name=graph.repository.name,
        repository_id=graph.repository.repository_id,
        file_count=len(graph.files),
        symbol_count=len(graph.symbols),
        relationship_count=len(graph.edges),
        unresolved_import_count=len(build.unresolved_imports),
        unresolved_call_count=len(build.unresolved_calls),
        source_digest=graph.source_digest,
        file_hashes={f.relative_path: f.content_hash for f in graph.files},
        generation_status=status,
        truncated=build.truncated,
        diagnostics=partial_errors[:20],
        outputs=sorted(outputs),
        exclude_dirs=sorted(opts.exclude_dirs),
        max_files=opts.max_files,
        timings_ms=dict(result.timings_ms),
    )
    outcome = atomic_write_text(layout, layout.metadata_file, metadata_to_json(meta))
    if outcome.status == "skipped_user_file":
        result.errors = ["Existing .kitehut/metadata.json is not a KiteHut file; refusing to overwrite it."]
        return result
    result.metadata = meta
    result.status = status
    result.ok = True
    return result


def refresh(path, options: Optional[GenerationOptions] = None, *, force: bool = False) -> GenerationResult:
    """Regenerate knowledge after code changes, reusing the previously chosen parts.

    Requires an earlier ``analyze``.  Unless ``force`` is set, nothing is
    rewritten when the source is unchanged.
    """
    from app.knowledge.store import check_freshness

    root = resolve_repository_root(path)
    layout = OutputLayout(root)
    meta = read_metadata(layout)  # raises KnowledgeNotFoundError with a clear message
    base = options or GenerationOptions()
    # Reuse earlier scan settings unless the caller overrode them.
    if not base.exclude_dirs and meta.exclude_dirs:
        base.exclude_dirs = tuple(meta.exclude_dirs)
    if base.max_files == DEFAULT_MAX_FILES and meta.max_files != DEFAULT_MAX_FILES:
        base.max_files = meta.max_files
    parts = tuple(p for p in ALL_PARTS if p in meta.outputs) or (PART_GRAPH,)
    base.parts = parts if PART_GRAPH in parts else (PART_GRAPH, *parts)

    if not force:
        report = check_freshness(root)
        if report.is_fresh and not report.kitehut_version_mismatch:
            return GenerationResult(
                ok=True, status=meta.generation_status, root=root, output_dir=layout.base,
                metadata=meta, previous=meta, up_to_date=True,
            )
    return generate(root, base)
