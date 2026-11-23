# Context / retrieval duplication audit (v0.1.0)

Brief: *audit context duplication before consolidating; preserve the legacy graph until its
dependencies are understood.* This is the audit. **Decision: no consolidation in v0.1.0.**

## What exists

| Module | Size | What it is | Used by |
|---|---|---|---|
| `app/context/` | 16 files, ~2.1k LOC | Project map, AST/import analysis, git context, snippet retriever, `ContextRanker` — builds context for the *agent* | ~35 importers (agent, CLI, intelligence, tests) |
| `app/retrieval/` | 6 files, ~0.8k LOC | Vector/hybrid retrieval over an embedding index (`HybridRetriever`, `VectorRetrieval`) | ~9 importers; `code_graph/integration.py` merges graph candidates into it |
| `app/rag/` | 5 files, ~0.9k LOC | `ContextBuilder` + `ContextBudget`, prompt building, LLM provider, pipeline — answer generation | 1 outside importer (+tests) |
| `app/graph/` | 3 files, ~0.8k LOC | **Legacy LangGraph analysis workflow** (not a code graph despite the name) | `services/analysis_service`, `agents/recon_agent`, tests |
| `app/code_graph/` | 12 files, ~8.6k LOC | The Code Graph (Engine A) | ~27 importers |
| `app/knowledge/context.py` | new | Graph-guided, freshness-checked, token-budgeted context for `.kitehut/` consumers | CLI, MCP |

## Overlap found

- Three components rank/assemble context: `context/ranking.py`, `rag/context_builder.py` (token budget), and the new `knowledge/context.py`.
- Two "retrieval" locations: `app/retrieval/` and `app/context/retrieval/` (snippet retriever).
- `app/graph/` name collides conceptually with `app/code_graph/`.

## Why not consolidate now

- The overlapping modules serve different consumers (LLM-driven agent/RAG vs. deterministic
  `.kitehut/` consumers) and have ~45 combined importers plus extensive tests; merging them within the
  release window risks regressions for no user-visible gain.
- The new context engine deliberately has **no LLM/vector dependency**, so it works with no
  Ollama, no embeddings and no database — required for a local-first core.
- `app/graph/` is an active dependency of the analysis service; deleting or renaming it would be unsafe.

## Integration that does exist

`knowledge/context.py` reuses `code_graph.integration.discover_graph_candidates`. The pre-existing
`integrate_graph_into_retrieval` still merges graph candidates into the hybrid retriever; it was not changed.

## Recommended follow-ups (PLANNED)

1. Expose `knowledge/context.py` as a candidate source *into* `HybridRetriever` (adapter, not rewrite).
2. Make `rag/context_builder.ContextBudget` and `knowledge.context.estimate_tokens` share one estimator.
3. Rename `app/graph/` → `app/analysis_workflow/` behind a deprecation shim once `analysis_service` is migrated.
