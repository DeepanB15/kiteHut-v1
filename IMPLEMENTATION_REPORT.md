# Implementation report — KiteHut v0.1.0

## Approach
Repository modified in place (baseline commit `85e47c4`). The existing Code Graph, retrieval and verification engines were reused; no heavy dependencies added. Historical RepoLens docs were archived, not deleted.

## What was done
1. **Baseline & audit** — 919 pass / 19 fail on a clean install; audit in `docs/architecture/CONTEXT_AUDIT.md`.
2. **Fixes** — undeclared deps (`click`, `colorama`, `networkx`), stale rename tests, non-portable serialized `root_path`, triplicated syntax-error diagnostics, GitHub status 500 + f-string bug, an offline-test stub, wrong version string, committed `egg-info`, Python 3.11 f-string syntax error.
3. **`.kitehut/` engine** (`backend/app/knowledge/`) — graph JSON, Markdown knowledge, offline HTML graph, metadata (written last), freshness, atomic writes, ownership marker, symlink/traversal guards, redaction.
4. **Engines B & C** — token-budgeted context; evidence with hashes and stale detection.
5. **CLI** — 6 required commands + `context`, `evidence`, `mcp`; defined exit codes.
6. **MCP** — `KiteHutMCPServer`, `kitehut_*` tools, read-only by default, `--allow-agent-tools` opt-in; legacy `RepoLensMCPServer` preserved unchanged.
7. **Performance** — memoized graph index (retrieval 12.5 s → 47–159 ms on 321 files).
8. **Tests** — 72 new; 5 legacy files had only rename strings/stub touched, plus two version assertions (see TEST_REPORT.md).
9. **Docs, demo, benchmark, CI** updated; ZIP built with no secrets.

## Verification
See [TEST_REPORT.md](TEST_REPORT.md). Honest gaps: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Feature labels: [FEATURE_STATUS.md](FEATURE_STATUS.md).

## Breaking changes
Rename to `kitehut` (alias `repolens` kept); `mcp<2`; no `root_path` in graph JSON; CLI version 0.1.0. See CHANGELOG.md.
