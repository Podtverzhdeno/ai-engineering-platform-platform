# Demo live: opencode serve + uvicorn с AIPLATFORM_OPENCODE_LIVE + aip demo
# Использование: .\scripts\demo_live.ps1 [-KillExisting]
param(
    [switch]$KillExisting
)
$ErrorActionPreference = "Stop"
cd $PSScriptRoot\..

$ocExe = "$env:APPDATA\npm\node_modules\opencode-ai\bin\opencode.exe"
if (-not (Test-Path $ocExe)) { throw "opencode.exe не найден: $ocExe" }

# 1) opencode serve на 4096 (если уже слушает — используем существующий)
$serving = $false
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:4096/provider/auth" -UseBasicParsing -TimeoutSec 2
    $serving = $true
    Write-Host "opencode serve уже запущен — используем его"
} catch {}
if (-not $serving) {
    $env:OPENCODE_SERVER_PASSWORD = [System.Guid]::NewGuid().ToString("N")
    $env:OPENCODE_SERVER_USERNAME = "opencode"
    $proc = Start-Process -FilePath $ocExe -ArgumentList "serve","--port","4096" -WindowStyle Hidden -PassThru
    Write-Host "opencode serve запущен (pid $($proc.Id))"
    Start-Sleep -Seconds 4
}

# 2) Control Plane с live-режимом
$env:AIPLATFORM_OPENCODE_LIVE = "1"
$env:AIPLATFORM_OPENCODE_URL = "http://127.0.0.1:4096"
$env:AIPLATFORM_DB_URL = "sqlite:///$PWD/demo-live.db"
$env:AIPLATFORM_PORT = "7777"

$server = Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" -ArgumentList "-m","uvicorn","app.main:app","--port","7777" -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 3

# 3) демо-поток целиком — контролирует real-session ветку agent_gateway
& "$PWD\.venv\Scripts\python.exe" -m app.cli demo

Write-Host "`nСервер сервер на http://127.0.0.1:7777 (pid $($server.Id)); UI: GET / ; README: раздел Live mode"
Write-Host "Остановить: Stop-Process -Id $($server.Id); удалите demo-live.db при необходимости"
