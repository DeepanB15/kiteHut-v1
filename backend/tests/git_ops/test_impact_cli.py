"""Tests for `kitehut impact` (Phase 3F Subphase C).

Every test runs against a real throwaway Git repository.  The command is
invoked through ``click.testing.CliRunner`` so exit codes and stream separation
are exercised exactly as a user would see them.

Exit-code contract under test::

    0  analysis completed cleanly
    1  workflow / repository error (ImpactWorkflowError)
    2  usage error (raised by click itself)
    3  analysis completed with warnings, degraded detection or truncation
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from app.cli.main import cli

pytestmark = pytest.mark.usefixtures("repo_factory")


SIMPLE_MODULE = (
    "def helper():\n"
    "    return 42\n"
    "\n"
    "\n"
    "def main():\n"
    "    return helper()\n"
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke_json(runner: CliRunner, *args: str):
    """Invoke the CLI in JSON mode and return ``(result, payload)``."""
    result = runner.invoke(cli, ["impact", *args, "--json"])
    assert result.stdout, f"no stdout; exit={result.exit_code} stderr={result.stderr!r}"
    payload = json.loads(result.stdout)  # must be valid JSON, stdout only
    return result, payload


class TestImpactCommandBasics:
    def test_clean_repository_exits_zero(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result = runner.invoke(cli, ["impact", str(repo.root)])
        assert result.exit_code == 0, result.output
        assert "Impact Analysis" in result.output
        assert "Change units: 0" in result.output

    def test_command_is_registered_on_the_root_group(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "impact" in result.output

    def test_help_lists_approved_options(self, runner):
        result = runner.invoke(cli, ["impact", "--help"])
        assert result.exit_code == 0
        for option in (
            "--base",
            "--working-tree",
            "--no-working-tree",
            "--staged-only",
            "--untracked",
            "--depth",
            "--kinds",
            "--exclusions",
            "--max-results",
            "--max-ranked",
            "--graph",
            "--max-files",
            "--exclude",
            "--json",
            "--no-json",
            "--ranked",
            "--no-ranked",
        ):
            assert option in result.output, f"{option} missing from --help"

    def test_modified_file_is_detected(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE + "\n\ndef extra():\n    return 0\n")

        result, payload = invoke_json(runner, str(repo.root))
        assert result.exit_code == 0
        assert payload["changeset_summary"]["unit_count"] == 1
        assert payload["changed_files"][0]["path"] == "pkg/main.py"
        assert payload["changed_files"][0]["status"] == "modified"

    def test_human_readable_mode_reports_sections(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "43"))

        result = runner.invoke(cli, ["impact", str(repo.root)])
        assert result.exit_code == 0
        output = result.output
        for heading in (
            "=== Impact Analysis:",
            "=== Detection ===",
            "=== Detected Changes ===",
            "=== Mapping ===",
            "=== Impacts ===",
            "=== Analysis Type ===",
        ):
            assert heading in output, f"missing section {heading!r}"
        assert "Changed files: 1" in output
        assert "static_impact_only: true" in output
        assert "runtime_failure_verified: false" in output

    def test_no_changes_reports_empty_changeset(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result, payload = invoke_json(runner, str(repo.root))
        assert result.exit_code == 0
        assert payload["changeset_summary"]["has_changes"] is False
        assert payload["changed_files"] == []
        assert payload["ranked_impacts"] == []
        assert payload["summary"]["impacted_node_count"] == 0


class TestImpactJsonOutput:
    def test_json_stdout_is_valid_and_parseable(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result, payload = invoke_json(runner, str(repo.root))
        assert result.exit_code == 0
        assert isinstance(payload, dict)
        assert payload["schema_version"] == 1

    def test_json_stdout_contains_only_json(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result, payload = invoke_json(runner, str(repo.root))
        # stdout parses with no trailing/leading prose.
        assert json.loads(result.stdout) == payload
        # Diagnostics (if any) belong to stderr, never the JSON stream.
        assert "static impact analysis" not in result.stdout

    def test_json_report_matches_approved_contract(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "99"))
        _, payload = invoke_json(runner, str(repo.root))

        for section in (
            "schema_version",
            "policy",
            "mode",
            "workflow",
            "repository",
            "graph",
            "change_detection",
            "changeset_summary",
            "changed_files",
            "changes_outside_scope",
            "changed_symbols",
            "symbol_mapping",
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
        ):
            assert section in payload, f"missing contract section {section!r}"

        assert payload["mode"]["changes_outside_scope"] == 0
        assert "graph_source" in payload["repository"]
        assert "relationship_count" in payload["repository"]
        assert "ranking_version" in payload["policy"]

    def test_root_safety_flags(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "7"))

        result, payload = invoke_json(runner, str(repo.root))
        assert result.exit_code == 0
        assert payload["navigation_only"] is True
        assert payload["static_impact_only"] is True
        assert payload["runtime_failure_verified"] is False

    def test_json_output_is_deterministic(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "11"))

        first, _ = invoke_json(runner, str(repo.root))
        second, _ = invoke_json(runner, str(repo.root))
        assert first.stdout == second.stdout

    def test_json_contains_no_absolute_paths(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "13"))
        result, payload = invoke_json(runner, str(repo.root))

        assert str(repo.root) not in result.stdout
        assert "\\" not in payload["changed_files"][0]["path"]


class TestImpactModes:
    def test_base_revision_mode(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        first = repo.commit("second")
        # A committed change after the base.
        repo.write("pkg/main.py", SIMPLE_MODULE + "\n\ndef later():\n    return 1\n")
        repo.commit("third")

        result, payload = invoke_json(runner, str(repo.root), "--base", "HEAD~1")
        assert result.exit_code == 0
        assert payload["mode"]["detection"] == "base"
        assert payload["mode"]["base_revision"] == "HEAD~1"
        assert payload["changeset_summary"]["unit_count"] == 1

    def test_base_revision_excludes_working_tree_edits(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.commit("second")
        # Dirty working tree only: must not appear in a base comparison.
        repo.write("pkg/main.py", SIMPLE_MODULE + "\n\ndef dirty():\n    return 2\n")

        _, payload = invoke_json(runner, str(repo.root), "--base", "HEAD~1")
        paths = {record["path"] for record in payload["changed_files"]}
        assert "pkg/main.py" in paths  # committed delta exists
        assert payload["workflow"]["base_comparison_excludes_working_tree"] is True

    def test_staged_only_mode(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE, "pkg/other.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "50"))
        repo.git("add", "pkg/main.py")
        # Unstaged-only edit in a second file must be invisible.
        repo.write("pkg/other.py", SIMPLE_MODULE.replace("42", "51"))

        result, payload = invoke_json(runner, str(repo.root), "--staged-only")
        assert result.exit_code == 0
        assert payload["workflow"]["staged_only"] is True
        paths = {record["path"] for record in payload["changed_files"]}
        assert "pkg/main.py" in paths
        assert "pkg/other.py" not in paths

    def test_no_working_tree_selects_staged_detection(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "60"))
        repo.git("add", "pkg/main.py")

        result, payload = invoke_json(runner, str(repo.root), "--no-working-tree")
        assert result.exit_code == 0
        assert payload["mode"]["detection"] == "staged"

    def test_untracked_toggle(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/new.py", "def fresh():\n    return 1\n")

        _, with_untracked = invoke_json(runner, str(repo.root))
        assert "pkg/new.py" in {r["path"] for r in with_untracked["changed_files"]}

        _, without_untracked = invoke_json(runner, str(repo.root), "--no-untracked")
        assert "pkg/new.py" not in {
            r["path"] for r in without_untracked["changed_files"]
        }


class TestImpactGraphModes:
    def test_cached_graph_without_cache_errors(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "70"))

        result = runner.invoke(
            cli, ["impact", str(repo.root), "--graph", "cached"]
        )
        assert result.exit_code == 1
        assert "cache" in result.output.lower() or "graph" in result.output.lower()

    def test_cached_graph_mode_reports_source(self, runner, repo_factory):
        """When a graph is cached, ``--graph cached`` loads it and says so."""
        from app.code_graph import CodeGraphBuilder
        from app.knowledge.store import save_graph

        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        graph = CodeGraphBuilder(repo.root).build().graph
        assert graph is not None
        save_graph(repo.root, graph)

        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "71"))

        result, payload = invoke_json(runner, str(repo.root), "--graph", "cached")
        assert result.exit_code == 0
        assert payload["repository"]["graph_source"] == "cached"
        assert payload["graph"]["source"] == "cached"


class TestImpactErrors:
    def test_invalid_base_revision_exits_one(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result = runner.invoke(cli, ["impact", str(repo.root), "--base", "no-such-rev"])
        assert result.exit_code == 1
        assert result.output.strip()

    def test_missing_path_is_a_usage_error(self, runner):
        result = runner.invoke(cli, ["impact", "/definitely/not/a/repository"])
        assert result.exit_code == 2

    def test_non_git_directory_exits_one(self, runner, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "main.py").write_text(SIMPLE_MODULE, encoding="utf-8")

        result = runner.invoke(cli, ["impact", str(plain)])
        assert result.exit_code == 1
        assert "git" in result.output.lower()

    def test_staged_only_with_base_is_rejected(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result = runner.invoke(
            cli, ["impact", str(repo.root), "--base", "HEAD", "--staged-only"]
        )
        assert result.exit_code == 1
        assert "staged" in result.output.lower()

    def test_unknown_edge_kind_is_rejected(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result = runner.invoke(
            cli, ["impact", str(repo.root), "--kinds", "contains"]
        )
        assert result.exit_code == 1
        assert "kind" in result.output.lower()

    def test_invalid_max_ranked_is_a_usage_error(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        result = runner.invoke(cli, ["impact", str(repo.root), "--max-ranked", "0"])
        assert result.exit_code == 2


class TestImpactWarningsAndTruncation:
    def test_degraded_detection_reports_and_exits_three(
        self, runner, repo_factory, monkeypatch
    ):
        from app.git_ops import change_detection as change_detection_module

        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "80"))

        original = change_detection_module.ChangeDetector.detect

        def degraded_detect(self, *args, **kwargs):
            changeset = original(self, *args, **kwargs)
            self.errors.append("simulated: could not read a file diff")
            return changeset

        monkeypatch.setattr(
            change_detection_module.ChangeDetector, "detect", degraded_detect
        )

        result, payload = invoke_json(runner, str(repo.root))
        assert result.exit_code == 3
        assert payload["change_detection"]["degraded"] is True
        assert payload["change_detection"]["errors"]

        human = runner.invoke(cli, ["impact", str(repo.root)])
        assert human.exit_code == 3
        assert "degraded" in human.output.lower()

    def test_truncated_ranking_reports_flags_and_exits_three(
        self, runner, repo_factory
    ):
        repo = repo_factory(
            {
                "pkg/a.py": "def a():\n    return 1\n",
                "pkg/b.py": "from pkg.a import a\n\ndef b():\n    return a()\n",
                "pkg/c.py": "from pkg.b import b\n\ndef c():\n    return b()\n",
            }
        )
        repo.write("pkg/a.py", "def a():\n    return 100\n")

        result, payload = invoke_json(runner, str(repo.root), "--max-ranked", "1")
        assert result.exit_code in (0, 3)
        assert payload["truncation"]["max_ranked"] == 1
        if payload["truncation"]["ranking_truncated"]:
            assert result.exit_code == 3

    def test_max_results_reaches_the_report(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "90"))

        _, payload = invoke_json(runner, str(repo.root), "--max-results", "5")
        assert payload["truncation"]["max_results"] == 5
        assert payload["workflow"]["max_results"] == 5

    def test_depth_and_kinds_reach_the_report(self, runner, repo_factory):
        repo = repo_factory({"pkg/main.py": SIMPLE_MODULE})
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "91"))

        _, payload = invoke_json(
            runner, str(repo.root), "--depth", "2", "--kinds", "imports"
        )
        assert payload["workflow"]["max_depth"] == 2
        assert payload["workflow"]["kinds"] == ["imports"]


class TestImpactScopeAndRankingPresentation:
    def test_subdirectory_scope_is_reported(self, runner, repo_factory):
        repo = repo_factory(
            {
                "pkg/main.py": SIMPLE_MODULE,
                "outside/other.py": SIMPLE_MODULE,
            }
        )
        repo.write("pkg/main.py", SIMPLE_MODULE.replace("42", "30"))
        repo.write("outside/other.py", SIMPLE_MODULE.replace("42", "31"))

        _, payload = invoke_json(runner, str(repo.root / "pkg"))
        assert payload["mode"]["scope"] == "pkg"
        assert "pkg/main.py" in {r["path"] for r in payload["changed_files"]}
        assert payload["mode"]["changes_outside_scope"] == 1
        outside_paths = {r["path"] for r in payload["changes_outside_scope"]}
        assert outside_paths == {"outside/other.py"}

    def test_no_ranked_flag_hides_ranked_section(self, runner, repo_factory):
        repo = repo_factory(
            {
                "pkg/a.py": "def a():\n    return 1\n",
                "pkg/b.py": "from pkg.a import a\n\ndef b():\n    return a()\n",
            }
        )
        repo.write("pkg/a.py", "def a():\n    return 5\n")

        result = runner.invoke(cli, ["impact", str(repo.root), "--no-ranked"])
        assert result.exit_code == 0
        assert "=== Ranked Impacts" not in result.output

    def test_ranked_section_lists_tiers_when_present(self, runner, repo_factory):
        repo = repo_factory(
            {
                "pkg/a.py": "def a():\n    return 1\n",
                "pkg/b.py": "from pkg.a import a\n\ndef b():\n    return a()\n",
            }
        )
        repo.write("pkg/a.py", "def a():\n    return 5\n")

        _, payload = invoke_json(runner, str(repo.root))
        if payload["ranked_impacts"]:
            result = runner.invoke(cli, ["impact", str(repo.root)])
            assert "=== Ranked Impacts" in result.output
