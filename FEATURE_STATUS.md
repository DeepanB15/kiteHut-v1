# Feature status — v0.1.0
Full table with notes: [docs/FEATURES.md](docs/FEATURES.md).

**IMPLEMENTED:** Python code graph; `.kitehut/` graph JSON, Markdown knowledge, offline HTML graph, metadata; freshness & refresh; safety guarantees; CLI (analyze, graph, knowledge, visualize, refresh, status, context, evidence, mcp); smart context (lexical + graph); evidence verification; MCP server (read-only default, verified over real stdio); benchmark script.
**PARTIALLY IMPLEMENTED:** call resolution (conservative; cross-file imported calls unresolved); secret redaction (heuristic); Claude Code integration (connects; no model-driven call tested); Codex integration (config accepted; connection untested).
**EXPERIMENTAL:** legacy RepoLens agent/LLM features, GitHub PR analysis, FastAPI service, React dashboard (tests pass; not re-validated live; dashboard not wired to `.kitehut/`).
**PLANNED:** incremental updates; embedding-assisted retrieval; token-savings benchmark.
**NOT IMPLEMENTED:** cross-file call resolution; non-Python languages; LICENSE file (owner decision).
