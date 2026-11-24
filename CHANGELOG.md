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
- Cross-file CALLS resolution for module-level imported functions, `as` aliases and relative imports;
  `self`/`cls` method calls, module-qualified calls (`mod.f()`) and same-file `Class.method()` calls.
  Ambiguous targets produce `call_ambiguous_cross_file` diagnostics instead of an edge.
- `queries.impacted_files`, `queries.impacted_symbols` (bounded reverse traversal with edge provenance)
  and `queries.import_cycles` (deterministic SCC over resolved IMPORTS edges). These are **library
  API only**: no CLI command or MCP tool exposes them yet.
- MIT `LICENSE` (Copyright (c) 2026 Deepan B), shipped inside the wheel and sdist; build metadata
  moved to the PEP 639 SPDX form (`license = "MIT"`, `license-files`).
- Explicit evidence verdicts: `VERIFIED`, `UNVERIFIED`, `STALE`, `INSUFFICIENT_EVIDENCE`, `NOT_FOUND`,
  plus `generated_at` and schema/version provenance on `EvidenceReport`. The existing
  `SUPPORTED`/`UNSUPPORTED`/`INCONCLUSIVE` status is unchanged.
- Ownership preflight: a user-created `.kitehut/metadata.json` or `.kitehut/graph/repository.json`
  now stops generation before any write; nested output directories are created with symlink checks.
### Changed (breaking)
- Product renamed RepoLens → KiteHut; CLI is `kitehut` (`repolens` kept as a deprecated alias). MCP server name is "KiteHut MCP"; the legacy `RepoLensMCPServer` class is kept. Internal logger names and the `repolens-report/v1` format are unchanged for compatibility.
- `mcp` is pinned `>=1.2,<2.0`.
- Serialized graph no longer contains the absolute `root_path`.
### Fixed
- README installation/quick start were corrupted and installed from the wrong directory; OS-specific
  instructions are now separate and the clone URL is correct.
- Documentation provenance no longer cites an unverifiable baseline commit (`85e47c4`).
- Windows test harness: hostile-filename and symlink tests now use platform-legal fixtures and skip
  (with a clear reason) where symlink creation is not permitted.
- MCP `initialize` reported the MCP SDK version (e.g. `1.12.4`) as the server version; it now reports
  KiteHut's own version (`0.1.0`).
### Moved
- Pre-KiteHut root docs → `archive/repolens-v2/pre-kitehut-docs/`.
