"""Tests for the Phase 3F change-detection corrections.

Defect coverage:

* D2 — untracked directories must expand to one unit per file.
* D3 — partial per-file failures must surface as errors and mark detection
  degraded, while fatal failures still raise.
* D4 — copy semantics must be honest (copies are reported as added).
* D6 — a file with both staged and unstaged changes must not combine two
  incompatible hunk coordinate systems.
"""

from __future__ import annotations

import pytest
from git import GitCommandError

from app.git_ops.change_detection import (
    ChangeDetectionError,
    ChangeDetector,
)
from app.git_ops.models.changeset import ChangeStatus


def detector(repo) -> ChangeDetector:
    return ChangeDetector(str(repo.root))


# ---------------------------------------------------------------------------
# D2 — untracked directory expansion
# ---------------------------------------------------------------------------


def test_untracked_directory_expands_to_one_unit_per_file(repo_factory):
    repo = repo_factory({"app/main.py": "def main():\n    return 0\n"})
    repo.write("newpkg/__init__.py", "")
    repo.write("newpkg/n.py", "def n():\n    return 1\n")
    repo.write("newpkg/nested/deep.py", "def deep():\n    return 2\n")
    repo.write("newpkg/nested/deeper/leaf.py", "def leaf():\n    return 3\n")

    units = detector(repo).detect().units
    untracked = sorted(unit.path for unit in units if unit.is_untracked)

    assert untracked == [
        "newpkg/__init__.py",
        "newpkg/n.py",
        "newpkg/nested/deep.py",
        "newpkg/nested/deeper/leaf.py",
    ]
    # No directory is ever reported as if it were a file.
    assert not any(unit.path.endswith("/") for unit in units)
    assert "newpkg" not in {unit.path for unit in units}
    assert all(unit.status == ChangeStatus.UNTRACKED for unit in units if unit.is_untracked)
    # Untracked files have no baseline, so they carry no diff text.
    assert all(unit.diff is None for unit in units if unit.is_untracked)


def test_untracked_expansion_respects_include_untracked_false(repo_factory):
    repo = repo_factory({"app/main.py": "def main():\n    return 0\n"})
    repo.write("newpkg/n.py", "def n():\n    return 1\n")

    units = detector(repo).detect(include_untracked=False).units
    assert units == []


# ---------------------------------------------------------------------------
# D4 — copy semantics
# ---------------------------------------------------------------------------


def test_copied_file_is_reported_as_added_not_copied(repo_factory):
    repo = repo_factory({"pkg/source.py": "def thing():\n    return 1\n"})
    # A copy with identical content: Git's rename detector may call this a
    # rename or an add, but never a copy, because we do not pass -C.
    repo.write("pkg/copied.py", "def thing():\n    return 1\n")
    repo.git("add", "pkg/copied.py")

    units = {unit.path: unit for unit in detector(repo).detect().units}
    unit = units["pkg/copied.py"]
    assert unit.status == ChangeStatus.ADDED
    assert unit.status != ChangeStatus.COPIED
    assert unit.previous_path is None


def test_no_unit_is_ever_reported_as_copied(repo_factory):
    repo = repo_factory(
        {
            "pkg/source.py": "def thing():\n    return 1\n",
            "pkg/other.py": "CONST = 2\n",
        }
    )
    repo.write("pkg/copied.py", "def thing():\n    return 1\n")
    repo.write("pkg/modified.py", "CONST = 3\n")
    repo.git("add", "-A")

    statuses = {unit.status for unit in detector(repo).detect().units}
    assert ChangeStatus.COPIED not in statuses

    # Base comparison runs `git diff -M`, which also never emits C.
    repo.commit("second")
    repo.tag("base")
    repo.write("pkg/third.py", "def thing():\n    return 1\n")
    repo.commit("third")
    base_statuses = {
        unit.status
        for unit in detector(repo).detect(mode="base", base_revision="base").units
    }
    assert ChangeStatus.COPIED not in base_statuses


# ---------------------------------------------------------------------------
# D6 — dual staged/unstaged state
# ---------------------------------------------------------------------------


def _dual_state_repo(repo_factory):
    base = (
        "LIMIT = 10\n"
        "\n"
        "\n"
        "def alpha():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def helper():\n"
        "    c = 3\n"
        "    return c\n"
    )
    repo = repo_factory({"m.py": base})
    # Stage one edit, then make a second, unstaged edit.
    repo.write("m.py", base.replace("b = 2", "b = 22"))
    repo.git("add", "m.py")
    repo.write(
        "m.py",
        base.replace("b = 2", "b = 22").replace("c = 3", "c = 33"),
    )
    return repo, base


