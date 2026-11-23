# Demo

```bash
pip install -e backend
scripts/demo.sh            # works on a temp copy of examples/demo_project; the repo is untouched
```
Steps it runs: `analyze` → list `.kitehut/` files → read `INDEX.md`, a module page and a symbol page →
`context "how is the order total calculated" --budget 800` → `evidence ... --verify` → `status` (fresh) →
edit a source file → `status` (stale) → `refresh` → `status` (fresh).

Open `.kitehut/visualization/graph.html` in any browser (offline, no network).

Observed limitation, visible in the demo: `OrderService.checkout` shows no resolved callees, because calls to functions
imported from other files are not resolved in v0.1.0 (see [../KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md)).
