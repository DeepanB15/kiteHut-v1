"""Structured Git Changeset Models for KiteHut Phase 3.

Defines the Changeset and ChangeUnit models for representing Git changes
as first-class data, enabling impact analysis and provenance-preserving
reporting across working-tree and base-revision comparison modes.
"""

from __future__ import annotations

from typing import Optional, Literal
from enum import Enum
from pydantic import BaseModel, Field


class ChangeStatus(str, Enum):
    """The status category of a Git change unit."""

    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"
    COPIED = "copied"
    IGNORED = "ignored"


class ChangeSource(str, Enum):
    """The source of the change detection."""

    WORKING_TREE = "working_tree"
    BASE_COMPARISON = "base_comparison"
    STAGED = "staged"


class ChangeUnit(BaseModel):
    """A single Git change unit representing one file's change.

    Each unit captures the full semantic information about a file-level change,
    including rename tracking, staging status, and optional diff content.
    """

    path: str = Field(
        description="The file path relative to the repository root."
    )
    previous_path: Optional[str] = Field(
        default=None,
        description="The old file path, if the file was renamed (Git old_a_path).",
    )
    status: ChangeStatus = Field(
        description="The high-level status category of the change."
    )
    is_staged: bool = Field(
        default=False,
        description="True if the change is staged for commit.",
    )
    is_unstaged: bool = Field(
        default=False,
        description="True if the change is unstaged (working tree).",
    )
    is_untracked: bool = Field(
        default=False,
        description="True if the file is untracked (new, not yet git-added).",
    )
    is_deleted: bool = Field(
        default=False,
        description="True if the file was deleted.",
    )
    is_renamed: bool = Field(
        default=False,
        description="True if the file was renamed (old_path -> new_path).",
    )
    diff: Optional[str] = Field(
        default=None,
        description="Optional unified diff text for this change unit.",
    )
    change_source: ChangeSource = Field(
        default=ChangeSource.WORKING_TREE,
        description="The source of the change detection.",
    )

    model_config = {
        "validate_by_name": True,
        "validate_by_alias": True,
        "protected_namespaces": (),
    }


class Changeset(BaseModel):
    """A complete Git changeset representing all changes in a repository.

    A changeset is a collection of ChangeUnit objects, each representing one
    file's change. The changeset provides consolidated view of working tree,
    staged, and base-comparison changes.
    """

    repository_root: str = Field(
        description="Path to the repository root."
    )
    units: list[ChangeUnit] = Field(
        default_factory=list,
        description="List of individual change units.",
    )
    generated_at: str = Field(
        default_factory=lambda: __import__('datetime').datetime.now().isoformat(),
        description="ISO timestamp when the changeset was generated.",
    )

    @property
    def modified(self) -> list[ChangeUnit]:
        """Return only modified change units."""
        return [u for u in self.units if u.status == ChangeStatus.MODIFIED]

    @property
    def added(self) -> list[ChangeUnit]:
        """Return only added (new) change units."""
        return [u for u in self.units if u.status == ChangeStatus.ADDED]

    @property
    def deleted(self) -> list[ChangeUnit]:
        """Return only deleted change units."""
        return [u for u in self.units if u.status == ChangeStatus.DELETED]

    @property
    def renamed(self) -> list[ChangeUnit]:
        """Return only renamed change units."""
        return [u for u in self.units if u.status == ChangeStatus.RENAMED]

    @property
    def untracked(self) -> list[ChangeUnit]:
        """Return only untracked change units."""
        return [u for u in self.units if u.status == ChangeStatus.UNTRACKED]

    @property
    def has_changes(self) -> bool:
        """Return True if the changeset contains any changes."""
        return len(self.units) > 0

    @property
    def staging_summary(self) -> dict[str, int]:
        """Return a summary of staged vs unstaged counts."""
        return {
            'staged': sum(1 for u in self.units if u.is_staged),
            'unstaged': sum(1 for u in self.units if u.is_unstaged),
            'untracked': sum(1 for u in self.units if u.is_untracked),
        }

    def get_by_path(self, path: str) -> Optional[ChangeUnit]:
        """Find a change unit by file path."""
        for u in self.units:
            if u.path == path:
                return u
        return None

    def to_dict(self) -> dict:
        """Serialize the changeset to a dictionary."""
        return {
            'repository_root': self.repository_root,
            'generated_at': self.generated_at,
            'units': [u.model_dump() for u in self.units],
        }

    def to_json(self) -> str:
        """Serialize the changeset to JSON."""
        import json
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_units(cls, repository_root: str, units: list[ChangeUnit]) -> Changeset:
        """Create a Changeset from a list of ChangeUnit objects."""
        return cls(
            repository_root=repository_root,
            units=units,
        )

    @classmethod
    def empty(cls, repository_root: str) -> Changeset:
        """Create an empty changeset for the given repository."""
        return cls(
            repository_root=repository_root,
            units=[],
        )