def test_dual_state_file_attaches_a_single_baseline_diff(repo_factory):
    repo, _ = _dual_state_repo(repo_factory)
    unit = detector(repo).detect().units[0]

    assert unit.is_staged is True
    assert unit.is_unstaged is True
    assert unit.diff is not None
    # One block only: the index diff is not concatenated with the work-tree diff.
    assert unit.diff.count("diff --git") == 1
    assert "c = 33" in unit.diff


def test_dual_state_file_emits_an_explicit_warning(repo_factory):
    repo, _ = _dual_state_repo(repo_factory)
    det = detector(repo)
    det.detect()

    assert any("both staged and unstaged" in warning for warning in det.warnings)
    assert any("working-tree diff is used" in warning for warning in det.warnings)


def test_single_state_files_still_get_their_own_diff(repo_factory):
    base = "def alpha():\n    a = 1\n    return a\n"
    repo = repo_factory({"m.py": base})
    repo.write("m.py", base.replace("a = 1", "a = 2"))
    repo.git("add", "m.py")

    unit = detector(repo).detect().units[0]
    assert unit.is_staged is True
    assert unit.is_unstaged is False
    assert "a = 2" in unit.diff


# ---------------------------------------------------------------------------
# D3 — partial failures, state reset, fatal failures
# ---------------------------------------------------------------------------


def test_per_file_failure_is_recorded_as_an_error_and_degrades(repo_factory, monkeypatch):
    repo = repo_factory({"m.py": "def alpha():\n    return 1\n"})
    repo.write("m.py", "def alpha():\n    return 2\n")

    det = detector(repo)

    def boom(self, entry, *, args, description):
        self.errors.append(
            f"Could not read the {description} diff for {entry.path}: boom (exit 128)"
        )
        self.warnings.append(
            f"Could not read the {description} diff for {entry.path}: boom (exit 128)"
        )
        return None

    monkeypatch.setattr(ChangeDetector, "_unit_diff", boom)
    changeset = det.detect()

    assert det.errors and "boom" in det.errors[0]
    assert det.degraded is True
    # The unit is still reported; only its diff text is missing.
    assert [unit.path for unit in changeset.units] == ["m.py"]
    assert changeset.units[0].diff is None


def test_git_diff_failure_sets_error_and_keeps_the_unit(repo_factory, monkeypatch):
    repo = repo_factory({"m.py": "def alpha():\n    return 1\n"})
    repo.write("m.py", "def alpha():\n    return 2\n")

    det = detector(repo)

    class _FailingGit:
        """Routes status to real Git and makes ``diff`` fail like Git would."""

        def __init__(self, real):
            self._real = real

        def status(self, *args, **kwargs):
            return self._real.status(*args, **kwargs)

        def diff(self, *args, **kwargs):
            raise GitCommandError(["git", "diff"], 128, b"", b"fatal: boom")

    class _RepoProxy:
        def __init__(self, real):
            self.git = _FailingGit(real.git)

    # ``Git`` uses ``__slots__``, so the command object is wrapped instead of
    # patched in place.
    monkeypatch.setattr(det, "repo", _RepoProxy(det.repo))
    changeset = det.detect()

    assert det.errors
    assert "m.py" in det.errors[0]
    assert "boom" in det.errors[0]
    assert changeset.units[0].diff is None


def test_state_is_reset_between_detection_runs(repo_factory, monkeypatch):
    repo = repo_factory({"m.py": "def alpha():\n    return 1\n"})
    repo.write("m.py", "def alpha():\n    return 2\n")

    det = detector(repo)

    def boom(self, entry, *, args, description):
        self.errors.append("first run failure")
        return None

    monkeypatch.setattr(ChangeDetector, "_unit_diff", boom)
    det.detect()
    assert det.errors == ["first run failure"]

    monkeypatch.undo()
    det.detect()
    assert det.errors == []
    assert det.warnings == []
    assert det.degraded is False


def test_direct_detection_calls_also_reset_state(repo_factory):
    repo = repo_factory({"m.py": "def alpha():\n    return 1\n"})
    det = detector(repo)
    det.warnings.append("stale warning")
    det.errors.append("stale error")

    det.detect_working_tree_changes()
    assert det.warnings == []
    assert det.errors == []


@pytest.mark.parametrize(
    "call",
    [
        lambda path: ChangeDetector(str(path / "does-not-exist")),
        lambda path: ChangeDetector(str(path)).detect(mode="nonsense"),
        lambda path: ChangeDetector(str(path)).detect(
            mode="base", base_revision="no-such-rev"
        ),
        lambda path: ChangeDetector(str(path)).detect(staged_only=True, base_revision="x"),
    ],
)
def test_fatal_failures_still_raise(tmp_path, call):
    repo_dir = tmp_path / "plain"
    repo_dir.mkdir()
    with pytest.raises(ChangeDetectionError):
        call(repo_dir)
