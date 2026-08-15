@echo off
rem Double-click to try the hosted version on this machine before putting it
rem on a server. Sets up its own virtual environment the first time.

chcp 65001 >nul
cd /d "%~dp0"
title wkjiten - local server

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo Python was not found. Get it from https://www.python.org/downloads/
    echo Remember to tick "Add python.exe to PATH" during installation.
    goto :done
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a virtual environment ^(one time^)...
    %PY% -m venv .venv
    if errorlevel 1 goto :failed
    echo Installing Flask and cryptography...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :failed
)

rem Everything lands in webapp\data so it never mixes with the local tool's
rem cache, and it is git-ignored.
if not exist "data" mkdir data
set WKJITEN_DB=%cd%\data\wkjiten.sqlite3
set WKJITEN_SECRET_FILE=%cd%\data\secret.key
set WKJITEN_PORT=8770
set WKJITEN_HOST=127.0.0.1

echo.
echo Starting on http://127.0.0.1:8770
echo.
echo First run prints an invitation link below - open it to create the first
echo account, which becomes the admin. Keep this window open while you use it,
echo and press Ctrl+C when you are done.
echo.
start "" "http://127.0.0.1:8770/"
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :failed
goto :done

:failed
echo.
echo Something went wrong - see the error above.

:done
echo.
pause
