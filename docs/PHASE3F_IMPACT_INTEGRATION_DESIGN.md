# Phase 3F — Impact Analysis Integration: Subphase A Design

**Status:** design for engineering review. No implementation code written yet.
**Scope:** unify Phase 3B (change detection), 3C (file/symbol mapping), 3D (impact
propagation) and 3E (explainable ranking) into one read-only workflow, then expose it
through the CLI and MCP.

---

## 1. Audit of existing components

### 1.1 `app/git_ops/change_detection.py` — partial coverage

`ChangeDetector(repository_path)` resolves the root with
`Repo(root, search_parent_directories=True)` and offers three entry points.

| Method | Git call | What it actually reports |
|---|---|---|
| `detect_working_tree_changes()` | `git diff` | **Unstaged tracked modifications only** |
| `detect_base_comparison_changes(base)` | `git diff <base> HEAD` | Committed changes between base and HEAD |
| `detect_all_changes(base=None)` | dispatcher | base comparison if `base`, else working tree |

Gaps that matter for Phase 3F:

1. **No staged changes.** `git diff` without `--cached` omits the index entirely, so a
   `git add`-ed modification is invisible.
2. **No untracked files.** `git diff` never lists them.
3. **`status` is hardcoded to `"modified"`** for every unit. Deleted files appear in the
   diff output (with `deleted file mode`) but are reported as modified with
   `is_deleted=False`; nothing sets `is_renamed`, `previous_path`, `is_staged` or
   `is_untracked`. `ChangeStatus` already models `ADDED/DELETED/RENAMED/UNTRACKED`, so the
   model is ahead of the detector.
4. **Whole-diff duplication.** The full multi-file diff text is attached to *every*
   `ChangeUnit.diff`, not the unit's own hunks. Harmless for 3D (which ignores `diff`),
   blocking for diff→symbol mapping.
5. **Silent failure.** `detect_working_tree_changes` wraps everything in
   `try/except Exception: logger.warning(...)` and returns an empty `Changeset`. A git
   failure is therefore indistinguishable from "clean repository" — the single most
   dangerous failure mode for an impact tool.

**Conclusion:** the detector cannot satisfy the Phase 3F test matrix (staged, untracked,
deleted) and must be extended. This is a *capability* extension, not duplicated logic.

### 1.2 `app/git_ops/mapping_service.py` — one real defect

`FileMapper.register_file(file_node, symbol_map)` records symbols in the private
`_path_to_symbols` cache and sets `MappingResult.symbol_confidence[symbol_id] = EXACT`,
but it **never writes `MappingResult.file_to_symbols`**. Since
`impact_analysis._mapping_confidence()` reads exactly that dict to decide between
`exact` and `file_level_only` for file nodes, every file-level impact will be reported as
`file_level_only` even when full symbol mapping succeeded. `MappingResult.fully_mapped`
is likewise never set by the mapper.

Nothing in `app/` constructs a `MappingResult` or a `FileMapper` today (grep: zero
call sites), so Phase 3F is the first consumer. Two options:

* **(A, recommended)** Have `register_file` also populate
  `file_to_symbols[norm_path] = set(symbol_map.values())`. Two lines, no existing
  consumer, makes mapping confidence truthful.
* **(B)** Build `MappingResult` in the orchestration layer by walking the graph. Rejected:
  it duplicates the mapper and leaves the mapper's own defect in place.

