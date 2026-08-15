@echo off
rem Double-click this file to push your WaniKani knowledge to jiten.moe
rem and see coverage for the titles listed in decks.txt.

chcp 65001 >nul
cd /d "%~dp0"
title WaniKani -^> jiten.moe

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo Python was not found. Get it from https://www.python.org/downloads/
    echo Remember to tick "Add python.exe to PATH" during installation.
    goto :done
)

echo.
echo == 1/5  Fetching your WaniKani data ==
%PY% wkjiten.py export --refresh
if errorlevel 1 goto :failed

echo.
echo == 2/5  Sending the word list to jiten.moe ==
%PY% wkjiten.py push
if errorlevel 1 goto :failed

echo.
echo == 3/5  Coverage for your titles ==
%PY% wkjiten.py batch
if errorlevel 1 goto :failed

echo.
echo == 4/5  Leeches blocking your reading ==
%PY% wkjiten.py leeches
if errorlevel 1 goto :failed

echo.
echo == 5/5  Dashboard with live search ==
echo.
echo Your browser will open with the dashboard, including a search box
echo for the whole jiten.moe catalogue. Keep THIS WINDOW OPEN while you
echo use it - closing it stops the search working.
echo.
echo Press Ctrl+C here when you are finished.
echo.
%PY% wkjiten.py serve
if errorlevel 1 goto :failed

echo.
echo Done. A static copy of the dashboard is saved as report.html, and
echo the full table is in coverage.csv.
goto :done

:failed
echo.
echo Something went wrong - see the error above.
echo If keys are missing, they belong in wanikani_token.txt and jiten_key.txt.

:done
echo.
pause
