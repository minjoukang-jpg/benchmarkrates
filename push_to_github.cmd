@echo off
REM Uploads this folder to a GitHub repository you have already created.
REM Double-click this file, paste the repository URL when asked, and it does
REM the rest. Safe to run more than once.

cd /d "%~dp0"

echo ============================================================
echo   Push Benchmark Rates to GitHub
echo ============================================================
echo.
echo Before running this you should have created an EMPTY private
echo repository on github.com  (no README, no .gitignore, no licence).
echo.

if not exist config.json goto :keycheck_done

REM config.json holds the PDEx API key. Refuse to upload if it is not ignored.
git check-ignore -q config.json
if errorlevel 1 goto :key_exposed
echo Checked: config.json is excluded, your API key will not be uploaded.
goto :keycheck_done

:key_exposed
echo.
echo ERROR: config.json is NOT ignored by git, and it holds your API key.
echo Stopping so the key is not published. Tell Claude about this.
echo.
pause
exit /b 1

:keycheck_done
echo.

REM If a repository URL is already configured, offer to just use it.
for /f "delims=" %%U in ('git remote get-url origin 2^>nul') do set "EXISTING=%%U"
if not defined EXISTING goto :askurl

echo A repository is already configured:
echo    %EXISTING%
echo.
set "USEIT="
set /p USEIT=Press Enter to use it, or type N to enter a different one:
if /i "%USEIT%"=="N" goto :askurl
set "REPOURL=%EXISTING%"
goto :confirm

:askurl
set "REPOURL="
set /p REPOURL=Paste your repository URL and press Enter:
if not defined REPOURL goto :nourl

:confirm
echo.
echo About to upload this folder to:
echo    %REPOURL%
echo.
set "CONFIRM="
set /p CONFIRM=Type YES to continue:
if /i not "%CONFIRM%"=="YES" goto :cancelled

echo.
git remote remove origin >nul 2>&1
git remote add origin "%REPOURL%"
if errorlevel 1 goto :badurl

echo Uploading. A browser window may open so you can sign in to GitHub.
echo.
git push -u origin main
if errorlevel 1 goto :pushfailed

echo.
echo ============================================================
echo   Done. Your code is on GitHub.
echo ============================================================
echo.
echo Next steps:
echo   1. In the repo: Settings ^> Secrets and variables ^> Actions
echo      Add a secret named  PDEX_API_KEY
echo   2. Go to https://share.streamlit.io and create the app
echo      Main file path:  streamlit_app.py
echo.
pause
exit /b 0

:nourl
echo No URL entered. Nothing was uploaded.
pause
exit /b 1

:cancelled
echo Cancelled. Nothing was uploaded.
pause
exit /b 1

:badurl
echo Could not set the repository URL. Check it and try again.
pause
exit /b 1

:pushfailed
echo.
echo ============================================================
echo   Upload did not finish.
echo ============================================================
echo Common causes:
echo   - The repository was created WITH a README. Make a new empty
echo     one, or ask Claude to reconcile the histories.
echo   - Sign-in was cancelled or failed. Just run this again.
echo   - The URL is wrong. It should end in .git
echo.
pause
exit /b 1
