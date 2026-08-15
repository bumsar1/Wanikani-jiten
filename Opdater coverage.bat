@echo off
rem Dobbeltklik denne fil for at opdatere din WaniKani-viden paa jiten.moe
rem og se coverage paa titlerne i decks.txt.

chcp 65001 >nul
cd /d "%~dp0"
title WaniKani -^> jiten.moe

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo Python blev ikke fundet. Hent det paa https://www.python.org/downloads/
    echo Husk at saette flueben i "Add python.exe to PATH" under installationen.
    goto :slut
)

echo.
echo == 1/3  Henter dine WaniKani-data ==
%PY% wkjiten.py export --refresh
if errorlevel 1 goto :fejl

echo.
echo == 2/3  Sender ordlisten til jiten.moe ==
%PY% wkjiten.py push
if errorlevel 1 goto :fejl

echo.
echo == 3/3  Coverage paa dine titler ==
%PY% wkjiten.py batch
if errorlevel 1 goto :fejl

echo.
echo Faerdig. Tallene er nu opdateret paa jiten.moe, og hele tabellen
echo ligger ogsaa i coverage.csv.
goto :slut

:fejl
echo.
echo Noget gik galt - se fejlen ovenfor.
echo Mangler der noegler, skal de ligge i wanikani_token.txt og jiten_key.txt.

:slut
echo.
pause
