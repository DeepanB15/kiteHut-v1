# Feature status — v0.1.0
Full table with notes: [docs/FEATURES.md](docs/FEATURES.md).

**IMPLEMENTED:** Python code graph; cross-file call resolution (imported functions, `as` aliases, relative imports, `self`/`cls` methods, module-qualified calls — ambiguous calls stay unresolved); dependency impact and circular-import queries (library API only — not exposed as CLI or MCP commands); `.kitehut/` graph JSON, Markdown knowledge, offline HTML graph, metadata; freshness & refresh; ownership/symlink/traversal safety; CLI (analyze, graph, knowledge, visualize, refresh, status, context, evidence, mcp); smart context (lexical + graph); evidence verification with explicit verdicts; MCP server (read-only default, verified over real stdio); benchmark script.
**PARTIALLY IMPLEMENTED:** function-local imports are treated as shadowed (conservative); secret redaction (heuristic); Claude Code integration (connects over MCP; no model-driven call tested); Codex integration (config documented; not verified in this environment).
**EXPERIMENTAL:** legacy RepoLens agent/LLM features, GitHub PR analysis, FastAPI service, React dashboard (tests pass; not re-validated live; dashboard not wired to `.kitehut/`).
**PLANNED:** incremental updates; embedding-assisted retrieval; token-savings benchmark.
**NOT IMPLEMENTED:** non-Python languages.
**LICENSE:** MIT (`LICENSE`, Copyright (c) 2026 Deepan B), shipped in the wheel and sdist. Covers KiteHut's own code only; third-party dependencies keep their own licenses.
