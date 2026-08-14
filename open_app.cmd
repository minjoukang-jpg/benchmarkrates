@echo off
REM Starts the rates dashboard and opens it in your browser.
REM Close this window to stop the app.

cd /d "%~dp0"

set PY=%LOCALAPPDATA%\Python\bin\python.exe
if not exist "%PY%" set PY=python

title Benchmark Rates - close this window to stop
"%PY%" serve.py --port 8765
pause
