# Claude Code

```bash
pip install -e backend            # provides the `kitehut` command
kitehut analyze /path/to/project
claude mcp add kitehut -- kitehut mcp /path/to/project
claude mcp list                   # kitehut: ... - ✓ Connected
```
If `kitehut` is installed in a virtualenv, use its absolute path in the `claude mcp add` command.
Remove with `claude mcp remove kitehut`.

## Status: PARTIALLY IMPLEMENTED
| Check | Result |
|---|---|
| `claude mcp add` accepts the stdio server and persists it | Verified (Claude Code CLI, local scope) |
| `claude mcp get` / `claude mcp list` report the server as **Connected** | Verified (temporary server, removed after the check) |
| MCP protocol over stdio (initialize, list tools, call tools) with the official Python client | Verified (automated: `tests/knowledge/test_mcp_cli.py`) |
| A Claude model actually choosing and calling a KiteHut tool in a session | **Not verified** (requires an authenticated model session, not available in the verification environment) |

The health check only proves the CLI can start the server and complete the MCP
handshake. It does not prove that a model will select the right tool, so the
integration stays *partially implemented*. If the connection fails, run
`kitehut mcp /path/to/project` by hand to confirm the server starts, and use an
absolute path to `kitehut`.

Suggested prompt once connected: *"Use kitehut_overview, then kitehut_get_context to explain how checkout works."*
