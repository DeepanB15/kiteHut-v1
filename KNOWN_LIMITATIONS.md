# Known limitations — v0.1.0
1. **Python only.**
2. **Call resolution is conservative.** Module-level imports (including `as` aliases and relative imports), `self`/`cls` method calls, module-qualified calls (`mod.f()`) and same-file `Class.method()` resolve when the target is unique. Not resolved: function-local imports, re-exports, `import *`, calls through arbitrary objects/attributes, `super()`, descriptors and dynamic dispatch. Those are counted as unresolved, never guessed.
3. **Context is lexical + graph only**; no embeddings. Token numbers are `chars/4` estimates.
4. **Token savings are not measured or claimed** (PENDING).
5. **Secret redaction is heuristic**; do not rely on it to sanitize sensitive repositories. Review `.kitehut/` before committing/sharing.
6. **CI "Smart Notifications" failure was not reproduced.** Independent investigation found 19 real local failures (fixed; the closest match was `test_8_github_status`). I could not access the original CI logs; confirm on your CI.
7. **Claude Code:** MCP registration and the `Connected` health check were re-verified; no model-driven tool call was tested. **Codex:** configuration is documented but the CLI was not available, so nothing was verified end to end.
8. **Top-level import package is named `app`**, which can clash with other packages; use a dedicated virtualenv.
9. **License covers first-party code only.** MIT (`LICENSE`, Copyright (c) 2026 Deepan B) applies to KiteHut's own code; third-party dependencies and any code inherited from other projects keep their own licenses.
10. **Legacy features** (LLM agents, GitHub, API, dashboard) were not re-validated against live services; frontend untested against a running backend.
11. Internal `repolens` logger names and `repolens-report/v1` kept for compatibility; `repolens` CLI alias is deprecated.
12. Large repositories: generation of ~320 files takes ≈ 6 s; Markdown pages and viz nodes are capped (all data remains in `repository.json`).
13. Platform coverage: Linux CI is configured; this checkout was verified on **Windows (Python 3.12)**. Ten symlink-safety tests and one named-pipe test skip automatically where the OS/user policy or platform does not support them (default Windows); the symlink tests run on Linux/macOS.
14. Ownership of `.kitehut/graph/repository.json` is proven by the KiteHut `metadata.json` from a previous run (the graph JSON cannot carry the text marker without a schema change). A user-created graph file in a fresh `.kitehut/` is refused, never overwritten.
