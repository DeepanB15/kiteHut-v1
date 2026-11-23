# Benchmarks

`python scripts/benchmark.py PATH` copies nothing into the target repo's sources; it measures scan, full generation,
context retrieval and evidence collection (median of repeated runs; "first" is the cold call).

## Results (single machine, cloud container; indicative only)
**examples/demo_project** — 6 files, 17 symbols, 47 edges
| Measure | Time |
|---|---|
| scan | 1.4 ms |
| generate (all outputs) | ~31 ms |
| context "order total" | 1.3 ms (5 items, ~170 tokens est.) |
| evidence (per claim) | ~1.3–1.5 ms |

**KiteHut's own `backend/app`** — 321 files, 1,956 symbols, 5,146 edges
| Measure | Time |
|---|---|
| scan | 52 ms |
| generate (all outputs) | ~5.75 s (graph build ≈ 5.1 s, graph JSON write 0.17 s, Markdown 0.29 s, HTML 0.03 s) |
| context (3 queries) | 47–159 ms warm; first call up to 533 ms |
| evidence (per claim) | 3–4 ms |
| all Python source | ≈ 395k tokens (chars/4 estimate) |

Context retrieval filled the 4,000-token budget on the large repository.

## Not claimed
**Token savings: PENDING.** Comparing "context via KiteHut" with "agent reads files itself" needs a controlled agent
task set, which was not run. No percentage is stated anywhere in this project. Token counts are `chars/4` estimates, not a tokenizer.

Performance note: retrieval was 12.5 s before graph-index memoization; the fix is covered by the test suite.
