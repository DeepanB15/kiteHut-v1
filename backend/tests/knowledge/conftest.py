"""Fixtures: every test works on a fresh temporary repository, never the real one."""

import os
from pathlib import Path

import pytest

SAMPLE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(x):\n    return x + 1\n",
    "pkg/core.py": (
        "from pkg.util import helper\n\n"
        "class Engine:\n    def run(self):\n        return helper(1)\n\n"
        "def main():\n    e = Engine()\n    return e.run()\n"
    ),
}


def write_tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


# Characters Windows refuses in filenames; a hostile-name test must substitute
# them there instead of failing to create its fixture.
_UNSAFE_FILENAME_CHARS = set('<>:"|?*')


def hostile_path(name: str) -> str:
    """A filesystem-legal path that keeps markup-hostile characters where allowed."""
    return "/".join(
        "".join("_" if ch in _UNSAFE_FILENAME_CHARS or ord(ch) < 32 else ch for ch in part)
        for part in name.split("/")
    )


def symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    """Create a symlink, or skip the test where the OS/user policy forbids it.

    Windows requires Developer Mode or the symlink privilege, so several
    safety tests cannot run in a default Windows environment.  Skipping is an
    environment limitation, never a relaxation of the assertion.
    """
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symlinks are not available in this environment: {error}")


@pytest.fixture
def repo(tmp_path) -> Path:
    return write_tree(tmp_path / "sample", SAMPLE)


@pytest.fixture
def analyzed(repo):
    from app.knowledge import generate

    result = generate(repo)
    assert result.ok, result.errors
    return repo, result
