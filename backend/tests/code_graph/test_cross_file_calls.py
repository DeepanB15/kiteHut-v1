"""Cross-file CALLS resolution: imported functions, aliases, relative imports,
``self``/``cls`` methods and module-qualified calls.

Every assertion checks a deterministic rule from
:func:`app.code_graph.relationships._resolve_calls_to_edges`: a call resolves
only when the target is unique, otherwise the call is reported as unresolved
and no edge is invented.
"""

from __future__ import annotations

import hashlib

from app.code_graph.models import EdgeKind, SourceFile, SymbolKind, compute_symbol_id
from app.code_graph.python_extractor import PythonASTExtractor
from app.code_graph.relationships import RelationshipExtractor


def _sf(relative_path: str, content: str) -> SourceFile:
    raw = content.encode("utf-8")
    return SourceFile(
        relative_path=relative_path,
        language="python",
        content=content,
        content_hash=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        line_count=content.count("\n") + 1 if content else 0,
    )


def _extract_all(source_files, repository_id="test-repo"):
    extractor = PythonASTExtractor()
    declarations = []
    for sf in source_files:
        declarations.extend(extractor.extract(sf))
    result = RelationshipExtractor().extract(
        source_files=source_files,
        declarations=declarations,
        repository_id=repository_id,
    )
    return declarations, result


def _symbol_id(decl, repository_id="test-repo") -> str:
    return compute_symbol_id(
        repository_id,
        decl.relative_path,
        decl.qualified_name,
        decl.kind,
        decl.occurrence_ordinal,
    )


def _calls_from(result, decl):
    source_id = _symbol_id(decl)
    return [e for e in result.edges if e.kind == EdgeKind.CALLS and e.source_id == source_id]


def _decl(declarations, qualified_name):
    return next(d for d in declarations if d.qualified_name == qualified_name)


def _target_of(edge) -> str:
    return edge.target_id


# ---------------------------------------------------------------------------
# Bare calls resolved through imports
# ---------------------------------------------------------------------------


def test_imported_function_call_is_resolved() -> None:
    files = [
        _sf("repositories/__init__.py", ""),
        _sf(
            "repositories/user_repository.py",
            "def get_user(user_id):\n    return {'id': user_id}\n",
        ),
        _sf(
            "services/user_service.py",
            "from repositories.user_repository import get_user\n\n"
            "def load_user(user_id):\n"
            "    return get_user(user_id)\n",
        ),
    ]
    declarations, result = _extract_all(files)
    load_user = _decl(declarations, "services/user_service.load_user")
    get_user = _decl(declarations, "repositories/user_repository.get_user")

    edges = _calls_from(result, load_user)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.target_id == _symbol_id(get_user)
    assert edge.confidence.value == "high"
    assert edge.metadata == {"callee": "get_user", "via": "import"}
    assert edge.source_file == "services/user_service.py"


def test_aliased_import_call_is_resolved() -> None:
    files = [
        _sf("services/__init__.py", ""),
        _sf("services/user.py", "def get_user(user_id):\n    return user_id\n"),
        _sf(
            "app.py",
            "from services.user import get_user as fetch_user\n\n"
            "def load():\n"
            "    return fetch_user(1)\n",
        ),
    ]
    declarations, result = _extract_all(files)
    load = _decl(declarations, "app.load")
    get_user = _decl(declarations, "services/user.get_user")

    edges = _calls_from(result, load)
    assert [edge.target_id for edge in edges] == [_symbol_id(get_user)]


def test_relative_import_call_is_resolved() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def work():\n    return 1\n"),
        _sf("pkg/service.py", "from .helper import work\n\ndef run():\n    return work()\n"),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")
    work = _decl(declarations, "pkg/helper.work")

    edges = _calls_from(result, run)
    assert [edge.target_id for edge in edges] == [_symbol_id(work)]


def test_function_local_import_stays_unresolved() -> None:
    """Conservative by design: a name bound inside the caller's own scope
    (including a function-local import) is treated as shadowed, so no edge is
    guessed.  Module-level imports are the supported case."""
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def work():\n    return 1\n"),
        _sf(
            "pkg/service.py",
            "def run():\n"
            "    from pkg.helper import work\n"
            "    return work()\n",
        ),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")
    work = _decl(declarations, "pkg/helper.work")

    assert _calls_from(result, run) == []
    assert _symbol_id(work) not in {e.target_id for e in result.edges if e.kind == EdgeKind.CALLS}
    assert [d.code for d in result.unresolved_calls] == ["call_shadowed_unresolved"]


def test_shadowed_imported_name_is_not_resolved() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def work():\n    return 1\n"),
        _sf(
            "pkg/service.py",
            "from pkg.helper import work\n\n"
            "def run(work):\n"
            "    return work()\n",
        ),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")
    work = _decl(declarations, "pkg/helper.work")

    assert _calls_from(result, run) == []
    assert _symbol_id(work) not in {e.target_id for e in result.edges if e.kind == EdgeKind.CALLS}
    assert [d.code for d in result.unresolved_calls] == ["call_shadowed_unresolved"]


# ---------------------------------------------------------------------------
# Ambiguity: never guess
# ---------------------------------------------------------------------------


def test_ambiguous_target_declarations_produce_no_edge() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def work():\n    return 1\n\ndef work():\n    return 2\n"),
        _sf("pkg/service.py", "from pkg.helper import work\n\ndef run():\n    return work()\n"),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")

    assert _calls_from(result, run) == []
    assert "call_ambiguous_cross_file" in [d.code for d in result.unresolved_calls]


