# Feature status (v0.1.0)

Legend: **IMPLEMENTED** works and is covered by tests · **PARTIALLY IMPLEMENTED** works with stated gaps ·
**EXPERIMENTAL** present but not re-validated in this release · **PLANNED** intended, not built · **NOT IMPLEMENTED**.

## Core (KiteHut)
| Feature | Status | Notes |
|---|---|---|
| Python file/symbol discovery (classes, functions, methods) | IMPLEMENTED | `.py` only |
| Resolved imports (in-repository) | IMPLEMENTED | third-party/unresolved imports are counted |
| Resolved calls | IMPLEMENTED (conservative) | same-file, imported functions (incl. `as` aliases and relative imports), `self`/`cls` methods, module-qualified (`mod.f()`) and same-file `Class.method()`; ambiguous/dynamic calls stay unresolved with diagnostics |
| Deterministic graph JSON + validation | IMPLEMENTED | |
| Graph queries (symbol lookup, callers/callees, imports/dependents) | IMPLEMENTED | |
| Dependency impact analysis (`impacted_files`, `impacted_symbols`) | IMPLEMENTED (library API) | bounded reverse traversal over resolved edges; **no CLI/MCP command exposes it yet** |
| Circular dependency detection (`import_cycles`) | IMPLEMENTED (library API) | SCC over resolved IMPORTS edges; members + edge source locations; **no CLI/MCP command exposes it yet** |
| `.kitehut/` generation, atomic writes, ownership marker | IMPLEMENTED | |
| Markdown knowledge (INDEX, architecture, module and symbol pages) | IMPLEMENTED | page caps on large repos |
| Offline HTML graph | IMPLEMENTED | verified in headless Chromium: no console errors, no network requests |
| Freshness / stale detection / refresh | IMPLEMENTED | content-hash based |
| Smart context (ranking, de-dup, token budget, freshness check) | IMPLEMENTED | lexical + graph; token counts are `chars/4` estimates |
| Evidence collection + verification | IMPLEMENTED | reuses existing verification engine; explicit `VERIFIED`/`UNVERIFIED`/`STALE`/`INSUFFICIENT_EVIDENCE`/`NOT_FOUND` verdict alongside the compatible `status` |
| Secret redaction of served snippets | PARTIALLY IMPLEMENTED | heuristic patterns only |
| CLI (`analyze graph knowledge visualize refresh status context evidence mcp`) | IMPLEMENTED | |
| Benchmark script | IMPLEMENTED | token-savings comparison PENDING |

## Integrations
| Feature | Status | Notes |
|---|---|---|
| MCP server (stdio), read-only default | IMPLEMENTED | tested with the official MCP Python client over real stdio |
| Claude Code | PARTIALLY IMPLEMENTED | `claude mcp add` + health check = Connected; no model-driven tool call tested |
| OpenAI Codex | PARTIALLY IMPLEMENTED | manual `config.toml` documented; Codex CLI not available for verification, connection/tool call not tested |
| Mutating agent tools over MCP | IMPLEMENTED (opt-in) | `--allow-agent-tools`; unchanged legacy behavior |

## Not built
| Feature | Status |
|---|---|
| Languages other than Python | NOT IMPLEMENTED |
| Incremental graph updates | PLANNED |
| Embedding-assisted retrieval integration | PLANNED |
| Token-savings benchmark | PLANNED (framework present) |

## Legacy RepoLens features (kept, optional)
Status: **EXPERIMENTAL** — they were not re-validated end-to-end in this release (no live LLM/Ollama,
GitHub or database was used). Their unit/integration tests pass (see TEST_REPORT.md).

| Feature | Entry point |
|---|---|
| LLM Q&A, review, remediation, findings, reports | `kitehut ask / review / fix / findings / report` |
| Guardian / interactive runtime | `kitehut start` |
| GitHub PR analysis, quality gate | `kitehut github`, `/api/github/*` |
| FastAPI service | `uvicorn app.main:app` (see `docker-compose.yml`) |
| React dashboard | `frontend/` — type-checks and builds (`npm ci && npm run build`, exit 0); **not wired to `.kitehut/` knowledge** and not exercised against a running backend in this release |
