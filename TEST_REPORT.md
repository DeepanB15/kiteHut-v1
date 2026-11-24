# Test report — KiteHut v0.1.0

## Historical Baseline (pre-session)
Results from the initial commit (04c5413) before any session changes. Preserved as history.

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

## Full suite

Verified on 2026-11-23. Everything below was executed in this checkout; results that
were not reproduced in this session are listed as **Not run** rather than carried
forward as claims.

## Environment

| Item | Value |
|---|---|
| OS | Windows (`win32`), default user account (no Developer Mode / symlink privilege) |
| Interpreter | CPython 3.12.3 |
| Install | existing project environment, `pytest` from the `dev` extra |
| Test root | `backend/` (the package is not installed system-wide for this run) |

## Full suite

```
cd backend
python -m pytest -q --no-header -p no:cacheprovider
```

**Result: 1030 passed, 11 skipped, 0 failed, 67 warnings — exit code 0 — 383.63s (6m23s).**

Baseline recorded on this machine *before* any change in this session (same command):
**4 failed, 1001 passed, 7 skipped**. This is not the same snapshot as the
`919 passed / 19 failed` clean-install baseline quoted in
`docs/IMPLEMENTATION_STATUS.md`: that figure was recorded during the original
release work, before the earlier fixes landed in this working tree. Both are
reported as measured; only the counts above were reproduced in this session.

The four remaining failures were test-harness
incompatibilities with Windows, not product defects:

| Baseline failure | Cause | Resolution |
|---|---|---|
| `test_hostile_symbol_names_cannot_break_markdown_or_paths` | fixture filename contained `|`, illegal on NTFS (`OSError [Errno 22]`) | `hostile_path()` substitutes only characters the OS forbids; the markup/path-safety assertions still run |
| `test_symlinked_output_dir_is_refused` | `os.symlink` → `WinError 1314` (privilege) | `symlink_or_skip()` skips with the OS error; the assertion is unchanged where symlinks are available |
| `test_symlinked_subdirectory_inside_output_is_refused` | same | same |
| `test_source_symlink_escaping_repo_is_not_indexed` | same | same |

No assertion was removed or weakened to reach green: the three symlink tests still
assert refusal and an untouched victim directory when the platform permits symlinks,
and the hostile-name test still checks generated paths and Markdown escaping.

### The 11 skips

All are conditional on the host, not on product state:

- **10 × Windows symlink privilege** (`WinError 1314`): 4 in `tests/knowledge/`,
  1 in `tests/test_phase11_12_e2e.py`, 3 in `tests/code_graph/test_discovery.py`,
  2 in `tests/test_evidence_collector_security.py`.
- **1 × named pipe** (`os.mkfifo` does not exist on Windows):
  `tests/code_graph/test_discovery.py::test_non_regular_python_path_marks_partial`.

The other `.kitehut/` safety paths are exercised positively on this machine by
`test_user_graph_artifact_in_fresh_output_is_never_overwritten` and
`test_user_metadata_file_blocks_before_any_write`, which do not need the privilege.
Refusal of a symlinked `graph/` subdirectory is implemented and covered by
`test_symlinked_graph_dir_is_refused`, but that test skips here for the same
privilege reason.

## Targeted suites run separately

| Command | Result |
|---|---|
| `python -m pytest -q tests/code_graph tests/test_patch_security.py tests/test_evidence_collector_security.py` | 337 passed, 6 skipped |
| `python -m pytest -q tests/knowledge tests/test_phase11_12_e2e.py` | 95 passed, 5 skipped |
| `python -m pytest -q tests/knowledge/test_mcp_cli.py` | 12 passed (MCP stdio, CLI exit codes) |

New tests added in this session (29 collected): 16 cross-file call-resolution tests,
5 dependency-impact / circular-import tests, 5 evidence-verdict tests, 3
ownership/symlinked-graph-directory tests.

## End-to-end and CLI smoke

