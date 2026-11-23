"""Human-readable errors raised by the knowledge layer."""

from __future__ import annotations


class KiteHutError(Exception):
    """Base class: the message is safe to show to a user as-is."""


class RepositoryPathError(KiteHutError):
    """The target repository path is missing, not a directory, or unusable."""


class UnsafeOutputError(KiteHutError):
    """An output location is unsafe (symlink, escape, or unrelated file)."""


class KnowledgeNotFoundError(KiteHutError):
    """No ``.kitehut/`` output exists (or it is unusable) for a repository."""
