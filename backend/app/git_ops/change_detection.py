"""Git change detection for KiteHut impact analysis (Phase 3B, extended in 3F).

The detector answers "what changed?" and nothing else.  It returns a
:class:`~app.git_ops.models.changeset.Changeset` of one
:class:`~app.git_ops.models.changeset.ChangeUnit` per file, with the unit's own
diff text attached.

Detection modes
---------------
``working_tree``
    Unstaged modifications, staged (index) changes and untracked files, merged
    into one unit per path.  ``staged_only=True`` restricts the result to the
    index.
``base``
    ``git diff -M <base> HEAD``: committed changes between two revisions.
    The working tree is deliberately **not** included.

Honesty rules
-------------
* A Git failure is never reported as "no changes".  Repositories that cannot
  be opened and revisions that do not exist raise :class:`ChangeDetectionError`.
  A failure to read one file's diff text is recorded in :attr:`ChangeDetector.errors`
  (it is a partial detection failure), leaves that unit's ``diff`` as ``None``,
  and leaves the detected changeset marked :attr:`ChangeDetector.degraded`.
  The unit itself is still reported.
* ``ChangeStatus`` reflects Git's porcelain status, so deleted, renamed, added
  and untracked files are distinguishable.  Untracked files have no diff text
  because they have no baseline.
* **Copies are not detected.**  ``git status --porcelain`` never reports
  ``C``, and this detector does not pass ``-C`` to ``git diff``.  A copied
  file is therefore reported as ``added`` (or ``renamed`` when Git's rename
  detection matches it).  :attr:`ChangeStatus.COPIED` exists in the model for
  forward compatibility but is never produced here.
* ``diff`` holds **only that file's** hunks (both paths for a rename), never the
  whole multi-file diff, so hunk coordinates unambiguously belong to one unit.
  A file with both staged and unstaged changes gets the **working-tree** diff
  only: the index diff and the working-tree diff are computed against
  different baselines, so their line numbers cannot be compared or combined.
* Untracked directories are expanded with ``--untracked-files=all`` so one
  unit is reported per file; a directory is never reported as a pseudo-file.

Path coordinates
----------------
Paths are repository-relative with forward slashes, exactly as Git prints them,
so they can be compared with ``FileNode.relative_path`` in the graph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from app.git_ops.models.changeset import (
    ChangeSource,
    ChangeStatus,
    ChangeUnit,
    Changeset,
)

logger = logging.getLogger("kitehut.git_change_detector")

__all__ = [
    "ChangeDetectionError",
    "ChangeDetector",
    "create_change_detector",
]

_MODE_WORKING_TREE = "working_tree"
_MODE_STAGED = "staged"
_MODE_BASE = "base"
DETECTION_MODES = (_MODE_WORKING_TREE, _MODE_STAGED, _MODE_BASE)


class ChangeDetectionError(Exception):
    """Raised when changes cannot be detected at all (not "no changes")."""


@dataclass(frozen=True)
class StatusEntry:
    """One file's parsed ``git status --porcelain=v1`` record."""

    path: str
    previous_path: Optional[str]
    status: ChangeStatus
    is_staged: bool
    is_unstaged: bool
    is_untracked: bool


