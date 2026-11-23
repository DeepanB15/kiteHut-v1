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
| `claude mcp add` accepts the server | Verified |
| `claude mcp list` health check reports **Connected** | Verified |
| MCP protocol over stdio (initialize, list tools, call tools) with the official Python client | Verified (automated test) |
| A Claude model actually choosing and calling a KiteHut tool in a session | **Not verified** (requires an authenticated model session, not available in the verification environment) |

Suggested prompt once connected: *"Use kitehut_overview, then kitehut_get_context to explain how checkout works."*
