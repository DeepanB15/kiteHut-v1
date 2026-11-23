# Contributing to KiteHut

1. Set up: see [docs/development/DEVELOPMENT.md](docs/development/DEVELOPMENT.md).
2. Run `pytest -q` in `backend/` before opening a PR; add tests for new behavior using temp directories.
3. Keep changes honest: update `docs/FEATURES.md` status labels when behavior changes; no unverified claims or invented benchmarks.
4. Safety invariants (never write outside `.kitehut/`, never touch unmarked files, no secrets in code/tests/docs) must hold.
5. Don't commit `.env`, `.kitehut/` outputs, databases or caches.
Document breaking changes in `CHANGELOG.md`.
