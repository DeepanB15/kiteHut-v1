"""End-to-end tests for :class:`ImpactWorkflow` against real Git repositories.

Defect coverage: D1 (changed-symbol precision), D5 (frozen report contract),
D7 (deletion attribution), D8 (effective staged-only metadata), plus the
report schema, ranking policy metadata and determinism requirements.
"""

from __future__ import annotations

import json

import pytest

from app.git_ops.change_detection import ChangeDetector
from app.git_ops.impact_service import (
    ImpactWorkflowError,
    ImpactWorkflowOptions,
    create_impact_workflow,
)

CORE = (
    "LIMIT = 10\n"
    "\n"
    "\n"
    "def compute(x):\n"
    "    return x * 2\n"
    "\n"
    "\n"
    "def helper(y):\n"
    "    return y + 1\n"
)

API = "from core import compute\n\n\ndef run(value):\n    return compute(value)\n"


def report(repo, **options):
    workflow = create_impact_workflow(repo.root, ImpactWorkflowOptions(**options))
    return workflow.run().to_dict()


def changed_symbol_names(payload) -> list[str]:
    return [symbol["qualified_name"] for symbol in payload["changed_symbols"]]


@pytest.fixture
def project(repo_factory):
    return repo_factory({"core.py": CORE, "api.py": API})


# ---------------------------------------------------------------------------
# D1 — changed-symbol precision
# ---------------------------------------------------------------------------


def test_edit_inside_one_function_reports_only_that_symbol(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert changed_symbol_names(payload) == ["core.compute"]
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["path"] == "core.py"
    assert mapping["precision"] == "symbol_level"
    assert mapping["matched_symbol_count"] == 1
    assert payload["summary"]["symbol_level_file_count"] == 1
    assert payload["summary"]["file_level_only_file_count"] == 0


def test_neighbouring_function_is_not_marked_changed(project):
    """The ±3 context window must not leak into the neighbouring function."""
    # helper() sits immediately after compute(), so Git's context overlaps it.
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert "core.helper" not in changed_symbol_names(payload)
    assert payload["symbol_mapping"]["files"][0]["reason"] is None


def test_change_spanning_two_functions_reports_both(project):
    project.write(
        "core.py",
        CORE.replace("    return x * 2", "    return x * 2 + 1").replace(
            "    return y + 1", "    return y + 2"
        ),
    )

    payload = report(project)

    assert changed_symbol_names(payload) == ["core.compute", "core.helper"]
    assert payload["symbol_mapping"]["files"][0]["precision"] == "symbol_level"


def test_module_level_change_falls_back_to_file_level(project):
    project.write("core.py", CORE.replace("LIMIT = 10", "LIMIT = 20"))

    payload = report(project)

    assert changed_symbol_names(payload) == []
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
    assert "module-level" in mapping["reason"]
    assert any(
        "file-level only" in warning and "module-level" in warning
        for warning in payload["analysis_warnings"]
    )
    # The file itself is still reported as changed and still propagates impact.
    assert payload["changed_files"][0]["path"] == "core.py"
    assert payload["summary"]["changed_file_count"] == 1


def test_new_file_uses_file_expansion_resolution(project):
    project.write("extra.py", "def extra():\n    return 1\n")

    payload = report(project)

    resolutions = {
        symbol["resolution"]
        for symbol in payload["changed_symbols"]
        if symbol["path"] == "extra.py"
    }
    assert resolutions == {"file_expansion"}
    assert "extra.extra" in changed_symbol_names(payload)


# ---------------------------------------------------------------------------
# D7 — deletion attribution
# ---------------------------------------------------------------------------


def test_wholly_deleted_function_is_not_blamed_on_a_neighbour(project):
    # Removing helper() (and its blank lines) leaves compute() adjacent.
    project.write("core.py", CORE.replace("def helper(y):\n    return y + 1\n", ""))

    payload = report(project)

    assert "core.compute" not in changed_symbol_names(payload)
    assert changed_symbol_names(payload) == []
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
    assert "removed block" in mapping["reason"]
    assert any("file-level only" in w for w in payload["analysis_warnings"])


def test_line_removed_inside_a_function_is_attributed_to_it(repo_factory):
    with_extra_line = CORE.replace(
        "    return x * 2", "    x = x\n    return x * 2"
    )
    repo = repo_factory({"core.py": with_extra_line})
    # Remove only the extra statement: a pure removal inside compute().
    repo.write("core.py", CORE)

    payload = report(repo)

    assert changed_symbol_names(payload) == ["core.compute"]
    assert payload["symbol_mapping"]["files"][0]["precision"] == "symbol_level"


THREE_FUNCTIONS = (
    "def first():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def middle():\n"
    "    return 2\n"
    "\n"
    "\n"
    "def last():\n"
    "    return 3\n"
)


@pytest.mark.parametrize(
    "removed, survivor",
    [
        ("def first():\n    return 1\n\n\n", "first"),
        ("def middle():\n    return 2\n\n\n", "middle"),
        ("\n\ndef last():\n    return 3\n", "last"),
    ],
)
def test_deleting_a_whole_function_never_blames_a_neighbour(
    repo_factory, removed, survivor
):
    repo = repo_factory({"m.py": THREE_FUNCTIONS})
    repo.write("m.py", THREE_FUNCTIONS.replace(removed, ""))

    payload = report(repo)

    names = changed_symbol_names(payload)
    assert names == []
    assert survivor not in names
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
    assert any("file-level only" in warning for warning in payload["analysis_warnings"])


def test_deleted_file_is_reported_but_never_traversed(project):
    project.remove("core.py")

    payload = report(project)

    deleted = payload["changed_files"][0]
    assert deleted["path"] == "core.py"
    assert deleted["status"] == "deleted"
    assert deleted["is_deleted"] is True
    assert payload["summary"]["deleted_change_count"] == 1
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
    assert "deleted" in mapping["reason"]
    assert any("never traversed" in line for line in payload["limitations"])


# ---------------------------------------------------------------------------
# Untracked, staged and renamed changes
# ---------------------------------------------------------------------------


def test_untracked_nested_package_files_are_analyzed(project):
    project.write("newpkg/__init__.py", "")
    project.write("newpkg/mod.py", "def mod():\n    return 1\n")
    project.write("newpkg/nested/deep.py", "def deep():\n    return 2\n")

    payload = report(project)

    paths = {entry["path"] for entry in payload["changed_files"]}
    assert {"newpkg/mod.py", "newpkg/nested/deep.py"} <= paths
    assert "newpkg" not in paths
    names = changed_symbol_names(payload)
    assert "newpkg/mod.mod" in names
    assert "newpkg/nested/deep.deep" in names
    assert all(
        entry["status"] == "untracked"
        for entry in payload["changed_files"]
        if entry["path"].startswith("newpkg/")
    )


def test_staged_change_is_reported_with_staged_metadata(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))
    project.git("add", "core.py")

    payload = report(project)

    entry = payload["changed_files"][0]
    assert entry["is_staged"] is True
    assert entry["is_unstaged"] is False
    assert entry["change_source"] == "staged"
    assert changed_symbol_names(payload) == ["core.compute"]


