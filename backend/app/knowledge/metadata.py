"""``.kitehut/metadata.json``: generation facts and freshness checking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from app.code_graph.models import CODE_GRAPH_SCHEMA_VERSION
from app.knowledge.constants import (
    GENERATED_MARKER,
    KITEHUT_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
)
from app.knowledge.errors import KnowledgeNotFoundError
from app.knowledge.safe_io import OutputLayout


class KnowledgeMetadata(BaseModel):
    """Facts recorded for one generation run.

    The absolute repository path is deliberately **not** stored, so the
    ``.kitehut/`` folder stays portable between machines and checkouts.
    """

    generator: str = GENERATED_MARKER  # doubles as the generated-file marker
    kitehut_version: str = KITEHUT_VERSION
    knowledge_schema_version: int = KNOWLEDGE_SCHEMA_VERSION
    graph_schema_version: int = CODE_GRAPH_SCHEMA_VERSION
    generated_at: str
    repository_name: str
    repository_id: str
    file_count: int = 0
    symbol_count: int = 0
    relationship_count: int = 0
    unresolved_import_count: int = 0
    unresolved_call_count: int = 0
    source_digest: str
    # path -> sha256 of file bytes at generation time (drives stale detection)
    file_hashes: Dict[str, str] = Field(default_factory=dict)
    generation_status: str
    truncated: bool = False
    diagnostics: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    # Scan options used, so freshness checks rescan exactly the same file set.
    exclude_dirs: List[str] = Field(default_factory=list)
    max_files: int = 10000
    timings_ms: Dict[str, float] = Field(default_factory=dict)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def metadata_to_json(meta: KnowledgeMetadata) -> str:
    """Deterministic JSON with the ``generator`` marker first.

    The marker must sit within the first bytes of the file: KiteHut only
    overwrites files it can recognise as its own, and sorted keys would
    otherwise push it behind the (large) ``file_hashes`` map.
    """
    data = meta.model_dump()
    ordered = {"generator": data.pop("generator")}
    ordered.update({k: data[k] for k in sorted(data)})
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


def read_metadata(layout: OutputLayout) -> KnowledgeMetadata:
    """Load and validate metadata, raising :class:`KnowledgeNotFoundError`."""
    path = layout.metadata_file
    if path.is_symlink() or not path.is_file():
        raise KnowledgeNotFoundError(
            "No KiteHut knowledge found for this repository. Run 'kitehut analyze <path>' first."
        )
    try:
        return KnowledgeMetadata.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise KnowledgeNotFoundError(
            f"KiteHut metadata is unreadable or invalid ({exc.__class__.__name__}). "
            "Run 'kitehut refresh <path>' or 'kitehut analyze <path>' to regenerate it."
        ) from exc


@dataclass
class FreshnessReport:
    """Result of comparing stored metadata with the current working tree."""

    state: str  # "fresh" | "stale" | "not_analyzed" | "invalid"
    message: str
    generated_at: Optional[str] = None
    generation_status: Optional[str] = None
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    kitehut_version_mismatch: bool = False

    @property
    def is_fresh(self) -> bool:
        return self.state == "fresh"

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "generated_at": self.generated_at,
            "generation_status": self.generation_status,
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "kitehut_version_mismatch": self.kitehut_version_mismatch,
        }


def diff_file_hashes(old: Dict[str, str], new: Dict[str, str]):
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    return added, removed, modified
