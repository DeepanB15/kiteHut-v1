#!/usr/bin/env python3
"""KiteHut lightweight benchmark.

Measures, on a *copy* of the target (the target itself is never modified):
repository scan + graph build, output generation, context retrieval, and
evidence verification, plus file/symbol counts and context-size token
estimates.

Timings: "first" is the cold first call (includes loading the graph from disk); "median" is over
--repeat in-process runs and benefits from in-process graph caching (as a long-lived MCP server does).

Token numbers are estimates (chars/4), NOT model token counts.  This script
deliberately does not compute a "token savings" percentage: that needs a
defined baseline task set and a real tokenizer, so it is reported as PENDING.

Usage:
  python scripts/benchmark.py PATH [--queries "q1" "q2" ...] [--repeat 3] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.code_graph.discovery import RepositoryScanner  # noqa: E402
from app.knowledge import GenerationOptions, generate  # noqa: E402
from app.knowledge.context import estimate_tokens, retrieve_context  # noqa: E402
from app.knowledge.evidence import collect_evidence  # noqa: E402

DEFAULT_QUERIES = ["main entry point", "configuration settings", "error handling", "database connection"]
IGNORE = shutil.ignore_patterns(".git", ".kitehut", "__pycache__", "node_modules", ".venv", "*.pyc", "*.db")


def timed(fn, repeat):
    runs, out = [], None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        runs.append((time.perf_counter() - t) * 1000)
    return out, {"first_ms": round(runs[0], 2), "median_ms": round(statistics.median(runs), 2), "min_ms": round(min(runs), 2), "runs": repeat}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--budget", type=int, default=4000)
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args()

    src = Path(a.path).resolve()
    if not src.is_dir():
        print(f"error: not a directory: {a.path}", file=sys.stderr)
        return 1
    work = Path(tempfile.mkdtemp(prefix="kitehut-bench-"))
    try:
        repo = work / "repo"
        shutil.copytree(src, repo, ignore=IGNORE, symlinks=True)

        scan, scan_t = timed(lambda: RepositoryScanner(repo).scan(), a.repeat)
        gen, gen_t = timed(lambda: generate(repo, GenerationOptions()), a.repeat)
        if not gen.ok:
            print("error: generation failed:", gen.errors, file=sys.stderr)
            return 1
        graph = gen.graph
        py_tokens = sum(estimate_tokens((repo / f.relative_path).read_text(encoding="utf-8", errors="replace")) for f in graph.files)

        ctx_rows = []
        for q in a.queries:
            bundle, t = timed(lambda q=q: retrieve_context(repo, q, token_budget=a.budget), a.repeat)
            ctx_rows.append({"query": q, "items": len(bundle.items), "tokens_est": bundle.tokens_used, "budget": a.budget, **t})

        ev_rows = []
        for sym in [s for s in graph.symbols if s.kind.value != "module"][:5]:
            rep, t = timed(lambda s=sym: collect_evidence(repo, f"{s.name} exists", target_file=s.relative_path, verify=True), a.repeat)
            ev_rows.append({"symbol": sym.qualified_name, "status": rep.status, "spans": len(rep.spans), **t})

        result = {
            "target": src.name, "kitehut_version": gen.metadata.kitehut_version,
            "files": len(graph.files), "symbols": len(graph.symbols), "relationships": len(graph.edges),
            "python_source_tokens_est": py_tokens,
            "scan": scan_t, "generate_all": gen_t, "generate_phase_ms": gen.timings_ms,
            "context": ctx_rows, "evidence": ev_rows,
            "token_savings": "PENDING: needs a defined baseline task set and a real tokenizer; no percentage is claimed.",
        }
        print(f"KiteHut benchmark: {result['target']}  ({result['files']} files, {result['symbols']} symbols, {result['relationships']} edges)")
        print(f"  scan            median {scan_t['median_ms']} ms (first {scan_t['first_ms']})")
        print(f"  generate (all)  median {gen_t['median_ms']} ms   phases: {gen.timings_ms}")
        print(f"  all python source ≈ {py_tokens} tokens (estimate)")
        for r in ctx_rows:
            print(f"  context {r['query']!r:34} {r['median_ms']:>8} ms (first {r['first_ms']})  {r['items']:>2} items  ~{r['tokens_est']} tokens (budget {r['budget']})")
        for r in ev_rows:
            print(f"  evidence {r['symbol'][:40]!r:44} {r['median_ms']:>8} ms (first {r['first_ms']})  {r['status']}  {r['spans']} spans")
        print("  token savings: PENDING (not claimed)")
        if a.json_out:
            Path(a.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