def test_ambiguous_module_candidates_produce_no_edge() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/mod.py", "def work():\n    return 1\n"),
        _sf("pkg/mod/__init__.py", "def work():\n    return 2\n"),
        _sf("pkg/service.py", "from pkg.mod import work\n\ndef run():\n    return work()\n"),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")

    assert _calls_from(result, run) == []
    assert "call_ambiguous_cross_file" in [d.code for d in result.unresolved_calls]


def test_missing_imported_symbol_produces_no_edge() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def other():\n    return 1\n"),
        _sf("pkg/service.py", "from pkg.helper import work\n\ndef run():\n    return work()\n"),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "pkg/service.run")

    assert _calls_from(result, run) == []
    assert "call_unresolved_no_target" in [d.code for d in result.unresolved_calls]


# ---------------------------------------------------------------------------
# Method calls
# ---------------------------------------------------------------------------


def test_self_method_call_is_resolved() -> None:
    content = (
        "class Service:\n"
        "    def run(self):\n"
        "        return self.helper()\n"
        "\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    declarations, result = _extract_all([_sf("pkg/service.py", content)])
    run = _decl(declarations, "pkg/service.Service.run")
    helper = _decl(declarations, "pkg/service.Service.helper")

    edges = _calls_from(result, run)
    assert [e.target_id for e in edges] == [_symbol_id(helper)]
    assert edges[0].metadata["via"] == "self_method"


def test_cls_method_call_is_resolved() -> None:
    content = (
        "class Service:\n"
        "    @classmethod\n"
        "    def build(cls):\n"
        "        return cls.make()\n"
        "\n"
        "    @classmethod\n"
        "    def make(cls):\n"
        "        return cls()\n"
    )
    declarations, result = _extract_all([_sf("pkg/service.py", content)])
    build = _decl(declarations, "pkg/service.Service.build")
    make = _decl(declarations, "pkg/service.Service.make")

    edges = _calls_from(result, build)
    assert [e.target_id for e in edges] == [_symbol_id(make)]
    assert edges[0].metadata["via"] == "cls_method"


def test_same_class_method_name_in_other_class_is_not_selected() -> None:
    content = (
        "class A:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        "    def run(self):\n"
        "        return self.run()\n"
    )
    declarations, result = _extract_all([_sf("pkg/classes.py", content)])
    b_run = _decl(declarations, "pkg/classes.B.run")
    a_run = _decl(declarations, "pkg/classes.A.run")

    targets = {e.target_id for e in _calls_from(result, b_run)}
    assert targets == {_symbol_id(b_run)}
    assert _symbol_id(a_run) not in targets


def test_unrelated_attribute_call_stays_unresolved() -> None:
    content = (
        "class C:\n"
        "    def method(self):\n"
        "        return self\n"
        "\n"
        "def caller(c):\n"
        "    return c.method()\n"
    )
    declarations, result = _extract_all([_sf("pkg/attr.py", content)])
    caller = _decl(declarations, "pkg/attr.caller")

    assert _calls_from(result, caller) == []
    assert [d.code for d in result.unresolved_calls] == ["attribute_call_unresolved"]


# ---------------------------------------------------------------------------
# Module-qualified and class-method calls
# ---------------------------------------------------------------------------


def test_module_qualified_call_is_resolved() -> None:
    files = [
        _sf("repositories/__init__.py", ""),
        _sf("repositories/user_repository.py", "def get_user(user_id):\n    return user_id\n"),
        _sf(
            "services/user_service.py",
            "import repositories.user_repository as repo\n\n"
            "def load_user(user_id):\n"
            "    return repo.get_user(user_id)\n",
        ),
    ]
    declarations, result = _extract_all(files)
    load_user = _decl(declarations, "services/user_service.load_user")
    get_user = _decl(declarations, "repositories/user_repository.get_user")

    edges = _calls_from(result, load_user)
    assert [e.target_id for e in edges] == [_symbol_id(get_user)]
    assert edges[0].confidence.value == "medium"
    assert edges[0].metadata["via"] == "module_qualified"


def test_from_package_import_module_call_is_resolved() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/mod.py", "def work():\n    return 1\n"),
        _sf("app.py", "from pkg import mod\n\ndef run():\n    return mod.work()\n"),
    ]
    declarations, result = _extract_all(files)
    run = _decl(declarations, "app.run")
    work = _decl(declarations, "pkg/mod.work")

    assert [e.target_id for e in _calls_from(result, run)] == [_symbol_id(work)]


def test_class_method_call_in_same_file_is_resolved() -> None:
    content = (
        "class Engine:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "def main(engine):\n"
        "    return Engine.run(engine)\n"
    )
    declarations, result = _extract_all([_sf("pkg/core.py", content)])
    main = _decl(declarations, "pkg/core.main")
    run = _decl(declarations, "pkg/core.Engine.run")

    edges = _calls_from(result, main)
    assert [e.target_id for e in edges] == [_symbol_id(run)]
    assert edges[0].metadata["via"] == "class_method"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_cross_file_edges_are_deterministic() -> None:
    files = [
        _sf("pkg/__init__.py", ""),
        _sf("pkg/helper.py", "def work():\n    return 1\n"),
        _sf("pkg/service.py", "from pkg.helper import work\n\ndef run():\n    return work()\n"),
    ]
    first = _extract_all(files)[1]
    second = _extract_all(list(reversed(files)))[1]
    assert [e.id for e in first.edges] == [e.id for e in second.edges]
