# KiteHut MCP server

MCP is **optional**. Everything KiteHut does is available from the CLI.

```bash
kitehut analyze /path/to/project          # create .kitehut/ first
kitehut mcp /path/to/project              # stdio server, read-only by default
kitehut mcp /path/to/project --allow-agent-tools   # opt in to tools that write/run/change git
```

## Tools (read-only default)
| Tool | Purpose |
|---|---|
| `kitehut_overview` | counts, status, freshness, entry points |
| `kitehut_search_symbols` | find symbols by name |
| `kitehut_find_callers` / `kitehut_find_callees` | resolved call relationships |
| `kitehut_find_dependencies` | imports / dependents of a file |
| `kitehut_get_context` | token-budgeted context with `path:line` references |
| `kitehut_get_evidence` | source-backed evidence + verdict |
| `kitehut_read_knowledge` | read a generated Markdown page (path-confined to `.kitehut/knowledge`) |
| `kitehut_check_freshness` | fresh / stale / missing, with changed files |

Plus the legacy read-only repository tools. Mutating legacy tools are registered **only** with `--allow-agent-tools`.
Tool results are bounded in size and snippets are passed through best-effort secret redaction.

## Verification performed
Automated (`backend/tests/knowledge/test_mcp_cli.py`): the official MCP Python client launches `kitehut mcp` over real stdio,
lists tools, and calls them; the read-only tool set and the opt-in gate are asserted.

Per-client results: [CLAUDE_CODE.md](CLAUDE_CODE.md), [CODEX.md](CODEX.md).
