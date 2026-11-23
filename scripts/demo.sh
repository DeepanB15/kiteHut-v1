#!/usr/bin/env bash
# KiteHut end-to-end demo. Works on a copy of examples/demo_project, so the
# repository itself is never modified.  Usage: scripts/demo.sh [workdir]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KITEHUT="${KITEHUT:-kitehut}"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
DEMO="$WORK/demo_project"
rm -rf "$DEMO"; cp -R "$HERE/examples/demo_project" "$DEMO"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. Analyze: graph + Markdown knowledge + HTML visualization"
"$KITEHUT" analyze "$DEMO"

step "2. Generated files"
(cd "$DEMO" && find .kitehut -type f | sort)

step "3. Read INDEX.md"
sed -n 1,30p "$DEMO/.kitehut/knowledge/INDEX.md"

step "4. A module page and a symbol page (with source line references)"
cat "$DEMO/.kitehut/knowledge/modules/shop__pricing.md"
cat "$DEMO/.kitehut/knowledge/symbols/shop__orders.OrderService.checkout.md"

step "5. Retrieve relevant context for a question (token-budgeted)"
"$KITEHUT" context "$DEMO" "how is the order total calculated" --budget 800

step "6. Evidence for a claim, verified against source"
"$KITEHUT" evidence "$DEMO" "calculate_total exists in shop/pricing.py" --file shop/pricing.py --verify

step "7. Status is fresh"
"$KITEHUT" status "$DEMO" --knowledge-only

step "8. Change the source, status becomes stale"
cat >> "$DEMO/shop/pricing.py" <<'PY'


def apply_tax(amount_cents, rate_percent):
    return amount_cents + (amount_cents * rate_percent) // 100
PY
"$KITEHUT" status "$DEMO" --knowledge-only

step "9. Refresh"
"$KITEHUT" refresh "$DEMO"
"$KITEHUT" status "$DEMO" --knowledge-only

step "10. Open the graph"
echo "Open this file in a browser: $DEMO/.kitehut/visualization/graph.html"
