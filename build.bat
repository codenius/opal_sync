@echo off
setlocal
cd /d "%~dp0" || exit /b 1

uv sync --locked --extra build
if errorlevel 1 exit /b %errorlevel%

powershell -NoProfile -ExecutionPolicy Bypass -Command "$uv = (Get-Command uv).Source; $env:PATH = (($env:PATH -split ';' | Where-Object { $_ -notmatch 'Java_Temurin|Windows Performance Toolkit|[\\/]Java[\\/]' }) -join ';'); & $uv run --locked --extra build python -m PyInstaller --clean --noconfirm opal_sync.spec; exit $LASTEXITCODE"
exit /b %errorlevel%
