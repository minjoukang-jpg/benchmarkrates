@echo off
REM Starts the Streamlit dashboard and opens it in your browser.
REM This is the same app that would run on Streamlit Cloud, just running locally.
REM Close this window to stop it.

cd /d "%~dp0"

set PY=%LOCALAPPDATA%\Python\bin\python.exe
if not exist "%PY%" set PY=python

"%PY%" -c "import streamlit" 2>nul
if errorlevel 1 (
  echo Streamlit is not installed. Installing it now, this takes a minute...
  "%PY%" -m pip install --user -r requirements.txt
)

title Benchmark Rates - Streamlit - close this window to stop
"%PY%" -m streamlit run streamlit_app.py --server.port 8501
pause
