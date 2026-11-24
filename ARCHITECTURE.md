# KiteHut Architecture

KiteHut is **one Python package with three integrated engines**, not separate services. It is
local-first: core analysis uses only the local filesystem and the Python standard library plus
Pydantic.

```
Repository Scanner
       ↓
Code Graph Engine
       ↓
Smart Context Engine
       ↓
Evidence Engine
       ↓
.kitehut/ Knowledge Output
       ↓
Claude Code / Codex / MCP
```

Principle: **source code is the truth.** The graph locates relevant code; evidence re-reads the
source to verify claims.

## Layers and where they live (`backend/app/`)

| Layer | Modules | Notes |
|---|---|---|
| Repository Scanner | `code_graph/discovery.py` | Python files only; `.gitignore` subset; built-in excludes (venvs, caches, build, `.git`, `.kitehut`); directory symlinks never followed; escaping file symlinks skipped; `max_files` cap |
| Engine A — Code Graph | `code_graph/` (`models`, `python_extractor`, `relationships`, `builder`, `validation`, `serialization`, `queries`, `graph_runtime`, `integration`, `evidence`) | Pre-existing; extended with cross-file call resolution and impact/cycle queries (see CHANGELOG). Deterministic JSON; only unique, source-backed relationships become edges |
| Engine B — Smart Context | `knowledge/context.py` (new), reusing `code_graph/integration.discover_graph_candidates` | Lexical + graph ranking, overlap de-duplication, token budget, freshness check per file |
| Engine C — Evidence | `knowledge/evidence.py` (new adapter) over `code_graph/evidence.py` and `verification/` | Spans re-read from disk, hashed, compared to graph snapshot; verifier verdicts reused |
| Knowledge output | `knowledge/` (`generator`, `markdown`, `visualize`, `metadata`, `store`, `safe_io`, `index`, `redact`) | Writes `.kitehut/` |
| Interfaces | `cli/` (Click), `mcp/` (FastMCP) | CLI is the primary UI; MCP is optional |
| Optional / legacy adapters | `agent/`, `agents/`, `intelligence/`, `engines/`, `rag/`, `retrieval/`, `context/`, `graph/`, `api/`, `services/`, `db/`, `llm/`, frontend | Former RepoLens. Kept, not required for core workflow |

## Data flow for `kitehut analyze PATH`

1. Resolve and validate `PATH` (exists, is a directory).
2. Scan + build graph (read-only). A failed build writes nothing.
3. Validate the graph.
4. Prepare `.kitehut/` (refuses symlinks/escapes) and run the ownership preflight: existing
   `metadata.json`/`repository.json` that KiteHut cannot prove it generated stop the run *before*
   any write.
5. Write `graph/repository.json` (validated, atomic).
6. Generate Markdown pages (bounded for large repos) and prune only *generated* pages that no longer apply.
7. Render `graph.html`.
8. Write `metadata.json` **last** — a complete metadata file means the run finished.

## Freshness model

`metadata.json` stores the source digest and a SHA-256 per file. `status` re-scans (read-only) with
the same exclusions and reports added/removed/modified files. Context retrieval re-hashes every file
it serves and **skips** any that no longer match the stored graph. Evidence flags mismatching spans
`stale`.

## Safety model

- Writes are confined to `<repo>/.kitehut`; no path component may be a symlink; traversal refused.
- Atomic writes (temp file in the same directory, fsync, `os.replace`).
- Only files carrying the `kitehut:generated` marker are overwritten or pruned. The graph JSON
  cannot embed the marker without a schema change, so it is only overwritten when a KiteHut
  `metadata.json` from a previous run proves the folder is ours.
- MCP: server root is fixed at startup; no argument can widen it; read-only by default.
- Served snippets get best-effort secret redaction (heuristic).

## Performance notes

Graph queries memoize their adjacency index per graph object, and loaded graphs are cached per
`repository.json` version, which is what makes retrieval interactive. Numbers:
[docs/development/BENCHMARKS.md](docs/development/BENCHMARKS.md).

## More

- [docs/architecture/KNOWLEDGE_OUTPUT.md](docs/architecture/KNOWLEDGE_OUTPUT.md) — output format
- [docs/architecture/CONTEXT_AUDIT.md](docs/architecture/CONTEXT_AUDIT.md) — overlap audit of context/RAG/retrieval modules
- Historical: [docs/architecture/](docs/architecture/) transition plans; `archive/repolens-v2/pre-kitehut-docs/ARCHITECTURE.md`
