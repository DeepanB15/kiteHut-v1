# KiteHut

**Graph to understand. Context to reason. Evidence to trust.**

KiteHut is a local-first **repository intelligence & evidence engine**. It analyzes a
software repository and writes persistent, structured knowledge into a `.kitehut/` folder
inside it — a code graph, Markdown documentation, and an offline interactive graph — that
developers and AI coding agents (Claude Code, OpenAI Codex, any MCP client) can read,
query and verify against the real source.

```
Analyze Repository → Build Code Graph → Generate Repository Knowledge
      → Retrieve Smart Context → Verify with Source Evidence → Connect AI Agents
```

Everything runs on your machine. Core analysis needs no network, no API key, and no LLM.

> **Status: v0.1.0.** Python repositories only. See [Limitations](#limitations) and
> [docs/FEATURES.md](docs/FEATURES.md) for what is and is not implemented.

---

## Problem and solution

AI agents and new teammates burn time and context re-discovering a repository: which files
matter, who calls what, what imports what — and they can't tell whether a stale summary still
matches the code.

KiteHut precomputes that structure once, keeps it next to the code, and ties every statement
to a **file path, line range and content hash**. The graph *locates* code; the **source is the
truth** that verifies claims. Stale knowledge is detected, not trusted.

KiteHut is **not** an autonomous coding agent, an IDE, a generic RAG chatbot, or a hosted SaaS.

## Architecture

```
Repository Scanner      (.gitignore-aware, symlink-safe, excludes venvs/caches/.kitehut)
        ↓
Code Graph Engine       (files, classes, functions, methods, imports, calls; deterministic JSON)
        ↓
Smart Context Engine    (graph-guided ranking, de-duplication, token-budgeted assembly)
        ↓
Evidence Engine         (source spans re-read and hashed; stale/missing flagged; verifier)
        ↓
.kitehut/ Knowledge Output   (repository.json · INDEX.md · modules · symbols · graph.html · metadata.json)
        ↓
Claude Code / Codex / any MCP client   (read-only by default)
```

One Python package, three integrated engines — not microservices. Details:
[ARCHITECTURE.md](ARCHITECTURE.md).

## Features

| Area | Status |
|---|---|
| Code graph for Python (files, symbols, resolved imports/calls), deterministic JSON | IMPLEMENTED |
| `.kitehut/` output: graph, Markdown knowledge, standalone HTML graph, metadata | IMPLEMENTED |
| Freshness / stale detection and `refresh` | IMPLEMENTED |
| Smart context retrieval (graph-guided, de-duplicated, token-budgeted, freshness-checked) | IMPLEMENTED (lexical + graph; no embeddings) |
| Evidence collection and verification with hashes and stale flags | IMPLEMENTED |
| MCP server with read-only `kitehut_*` tools | IMPLEMENTED |
| Claude Code integration | PARTIALLY IMPLEMENTED (registers and connects; see [docs](docs/integrations/CLAUDE_CODE.md)) |
| Codex integration | PARTIALLY IMPLEMENTED (config persisted; end-to-end not verified) |
| Cross-file call resolution (imported functions, aliases, `self`/`cls` methods) | IMPLEMENTED (conservative; ambiguous calls stay unresolved) |
| Languages other than Python | NOT IMPLEMENTED |
| Legacy agent/guardian/dashboard (from RepoLens) | EXPERIMENTAL, kept for compatibility |

Full matrix: [docs/FEATURES.md](docs/FEATURES.md).

## Installation

Requires Python 3.11+. The Python package lives in `backend/`; install from there.

```bash
git clone https://github.com/DeepanB15/kiteHut-v1.git
cd kiteHut-v1/backend
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # drop "[dev]" if you do not need the test tools
kitehut --version
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
kitehut --version
```

More options (including a non-editable install): [docs/installation/INSTALLATION.md](docs/installation/INSTALLATION.md).

## Quick start

Run these from the `backend/` directory (or anywhere, once the CLI is installed):

```bash
kitehut analyze ./my-project        # builds ./my-project/.kitehut/
```

Then open `my-project/.kitehut/knowledge/INDEX.md` and
`my-project/.kitehut/visualization/graph.html` (offline; double-click it).

```bash
kitehut context ./my-project "how is the order total calculated"
kitehut evidence ./my-project "calculate_total exists" --file shop/pricing.py --verify

# after editing code:
kitehut status ./my-project         # reports STALE with the changed files
kitehut refresh ./my-project
```

A complete, reproducible walkthrough on a bundled sample project: `scripts/demo.sh`
(expected output in [docs/DEMO.md](docs/DEMO.md)).

## CLI commands

| Command | What it does |
|---|---|
| `kitehut analyze PATH` | Generate the full `.kitehut/` folder (graph + knowledge + visualization + metadata) |
| `kitehut graph PATH` | Only `graph/repository.json` (+ metadata) |
| `kitehut knowledge PATH` | Graph + Markdown knowledge |
| `kitehut visualize PATH [--open]` | Graph + standalone `graph.html` |
| `kitehut refresh PATH [--force]` | Regenerate after changes (reuses previously generated parts; no-op if unchanged) |
| `kitehut status PATH [-k] [--json] [--strict]` | `.kitehut/` freshness (then legacy repo health unless `-k`) |
| `kitehut context PATH "question" [--budget N] [--format json]` | Ranked, token-budgeted source context |
| `kitehut evidence PATH "statement" [--file F] [--line N] [--pattern P] [--verify]` | Source-backed evidence |
| `kitehut mcp PATH [--allow-agent-tools]` | MCP server over stdio |

`PATH` defaults to `.` for the generating commands. Generation options: `--exclude DIR`
(repeatable), `--max-files N`, `--strict`. **Exit codes:** `0` success · `1` error (bad path,
build or safety failure; message printed, no traceback) · `2` usage error · `3` `--strict` and
the result was partial/stale.

The legacy RepoLens commands (`init`, `ask`, `review`, `fix`, `findings`, `git`, `github`,
`report`, `doctor`, `models`, `start`) are unchanged. `status PATH` and `status --path PATH`
both work.

## `.kitehut/` output structure

```
my-project/
├── src/ …
└── .kitehut/
    ├── graph/repository.json        # schema-versioned, deterministic, validated, written atomically
    ├── knowledge/
    │   ├── INDEX.md                 # start here
    │   ├── architecture.md          # packages, most-imported modules, import cycles, limitations
    │   ├── modules/<module>.md      # one page per module: symbols, imports, source line links
    │   └── symbols/<symbol>.md      # one page per symbol: location, members, calls, callers
    ├── visualization/graph.html     # standalone, offline, zoom/pan/select/filter/search
    └── metadata.json                # version, schema, timestamp, counts, source fingerprint, per-file hashes, status
```

Guarantees: source files are never written; `.kitehut/` is excluded from its own indexing;
`.gitignore`, virtualenvs, caches and build output are respected; files KiteHut did not
generate are never overwritten or deleted; symlinked or escaping output locations are refused;
no absolute machine paths are stored. Full spec: [docs/architecture/KNOWLEDGE_OUTPUT.md](docs/architecture/KNOWLEDGE_OUTPUT.md).

Whether to commit `.kitehut/` is your call; this repository's `.gitignore` ignores it.

## Claude Code and Codex (MCP)

MCP is optional — KiteHut is useful without any agent.

```bash
kitehut analyze /path/to/project
claude mcp add kitehut -- kitehut mcp /path/to/project     # Claude Code
codex  mcp add kitehut -- kitehut mcp /path/to/project     # OpenAI Codex
```

The server is **read-only by default**: `kitehut_overview`, `kitehut_search_symbols`,
`kitehut_find_callers`, `kitehut_find_callees`, `kitehut_find_dependencies`,
`kitehut_get_context`, `kitehut_get_evidence`, `kitehut_read_knowledge`,
`kitehut_check_freshness`, plus read-only repository tools. Tools that write files, run
commands or change git state exist only behind `--allow-agent-tools`.

What was actually verified (and what was not) is spelled out in
[docs/integrations/](docs/integrations/CLAUDE_CODE.md).

## Testing

```bash
cd backend
pip install -e ".[dev]"
pytest -q
```

Tests use temporary repositories and never touch your real repository or databases. Results of
the release verification run are in [TEST_REPORT.md](TEST_REPORT.md).

## Performance

`python scripts/benchmark.py PATH` measures scan, graph build, generation, context retrieval
and evidence time on a copy of the target. **No token-savings percentage is claimed**:
that comparison is PENDING (framework and rationale in
[docs/development/BENCHMARKS.md](docs/development/BENCHMARKS.md)).

## Limitations

- **Python only.** Other languages are not analyzed.
- **Conservative resolution.** Calls are resolved only when the target is unique: same-file,
  imported (including aliases), module-qualified (`mod.func`) and `self`/`cls` method calls.
  Ambiguous or dynamic calls are counted as unresolved, never guessed.
- **Static analysis.** Dynamic imports, reflection and monkey-patching are not modeled.
- Context retrieval is lexical + graph ranking; no embeddings. Token counts are `chars/4` estimates.
- Hard-coded secrets in `.py` files can appear in source snippets; KiteHut applies best-effort redaction to what it *serves*, which is a heuristic, not a scanner.

Complete list: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Roadmap

Planned, **not** implemented: more languages (TypeScript first);
incremental graph updates; embedding-assisted retrieval via the existing retrieval stack; a
token-savings benchmark with a real tokenizer; verified Codex end-to-end test; consolidating the
legacy context/retrieval modules ([audit](docs/architecture/CONTEXT_AUDIT.md)).

## Legacy RepoLens features

This codebase began as *RepoLens*, a local AI engineering agent with a FastAPI service and React
dashboard. That code is preserved and still wired in as **optional, experimental adapters**
(see [docs/FEATURES.md](docs/FEATURES.md)). The previous README and PRDs are kept in
`archive/repolens-v2/pre-kitehut-docs/`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## License

KiteHut is released under the MIT License — see [LICENSE](LICENSE) (Copyright (c) 2026 Deepan B).
`backend/LICENSE` is an identical copy so the license ships inside the wheel and sdist.
Third-party dependencies, and any code inherited from other projects, keep their own licenses and
are **not** covered by KiteHut's MIT license.
