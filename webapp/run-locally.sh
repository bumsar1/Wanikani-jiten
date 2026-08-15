#!/bin/bash
# Try the hosted version on this machine before putting it on a server.
# Creates its own virtual environment the first time.
#
# On Bazzite this works too, and needs nothing installed on the host beyond
# python3 - but for anything permanent use the container instead, see README.md.

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
    echo "Python was not found. Install it with:  brew install python"
    read -r -p "Press return to close..."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating a virtual environment (one time)..."
    "$PY" -m venv .venv || exit 1
    echo "Installing Flask and cryptography..."
    ./.venv/bin/python -m pip install --quiet --upgrade pip
    ./.venv/bin/python -m pip install --quiet -r requirements.txt || exit 1
fi

mkdir -p data
export WKJITEN_DB="$PWD/data/wkjiten.sqlite3"
export WKJITEN_SECRET_FILE="$PWD/data/secret.key"
export WKJITEN_PORT=8770
export WKJITEN_HOST=127.0.0.1
export PYTHONIOENCODING=utf-8

echo
echo "Starting on http://127.0.0.1:8770"
echo
echo "First run prints an invitation link below - open it to create the first"
echo "account, which becomes the admin. Ctrl+C when you are done."
echo

(command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:8770/" ||
 command -v open >/dev/null && open "http://127.0.0.1:8770/") 2>/dev/null &

./.venv/bin/python app.py
