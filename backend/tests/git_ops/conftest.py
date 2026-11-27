"""Fixtures for the Phase 3F git-ops / impact-workflow tests.

Every test gets a throwaway Git repository under ``tmp_path`` so the assertions
run against real ``git`` output rather than hand-written diff text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Optional

import pytest

__all__ = ["GitRepo", "repo_factory"]


class GitRepo:
    """A temporary repository with a few conveniences for tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("config", "user.email", "phase3f@example.test")
        self.git("config", "user.name", "Phase 3F")
        # Keep the tests independent of the host's Git defaults.
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    # -- plumbing ------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result.stdout

    def path(self, relative: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write(self, relative: str, text: str) -> Path:
        target = self.path(relative)
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    def remove(self, relative: str) -> None:
        (self.root / relative).unlink()

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def tag(self, name: str) -> None:
        self.git("tag", name)


@pytest.fixture
def repo_factory(tmp_path):
    """Factory creating independent repositories under ``tmp_path``.

    ``repo_factory({"pkg/a.py": "..."})`` initialises a repository, writes the
    files, and commits them, returning a :class:`GitRepo`.
    """

    created: list[GitRepo] = []

    def factory(
        files: Optional[Mapping[str, str]] = None,
        *,
        name: Optional[str] = None,
    ) -> GitRepo:
        root = tmp_path / (name or f"repo{len(created)}")
        repo = GitRepo(root)
        for relative, text in (files or {}).items():
            repo.write(relative, text)
        if files:
            repo.commit("initial")
        created.append(repo)
        return repo

    yield factory


@pytest.fixture
def two_function_file() -> str:
    """A module with two functions and a module-level constant."""
    return (
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