def test_staged_and_unstaged_change_is_attributed_from_the_working_tree(project):
    project.write("core.py", CORE.replace("LIMIT = 10", "LIMIT = 11"))
    project.git("add", "core.py")
    project.write(
        "core.py",
        CORE.replace("LIMIT = 10", "LIMIT = 11").replace(
            "    return x * 2", "    return x * 3"
        ),
    )

    payload = report(project)

    entry = payload["changed_files"][0]
    assert entry["is_staged"] is True and entry["is_unstaged"] is True
    assert any("both staged and unstaged" in w for w in payload["analysis_warnings"])
    # The working-tree change is inside compute(); the staged constant edit is
    # not combined into the same coordinate space.
    assert changed_symbol_names(payload) == ["core.compute"]


def test_rename_reports_previous_path_and_file_level(project):
    project.git("mv", "core.py", "renamed_core.py")

    payload = report(project)

    entry = payload["changed_files"][0]
    assert entry["path"] == "renamed_core.py"
    assert entry["previous_path"] == "core.py"
    assert entry["status"] == "renamed"
    assert entry["is_renamed"] is True
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
    assert "no hunks" in mapping["reason"]


# ---------------------------------------------------------------------------
# Detection errors (D3)
# ---------------------------------------------------------------------------


def test_detection_errors_are_surfaced_and_mark_the_report_degraded(project, monkeypatch):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    def boom(self, entry, *, args, description):
        message = f"Could not read the {description} diff for {entry.path}: boom (exit 128)"
        self.errors.append(message)
        self.warnings.append(message)
        return None

    monkeypatch.setattr(ChangeDetector, "_unit_diff", boom)
    payload = report(project)

    detection = payload["change_detection"]
    assert detection["degraded"] is True
    assert detection["errors"]
    assert "boom" in detection["errors"][0]
    assert any("degraded" in line for line in payload["limitations"])
    # The file is still reported; it simply has no symbol attribution.
    assert payload["changed_files"][0]["path"] == "core.py"
    assert payload["symbol_mapping"]["files"][0]["precision"] == "file_level_only"