Also note `FileMapper.normalize_path` is a **third** path normaliser
(`\`→`/`, `//`→`/`), disagreeing with `impact_analysis._normalize_path` (also drops `.`)
and `models.normalize_repository_path` (rejects `..` and absolute paths). Orchestration
will normalise with `normalize_repository_path` before calling the mapper so the three
never disagree in practice; the consolidation itself is out of scope.

`symbol_map` is keyed by bare `name`, so two symbols sharing a name inside one file (e.g.
a `run` method in two classes) collide. Orchestration will key on `qualified_name`
instead, which is unique per file.

### 1.3 `app/git_ops/impact_analysis.py` — stable, no changes

`create_impact_analysis_service(graph, mapping_result, traversal_config).analyze(changeset)`.
Config surface: `max_depth`, `max_results`, `kinds`, `exclusions`, `changed_symbols`.
Output buckets: `direct_impacts`, `transitive_impacts`, `unresolved_impacts`,
`ambiguous_impacts`, `unmapped_changes`, `dependency_paths`, `traversal_metadata`,
`analysis_warnings`, `analysis_summary`. Phase 3F treats it as a black box; no change.

### 1.4 `app/git_ops/ranking.py` — stable, one additive change

`rank_impacts(result, *, max_ranked=50, max_unconfirmed=None)` returns
`ImpactRankingResult(ranked_impacts, unconfirmed_impacts, ranking_metadata,
confidence_limitations)`. Weights and tier bands live in module constants
(`FACTOR_WEIGHTS`, `TIER_BANDS`).

Additive change only: publish the policy identity (`RANKING_POLICY_VERSION = "v0.2.0"`,
`CALIBRATION_NOTE = "heuristic weights, not empirically calibrated"`) and include both in
`ranking_metadata`. **No weight, threshold or ordering changes.**

### 1.5 `app/code_graph/traversal.py` — no changes

`_build_index` is consumed privately by the impact engine. Untouched.

### 1.6 Graph construction and loading conventions

| Path | API | Failure |
|---|---|---|
| Fresh build | `CodeGraphBuilder(root, scanner=RepositoryScanner(root, exclude_dirs=..., max_files=...)).build()` → `.graph`, `.status`, `.diagnostics` | `.graph is None` |
| Cached | `app.knowledge.store.load_graph(root)` reads `.kitehut/graph/repository.json` | `KnowledgeNotFoundError` |

Both are established: `app/knowledge/generator.py` builds, MCP `overview`/`find_callers`
load, and `kitehut traverse` builds. A stale cached graph silently mis-reports impact, so
**fresh build is the default** and cached load is an explicit opt-in.

### 1.7 CLI conventions

* Commands are click commands in `app/cli/commands/*.py`, registered in `app/cli/main.py`.
* `PATH_ARG = click.argument("path", default=".", type=click.Path(...))`.
* Bounds use `click.IntRange`; `--depth` is `IntRange(min=-1)` with `-1 = unlimited`.
* JSON output is a flag: `--json/--no-json` (traverse) or `--json` `is_flag` (knowledge).
* Presentation helpers from `app.cli.ui`: `print_header`, `print_success`, `print_error`,
  `print_warning`, `print_info`, `print_bullet`, `print_table`, `bold`, `dim`.
* Exit codes: `0` success, `1` failure, `2` click usage error, `3` `--strict` partial.

**Pre-existing defect, unrelated to Phase 3F:** `traverse_cmd.py` calls `print_bullet`
and references `EdgeKind` without importing either (NameError on every run), and its
`as_json` parameter is never used, so `kitehut traverse --json` prints prose. Reported for
triage; not fixed here.

### 1.8 MCP conventions

`app/mcp/knowledge_tools.py` defines a `KnowledgeTools` class bound to one repository root,
each method wrapped in `self._guard(...)` which returns `{"success": bool, "error": str,
"duration_ms": float}` and never leaks a traceback. Tools are declared as
`(tool_name, method_name, description)` tuples in `TOOL_SPECS`; `server.py`
`_register_knowledge_tools` resolves type hints and drops `None` arguments. Output is
capped (`MAX_RESULTS = 50`, `MAX_PAGE_CHARS = 60_000`).

`mcp` is not installed in this environment, so MCP tests cannot run here (unchanged from
Phase 3D/3E). CLI tests can.

---

## 2. Orchestration design

### 2.1 Location

`backend/app/git_ops/impact_service.py`, beside the four services it coordinates, exported
as `create_impact_workflow(...)` / `run_impact_analysis(...)`. Not re-exported from
`app/git_ops/__init__.py`, consistent with `impact_analysis` and `ranking`.

### 2.2 Options object

```python
@dataclass(frozen=True)
class ImpactWorkflowOptions:
    mode: str = "working_tree"        # "working_tree" | "staged" | "base"
    base_revision: str | None = None
    staged_only: bool = False
    include_untracked: bool = True
    max_depth: int = -1
    max_results: int = 1000
    max_ranked: int = 25
    max_unconfirmed: int | None = None
    kinds: tuple[EdgeKind, ...] | None = None
    exclusions: tuple[str, ...] = ()
    changed_symbols: tuple[str, ...] = ()
    graph_source: str = "build"       # "build" | "cached"
    max_files: int = DEFAULT_MAX_FILES
    exclude_dirs: tuple[str, ...] = ()
```

Validation happens once, in `__post_init__`-style explicit `_validate()`: unknown `mode`,
`base` missing for `mode="base"`, `max_depth < -1`, `max_results < 1`, `max_ranked < 1`,
`kinds` containing structural kinds. Errors raise `ImpactWorkflowError`, a single
exception type the CLI maps to exit 1 and MCP maps to `{"success": false}`.

### 2.3 Pipeline

```
run()
 ├─ 1. detect        ChangeDetector.detect(mode=..., base=..., staged_only=..., include_untracked=...)
 ├─ 2. graph         _build_graph()  → CodeGraphBuilder | load_graph
 ├─ 3. map           _build_mapping(graph) → MappingResult via FileMapper
 ├─ 4. symbols       _resolve_changed_symbols(changeset, graph) → list[str] refs
 ├─ 5. propagate     create_impact_analysis_service(...).analyze(changeset)
 ├─ 6. rank          rank_impacts(impact, max_ranked=..., max_unconfirmed=...)
 └─ 7. report        _build_report(...) → ImpactReport
```

No step reimplements a neighbour: 1 delegates to `ChangeDetector`, 2 to the builder or
store, 3 to `FileMapper`, 5 to `ImpactAnalysisService`, 6 to `rank_impacts`. Step 4 is
the only new analysis logic and it is Subphase E; until then it returns
`options.changed_symbols` verbatim.

`_build_mapping` walks `graph.files`, groups `graph.symbols` by `relative_path`, and calls
`register_file(file, {sym.qualified_name: sym.id})`. `fully_mapped` is set to `True` only
when the number of registered files equals the number of graph files; symbol coverage is
reported separately so a file with zero symbols is not counted as a mapping failure.

**Scope filtering.** `ChangeDetector` uses `search_parent_directories=True`, so running
`kitehut impact subdir/` inside a larger checkout returns the whole repository's changes.
The orchestrator will filter units to those under the requested subdirectory and record
`scope` plus a `changes_outside_scope` count, rather than silently widening or narrowing
without evidence.

### 2.4 Change detection extension (Subphase B, needs approval)

Extend `ChangeDetector` without changing existing signatures:

* `detect_working_tree_changes()` → union of unstaged, staged and (optionally) untracked
  entries, built from `git status --porcelain=v1 -z` plus `git diff` / `git diff --cached`
  for hunk text. Correct `ChangeStatus`, `is_staged`, `is_unstaged`, `is_untracked`,
  `is_deleted`, `is_renamed`, `previous_path`.
* `detect_base_comparison_changes(base)` → parse `git diff --name-status` for status and
  renames, per-file diff for hunks.
* New `detect(mode, base, staged_only, include_untracked)` façade that merges and
  de-duplicates by path, keeping the strongest status (deleted > renamed > added >
  modified > untracked).
* Per-file `diff` text instead of the whole diff on every unit.
* Replace the swallowed exception with a recorded warning that reaches
  `report.analysis_warnings`, so "git unavailable" never reads as "no changes".

This is the only place Phase 3F modifies an earlier phase's file, and it is additive: no
existing signature or default changes, and the sole current consumer is Phase 3F itself.

---

## 3. Final report contract

`ImpactReport.to_dict()` — deterministic, no timestamps, no absolute paths.

```jsonc
{
  "schema_version": 1,
  "policy": {
    "ranking_version": "v0.2.0",
    "calibration": "heuristic weights, not empirically calibrated"
  },
  "mode": { "detection": "working_tree", "base_revision": null,
            "staged_only": false, "include_untracked": true,
            "scope": null, "changes_outside_scope": 0 },
  "repository": { "repository_id": "...", "name": "...", "graph_source": "build",
                  "revision": "...", "source_digest": "...",
                  "file_count": 0, "symbol_count": 0, "relationship_count": 0 },

  "workflow": { "mode", "base_revision", "base_overrides_working_tree",
                "base_comparison_excludes_working_tree", "staged_only",
                "include_untracked", "scope", "graph_source", "max_depth",
                "max_results", "max_ranked", "max_unconfirmed", "kinds",
                "exclusions", "changed_symbols", "max_files", "exclude_dirs",
                "read_only" },
  "graph": { "source", "fresh", "cache_path", "build_status", "truncated",
             "file_count", "symbol_count", "edge_count", "diagnostic_count" },
  "change_detection": { "unit_count": 0, "degraded": false,
                        "errors": [], "warnings": [] },

  "changeset_summary": { "unit_count": 0,
                         "by_status": {"modified": 0, "added": 0, "deleted": 0,
                                       "renamed": 0, "untracked": 0},
                         "staging": {"staged": 0, "unstaged": 0, "untracked": 0},
                         "has_changes": false },
  "changed_files":  [ { "path", "previous_path", "status", "is_staged", "is_unstaged",
                        "is_untracked", "is_deleted", "is_renamed", "change_source" } ],
  "changed_symbols":[ { "node_id", "name", "qualified_name", "path", "start_line",
                        "end_line", "kind", "resolution": "hunk|explicit|file_expansion",
                        "confidence": "symbol_level|file_level_only" } ],
  "symbol_mapping":  { "scope": "reporting_and_evidence", "method": "...",
                       "semantic_analysis": false,
                       "files": [ { "path", "precision", "reason", "hunk_count",
                                    "matched_symbol_count", "notes" } ],
                       "symbol_level_file_count": 0,
                       "file_level_only_file_count": 0 },

  "mapped_changes":  [ { "path", "node_id", "symbol_ids", "symbol_count",
                         "mapping_confidence": "exact|file_level_only" } ],
  "unmapped_changes":[ { "path", "status", "reason", "mapped" } ],

  "direct_impacts":     [ /* Phase 3D records, verbatim */ ],
  "transitive_impacts": [ /* Phase 3D records, verbatim */ ],
  "dependency_paths":   { "<node_id>": [["<node_id>", "..."] ] },

  "ranked_impacts":     [ { "original_impact": {...}, "node_id", "priority_score",
                            "priority_tier", "classification", "depth",
                            "factor_values", "factor_contributions",
                            "evidence_paths", "independent_path_count",
                            "explanation", "confidence_limitations" } ],
  "unconfirmed_impacts":[ /* same shape, confirmed=false, separate list */ ],

  "evidence": { "edge_records": [ { "edge_id", "kind", "resolution", "confidence",
                                    "discovery_method", "source_file",
                                    "start_line", "end_line", "start_column",
                                    "end_column", "source_hash" } ],
                "record_count": 0 },

  "ranking": { "factor_weights": {...}, "tier_bands": [[85.0,"P0"], ...],
               "tier_distribution": {...}, "metadata": {...} },

  "analysis_warnings": ["..."],
  "truncation": { "analysis_truncated": false, "ranking_truncated": false,
                  "max_results": 1000, "max_ranked": 25,
                  "impacts_discovered": 0, "impacts_returned": 0,
                  "components_found": 0, "components_returned": 0 },

  "summary": { "changed_file_count", "changed_node_count", "impacted_node_count",
               "direct_impact_count", "transitive_impact_count",
               "unresolved_impact_count", "ambiguous_impact_count",
               "unmapped_change_count", "confirmed_impact_count",
               "top_priority_tier", "file_level_only_impact_count" },

  "limitations": ["..."],
  "navigation_only": true,
  "static_impact_only": true,
  "runtime_failure_verified": false
}
```

Contract freeze notes:

* `workflow`, `graph`, `change_detection`, `symbol_mapping` and the extra
  `repository` fields (`revision`, `source_digest`) are **additive**: they carry the run
  configuration, graph provenance and mapping evidence the report needs to be
  self-describing. They are part of the frozen contract, not an alternative one.
* `repository.graph_source` deliberately repeats `graph.source` so a consumer can read
  provenance from the repository summary without loading the graph section.
* `change_detection.degraded` is `true` when one or more per-file diffs could not be
  read; `change_detection.errors` then names each failure. A failure that prevents
  detection entirely raises instead of producing a report.
* `mode.staged_only` and `workflow.staged_only` report the **effective** state, so
  `mode="staged"` sets both to `true` even when the flag was left at its default, and a
  base comparison reports `false`.

Coverage of the required contract: changeset summary → `changeset_summary`; changed files
and symbols → `changed_files`/`changed_symbols`; mapped/unmapped → `mapped_changes`/
`unmapped_changes`; direct/transitive → `direct_impacts`/`transitive_impacts`; ranked and
unconfirmed → `ranked_impacts`/`unconfirmed_impacts`; dependency paths →
`dependency_paths`; evidence and provenance → `evidence.edge_records`; factor
contributions → inside each ranked record **and** `ranking`; warnings →
`analysis_warnings`; truncation → `truncation`; statistics → `summary`.

**Determinism decision:** `Changeset.generated_at` defaults to `datetime.now()`, so the
report never embeds `Changeset.to_dict()`. `changeset_summary` is computed from the units
instead, and `ImpactReport` carries no clock field. All lists are sorted with the same
keys Phase 3D/3E already use.

**Static vs runtime** is stated in three places, deliberately: `navigation_only`,
`static_impact_only`, `runtime_failure_verified: false` at the root, plus the per-record
`confidence_limitations` inherited from Phase 3E.

---

## 4. CLI design — `kitehut impact`

```
kitehut impact [PATH]
  --base REV                  Compare REV..HEAD instead of the working tree.
                              Implies --no-working-tree.
  --working-tree/--no-working-tree   Working-tree mode (default). Overridden by --base.
  --staged-only               Only staged changes (working-tree mode; error with --base).
  --untracked/--no-untracked  Include untracked files (default on, working-tree mode).
  --depth N                   Max propagation hops; -1 = unlimited (default -1).
  --kinds K1,K2               Dependency edge kinds (default imports,calls).
  --exclusions IDS            Comma-separated node IDs to exclude.
  --max-results N             Impact record cap (default 1000, min 1).
  --max-ranked N              Ranked component cap (default 25, min 1).
  --graph [build|cached]      Build fresh (default) or read .kitehut/graph/repository.json.
  --max-files N, --exclude DIR   Build-time scan limits, as in `kitehut graph`.
  --json/--no-json            Machine-readable output (default off).
  --ranked/--no-ranked        Order the text report by priority (default on).
```

Resolution order in the command body: build options → `ImpactWorkflow.run()` →
`click.echo(json.dumps(report.to_dict(), indent=2))` when `--json`, else a `ui`-based
report (header, changeset line, ranked table, warnings via `print_warning`, limitations
via `print_info`). Exit `0` on success (including clean repository), `1` on
`ImpactWorkflowError`, `2` on usage errors from click.

`--json` intentionally prints to stdout only; warnings go into the payload, never mixed
into the JSON stream.

---

## 5. MCP design — `kitehut_impact`

```python
def impact(self, base: Optional[str] = None, staged_only: Optional[bool] = None,
           depth: Optional[int] = None, kinds: Optional[str] = None,
           max_results: Optional[int] = None, max_ranked: Optional[int] = None,
           include_ranked: Optional[bool] = None) -> Dict[str, Any]:
    """Read-only change impact analysis: changeset, impacts, ranking, evidence."""
```

Registered by appending `("kitehut_impact", "impact", "...")` to `TOOL_SPECS` — no server
change. The method builds `ImpactWorkflowOptions`, calls the **same**
`ImpactWorkflow.run()` as the CLI, and returns
`{"success": True, "data": report.to_dict(), "duration_ms": ...}` through `_guard`.
`None` arguments are dropped by the server wrapper, so defaults come from the options
object. `repository_path` is never an argument: the tool is bound to the server root.

`include_ranked=False` omits `ranked_impacts`/`unconfirmed_impacts` from `data` for token
economy while keeping `summary.top_priority_tier`. Output is capped as elsewhere
(`MAX_RESULTS`) — ranked lists beyond the cap are dropped and `truncation` says so.

---

## 6. Diff-to-symbol assessment (Subphase E)

**Verdict: feasible, moderate size, no architectural change.** Deferred to its own
subphase so the orchestration layer can be reviewed without it.

Inputs already exist: `ChangeUnit.diff`, and `SymbolNode.start_line`/`end_line` in current-
file coordinates. Plan:

1. New pure module `app/git_ops/diff_hunks.py`:
   `parse_unified_diff(text) -> list[FileHunk]` where a hunk is
   `(old_start, old_len, new_start, new_len)` per file. ~120 lines, no dependencies.
2. Change detection attaches **only that file's** diff to each unit (already planned in
   §2.4), so a unit's hunks are unambiguous.
