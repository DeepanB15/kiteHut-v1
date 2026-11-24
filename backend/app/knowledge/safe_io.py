"""Safe filesystem primitives for the ``.kitehut/`` output folder.

Guarantees
----------
* Source files are never written: every write target must resolve *inside*
  ``<repo>/.kitehut``.
* ``.kitehut`` and every directory below it must be a real directory, never a
  symlink, so a hostile checkout cannot redirect output elsewhere.
* Writes are atomic (temporary file in the same directory, fsync, replace).
* An existing file is only overwritten or pruned when it carries the KiteHut
  generated marker; anything else is reported and left untouched.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from app.knowledge.constants import (
    GENERATED_MARKER,
    MARKER_SCAN_BYTES,
    OUTPUT_DIR_NAME,
)
from app.knowledge.errors import RepositoryPathError, UnsafeOutputError

PathLike = Union[str, "os.PathLike[str]"]


def resolve_repository_root(path: PathLike) -> Path:
    """Validate ``path`` and return its resolved absolute directory."""
    if path is None or str(path).strip() == "":
        raise RepositoryPathError("Repository path must not be empty.")
    try:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise RepositoryPathError(f"Repository path does not exist: {path}")
        if not candidate.is_dir():
            raise RepositoryPathError(f"Repository path is not a directory: {path}")
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepositoryPathError(f"Repository path is not usable: {path} ({exc})") from exc


def is_generated_file(path: Path) -> bool:
    """True when ``path`` is a regular file carrying the KiteHut marker."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as handle:
            head = handle.read(MARKER_SCAN_BYTES)
        return GENERATED_MARKER.encode("utf-8") in head
    except OSError:
        return False


@dataclass(frozen=True)
class OutputLayout:
    """Well-known locations inside ``<repo>/.kitehut``."""

    root: Path

    @property
    def base(self) -> Path:
        return self.root / OUTPUT_DIR_NAME

    @property
    def graph_dir(self) -> Path:
        return self.base / "graph"

    @property
    def graph_file(self) -> Path:
        return self.graph_dir / "repository.json"

    @property
    def knowledge_dir(self) -> Path:
        return self.base / "knowledge"

    @property
    def index_file(self) -> Path:
        return self.knowledge_dir / "INDEX.md"

    @property
    def architecture_file(self) -> Path:
        return self.knowledge_dir / "architecture.md"

    @property
    def modules_dir(self) -> Path:
        return self.knowledge_dir / "modules"

    @property
    def symbols_dir(self) -> Path:
        return self.knowledge_dir / "symbols"

    @property
    def visualization_dir(self) -> Path:
        return self.base / "visualization"

    @property
    def graph_html(self) -> Path:
        return self.visualization_dir / "graph.html"

    @property
    def metadata_file(self) -> Path:
        return self.base / "metadata.json"

    def prepare(self) -> None:
        """Create ``.kitehut/`` if needed, refusing symlinks and escapes."""
        base = self.base
        if base.is_symlink():
            raise UnsafeOutputError(
                f"Refusing to write: '{OUTPUT_DIR_NAME}' is a symbolic link."
            )
        if base.exists() and not base.is_dir():
            raise UnsafeOutputError(
                f"Refusing to write: '{OUTPUT_DIR_NAME}' exists and is not a directory."
            )
        try:
            base.mkdir(exist_ok=True)
            if base.resolve().parent != self.root.resolve():
                raise UnsafeOutputError(
                    f"Refusing to write: '{OUTPUT_DIR_NAME}' resolves outside the repository."
                )
        except OSError as exc:
            raise UnsafeOutputError(
                f"Cannot create '{OUTPUT_DIR_NAME}' in the repository: {exc}"
            ) from exc

    def safe_mkdir(self, directory: Path) -> Path:
        """Create ``directory`` inside ``.kitehut`` one component at a time.

        Every component is checked for symlinks before and after creation, so
        a hostile checkout cannot redirect a nested output directory (e.g.
        ``graph/``) outside the repository.
        """
        try:
            relative = directory.relative_to(self.base)
        except ValueError:
            raise UnsafeOutputError(
                f"Refusing to create a directory outside '{OUTPUT_DIR_NAME}': {directory}"
            ) from None
        if ".." in relative.parts:
            raise UnsafeOutputError(f"Refusing path traversal: {directory}")
        current = self.base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeOutputError(
                    f"Refusing to write through a symbolic link: {current}"
                )
            try:
                current.mkdir(exist_ok=True)
            except OSError as exc:
                raise UnsafeOutputError(
                    f"Cannot create output directory {current}: {exc}"
                ) from exc
            if current.is_symlink():
                raise UnsafeOutputError(
                    f"Refusing to write through a symbolic link: {current}"
                )
        return directory

    def safe_target(self, target: Path) -> Path:
        """Return ``target`` if it is a safe write location, else raise.

        ``target`` must be lexically inside ``.kitehut`` and no existing path
        component between ``.kitehut`` and the file may be a symlink.
        """
        base = self.base
        try:
            relative = target.relative_to(base)
        except ValueError:
            raise UnsafeOutputError(
                f"Refusing to write outside '{OUTPUT_DIR_NAME}': {target}"
            ) from None
        if ".." in relative.parts:
            raise UnsafeOutputError(f"Refusing path traversal: {target}")
        current = base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeOutputError(
                    f"Refusing to write through a symbolic link: {current}"
                )
        if target.exists() and target.is_dir():
            raise UnsafeOutputError(f"Refusing to overwrite a directory: {target}")
        base_real = base.resolve()
        parent_real = target.parent.resolve() if target.parent.exists() else None
        if parent_real is not None and base_real != parent_real and base_real not in parent_real.parents:
            raise UnsafeOutputError(f"Refusing to write outside '{OUTPUT_DIR_NAME}': {target}")
        return target


