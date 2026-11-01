# Windows installer: uv -> Python 3.12 -> dependencies -> Chromium (optional engine).
# Usage: double click install.cmd, or in PowerShell: .\install.ps1 [-SkipBrowser]
param([switch]$SkipBrowser)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

Write-Host "== Plan de Cuidado: instalación ==" -ForegroundColor Cyan

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando uv (no requiere permisos de administrador)..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "No se pudo instalar uv. Cierre esta ventana, abra una nueva y vuelva a ejecutar el instalador." -ForegroundColor Red
    exit 1
}

Write-Host "Instalando Python 3.12..."
uv python install 3.12
Write-Host "Instalando dependencias..."
uv sync --frozen 2>$null
if ($LASTEXITCODE -ne 0) { uv sync }
if ($LASTEXITCODE -ne 0) { Write-Host "Falló la instalación de dependencias." -ForegroundColor Red; exit 1 }

if (-not $SkipBrowser) {
    Write-Host "Instalando Chromium para el motor 'browser' (opcional)..."
    uv run playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Host "Chromium no se instaló; el motor http sigue funcionando." -ForegroundColor Yellow }
}

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

uv run pc --help | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "La instalación terminó con errores." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Instalación completa." -ForegroundColor Green
Write-Host "Siguiente paso: ponga credentials.json en esta carpeta y ejecute:  .\pc.cmd setup"
