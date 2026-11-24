# Installation

Requires **Python 3.11+**. No network access, API key, database, or LLM is needed for core analysis.

## From source (recommended)

```bash
git clone https://github.com/DeepanB15/kiteHut-v1.git
cd kiteHut-v1/backend
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                     # developers: pip install -e ".[dev]"
kitehut --version                    # kitehut, version 0.1.0
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
kitehut --version
```

## Non-editable install
`pip install ./backend` installs the `kitehut` command and the `app` package. Verified in a fresh virtualenv, running from outside the source tree.

> **Note:** the package's top-level import name is `app` (inherited from the project layout). Install KiteHut in its own virtualenv to avoid clashing with another package called `app`.

## Dependencies
Declared in `backend/pyproject.toml` and `backend/requirements.txt` (kept in sync): FastAPI/uvicorn and
LangGraph for the optional legacy service, `mcp>=1.2,<2.0`, `networkx`, `click`, `colorama`, `pydantic`, and others. `mcp` 2.x is **not** supported.

## Verify the installation

```bash
cp -R ../examples/demo_project /tmp/kh-demo
kitehut analyze /tmp/kh-demo
kitehut status /tmp/kh-demo --knowledge-only --strict && echo fresh
```

On Windows PowerShell, copy the sample with `Copy-Item -Recurse ..\examples\demo_project $env:TEMP\kh-demo`
and run the same `kitehut` commands. You can also run `../scripts/demo.sh`.

## Optional components
- **Legacy API/dashboard:** `docker compose up` or see `start-backend.bat` / `start-frontend.bat`. Copy `backend/.env.example` to `backend/.env` first (placeholders only; never commit `.env`).
- **LLM features** (`ask`, `review`, `fix`): need a local Ollama; not required for anything in this document.

## Uninstall
`pip uninstall kitehut`. To remove generated knowledge from a project, delete its `.kitehut/` folder; KiteHut never deletes it for you.
