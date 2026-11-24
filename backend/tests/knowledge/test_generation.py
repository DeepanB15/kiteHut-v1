import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from app.code_graph.serialization import deserialize_graph_json
from app.code_graph.validation import validate_graph
from app.knowledge import (
    GenerationOptions, KiteHutError, RepositoryPathError, UnsafeOutputError, check_freshness,
    generate, refresh,
)
from app.knowledge.markdown import generate_knowledge
from app.knowledge.safe_io import OutputLayout, atomic_write_text
from tests.knowledge.conftest import hostile_path, symlink_or_skip, write_tree


def tree_hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".kitehut" not in p.parts}


# 1-3 graph json, determinism, validation
def test_graph_json_generated_and_valid(analyzed):
    repo, result = analyzed
    text = (repo / ".kitehut/graph/repository.json").read_text(encoding="utf-8")
    graph, validation = deserialize_graph_json(text)
    assert graph is not None and validation.valid
    assert validate_graph(graph).valid
    data = json.loads(text)
    assert data["schema_version"] == 1
    assert {f["relative_path"] for f in data["files"]} == {"pkg/__init__.py", "pkg/core.py", "pkg/util.py"}


def test_graph_json_contains_no_absolute_paths(analyzed):
    repo, _ = analyzed
    for p in (repo / ".kitehut").rglob("*"):
        if p.is_file():
            assert str(repo) not in p.read_text(encoding="utf-8"), p


def test_output_is_deterministic_across_runs_and_locations(tmp_path, repo):
    from tests.knowledge.conftest import SAMPLE
    other = write_tree(tmp_path / "elsewhere" / "sample", SAMPLE)
    generate(repo); generate(other)
    for rel in ("graph/repository.json", "knowledge/INDEX.md", "knowledge/architecture.md", "visualization/graph.html"):
        assert (repo / ".kitehut" / rel).read_bytes() == (other / ".kitehut" / rel).read_bytes(), rel
    first = {p.name: p.read_bytes() for p in (repo / ".kitehut/knowledge/symbols").iterdir()}
    generate(repo)
    assert first == {p.name: p.read_bytes() for p in (repo / ".kitehut/knowledge/symbols").iterdir()}


# 4-6 markdown
def test_index_lists_modules_and_links_resolve(analyzed):
    repo, _ = analyzed
    kdir = repo / ".kitehut/knowledge"
    index = (kdir / "INDEX.md").read_text(encoding="utf-8")
    assert "pkg/core.py" in index and "architecture.md" in index
    for page in kdir.rglob("*.md"):
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", page.read_text(encoding="utf-8")):
            if target.startswith("http"):
                continue
            assert (page.parent / target).resolve().exists(), f"broken link {target} in {page}"


def test_module_and_symbol_pages_have_source_and_line_refs(analyzed):
    repo, _ = analyzed
    kdir = repo / ".kitehut/knowledge"
    module = (kdir / "modules/pkg__core.md").read_text(encoding="utf-8")
    assert "../../../pkg/core.py#L3-L5" in module  # class Engine spans lines 3-5
    run = (kdir / "symbols/pkg__core.Engine.run.md").read_text(encoding="utf-8")
    assert "pkg/core.py L4-L5" in run
    main = (kdir / "symbols/pkg__core.main.md").read_text(encoding="utf-8")
    assert "pkg/core.Engine" in main  # resolved call main -> Engine
    helper = (kdir / "symbols/pkg__util.helper.md").read_text(encoding="utf-8")
    assert "Called by" in helper


def test_line_references_match_real_source(analyzed):
    repo, result = analyzed
    graph = result.graph
    for sym in graph.symbols:
        lines = (repo / sym.relative_path).read_text(encoding="utf-8").splitlines()
        assert 1 <= sym.start_line <= sym.end_line <= max(len(lines), 1)


def test_no_invented_relationships(analyzed):
    repo, _ = analyzed
    helper = (repo / ".kitehut/knowledge/symbols/pkg__util.helper.md").read_text(encoding="utf-8")
    assert "None found by the conservative resolver" in helper.split("## Calls (resolved)")[1].split("##")[0]


