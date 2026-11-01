@echo off
rem Launcher for Windows: pc.cmd <command>  (double click opens a console)
setlocal
set PYTHONUTF8=1
chcp 65001 >nul
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
cd /d "%~dp0"
if "%~1"=="" (
  uv run --quiet pc --help
  cmd /k
) else (
  uv run --quiet pc %*
)
endlocal
