@echo off
rem Double-click installer for Windows: runs install.ps1 without changing the system policy.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