3. Affected range per hunk in *current* coordinates:
   `[max(1, new_start), new_start + max(new_len, 1) - 1]`, which also covers pure deletions
   (`new_len == 0`), where the change is anchored at the deletion point.
4. Intersect with each `SymbolNode` span in the same file; matched symbols become
   `changed_symbols` refs (`path::qualified_name`, already supported by Phase 3D).
5. Precision reporting per file: `symbol_level` only when the diff parsed cleanly, the file
   node exists and every touched range resolved; otherwise `file_level_only` **plus** a
   warning naming the file. Renames (`similarity index`, no hunks), binaries and files with
   no symbols all fall back rather than guess.
6. Ceiling, stated up front: hunk→symbol is *line-range intersection*, not semantic
   analysis. A hunk spanning two functions marks both; a change on a decorator line marks
   the function it decorates only if the span includes it. This is why the capability is
   labelled `symbol_level` **precision**, never `symbol_level` certainty, and why every
   such record still carries the Phase 3E runtime-failure disclaimer.

If step 1 or 3 proves unreliable against the real diff fixtures, the fallback is to keep
`file_level_only` confidence for all units and document the limitation — the brief
explicitly allows that outcome.

---

## 7. Test plan (Subphase F)

| Area | Test | Fixture |
|---|---|---|
| Clean repository | no units, empty impacts, exit 0, `has_changes: false` | temp git repo, no edits |
| Modified file | one unit, `modified`, direct + ranking present | edit a tracked file |
| Staged / unstaged | `is_staged` / `is_unstaged` distinct, both merged once | `git add` one file, edit another |
| Untracked / deleted | `untracked`, `deleted` statuses; deleted never traversed | new file, `rm` a tracked file |
| Base revision | `mode.base_comparison`, only committed delta | two commits, compare |
| File mapping | `mapped_changes` links path → node → symbols; `exact` when symbols exist | graph with symbols |
| Unmapped changes | `not_in_graph`, warnings, still counted | changeset path absent from graph |
| Direct / transitive | counts and `dependency_paths` match Phase 3D | imports chain |
| Ranking | weights sum to 1, tier bands applied, explanations present | mixed graph |
| Unresolved / ambiguous | separate `unconfirmed_impacts`, never in `ranked_impacts` | resolution kinds |
| Truncation | `truncation.*` flags and counts; no silent caps | many importers |
| CLI | `CliRunner` exit codes, `--json` parses, `--kinds`/`--depth` respected, bad `--base` exits 1 | temp git repo |
| MCP | `KnowledgeTools.impact()` returns `success` and the same payload as the CLI | skipped locally (no `mcp`) |
| Determinism | two runs byte-identical; reordered edges identical | repeated invocation |
| Error handling | not a git repo, invalid base, missing cached graph, structural `--kinds` | each failure path |