def test_page_limits_bound_output_on_large_repo(tmp_path):
    files = {f"m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(30)}
    repo = write_tree(tmp_path / "big", files)
    res = generate(repo, GenerationOptions(max_module_pages=5, max_symbol_pages=7))
    assert res.ok
    assert len(list((repo / ".kitehut/knowledge/modules").iterdir())) == 5
    assert len(list((repo / ".kitehut/knowledge/symbols").iterdir())) <= 7
    assert any("Page limits applied" in w for w in res.warnings)


def test_hostile_symbol_names_cannot_break_markdown_or_paths(tmp_path):
    # ``|`` is illegal in Windows filenames; hostile_path substitutes it there.
    hostile = hostile_path("a b/we`ird|mod.py")
    repo = write_tree(tmp_path / "h", {hostile: "def f():\n    pass\n"})
    assert generate(repo).ok
    for p in (repo / ".kitehut/knowledge").rglob("*.md"):
        assert p.resolve().is_relative_to((repo / ".kitehut").resolve())


# 7 html
def test_html_is_standalone_offline_and_inert(analyzed, tmp_path):
    repo, _ = analyzed
    html = (repo / ".kitehut/visualization/graph.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert not re.search(r"(src|href)\s*=\s*[\"']?https?://", html)
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "import(" not in html
    assert '"pkg/core.py"' in html
    assert 'addEventListener("wheel"' in html and "mousemove" in html  # zoom + pan handlers present


def test_html_payload_cannot_break_out_of_script_tag(tmp_path):
    from app.knowledge.visualize import _json_for_script
    out = _json_for_script({"n": "</script><script>alert(1)</script><!--\u2028"})
    assert "<" not in out and ">" not in out and "\u2028" not in out
    import json as _j
    assert _j.loads(out)["n"].startswith("</script>")  # still round-trips as data


# 8 empty
def test_empty_repository_produces_valid_empty_output(tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    res = generate(empty)
    assert res.ok and res.metadata.file_count == 0
    assert any("No Python files" in w for w in res.warnings)
    assert "No Python files were discovered" in (empty / ".kitehut/knowledge/INDEX.md").read_text(encoding="utf-8")
    assert "<canvas" in (empty / ".kitehut/visualization/graph.html").read_text(encoding="utf-8")
    assert check_freshness(empty).state == "fresh"


# 9 invalid paths
@pytest.mark.parametrize("bad", ["", "   ", "does/not/exist"])
def test_invalid_paths_raise_clear_errors(tmp_path, bad):
    with pytest.raises(RepositoryPathError):
        generate(tmp_path / bad if bad.strip() else bad)


def test_file_path_is_rejected(repo):
    with pytest.raises(RepositoryPathError, match="not a directory"):
        generate(repo / "pkg/core.py")


# 10-11 exclusions
def test_excluded_dirs_gitignore_and_secrets_not_indexed(tmp_path):
    repo = write_tree(tmp_path / "r", {
        "app.py": "def ok():\n    pass\n",
        ".venv/lib/x.py": "def venv(): pass\n", "node_modules/y.py": "def nm(): pass\n",
        "__pycache__/z.py": "def pc(): pass\n", "build/b.py": "def b(): pass\n",
        "generated/g.py": "def g(): pass\n", ".gitignore": "generated/\n",
        ".env": "API_KEY=supersecretvalue\n",
    })
    res = generate(repo)
    paths = set(res.metadata.file_hashes)
    assert paths == {"app.py"}
    assert "supersecretvalue" not in "".join(p.read_text(encoding="utf-8") for p in (repo / ".kitehut").rglob("*") if p.is_file())


def test_kitehut_dir_never_indexes_itself(repo):
    generate(repo)
    (repo / ".kitehut/knowledge/planted.py").write_text("def planted(): pass\n", encoding="utf-8")
    res = generate(repo)
    assert not any(".kitehut" in p for p in res.metadata.file_hashes)
    assert check_freshness(repo).state == "fresh"


def test_user_exclude_option(repo):
    res = generate(repo, GenerationOptions(exclude_dirs=("pkg",)))
    assert res.metadata.file_count == 0


# 12 refresh / stale
def test_stale_detection_and_refresh_cycle(analyzed):
    repo, _ = analyzed
    assert check_freshness(repo).state == "fresh"
    (repo / "pkg/util.py").write_text("def helper(x):\n    return x + 2\n\ndef extra():\n    pass\n", encoding="utf-8")
    (repo / "pkg/new.py").write_text("def n(): pass\n", encoding="utf-8")
    report = check_freshness(repo)
    assert report.state == "stale" and report.modified == ["pkg/util.py"] and report.added == ["pkg/new.py"]
    res = refresh(repo)
    assert res.ok and res.metadata.file_count == 4 and not res.up_to_date
    assert check_freshness(repo).state == "fresh"
    assert (repo / ".kitehut/knowledge/symbols/pkg__util.extra.md").exists()
    assert refresh(repo).up_to_date


def test_refresh_prunes_only_generated_pages_and_keeps_user_files(analyzed):
    repo, _ = analyzed
    user = repo / ".kitehut/knowledge/symbols/MY_NOTES.md"
    user.write_text("# my notes\n", encoding="utf-8")
    (repo / "pkg/util.py").write_text("def renamed(x):\n    return x\n", encoding="utf-8")
    res = refresh(repo)
    assert any(p.endswith("pkg__util.helper.md") for p in res.pruned)
    assert not (repo / ".kitehut/knowledge/symbols/pkg__util.helper.md").exists()
    assert user.read_text(encoding="utf-8") == "# my notes\n"


def test_refresh_requires_prior_analysis(repo):
    with pytest.raises(KiteHutError, match="analyze"):
        refresh(repo)


def test_status_when_never_analyzed_and_corrupt_metadata(repo):
    assert check_freshness(repo).state == "not_analyzed"
    generate(repo)
    (repo / ".kitehut/metadata.json").write_text("{not json", encoding="utf-8")
    assert check_freshness(repo).state == "invalid"


def test_metadata_fields(analyzed):
    repo, _ = analyzed
    meta = json.loads((repo / ".kitehut/metadata.json").read_text(encoding="utf-8"))
    for key in ("kitehut_version", "graph_schema_version", "generated_at", "file_count", "symbol_count",
                "relationship_count", "source_digest", "generation_status", "repository_id", "file_hashes"):
        assert key in meta
    assert meta["file_count"] == 3 and meta["generation_status"] == "complete"


def test_sources_never_modified(repo):
    before = tree_hashes(repo)
    generate(repo); refresh(repo, force=True)
    assert tree_hashes(repo) == before


# 13 atomic writes
def test_atomic_write_failure_keeps_previous_content_and_leaves_no_temp(analyzed, monkeypatch):
    repo, _ = analyzed
    layout = OutputLayout(repo)
    target = layout.index_file
    original = target.read_text(encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        atomic_write_text(layout, target, "<!-- kitehut:generated -->\nNEW\n")
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == original
    assert not list(target.parent.glob(".kh-*.tmp"))


def test_unchanged_content_is_not_rewritten(analyzed):
    repo, _ = analyzed
    layout = OutputLayout(repo)
    mtime = layout.index_file.stat().st_mtime_ns
    out = atomic_write_text(layout, layout.index_file, layout.index_file.read_text(encoding="utf-8"))
    assert out.status == "unchanged" and layout.index_file.stat().st_mtime_ns == mtime


def test_existing_user_file_is_not_overwritten(repo):
    (repo / ".kitehut/knowledge").mkdir(parents=True)
    (repo / ".kitehut/knowledge/INDEX.md").write_text("MY OWN INDEX\n", encoding="utf-8")
    res = generate(repo)
    assert (repo / ".kitehut/knowledge/INDEX.md").read_text(encoding="utf-8") == "MY OWN INDEX\n"
    assert any("INDEX.md" in s for s in res.skipped)


def test_user_graph_artifact_in_fresh_output_is_never_overwritten(repo):
    """A graph-shaped file copied into a fresh .kitehut/ is user data, not ours."""
    graph_dir = repo / ".kitehut/graph"
    graph_dir.mkdir(parents=True)
    planted = {"schema_version": 1, "repository": {}, "files": [], "symbols": [], "edges": []}
    target = graph_dir / "repository.json"
    target.write_text(json.dumps(planted), encoding="utf-8")

    res = generate(repo)

    assert not res.ok
    assert any("not generated by KiteHut" in error for error in res.errors)
    assert json.loads(target.read_text(encoding="utf-8")) == planted
    assert not (repo / ".kitehut/knowledge").exists()  # preflight ran before any write


def test_user_metadata_file_blocks_before_any_write(repo):
    (repo / ".kitehut").mkdir(parents=True)
    (repo / ".kitehut/metadata.json").write_text('{"mine": true}\n', encoding="utf-8")

    res = generate(repo)

    assert not res.ok
    assert any("metadata.json" in error for error in res.errors)
    assert not (repo / ".kitehut/graph").exists()


# 14 traversal / symlink safety
def test_symlinked_output_dir_is_refused(repo, tmp_path):
    victim = tmp_path / "victim"; victim.mkdir()
    symlink_or_skip(victim, repo / ".kitehut", directory=True)
    with pytest.raises(UnsafeOutputError, match="symbolic link"):
        generate(repo)
    assert list(victim.iterdir()) == []


def test_symlinked_graph_dir_is_refused(repo, tmp_path):
    generate(repo)
    import shutil
    shutil.rmtree(repo / ".kitehut/graph")
    victim = tmp_path / "victim_graph"; victim.mkdir()
    symlink_or_skip(victim, repo / ".kitehut/graph", directory=True)
    res = generate(repo)
    assert not res.ok and any("symbolic link" in error for error in res.errors)
    assert list(victim.iterdir()) == []


def test_symlinked_subdirectory_inside_output_is_refused(repo, tmp_path):
    generate(repo)
    victim = tmp_path / "victim2"; victim.mkdir()
    import shutil
    shutil.rmtree(repo / ".kitehut/knowledge")
    symlink_or_skip(victim, repo / ".kitehut/knowledge", directory=True)
    with pytest.raises(UnsafeOutputError):
        generate(repo)
    assert list(victim.iterdir()) == []


def test_source_symlink_escaping_repo_is_not_indexed(tmp_path):
    outside = tmp_path / "outside.py"; outside.write_text("def leaked(): pass\n", encoding="utf-8")
    repo = write_tree(tmp_path / "r", {"a.py": "def a(): pass\n"})
    symlink_or_skip(outside, repo / "link.py")
    res = generate(repo)
    assert set(res.metadata.file_hashes) == {"a.py"}


def test_write_outside_output_dir_is_refused(repo, tmp_path):
    layout = OutputLayout(repo); layout.prepare()
    for bad in (repo / "pkg/core.py", tmp_path / "x.md", layout.base / ".." / "evil.md"):
        with pytest.raises(UnsafeOutputError):
            atomic_write_text(layout, bad, "x")
    assert "def main" in (repo / "pkg/core.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["../metadata.json", "../../pkg/core.py", "/etc/passwd", "a/../../x.md", "notes.txt", ""])
def test_read_knowledge_page_rejects_traversal(analyzed, page):
    from app.knowledge import read_knowledge_page
    with pytest.raises(KiteHutError):
        read_knowledge_page(analyzed[0], page)


def test_read_knowledge_page_works(analyzed):
    from app.knowledge import list_knowledge_pages, read_knowledge_page
    assert "INDEX.md" in list_knowledge_pages(analyzed[0])
    assert read_knowledge_page(analyzed[0]).startswith("<!-- kitehut:generated -->")


def test_tests_do_not_touch_real_repo_outputs():
    here = Path(__file__).resolve().parents[3]
    assert not (here / ".kitehut").exists()


def test_large_repo_can_be_regenerated_over_its_own_metadata(tmp_path):
    """Regression: marker must stay recognisable when file_hashes is large (>4 KB)."""
    repo = write_tree(tmp_path / "large", {f"pkg/module_{i:03d}.py": f"def f{i}():\n    return {i}\n" for i in range(120)})
    assert generate(repo).ok
    assert (repo / ".kitehut/metadata.json").stat().st_size > 8192
    assert (repo / ".kitehut/metadata.json").read_text(encoding="utf-8").startswith('{\n  "generator": "kitehut:generated"')
    again = refresh(repo, force=True)
    assert again.ok, again.errors


def test_target_syntax_warnings_do_not_leak(tmp_path, recwarn):
    repo = write_tree(tmp_path / "w", {"a.py": 'import re\nX = re.compile("\\d+ \\ ")\n'})
    assert generate(repo).ok
    assert not [w for w in recwarn.list if issubclass(w.category, SyntaxWarning)]
