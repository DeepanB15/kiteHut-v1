# Test report — KiteHut v0.1.0

## Results
| Environment | Command | Result |
|---|---|---|
| Python 3.11.18 (editable install, `.[dev]`) | `cd backend && pytest -q` | **1010 passed, 2 skipped, 0 failed** |
| Python 3.13.18 | `cd backend && pytest -q` | **1010 passed, 2 skipped, 0 failed** |
| Clean non-editable install, fresh 3.11 venv, run from outside the tree | `pip install ./backend; kitehut --version; kitehut analyze; kitehut status -k --strict` | version 0.1.0, 21 files written, strict status OK, `mcp` imports |
| Demo | `scripts/demo.sh` | exit 0, all 10 steps (analyze → context → evidence → stale → refresh → fresh) |
| Frontend | `cd frontend && npm ci && npm run build` | exit 0 (not run against a live backend) |
| MCP over stdio | official MCP Python client (automated) | pass; `claude mcp list` → Connected |

Baseline before changes (repository as supplied, clean install): **919 passed, 19 failed**. All 19 were fixed by root cause (see docs/IMPLEMENTATION_STATUS.md).
The 2 skips are pre-existing conditional skips.

## Spec test categories (new, `backend/tests/knowledge/`, 72 tests)
generation/structure · graph JSON validity & determinism · Markdown content & links · HTML visualization · freshness/stale/refresh · safety (symlinks, traversal, unmarked files, self-exclusion, `.gitignore`) · edge cases (empty repo, syntax errors, unicode, large caps) · context · evidence · redaction · MCP tools & read-only gate · CLI exit codes · version.

## Defects found during final verification (fixed)
- `app/knowledge/markdown.py` used f-strings with nested same-type quotes: valid on Python 3.12+, a **SyntaxError on 3.11**, which the project declares as its minimum. Found by running the suite on 3.11; fixed; both interpreters now pass.
- Two pre-existing tests hardcoded `"2.0.0"` as the CLI version. The release version is now 0.1.0 (a documented breaking change), so they now compare against `app.__version__`. Expected behavior was changed because the specified version changed, not to hide a failure.

## Notes
- Console-script tests require the venv's `bin` on `PATH` (activate the venv); otherwise 3 tests fail with "entry point not found". That is an environment requirement, not a code failure.
- Not tested: live LLM/Ollama, live GitHub, a model-driven Claude Code or Codex tool call, a Windows/macOS run.
