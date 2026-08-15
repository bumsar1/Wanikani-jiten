#!/bin/bash
# Double-click this file to push your WaniKani knowledge to jiten.moe
# and see coverage for the titles listed in decks.txt.
#
# The first time on a Mac the file has to be made executable: open Terminal,
# type "chmod +x " (with a trailing space), drag this file into the window
# and press return. After that, double-clicking works.

cd "$(dirname "$0")" || exit 1

export PYTHONIOENCODING=utf-8

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python was not found."
    echo "Install it with:  brew install python"
    echo "or get it from https://www.python.org/downloads/"
    read -r -p "Press return to close..."
    exit 1
fi

failed() {
    echo
    echo "Something went wrong - see the error above."
    echo "If keys are missing, they belong in wanikani_token.txt and jiten_key.txt."
    read -r -p "Press return to close..."
    exit 1
}

echo
echo "== 1/5  Fetching your WaniKani data =="
"$PY" wkjiten.py export --refresh || failed

echo
echo "== 2/5  Sending the word list to jiten.moe =="
"$PY" wkjiten.py push || failed

echo
echo "== 3/5  Coverage for your titles =="
"$PY" wkjiten.py batch || failed

echo
echo "== 4/5  Leeches blocking your reading =="
"$PY" wkjiten.py leeches || failed

echo
echo "== 5/5  Building the dashboard =="
"$PY" wkjiten.py report || failed

open "https://jiten.moe/decks/media"

echo
echo "Done. The dashboard and jiten.moe should both have opened in your"
echo "browser; the dashboard is also saved as report.html, and the full"
echo "table is in coverage.csv."
echo
read -r -p "Press return to close..."
