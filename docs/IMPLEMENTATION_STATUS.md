# Implementation status

Snapshot for **v0.1.0**. It describes this repository's working tree; no external baseline
commit is assumed (the checkout's history contains a single initial commit, so the older
`85e47c4` reference inherited from the RepoLens-era notes could not be verified).
Feature-level detail: [FEATURES.md](FEATURES.md).
The pre-KiteHut status document is preserved at `archive/repolens-v2/pre-kitehut-docs/IMPLEMENTATION_STATUS.md`.

## Done
- P0 baseline established: 919 passed / 19 failed on a clean install of the repository as supplied.
- P1 blockers fixed: undeclared dependencies, stale rename in tests, 3 real bugs (below), committed `egg-info` removed.
- P2 `.kitehut/` generation (graph, Markdown, HTML, metadata, freshness, safety).
- P3 CLI commands and exit codes.
- P4 MCP: KiteHut server, read-only default, `kitehut_*` tools; Claude Code registration verified.
- P5 documentation set.
- P6 tests (72 new), demo project, benchmark framework.
- P7 clean-install verification (see TEST_REPORT.md).

## Baseline failures and their disposition
| Failure | Disposition |
|---|---|
| `colorama`, `networkx`, `click` missing from `requirements.txt` → CLI could not import on a clean install | Fixed (declared) |
| Tests asserting old `RepoLens` strings/`repolens` console script | Tests updated to the KiteHut identity; `repolens` kept as a deprecated alias script |
| Serialized graph embedded absolute `root_path` (breaks determinism/portability; test existed) | Fixed: field excluded from serialization |
| Syntax error reported 3× per file instead of once | Fixed: deduplicated in builder |
| `/api/github/status` returned 500 on a rejected token; literal `{path}` in error message | Fixed: reports `authenticated=false`; f-string fixed |
| `test_answer_preserves_provenance` required a live Ollama server | Fixed: uses the same offline stub as its sibling test |
| CI "Smart Notifications" failure from the review | **Not reproduced** (see KNOWN_LIMITATIONS.md) |

## Remaining / planned
See [../KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) and the roadmap in the README.
