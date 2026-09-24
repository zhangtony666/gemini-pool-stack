$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --frozen ruff check src tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --frozen ruff format --check src tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --frozen pytest -q --tb=short
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --frozen python scripts/smoke.py
exit $LASTEXITCODE
