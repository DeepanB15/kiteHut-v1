# Changelog

## 0.1.0 — KiteHut first public release
### Added
- `.kitehut/` generation: `graph/repository.json`, `knowledge/` (INDEX, architecture, modules, symbols), offline `visualization/graph.html`, `metadata.json`.
- CLI: `analyze`, `graph`, `knowledge`, `visualize`, `refresh`, `status` (+ `context`, `evidence`, `mcp`).
- Freshness detection by per-file SHA-256; stale-aware context.
- Smart context retrieval (lexical + graph, token-budgeted) and source-backed evidence.
- MCP server with `kitehut_*` tools, read-only by default; `--allow-agent-tools` opt-in.
- Demo project, `scripts/demo.sh`, `scripts/benchmark.py`; 72 new tests.
- Documentation set and status labels.
### Changed (breaking)
- Product renamed RepoLens → KiteHut; CLI is `kitehut` (`repolens` kept as a deprecated alias). MCP server name is "KiteHut MCP"; the legacy `RepoLensMCPServer` class is kept. Internal logger names and the `repolens-report/v1` format are unchanged for compatibility.
- `mcp` is pinned `>=1.2,<2.0`.
- Serialized graph no longer contains the absolute `root_path`.
### Fixed
- Undeclared `click`/`colorama`/`networkx` dependencies; syntax errors reported 3×; `/api/github/status` 500 on rejected token; offline test required live Ollama; version string reported 2.0.0; committed `egg-info` removed.
### Moved
- Pre-KiteHut root docs → `archive/repolens-v2/pre-kitehut-docs/`.