def test_clean_detection_reports_no_errors(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert payload["change_detection"]["degraded"] is False
    assert payload["change_detection"]["errors"] == []


def test_fatal_detection_failure_raises(project):
    with pytest.raises(ImpactWorkflowError):
        report(project, mode="base", base_revision="no-such-revision")


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


def test_unscoped_run_analyzes_every_changed_file(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))
    project.write(
        "api.py", API.replace("return compute(value)", "return compute(value) + 1")
    )

    payload = report(project)

    assert payload["mode"]["scope"] is None
    assert payload["mode"]["changes_outside_scope"] == 0
    assert payload["changes_outside_scope"] == []
    assert payload["summary"]["changed_file_count"] == 2
    assert payload["summary"]["symbol_level_file_count"] == 2


def test_scope_limits_analyzed_files_and_lists_the_rest(repo_factory):
    repo = repo_factory(
        {
            "pkg/a.py": "def a():\n    return 1\n",
            "pkg/sub/b.py": "def b():\n    return 2\n",
            "other.py": "def c():\n    return 3\n",
        }
    )
    repo.write("pkg/a.py", "def a():\n    return 11\n")
    repo.write("other.py", "def c():\n    return 33\n")

    workflow = create_impact_workflow(repo.root / "pkg", ImpactWorkflowOptions())
    payload = workflow.run().to_dict()

    assert payload["mode"]["scope"] == "pkg"
    assert [entry["path"] for entry in payload["changed_files"]] == ["pkg/a.py"]
    assert [entry["path"] for entry in payload["changes_outside_scope"]] == ["other.py"]
    assert payload["mode"]["changes_outside_scope"] == 1
    assert payload["summary"]["changed_file_count_outside_scope"] == 1
    assert any("outside the requested scope" in line for line in payload["limitations"])


def test_nested_scope_keeps_deeper_files_and_excludes_siblings(repo_factory):
    repo = repo_factory(
        {
            "pkg/a.py": "def a():\n    return 1\n",
            "pkg/sub/b.py": "def b():\n    return 2\n",
        }
    )
    repo.write("pkg/a.py", "def a():\n    return 11\n")
    repo.write("pkg/sub/b.py", "def b():\n    return 22\n")

    workflow = create_impact_workflow(repo.root / "pkg" / "sub", ImpactWorkflowOptions())
    payload = workflow.run().to_dict()

    assert payload["mode"]["scope"] == "pkg/sub"
    assert [entry["path"] for entry in payload["changed_files"]] == ["pkg/sub/b.py"]
    assert [entry["path"] for entry in payload["changes_outside_scope"]] == ["pkg/a.py"]


# ---------------------------------------------------------------------------
# Graph source metadata
# ---------------------------------------------------------------------------


def test_fresh_build_reports_its_source_and_freshness(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project, graph_source="build")

    assert payload["graph"]["source"] == "build"
    assert payload["graph"]["fresh"] is True
    assert payload["repository"]["graph_source"] == "build"
    assert payload["workflow"]["graph_source"] == "build"
    assert payload["repository"]["relationship_count"] == payload["graph"]["edge_count"]


def test_cached_source_without_a_cache_raises(project):
    with pytest.raises(ImpactWorkflowError):
        report(project, graph_source="cached")


# ---------------------------------------------------------------------------
# Report contract (D5) and static-only flags
# ---------------------------------------------------------------------------


CONTRACT_KEYS = {
    "schema_version",
    "policy",
    "mode",
    "repository",
    "changeset_summary",
    "changed_files",
    "changed_symbols",
    "mapped_changes",
    "unmapped_changes",
    "direct_impacts",
    "transitive_impacts",
    "dependency_paths",
    "ranked_impacts",
    "unconfirmed_impacts",
    "evidence",
    "ranking",
    "analysis_warnings",
    "truncation",
    "summary",
    "limitations",
}


