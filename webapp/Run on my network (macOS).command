#!/bin/bash
# Like "Run locally (macOS).command", but reachable from other machines on your
# home network - a Windows PC, a laptop, a phone. Same database, same account.
#
# The one difference from running locally: it listens on every interface.

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
export WKJITEN_HOST=0.0.0.0
export PYTHONIOENCODING=utf-8

# The address the others have to type. Ask for the interface the default route
# uses, so it stays right on both Wi-Fi and ethernet.
IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
LAN=$(ipconfig getifaddr "$IFACE" 2>/dev/null)
[ -z "$LAN" ] && LAN=$(ipconfig getifaddr en0 2>/dev/null)
[ -z "$LAN" ] && LAN=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$LAN" ] && LAN="this-machine"

echo
echo "macOS may ask whether to allow Python to accept incoming connections."
echo "Say yes - that is what makes other machines in your home able to reach"
echo "this. Keep this window open."
echo
echo "The address to type on the other machine:"
echo
echo "    http://$LAN:8770/"
echo
echo "This is the development server, on your home network in clear text."
echo "Fine among your own machines, not something to forward a port to -"
echo "see README.md for how to let someone outside in."
echo

./.venv/bin/python app.py
