"""Fixtures: every test works on a fresh temporary repository, never the real one."""

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


@pytest.fixture
def repo(tmp_path) -> Path:
    return write_tree(tmp_path / "sample", SAMPLE)


@pytest.fixture
def analyzed(repo):
    from app.knowledge import generate

    result = generate(repo)
    assert result.ok, result.errors
    return repo, result