def test_report_contains_every_contract_section(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert CONTRACT_KEYS <= set(payload)
    assert payload["schema_version"] == 1


def test_root_flags_are_static_only(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert payload["navigation_only"] is True
    assert payload["static_impact_only"] is True
    assert payload["runtime_failure_verified"] is False


def test_contract_uses_the_approved_field_names(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    # D5: design names, not the pre-freeze placeholders.
    assert "changes_outside_scope" in payload["mode"]
    assert "changes_outside_scope_count" not in payload["mode"]
    assert "graph_source" in payload["repository"]
    assert "relationship_count" in payload["repository"]
    assert "edge_count" not in payload["repository"]
    for record in payload["ranked_impacts"]:
        assert "original_impact" in record
        assert "record" not in record


def test_mapped_and_unmapped_changes_agree_with_the_graph(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    mapped = payload["mapped_changes"][0]
    assert mapped["path"] == "core.py"
    assert mapped["mapping_confidence"] == "exact"
    assert mapped["symbol_count"] == len(mapped["symbol_ids"]) > 0
    assert payload["unmapped_changes"] == []


def test_ranking_policy_metadata_is_unchanged(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert payload["policy"]["ranking_version"] == "v0.2.0"
    assert payload["policy"]["calibration"]
    assert sum(payload["ranking"]["factor_weights"].values()) == pytest.approx(1.0)
    assert [band[1] for band in payload["ranking"]["tier_bands"]] == [
        "P0",
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
    ]
    assert payload["ranking"]["tier_bands"][0][0] == 85.0
    assert payload["ranking"]["tier_bands"][-1][0] == 0.0
    for record in payload["ranked_impacts"]:
        assert record["confirmed"] is True
        assert record["explanation"]
        assert record["factor_contributions"]


def test_unconfirmed_impacts_never_appear_in_the_ranked_list(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project)

    assert all(record["confirmed"] for record in payload["ranked_impacts"])
    assert all(
        record["confirmed"] is False for record in payload["unconfirmed_impacts"]
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_independent_runs_produce_byte_identical_json(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))
    project.write("extra.py", "def extra():\n    return 1\n")

    first = create_impact_workflow(project.root, ImpactWorkflowOptions()).run().to_json()
    second = create_impact_workflow(project.root, ImpactWorkflowOptions()).run().to_json()

    assert first == second
    assert json.loads(first) == json.loads(second)


def test_json_contains_no_absolute_paths_or_timestamps(project):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    serialized = create_impact_workflow(project.root, ImpactWorkflowOptions()).run().to_json()

    assert str(project.root) not in serialized
    assert project.root.as_posix() not in serialized
    assert "generated_at" not in serialized
    assert "C:\\\\" not in serialized


# ---------------------------------------------------------------------------
# D8 — effective staged-only metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options, expected_mode, expected_staged_only",
    [
        ({}, "working_tree", False),
        ({"mode": "staged"}, "staged", True),
        ({"staged_only": True}, "working_tree", True),
    ],
)
def test_staged_only_metadata_reflects_the_effective_mode(
    project, options, expected_mode, expected_staged_only
):
    project.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))

    payload = report(project, **options)

    assert payload["mode"]["detection"] == expected_mode
    assert payload["mode"]["staged_only"] is expected_staged_only
    assert payload["workflow"]["staged_only"] is expected_staged_only


def test_base_comparison_reports_staged_only_as_inactive(repo_factory):
    repo = repo_factory({"core.py": CORE})
    repo.tag("base")
    repo.write("core.py", CORE.replace("    return x * 2", "    return x * 3"))
    repo.commit("second")

    payload = report(repo, base_revision="base")

    assert payload["mode"]["detection"] == "base"
    assert payload["mode"]["staged_only"] is False
    assert payload["workflow"]["staged_only"] is False
    assert payload["workflow"]["base_comparison_excludes_working_tree"] is True


def test_base_comparison_ignores_working_tree_edits(repo_factory):
    repo = repo_factory({"core.py": CORE})
    repo.tag("base")
    # A committed change and a separate uncommitted one.
    repo.write("core.py", CORE.replace("LIMIT = 10", "LIMIT = 11"))
    repo.commit("second")
    repo.write("core.py", CORE.replace("LIMIT = 11", "LIMIT = 12").replace(
        "    return y + 1", "    return y + 99"
    ))

    payload = report(repo, base_revision="base")

    # Only the committed constant edit is in scope; the dirty helper() edit is not.
    assert changed_symbol_names(payload) == []
    mapping = payload["symbol_mapping"]["files"][0]
    assert mapping["precision"] == "file_level_only"
