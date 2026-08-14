@echo off
REM Daily rates update. Run by Windows Task Scheduler; also safe to double-click.
REM Appends to update.log and keeps the last ~2000 lines.

setlocal
cd /d "%~dp0"

set PY=%LOCALAPPDATA%\Python\bin\python.exe
if not exist "%PY%" set PY=python

echo. >> update.log
"%PY%" cli.py update >> update.log 2>&1
set RC=%ERRORLEVEL%

REM Trim the log so it cannot grow without bound.
powershell -NoProfile -Command ^
  "if ((Get-Content update.log).Count -gt 2000) { Get-Content update.log -Tail 1500 | Set-Content update.log.tmp -Encoding utf8; Move-Item update.log.tmp update.log -Force }" 2>nul

exit /b %RC%