Determinism and error handling are pure-Python and always runnable here. MCP tests follow
the existing `tests/knowledge/test_mcp_cli.py` pattern and stay skipped/failing on the
pre-existing missing `mcp` dependency, reported as such rather than claimed as passing.

---

## 8. Documentation plan (Subphase F)

`docs/IMPACT_ANALYSIS.md` covering: command usage with examples, MCP tool schema,
classification table (`direct`/`transitive`/`unresolved`/`ambiguous`/`unmapped`), ranking
formula with the six weights and tier bands, the explicit statement that weights are
heuristic and not empirically calibrated, evidence limitations, truncation behaviour in
both layers, and a "static impact ≠ runtime verification" section. Linked from
`FEATURE_STATUS.md` if that file already tracks phases.

---

## 9. Decisions requiring approval before Subphase B

1. **Extend `ChangeDetector`** (staged, untracked, deleted, renamed, per-file diff,
   surfaced git errors). Additive, but it is the one place an earlier phase's file changes.
2. **Fix `FileMapper.register_file`** to populate `MappingResult.file_to_symbols`. Two
   lines; without it every file-level impact is mislabelled `file_level_only`.
3. **Scope filtering** for subdirectory invocations, with `changes_outside_scope` counted.
4. **Fresh graph build as the default**, `--graph cached` as the opt-in fast path.
5. **No timestamp in the report**, to keep `--json` output byte-reproducible.
6. **`--base` overrides `--working-tree`** rather than erroring, documented in `--help`.
