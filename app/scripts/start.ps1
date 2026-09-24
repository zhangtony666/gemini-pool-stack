param([switch]$ApiOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$runArgs = @('run')
if (Test-Path -LiteralPath '.env') { $runArgs += @('--env-file', '.env') }
$runArgs += @('image-verifier', 'serve')
if ($ApiOnly) { $runArgs += '--api-only' }
& uv @runArgs
exit $LASTEXITCODE
