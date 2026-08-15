#!/bin/bash
# Double-click this file to push your WaniKani knowledge to jiten.moe
# and see coverage for the titles listed in decks.txt.
#
# If macOS refuses to open it, the folder came from a downloaded zip and is
# quarantined: open Terminal, type "xattr -dr com.apple.quarantine " (with a
# trailing space), drag the project folder into the window and press return.
# Cloning with git instead of downloading the zip avoids this entirely.

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

# Turns the shipped templates into real key files on a fresh download.
"$PY" wkjiten.py setup

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
echo "== 5/5  Dashboard with live search =="
echo
echo "Your browser will open with the dashboard, including a search box"
echo "for the whole jiten.moe catalogue. Keep THIS WINDOW OPEN while you"
echo "use it - closing it stops the search working."
echo
echo "Press Ctrl+C here when you are finished."
echo
"$PY" wkjiten.py serve || failed

echo
echo "Done. A static copy of the dashboard is saved as report.html, and"
echo "the full table is in coverage.csv."
echo
read -r -p "Press return to close..."
