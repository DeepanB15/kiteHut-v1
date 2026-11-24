# OpenAI Codex

```bash
pip install -e backend
kitehut analyze /path/to/project
codex mcp add kitehut -- kitehut mcp /path/to/project
codex mcp list
```
Equivalent manual config in `~/.codex/config.toml`:
```toml
[mcp_servers.kitehut]
command = "kitehut"
args = ["mcp", "/path/to/project"]
```

## Status: PARTIALLY IMPLEMENTED
| Check | Result |
|---|---|
| `codex mcp add` accepts the server and persists it to the config | **Not verified** (Codex CLI not installed in the verification environment) |
| Codex launching the server and listing tools | **Not verified** |
| A model-driven tool call from Codex | **Not verified** |
| MCP protocol over stdio with the official Python client | Verified (automated test) |

If the Codex connection fails, run `kitehut mcp /path/to/project` by hand to confirm the server starts, and use an absolute path to `kitehut`.
