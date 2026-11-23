"""Constants shared by the ``.kitehut/`` knowledge output layer."""

from __future__ import annotations

from app import __version__ as KITEHUT_VERSION

#: Name of the generated output folder inside a target repository.
OUTPUT_DIR_NAME = ".kitehut"

#: Version of the ``metadata.json`` / knowledge output layout.
KNOWLEDGE_SCHEMA_VERSION = 1

#: Marker embedded in every file KiteHut generates.  KiteHut only ever
#: overwrites or prunes files that carry this marker, so user-authored files
#: that happen to live in ``.kitehut/`` are never touched.
GENERATED_MARKER = "kitehut:generated"

#: How many leading bytes of an existing file are inspected for the marker.
MARKER_SCAN_BYTES = 4096

#: Output parts a command may generate.
PART_GRAPH = "graph"
PART_KNOWLEDGE = "knowledge"
PART_VISUALIZATION = "visualization"
ALL_PARTS = (PART_GRAPH, PART_KNOWLEDGE, PART_VISUALIZATION)

#: Defaults that keep output bounded on large repositories.
DEFAULT_MAX_MODULE_PAGES = 500
DEFAULT_MAX_SYMBOL_PAGES = 1500
DEFAULT_MAX_VIZ_NODES = 2000

#: Generation statuses recorded in ``metadata.json``.
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

__all__ = [name for name in dir() if name.isupper()]
