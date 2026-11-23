"""KiteHut knowledge layer: ``.kitehut/`` generation, storage, context, evidence."""

from app.knowledge.constants import KITEHUT_VERSION, OUTPUT_DIR_NAME
from app.knowledge.errors import (
    KiteHutError,
    KnowledgeNotFoundError,
    RepositoryPathError,
    UnsafeOutputError,
)
from app.knowledge.generator import GenerationOptions, GenerationResult, generate, refresh
from app.knowledge.store import (
    check_freshness,
    list_knowledge_pages,
    load_graph,
    load_metadata,
    read_knowledge_page,
)

__all__ = [
    "KITEHUT_VERSION", "OUTPUT_DIR_NAME", "KiteHutError", "KnowledgeNotFoundError",
    "RepositoryPathError", "UnsafeOutputError", "GenerationOptions", "GenerationResult",
    "generate", "refresh", "check_freshness", "list_knowledge_pages", "load_graph",
    "load_metadata", "read_knowledge_page",
]