| Check | Result |
|---|---|
| Fresh venv, `pip install -e ".[dev]"`, console script (2026-11-23) | `kitehut --version` → `0.1.0`; `kitehut --help` lists all commands; editable install succeeded, emitting only a setuptools `license` deprecation warning. That warning belonged to the metadata in use at the time. On 2026-11-24 the metadata was replaced with PEP 639 (`license = "MIT"`, `license-files = ["LICENSE"]`, `setuptools>=77.0.3`), and a fresh wheel/sdist build on that date emitted no license warning. The editable install was **not** rerun after the metadata change, so only its 2026-11-23 behavior is recorded. |
| `kitehut analyze examples/demo_project` | 6 files, 17 symbols, 56 relationships; exit 0 |
| `kitehut status --knowledge-only --strict` | `FRESH`, exit 0 |
| `kitehut context`, `kitehut evidence` | JSON output; evidence report ends with `Verdict: VERIFIED` |
| Demo graph, cross-file behavior | 10 resolved CALLS edges: 8 cross-file via imports, 1 same-file, 1 `self` method; 3 calls left unresolved with diagnostics |
| MCP server over stdio | verified by `tests/knowledge/test_mcp_cli.py` using the official Python client (initialize, list tools, call tools, read-only refusal) |

## Not run / not verified here

- Linux or macOS (the CI workflow targets `ubuntu-latest`, Python 3.11; it has not
  been executed from this machine).
- Python 3.11 specifically, although it is the declared minimum. The code uses no
  syntax newer than 3.11; the previous 3.11 syntax issue is documented in
  `docs/IMPLEMENTATION_STATUS.md`.
- A live Claude Code or Codex session where a model chooses and calls a KiteHut
  tool. Protocol-level MCP behavior is covered; model-driven selection is not.
- Live LLM/Ollama, GitHub API, or frontend build in this session.
- Symlink-based safety tests (skipped on this account, see above).

## Warnings

67 warnings were reported in the 2026-11-23 run, none failing: `pytest-asyncio`
unset default fixture loop scope and pytest `return-not-None` warnings from
pre-existing tests. They are tracked, not suppressed by this work. The setuptools
`license` deprecation warning noted above was part of the preceding editable-install
metadata and is no longer current: the 2026-11-24 PEP 639 metadata replaced it.

---

## Final Pre-Commit Verification — 2026-11-24

This is the final pre-commit verification run for v0.1.0. Everything above is the
**2026-11-23 snapshot and is kept unchanged as history**; the figures in this
section are a later run and are not retroactively merged into it. The counts differ
from that snapshot for one known reason: the MCP server-version regression test was
added after the 2026-11-23 run, which raises the full suite by one test and
`test_mcp_cli.py` from 12 to 13 tests.

| Check | Verified result |
|---|---|
| Full Python suite | **1,031 passed, 11 skipped, 0 failed** |
| MCP tests (`tests/knowledge/test_mcp_cli.py`) | **13 passed** |
| Cross-file relationship tests (`tests/code_graph/test_cross_file_calls.py`) | **16 passed** |
| Fresh wheel and sdist builds | **succeeded** (no license warning) |
| Wheel metadata | **MIT**; `License-Expression: MIT`; `License-File: LICENSE` |
| Fresh wheel installation | **succeeded** (clean virtualenv) |
| CLI version | **0.1.0** |
| Demo analysis | **6 files, 17 symbols, 56 relationships** |
| Knowledge status | **FRESH** (`--knowledge-only --strict`) |
| Evidence | **VERIFIED / SUPPORTED**, 8 spans, 0 stale, 0 missing |
| Real MCP stdio | **KiteHut 0.1.0**, **29** default read-only tools |
| Python 3.11 CI verification | **PENDING** — not executed in this run |

The 11 skips are the same host-conditional skips described in the 2026-11-23 section
above (symlink privilege and named-pipe support), not new or suppressed failures.
Python 3.11 remains the declared minimum and is now a CI matrix leg, but it has not
been run here; until CI executes it, 3.11 support is **pending** rather than verified.
