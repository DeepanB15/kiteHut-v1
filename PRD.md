# KiteHut — Product Requirements (v0.1.0)

**Tagline:** Graph to understand. Context to reason. Evidence to trust.
**Category:** Local-first repository intelligence & evidence engine.

## Problem
Developers and AI coding agents repeatedly rediscover a repository's structure, and cannot tell
whether a cached summary still matches the code.

## Users
Software developers · AI engineers · Claude Code users · OpenAI Codex users · teams with large repositories.

## Product
Analyze a repository and persist structured knowledge in `.kitehut/` that people and agents can read,
query, and verify against source. The local repository and its generated knowledge are the center of the product.

## Core workflow
Analyze → Build Code Graph → Generate Knowledge → Retrieve Smart Context → Verify with Source Evidence → Connect AI Agents.

## Requirements and v0.1.0 status
| # | Requirement | Status |
|---|---|---|
| R1 | Persistent `.kitehut/` with graph JSON, Markdown knowledge, HTML graph, metadata | IMPLEMENTED |
| R2 | Deterministic, validated, atomically written graph JSON with schema/version metadata | IMPLEMENTED |
| R3 | Knowledge pages with source paths, line references, links; no invented relationships; bounded on large repos | IMPLEMENTED |
| R4 | Standalone offline `graph.html` with zoom/pan/select/filter | IMPLEMENTED |
| R5 | Freshness detection and refresh | IMPLEMENTED |
| R6 | Safety: never write sources; self-exclusion; `.gitignore`/venv/cache exclusion; symlink/path safety; never delete user data | IMPLEMENTED |
| R7 | CLI: analyze, graph, knowledge, visualize, refresh, status | IMPLEMENTED |
| R8 | Smart context: graph-guided, de-duplicated, token-budgeted, with path:line refs | IMPLEMENTED (lexical+graph) |
| R9 | Evidence: source-backed, hashed, stale detection, clear verdicts | IMPLEMENTED |
| R10 | MCP tools: overview, search, callers/callees, dependencies, context, evidence, knowledge, freshness | IMPLEMENTED |
| R11 | Claude Code / Codex setup documentation | IMPLEMENTED (docs); verification is PARTIAL, see integrations |
| R12 | Measurements: scan, graph, retrieval, context size, evidence time | IMPLEMENTED (framework + results); token-savings % PENDING |
| R13 | Cross-file call resolution; non-Python languages | NOT IMPLEMENTED |
| R14 | Embedding-assisted retrieval via existing stack | PLANNED |

## Non-goals
Autonomous coding agent · IDE replacement · generic RAG chatbot · multi-agent swarm platform · hosted SaaS dashboard as the primary product.

## Constraints
Python, Click, Pydantic, existing graph and retrieval. No new services/databases. No external API needed for core analysis.

## Success criteria (v0.1.0)
A user can install, run `kitehut analyze`, read `INDEX.md`, open `graph.html`, connect an MCP client, and `refresh` after changes — on a clean install, with tests passing. See [TEST_REPORT.md](TEST_REPORT.md).
