"""Phase 11.8 — focused tests for the query helpers and NetworkX projection.

A purpose-built toy repository is scanned/built once per module; the tests
exercise exact/partial symbol lookup, CALLS/IMPORTS traversals, depth
bounds, cycle termination, deterministic ordering and provenance payloads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.code_graph import CodeGraphBuilder
from app.code_graph.graph_runtime import to_networkx
from app.code_graph.models import EdgeKind
from app.code_graph.queries import (
    callers,
    callees,
    dependents,
    find_symbols,
    imports,
)


def _write(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def sample_repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("graph_repo")
    _write(
        root,
        "pkg/__init__.py",
        "from pkg.util import helper\n\n\ndef main():\n    return helper()\n",
    )
    _write(root, "pkg/util.py", "def helper():\n    return 1\n")
    _write(root, "pkg/other.py", "def other():\n    return 2\n")
    return root


@pytest.fixture(scope="module")
def graph(sample_repo: Path):
    result = CodeGraphBuilder(sample_repo).build()
    assert result.graph is not None
    return result.graph


# ------------------------------------------------------- symbol lookup ----


def test_find_symbols_exact_match(graph) -> None:
    matches = find_symbols(graph, "helper")
    assert matches, "expected exact matches for 'helper'"
    assert all(s.name == "helper" for s in matches)


def test_find_symbols_exact_qualified_name(graph) -> None:
    target = next(s for s in graph.symbols if s.name == "helper")
    matches = find_symbols(graph, target.qualified_name)
    assert [s.qualified_name for s in matches] == [target.qualified_name]


def test_find_symbols_partial_fallback(graph) -> None:
    matches = find_symbols(graph, "help")
    assert any(s.name == "helper" for s in matches)


def test_find_symbols_unknown_returns_empty(graph) -> None:
    assert find_symbols(graph, "definitely_not_here_xyz") == []


def test_find_symbols_empty_query_returns_empty(graph) -> None:
    assert find_symbols(graph, "") == []


def test_find_symbols_deterministic_order(graph) -> None:
    first = find_symbols(graph, "help")
    second = find_symbols(graph, "help")
    assert first and second
    assert [s.id for s in first] == [s.id for s in second]


# ------------------------------------------------------- call traversal ---


def test_callees_returns_call_edges(graph) -> None:
    main_symbol = next(s for s in graph.symbols if s.name == "main")
    result = callees(graph, main_symbol.id)
    assert all(hit.kind == EdgeKind.CALLS for hit in result)
    assert all(hit.source_id == main_symbol.id for hit in result)


def test_callers_unknown_symbol_returns_empty(graph) -> None:
    result = callers(graph, "symbol:v1:" + "0" * 64)
    assert len(result) == 0


def test_callees_depth_zero_returns_empty(graph) -> None:
    main_symbol = next(s for s in graph.symbols if s.name == "main")
    assert len(callees(graph, main_symbol.id, depth=0)) == 0


def test_depth_bound_is_respected(graph) -> None:
    main_symbol = next(s for s in graph.symbols if s.name == "main")
    shallow = callees(graph, main_symbol.id, depth=1)
    deep = callees(graph, main_symbol.id, depth=3)
    assert len(shallow) <= len(deep)
    assert all(hit.depth <= 3 for hit in deep)


def test_traversal_terminates_on_cycles(graph) -> None:
    # The toy repository contains no cycles, but the traversal must still
    # terminate and never exceed the supplied depth.
    main_symbol = next(s for s in graph.symbols if s.name == "main")
    result = callees(graph, main_symbol.id, depth=10)
    assert len(result) < 10 * max(len(graph.edges), 1) + 1


def test_callers_hit_carries_provenance(graph) -> None:
    main_symbol = next(s for s in graph.symbols if s.name == "main")
    outgoing = callees(graph, main_symbol.id)
    for hit in outgoing:
        provenance = hit.provenance()
        assert provenance["source_file"]
        assert provenance["confidence"]
        assert provenance["resolution"]


# ------------------------------------------------------- import traversal --


def test_imports_outgoing_edges(graph) -> None:
    init_file = next(f for f in graph.files if f.relative_path == "pkg/__init__.py")
    result = imports(graph, init_file.id)
    assert all(hit.kind == EdgeKind.IMPORTS for hit in result)
    assert all(hit.source_id == init_file.id for hit in result)


def test_dependents_inverse_of_imports(graph) -> None:
    util_file = next(f for f in graph.files if f.relative_path == "pkg/util.py")
    incoming = dependents(graph, util_file.id)
    assert all(hit.kind == EdgeKind.IMPORTS for hit in incoming)
    assert all(hit.target_id == util_file.id for hit in incoming)


def test_imports_unknown_file_returns_empty(graph) -> None:
    assert len(imports(graph, "file:v1:" + "0" * 64)) == 0


# ------------------------------------------------------- networkx view -----


def test_to_networkx_projection_preserves_counts(graph) -> None:
    projected = to_networkx(graph)
    node_count = 1 + len(graph.files) + len(graph.symbols)
    assert projected.number_of_nodes() == node_count
    assert projected.number_of_edges() == len(graph.edges)


def test_to_networkx_edge_attributes(graph) -> None:
    projected = to_networkx(graph)
    assert projected.number_of_edges() > 0
    for _, _, _, attrs in projected.edges(keys=True, data=True):
        assert attrs["kind"] in {"contains", "defines", "imports", "calls"}
        assert attrs["provenance_kind"]
        assert attrs["confidence"]
        assert attrs["resolution"]


def test_to_networkx_parallel_edges_preserved(graph) -> None:
    projected = to_networkx(graph)
    # MultiDiGraph must allow parallel edges; total edge count matches.
    assert projected.is_multigraph()
    assert projected.number_of_edges() == len(graph.edges)


# ------------------------------------------------ impact & import cycles ----


@pytest.fixture(scope="module")
def cyclic_repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cyclic_repo")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/a.py", "from pkg.b import bfun\n\ndef afun():\n    return bfun()\n")
    _write(root, "pkg/b.py", "from pkg.a import afun\n\ndef bfun():\n    return 1\n")
    _write(root, "pkg/c.py", "from pkg.a import afun\n\ndef cfun():\n    return afun()\n")
    _write(root, "pkg/d.py", "from pkg.c import cfun\n\ndef dfun():\n    return cfun()\n")
    return root


@pytest.fixture(scope="module")
def cyclic_graph(cyclic_repo: Path):
    result = CodeGraphBuilder(cyclic_repo).build()
    assert result.graph is not None
    return result.graph


def test_import_cycles_detects_members_and_edges(cyclic_graph) -> None:
    from app.code_graph.queries import import_cycles

    cycles = import_cycles(cyclic_graph)
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.members == ["pkg/a.py", "pkg/b.py"]
    assert {edge.source_file for edge in cycle.edges} == {"pkg/a.py", "pkg/b.py"}
    assert all(edge.start_line >= 1 for edge in cycle.edges)
    description = cycle.describe()
    assert "pkg/a.py" in description and "pkg/b.py" in description
    assert "pkg/a.py:" in description  # source location, not just the path


def test_import_cycles_absent_in_acyclic_graph(graph) -> None:
    from app.code_graph.queries import import_cycles

    assert import_cycles(graph) == []


def test_impacted_files_is_transitive_and_bounded(cyclic_graph) -> None:
    from app.code_graph.queries import impacted_files

    target = next(f for f in cyclic_graph.files if f.relative_path == "pkg/a.py")
    impacted = impacted_files(cyclic_graph, target.id, max_depth=3)
    assert [(item.relative_path, item.depth) for item in impacted] == [
        ("pkg/b.py", 1),
        ("pkg/c.py", 1),
        ("pkg/d.py", 2),
    ]
    assert impacted_files(cyclic_graph, target.id, max_depth=0) == []


def test_impacted_symbols_follow_resolved_calls(cyclic_graph) -> None:
    from app.code_graph.queries import impacted_symbols

    bfun = next(s for s in cyclic_graph.symbols if s.qualified_name == "pkg/b.bfun")
    impacted = impacted_symbols(cyclic_graph, bfun.id, max_depth=2)
    assert [(item.qualified_name, item.depth) for item in impacted] == [
        ("pkg/a.afun", 1),
        ("pkg/c.cfun", 2),
    ]
    assert all(item.relationship == "calls" for item in impacted)


def test_impact_entry_carries_edge_provenance(cyclic_graph) -> None:
    from app.code_graph.queries import impacted_files

    target = next(f for f in cyclic_graph.files if f.relative_path == "pkg/a.py")
    first = impacted_files(cyclic_graph, target.id)[0]
    assert first.edge.kind == EdgeKind.IMPORTS
    assert first.edge.source_file == first.relative_path
    assert first.edge.start_line >= 1
