# `.kitehut/` output specification (knowledge schema v1)

```
.kitehut/
├── graph/repository.json
├── knowledge/{INDEX.md, architecture.md, modules/*.md, symbols/*.md}
├── visualization/graph.html
└── metadata.json
```

## graph/repository.json
Serialized `CodeGraph` (`schema_version` = `CODE_GRAPH_SCHEMA_VERSION`, currently 1). Sorted and
deterministic: the same sources produce byte-identical output, independent of machine or checkout
location. The runtime-only `repository.root_path` is **never** serialized. Validated before writing and on
load; written atomically.

## knowledge/*.md
Every page starts with `<!-- kitehut:generated -->`. Content is limited to facts in the graph: paths,
kinds, line ranges, and **resolved** relationships. Unresolved calls/imports are counted, never guessed.
Source links are relative (`../../../path/to/file.py#L10-L20`). Page names are slugs (`/` becomes `__`),
collision-free and case-insensitively unique. Large repos are bounded by `--max-module-pages` (default
500) and `--max-symbol-pages` (default 1500), ranked by importers / incoming calls; the remainder stays in
`repository.json` and a warning says so.

## visualization/graph.html
One self-contained file: inline CSS/JS/data, a `Content-Security-Policy` of `default-src 'none'`
(no network), canvas rendering, no external libraries. Pan, zoom, click-to-select, node-type and
relationship filters, search, details panel with path and line range. Bounded by `--max-nodes` (default
2000). Embedded JSON is escaped against `</script>` breakout; labels are drawn as canvas/`textContent`.

## metadata.json
`generator` (marker, first key), `kitehut_version`, `knowledge_schema_version`, `graph_schema_version`,
`generated_at` (UTC), `repository_name`, `repository_id`, `file_count`, `symbol_count`,
`relationship_count`, `unresolved_import_count`, `unresolved_call_count`, `source_digest`,
`file_hashes` (path → sha256), `generation_status` (`complete` | `partial`), `truncated`, `diagnostics`,
`outputs`, scan options (`exclude_dirs`, `max_files`), `timings_ms`. The absolute repository path is
deliberately **not** stored.

## Ownership and safety
- `graph/repository.json`, `metadata.json`, and any file with the marker are KiteHut-owned.
- A pre-existing `.md`/`.html`/`metadata.json` **without** the marker is left untouched and reported.
- Stale generated pages are pruned only if they carry the marker; other files (e.g. your notes) stay.
- `.kitehut` and subdirectories must be real directories; symlinks are refused.
