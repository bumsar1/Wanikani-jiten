@echo off
rem Like "Run locally (Windows).bat", but reachable from other machines on your home
rem network - a Mac, a laptop, a phone. Same database, same account.

chcp 65001 >nul
cd /d "%~dp0"
title wkjiten - shared on this network

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo Python was not found. Get it from https://www.python.org/downloads/
    goto :done
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a virtual environment ^(one time^)...
    %PY% -m venv .venv
    if errorlevel 1 goto :failed
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :failed
)

if not exist "data" mkdir data
set WKJITEN_DB=%cd%\data\wkjiten.sqlite3
set WKJITEN_SECRET_FILE=%cd%\data\secret.key
set WKJITEN_PORT=8770
rem The one difference from Run locally (Windows).bat: listen on every interface.
set WKJITEN_HOST=0.0.0.0

echo.
echo Windows may ask whether to allow Python through the firewall.
echo Say yes for PRIVATE networks only - that is what makes other machines
echo in your home able to reach this. Decline the public option.
echo.
echo The address to type on your Mac is printed below. Keep this window open.
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :failed
goto :done

:failed
echo.
echo Something went wrong - see the error above.

:done
echo.
pause
