import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from app.cli.main import cli
from app.mcp.knowledge_tools import KnowledgeTools, TOOL_SPECS
from app.mcp.server import KiteHutMCPServer, RepoLensMCPServer


# ---- CLI (in-process, temporary repos only) -------------------------------------
def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def test_cli_full_journey(repo):
    r = invoke("analyze", str(repo)); assert r.exit_code == 0, r.output
    assert (repo / ".kitehut/knowledge/INDEX.md").exists() and (repo / ".kitehut/visualization/graph.html").exists()
    r = invoke("status", str(repo), "--knowledge-only"); assert r.exit_code == 0 and "FRESH" in r.output
    (repo / "pkg/util.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    r = invoke("status", str(repo), "-k", "--strict"); assert r.exit_code == 3 and "STALE" in r.output
    r = invoke("refresh", str(repo)); assert r.exit_code == 0, r.output
    assert invoke("status", str(repo), "-k", "--strict").exit_code == 0
    r = invoke("refresh", str(repo)); assert r.exit_code == 0 and "Already up to date" in r.output


def test_cli_subcommands_generate_only_their_parts(repo):
    assert invoke("graph", str(repo)).exit_code == 0
    assert (repo / ".kitehut/graph/repository.json").exists() and not (repo / ".kitehut/knowledge").exists()
    assert invoke("knowledge", str(repo)).exit_code == 0 and (repo / ".kitehut/knowledge/INDEX.md").exists()
    assert not (repo / ".kitehut/visualization").exists()
    assert invoke("visualize", str(repo)).exit_code == 0 and (repo / ".kitehut/visualization/graph.html").exists()
    meta = json.loads((repo / ".kitehut/metadata.json").read_text(encoding="utf-8"))
    assert set(meta["outputs"]) == {"graph", "knowledge", "visualization"}


def test_cli_errors_have_human_message_and_exit_code_1(tmp_path, repo):
    r = invoke("analyze", str(tmp_path / "nope")); assert r.exit_code == 1 and "does not exist" in r.output
    assert "Traceback" not in r.output
    r = invoke("analyze", str(repo / "pkg/core.py")); assert r.exit_code == 1 and "not a directory" in r.output
    r = invoke("refresh", str(repo)); assert r.exit_code == 1 and "analyze" in r.output
    r = invoke("context", str(repo), "x"); assert r.exit_code == 1 and "analyze" in r.output


def test_cli_context_and_evidence(analyzed):
    repo, _ = analyzed
    r = invoke("context", str(repo), "Engine run", "--format", "json")
    data = json.loads(r.output); assert r.exit_code == 0 and data["items"] and data["items"][0]["reference"]
    r = invoke("evidence", str(repo), "main exists", "--file", "pkg/core.py", "--verify", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["status"] == "SUPPORTED"


def test_cli_help_lists_new_commands():
    out = invoke("--help").output
    for name in ("analyze", "graph", "knowledge", "visualize", "refresh", "status", "context", "evidence", "mcp"):
        assert name in out


def test_cli_does_not_modify_repo_sources(repo):
    before = {p: p.read_bytes() for p in repo.rglob("*.py")}
    invoke("analyze", str(repo)); invoke("refresh", str(repo), "--force")
    assert before == {p: p.read_bytes() for p in repo.rglob("*.py") if ".kitehut" not in p.parts}


# ---- MCP: direct tool functions -------------------------------------------------
def test_mcp_tools_direct(analyzed):
    repo, _ = analyzed
    t = KnowledgeTools(str(repo))
    o = t.overview(); assert o["success"] and o["files"] == 3 and o["freshness"]["state"] == "fresh"
    assert t.search_symbols("helper")["symbols"][0]["reference"] == "pkg/util.py:1-2"
    assert [c["qualified_name"] for c in t.find_callers("pkg/core.Engine")["callers"]] == ["pkg/core.main"]
    assert [c["qualified_name"] for c in t.find_callees("pkg/core.main")["callees"]][:1] == ["pkg/core.Engine"]
    assert t.find_dependencies("pkg/core.py")["imports"] == [{"path": "pkg/util.py"}]
    assert t.find_dependencies("pkg/util.py")["imported_by"] == [{"path": "pkg/core.py"}]
    assert not t.find_dependencies("../../etc/passwd")["success"]
    assert t.get_context("Engine run")["items"]
    assert t.get_evidence("m", file="pkg/core.py", verify=True)["status"] == "SUPPORTED"
    assert t.read_knowledge()["content"].startswith("<!--")
    assert not t.read_knowledge("../metadata.json")["success"]
    assert t.check_freshness()["state"] == "fresh"
    assert not t.find_callers("nonexistent_symbol")["success"]


def test_mcp_tools_report_missing_knowledge_cleanly(repo):
    r = KnowledgeTools(str(repo)).overview()
    assert r["success"] is False and "analyze" in r["error"]


def test_mcp_server_default_is_read_only_and_optin_exposes_agent_tools(analyzed):
    repo, _ = analyzed
    default = set(KiteHutMCPServer(str(repo)).get_registered_tool_names())
    assert {n for n, _, _ in TOOL_SPECS} <= default
    assert not default & {"write_file", "edit_file", "delete_file", "run_command", "git_commit", "git_checkout"}
    registered = {t.name for t in KiteHutMCPServer(str(repo)).mcp._tool_manager.list_tools()}
    assert registered == default
    opted = set(KiteHutMCPServer(str(repo), allow_agent_tools=True).get_registered_tool_names())
    assert {"write_file", "run_command"} <= opted
    legacy = set(RepoLensMCPServer(str(repo)).get_registered_tool_names())  # deprecated, behavior unchanged
    assert {"write_file", "run_command"} <= legacy and len(legacy) == 31 and not any(n.startswith("kitehut_") for n in legacy)


def test_mcp_discover_is_kitehut_branded(analyzed):
    d = KiteHutMCPServer(str(analyzed[0])).discover()
    assert d["server"] == "KiteHut MCP" and d["allow_agent_tools"] is False


# ---- MCP: real stdio protocol smoke test ----------------------------------------
def _stdio_server_params(repo):
    from mcp import StdioServerParameters

    exe = shutil.which("kitehut")
    return StdioServerParameters(command=exe, args=["mcp", str(repo)]) if exe else \
        StdioServerParameters(command=sys.executable, args=["-m", "app.cli.main", "mcp", str(repo)])


def test_mcp_stdio_advertises_application_version(analyzed):
    """The initialize handshake must report KiteHut 0.1.0, not the MCP SDK version."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    repo, _ = analyzed
    params = _stdio_server_params(repo)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                return await s.initialize()

    init = asyncio.run(asyncio.wait_for(go(), 60))
    assert init.serverInfo.name == "KiteHut"
    assert init.serverInfo.version == "0.1.0"


def test_mcp_stdio_protocol_smoke(analyzed):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    repo, _ = analyzed
    params = _stdio_server_params(repo)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool("kitehut_search_symbols", {"query": "Engine"})
                ctx = await s.call_tool("kitehut_get_context", {"query": "Engine run"})
                return names, json.loads(res.content[0].text), json.loads(ctx.content[0].text)

    names, search, ctx = asyncio.run(asyncio.wait_for(go(), 60))
    assert "kitehut_overview" in names and "write_file" not in names
    assert search["total"] >= 1 and ctx["items"]


def test_cli_version_matches_package_version():
    from app import __version__
    r = invoke("--version")
    assert r.exit_code == 0 and __version__ in r.output and "2.0.0" not in r.output