class ChangeDetector:
    """Read-only Git change detection for one repository."""

    def __init__(self, repository_path: str):
        requested = Path(repository_path).expanduser()
        try:
            self.repo = Repo(requested, search_parent_directories=True)
        except (InvalidGitRepositoryError, NoSuchPathError) as error:
            raise ChangeDetectionError(
                f"Directory is not a valid Git repository: {requested}"
            ) from error
        working_tree = self.repo.working_tree_dir
        if not working_tree:
            raise ChangeDetectionError(
                f"Repository has no working tree (bare repository?): {requested}"
            )
        # ``search_parent_directories=True`` means the repository root is not
        # necessarily the path the caller passed.  Everything downstream
        # (graph build, file mapping, scope filtering) needs the real root, so
        # it is resolved once, here.
        self.root = Path(working_tree).resolve()
        self.requested_path = requested.resolve()
        self.warnings: List[str] = []
        #: Partial detection failures (fatal ones raise ``ChangeDetectionError``).
        self.errors: List[str] = []

    @property
    def degraded(self) -> bool:
        """True when detection produced units but at least one detail failed."""
        return bool(self.errors)

    def _reset_state(self) -> None:
        """Clear per-run diagnostics so results never accumulate across runs."""
        self.warnings = []
        self.errors = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        *,
        mode: str = _MODE_WORKING_TREE,
        base_revision: Optional[str] = None,
        staged_only: bool = False,
        include_untracked: bool = True,
    ) -> Changeset:
        """Detect changes for ``mode`` and return a deterministic changeset."""
        self._reset_state()

        if mode not in DETECTION_MODES:
            raise ChangeDetectionError(
                f"Unknown detection mode {mode!r}; expected one of "
                f"{', '.join(DETECTION_MODES)}."
            )
        if mode == _MODE_BASE:
            if staged_only:
                raise ChangeDetectionError(
                    "staged_only is only meaningful for working-tree detection."
                )
            if not base_revision:
                raise ChangeDetectionError(
                    "A base revision is required for base-comparison detection."
                )
            return self.detect_base_comparison_changes(base_revision)
        if base_revision:
            raise ChangeDetectionError(
                "base_revision requires mode='base'; a base comparison never "
                "includes working-tree changes."
            )
        return self.detect_working_tree_changes(
            staged_only=(staged_only or mode == _MODE_STAGED),
            include_untracked=include_untracked,
        )

    def detect_working_tree_changes(
        self,
        *,
        staged_only: bool = False,
        include_untracked: bool = True,
    ) -> Changeset:
        """Working-tree units: staged, unstaged and (optionally) untracked."""
        self._reset_state()
        entries = self._status_entries()

        units: List[ChangeUnit] = []
        for entry in entries:
            if entry.status == ChangeStatus.IGNORED:
                continue
            if entry.is_untracked and not include_untracked:
                continue
            if staged_only and not entry.is_staged:
                continue
            units.append(self._working_tree_unit(entry, staged_only=staged_only))

        return self._changeset(units)

    def detect_base_comparison_changes(self, base_revision: str) -> Changeset:
        """Committed changes between ``base_revision`` and ``HEAD``."""
        self._reset_state()
        self._validate_revision(base_revision)

        entries = self._name_status_entries(base_revision)
        units: List[ChangeUnit] = []
        for entry in entries:
            if entry.status == ChangeStatus.IGNORED:
                continue
            diff = self._unit_diff(
                entry,
                args=("-M", base_revision, "HEAD"),
                description=f"{base_revision}..HEAD",
            )
            units.append(
                ChangeUnit(
                    path=entry.path,
                    previous_path=entry.previous_path,
                    status=entry.status,
                    is_deleted=entry.status == ChangeStatus.DELETED,
                    is_renamed=entry.status == ChangeStatus.RENAMED,
                    diff=diff,
                    change_source=ChangeSource.BASE_COMPARISON,
                )
            )
        return self._changeset(units)

    def detect_all_changes(self, base_revision: Optional[str] = None) -> Changeset:
        """Compatibility façade: base comparison when a revision is given."""
        if base_revision:
            return self.detect(mode=_MODE_BASE, base_revision=base_revision)
        return self.detect(mode=_MODE_WORKING_TREE)

    # ------------------------------------------------------------------
    # Git plumbing
    # ------------------------------------------------------------------

    def _status_entries(self) -> List[StatusEntry]:
        """Parse ``git status --porcelain=v1 -z -uall`` into one entry per file.

        ``-uall`` lists every file inside an untracked directory instead of
        collapsing the directory into a single ``?? dir/`` entry, so one unit is
        produced per file and no untracked file is silently dropped.
        """
        try:
            output = self.repo.git.status("--porcelain=v1", "-z", "-uall")
        except GitCommandError as error:
            raise ChangeDetectionError(
                "git status failed; changes could not be detected. "
                f"Repository: {self.root}"
            ) from error

        tokens = output.split("\x00")
        entries: List[StatusEntry] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            if len(token) < 4 or token[2] != " ":
                self.warnings.append(
                    f"Unrecognized git status entry was skipped: {token[:60]!r}"
                )
                continue
            xy = token[:2]
            path = token[3:]
            previous_path = None
            if "R" in xy:
                if index < len(tokens) and tokens[index]:
                    # Porcelain -z prints <new path> then <old path>.
                    previous_path = tokens[index]
                    index += 1
                else:
                    self.warnings.append(
                        f"Rename entry for {path} had no source path."
                    )
            elif "C" in xy:
                # Not produced by ``git status`` (which never reports copies);
                # tolerated only so a future switch to copy detection degrades
                # to an added file instead of silently losing the entry.
                self.warnings.append(
                    f"Copy entry for {path} is reported as an added file; "
                    "this detector does not perform copy detection."
                )
                if index < len(tokens) and tokens[index]:
                    previous_path = tokens[index]
                    index += 1
            entries.append(self._entry_from_xy(path, previous_path, xy))

        entries.sort(key=lambda entry: (entry.path, entry.previous_path or ""))
        return entries

    def _name_status_entries(self, base_revision: str) -> List[StatusEntry]:
        """Parse ``git diff -M --name-status -z <base> HEAD``."""
        try:
            output = self.repo.git.diff(
                "-M", "--name-status", "-z", base_revision, "HEAD"
            )
        except GitCommandError as error:
            raise ChangeDetectionError(
                f"git diff {base_revision}..HEAD failed; changes could not be "
                "detected."
            ) from error

        tokens = output.split("\x00")
        entries: List[StatusEntry] = []
        index = 0
        while index < len(tokens):
            raw_status = tokens[index]
            index += 1
            if not raw_status:
                continue
            letter = raw_status[0]
            if letter in ("R", "C"):
                if index + 1 >= len(tokens):
                    self.warnings.append(
                        f"Truncated rename entry in git diff output: {raw_status!r}"
                    )
                    break
                previous_path = tokens[index]
                path = tokens[index + 1]
                index += 2
            else:
                if index >= len(tokens):
                    self.warnings.append(
                        f"Truncated entry in git diff output: {raw_status!r}"
                    )
                    break
                previous_path = None
                path = tokens[index]
                index += 1
            if not path:
                continue
            entries.append(
                StatusEntry(
                    path=path,
                    previous_path=previous_path,
                    status=self._status_for_letter(letter),
                    is_staged=False,
                    is_unstaged=False,
                    is_untracked=False,
                )
            )

        entries.sort(key=lambda entry: (entry.path, entry.previous_path or ""))
        return entries

    def _validate_revision(self, base_revision: str) -> None:
        try:
            self.repo.commit(base_revision)
        except Exception as error:  # gitdb raises a variety of types
            raise ChangeDetectionError(
                f"Invalid revision: {base_revision}"
            ) from error

    # ------------------------------------------------------------------
    # Unit assembly
    # ------------------------------------------------------------------

    def _working_tree_unit(
        self, entry: StatusEntry, *, staged_only: bool
    ) -> ChangeUnit:
        is_untracked = entry.is_untracked
        if is_untracked:
            diff = None  # no baseline to diff against
            is_staged = False
            is_unstaged = False
        elif staged_only:
            diff = self._unit_diff(entry, args=("--cached", "-M"), description="index")
            is_staged = True
            is_unstaged = False
        elif entry.is_staged and entry.is_unstaged:
            # The index diff (HEAD -> index) and the working-tree diff
            # (index -> worktree) are computed against different baselines.
            # Their hunk coordinates are not comparable, so only the
            # working-tree diff is attached: it is the one whose line numbers
            # match the files the code graph was built from.
            diff = self._unit_diff(
                entry, args=("-M",), description="working tree"
            )
            is_staged = True
            is_unstaged = True
            self.warnings.append(
                f"{entry.path} has both staged and unstaged changes; only its "
                "working-tree diff is used for symbol attribution because the "
                "index diff is measured against a different baseline."
            )
        else:
            if entry.is_staged:
                diff = self._unit_diff(
                    entry, args=("--cached", "-M"), description="index"
                )
            else:
                diff = self._unit_diff(
                    entry, args=("-M",), description="working tree"
                )
            is_staged = entry.is_staged
            is_unstaged = entry.is_unstaged

        source = (
            ChangeSource.STAGED
            if is_staged and not is_unstaged
            else ChangeSource.WORKING_TREE
        )
        return ChangeUnit(
            path=entry.path,
            previous_path=entry.previous_path,
            status=entry.status,
            is_staged=is_staged,
            is_unstaged=is_unstaged,
            is_untracked=is_untracked,
            is_deleted=entry.status == ChangeStatus.DELETED,
            is_renamed=entry.status == ChangeStatus.RENAMED,
            diff=diff,
            change_source=source,
        )

    def _unit_diff(
        self,
        entry: StatusEntry,
        *,
        args: Sequence[str],
        description: str,
    ) -> Optional[str]:
        """Per-file diff text, or ``None`` when Git refuses (recorded)."""
        paths = [entry.path]
        if entry.previous_path:
            # Without the rename source, Git reports the new path as a new
            # file and the similarity/rename evidence is lost.
            paths.append(entry.previous_path)
        try:
            text = self.repo.git.diff(*args, "--", *paths)
        except GitCommandError as error:
            detail = self._error_line(error)
            # A per-file failure is a partial detection failure: the changeset
            # is still usable, but it is degraded and must say so.
            self.errors.append(
                f"Could not read the {description} diff for {entry.path}: {detail}"
            )
            self.warnings.append(
                f"Could not read the {description} diff for {entry.path}: {detail}"
            )
            return None
        return text or None

    @staticmethod
    def _error_line(error: GitCommandError) -> str:
        status = getattr(error, "status", None)
        message = (getattr(error, "stderr", None) or "").strip().splitlines()
        detail = message[-1].strip() if message else str(error).strip()
        return f"{detail} (exit {status})" if status else detail

    # ------------------------------------------------------------------
    # Status mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_from_xy(
        path: str, previous_path: Optional[str], xy: str
    ) -> StatusEntry:
        x, y = xy[0], xy[1]
        is_untracked = xy == "??"
        is_ignored = xy == "!!"
        is_staged = x not in (" ", "?", "!")
        is_unstaged = (not is_untracked) and y not in (" ", "?", "!")

        if is_ignored:
            status = ChangeStatus.IGNORED
        elif is_untracked:
            status = ChangeStatus.UNTRACKED
        elif "R" in xy:
            status = ChangeStatus.RENAMED
        elif "D" in xy:
            status = ChangeStatus.DELETED
        elif x == "A" or "C" in xy:
            # A copy has no dedicated status here: ``git status`` never reports
            # one, and reporting it as added is the honest fallback (the new
            # path is in fact a new file in the index).
            status = ChangeStatus.ADDED
        else:
            status = ChangeStatus.MODIFIED

        return StatusEntry(
            path=path,
            previous_path=previous_path,
            status=status,
            is_staged=is_staged,
            is_unstaged=is_unstaged,
            is_untracked=is_untracked,
        )

    @staticmethod
    def _status_for_letter(letter: str) -> ChangeStatus:
        return {
            "A": ChangeStatus.ADDED,
            # ``git diff -M`` (the only mode this detector runs) never emits C.
            # If it ever does, an added file is the truthful reading.
            "C": ChangeStatus.ADDED,
            "D": ChangeStatus.DELETED,
            "M": ChangeStatus.MODIFIED,
            "R": ChangeStatus.RENAMED,
            "T": ChangeStatus.MODIFIED,  # type change (e.g. file -> symlink)
            "U": ChangeStatus.MODIFIED,  # unmerged
            "X": ChangeStatus.MODIFIED,
        }.get(letter, ChangeStatus.MODIFIED)

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def _changeset(self, units: List[ChangeUnit]) -> Changeset:
        ordered = sorted(units, key=lambda unit: (unit.path, unit.previous_path or ""))
        return Changeset(repository_root=str(self.root), units=ordered)


def create_change_detector(repository_path: str) -> ChangeDetector:
    """Factory function to create a ChangeDetector instance."""
    return ChangeDetector(repository_path)
