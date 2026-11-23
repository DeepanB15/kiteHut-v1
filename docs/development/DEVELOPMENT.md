# Development

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # full suite (temp dirs only)
pytest tests/knowledge -q       # KiteHut-specific suite (72 tests)
```
Frontend (optional legacy dashboard): `cd frontend && npm ci && npm run build`.

## Layout
- `backend/app/code_graph/` — Engine A: scanner, builder, models, queries, serialization, validation
- `backend/app/knowledge/` — `.kitehut/` generation, freshness, context (Engine B), evidence (Engine C), redaction, safe I/O
- `backend/app/mcp/` — MCP server and `knowledge_tools.py`
- `backend/app/cli/` — Click CLI (`commands/knowledge_cmds.py`, `status_cmd.py`)
- `examples/demo_project/`, `scripts/demo.sh`, `scripts/benchmark.py`

## Rules for changes
- Never write outside `.kitehut/`; go through `knowledge/safe_io.py`.
- Never overwrite/delete files lacking the generated marker.
- Tests use `tmp_path`; never real repos or databases. Don't loosen assertions to make tests pass.
- Don't make claims in docs that a test or smoke run doesn't back.

See [../../CONTRIBUTING.md](../../CONTRIBUTING.md).
