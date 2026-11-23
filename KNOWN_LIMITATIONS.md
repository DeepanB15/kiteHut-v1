# Known limitations — v0.1.0
1. **Python only.**
2. **Cross-file calls to imported functions are not resolved** (e.g. `OrderService.checkout` shows no resolved callees). Unresolved calls are counted, never guessed.
3. **Context is lexical + graph only**; no embeddings. Token numbers are `chars/4` estimates.
4. **Token savings are not measured or claimed** (PENDING).
5. **Secret redaction is heuristic**; do not rely on it to sanitize sensitive repositories. Review `.kitehut/` before committing/sharing.
6. **CI "Smart Notifications" failure was not reproduced.** Independent investigation found 19 real local failures (fixed; the closest match was `test_8_github_status`). I could not access the original CI logs; confirm on your CI.
7. **Claude Code:** only connection verified. **Codex:** only config registration verified. No model-driven tool call tested for either.
8. **Top-level import package is named `app`**, which can clash with other packages; use a dedicated virtualenv.
9. **No LICENSE file** was present; none was invented. Add one before public release.
10. **Legacy features** (LLM agents, GitHub, API, dashboard) were not re-validated against live services; frontend untested against a running backend.
11. Internal `repolens` logger names and `repolens-report/v1` kept for compatibility; `repolens` CLI alias is deprecated.
12. Large repositories: generation of ~320 files takes ≈ 6 s; Markdown pages and viz nodes are capped (all data remains in `repository.json`).
13. Tested on Linux only. `.kitehut/graph/repository.json` is treated as KiteHut-owned and overwritten without a marker check.
14. The repository was cloned read-only: **nothing was pushed**; changes exist only in the ZIP.
