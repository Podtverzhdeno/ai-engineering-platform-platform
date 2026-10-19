$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot\..

$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    python -m venv .venv | Out-Null
}
& $python -m pip install -q fastapi uvicorn sqlalchemy pydantic httpx pytest

Write-Host "Starting Control Plane on http://127.0.0.1:7700 (docs at /docs)"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 7700