@dataclass
class WriteOutcome:
    """Result of one guarded write."""

    path: Path
    status: str  # "written" | "unchanged" | "skipped_user_file"
    detail: Optional[str] = None


def atomic_write_text(
    layout: OutputLayout,
    target: Path,
    text: str,
    *,
    require_marker_to_overwrite: bool = True,
) -> WriteOutcome:
    """Atomically write ``text`` to ``target`` inside ``.kitehut``.

    * Identical existing content is left alone (``unchanged``) so refreshes
      do not churn mtimes.
    * An existing file without the generated marker is never replaced
      (``skipped_user_file``) unless ``require_marker_to_overwrite`` is False.
    """
    layout.safe_target(target)
    if target.exists():
        try:
            if target.read_text(encoding="utf-8") == text:
                return WriteOutcome(target, "unchanged")
        except (OSError, UnicodeDecodeError):
            pass
        if require_marker_to_overwrite and not is_generated_file(target):
            return WriteOutcome(
                target,
                "skipped_user_file",
                "existing file was not generated by KiteHut; left untouched",
            )

    tmp_name: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        layout.safe_target(target)  # re-check after creating parents
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".kh-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(target))
        tmp_name = None
        return WriteOutcome(target, "written")
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def prune_stale_generated(
    layout: OutputLayout, directory: Path, keep: set[str]
) -> List[Path]:
    """Delete generated ``*.md`` pages in ``directory`` that are not in ``keep``.

    Only regular files that carry the KiteHut marker are removed; user files
    and symlinks are never deleted.  Returns the removed paths.
    """
    removed: List[Path] = []
    if not directory.is_dir() or directory.is_symlink():
        return removed
    layout.safe_target(directory / "placeholder.md")
    for entry in sorted(directory.iterdir()):
        if entry.name in keep or entry.suffix != ".md":
            continue
        if is_generated_file(entry):
            try:
                entry.unlink()
                removed.append(entry)
            except OSError:
                continue
    return removed
