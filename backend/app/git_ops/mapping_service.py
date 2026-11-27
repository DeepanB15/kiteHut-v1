from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from enum import Enum


class EvidenceConfidence(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    FILE_LEVEL_ONLY = "file_level_only"


@dataclass
class MappingResult:
    """Result of Phase 3C file-to-symbol mapping."""

    # Maps file paths to sets of symbol node IDs that were mapped
    file_to_symbols: Dict[str, Set[str]] = field(default_factory=dict)

    # Maps symbol node IDs to their mapping confidence
    symbol_confidence: Dict[str, EvidenceConfidence] = field(default_factory=dict)

    # Whether all files in the repository have been fully mapped
    fully_mapped: bool = False

    # Any warnings or errors encountered during mapping
    warnings: List[str] = field(default_factory=list)


class FileMapper:
    """Phase 3C: File path normalization and graph lookup service.

    Responsibilities:
    - Normalize file paths across operating systems
    - Look up graph nodes by file path
    - Report deleted/unmapped files
    - Track renames
    - Provide symbol mapping with confidence marking
    """

    def __init__(self, mapping_result: MappingResult = None):
        self._mapping_result = mapping_result or MappingResult()
        # Internal cache: norm_path -> set of symbol node IDs
        self._path_to_symbols: Dict[str, Set[str]] = {}
        # Internal cache: norm_path -> FileNode
        self._path_to_file: Dict[str, Any] = {}

    def normalize_path(self, path: str) -> str:
        """Normalize a file path to forward slashes and remove duplicates."""
        return path.replace("\\", "/").replace("//", "/")

    def register_file(self, file_node: Any, symbol_map: Optional[Dict[str, str]] = None):
        """Register a file node with its symbol mappings.

        Args:
            file_node: The FileNode from the CodeGraph
            symbol_map: Optional dict mapping symbol names to node IDs
        """
        norm_path = self.normalize_path(file_node.relative_path)
        self._path_to_file[norm_path] = file_node

        if symbol_map:
            if norm_path not in self._path_to_symbols:
                self._path_to_symbols[norm_path] = set()
            # The impact engine reads ``file_to_symbols`` (keyed by path, as a
            # collection of symbol IDs) to decide between exact and
            # file-level-only confidence for a *file* impact.  Without this
            # write, a fully symbol-mapped file was still reported as
            # ``file_level_only``, so the confidence was never truthful.
            file_symbols = self._mapping_result.file_to_symbols.setdefault(
                norm_path, set()
            )
            for symbol_name, symbol_id in symbol_map.items():
                self._path_to_symbols[norm_path].add(symbol_id)
                file_symbols.add(symbol_id)
                # Record mapping confidence
                if symbol_id not in self._mapping_result.symbol_confidence:
                    self._mapping_result.symbol_confidence[symbol_id] = EvidenceConfidence.EXACT

    def lookup_file(self, path: str) -> Optional[Any]:
        """Look up a FileNode by normalized path."""
        norm_path = self.normalize_path(path)
        return self._path_to_file.get(norm_path)

    def lookup_symbols(self, path: str) -> Set[str]:
        """Look up symbol node IDs for a given file path."""
        norm_path = self.normalize_path(path)
        return self._path_to_symbols.get(norm_path, set())

    def is_mapped(self, path: str) -> bool:
        """Check if a file path has been mapped to graph nodes."""
        norm_path = self.normalize_path(path)
        return norm_path in self._path_to_file

    def get_mapping_result(self) -> MappingResult:
        """Get the full mapping result."""
        return self._mapping_result

    def report_unmapped(self, path: str, reason: str = ""):
        """Report a file as unmapped."""
        norm_path = self.normalize_path(path)
        if norm_path not in self._path_to_file:
            self._mapping_result.warnings.append(
                f"File not mapped: {path} - {reason}"
            )

    def report_renamed(self, old_path: str, new_path: str):
        """Track a file rename."""
        old_norm = self.normalize_path(old_path)
        new_norm = self.normalize_path(new_path)
        # Move any existing mappings from old to new
        if old_norm in self._path_to_file:
            self._path_to_file[new_norm] = self._path_to_file.pop(old_norm)
            # Also move symbol mappings
            if old_norm in self._path_to_symbols:
                self._path_to_symbols[new_norm] = self._path_to_symbols.pop(old_norm)
