#!/bin/bash
# Dobbeltklik denne fil for at opdatere din WaniKani-viden på jiten.moe
# og se coverage på titlerne i decks.txt.
#
# Første gang på en Mac skal filen gøres kørbar. Åbn Terminal, skriv
# "chmod +x " (med mellemrum til sidst), træk denne fil ind i vinduet
# og tryk retur. Derefter virker dobbeltklik.

cd "$(dirname "$0")" || exit 1

export PYTHONIOENCODING=utf-8

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python blev ikke fundet."
    echo "Installer det med:  brew install python"
    echo "eller hent det på https://www.python.org/downloads/"
    read -r -p "Tryk retur for at lukke..."
    exit 1
fi

fejl() {
    echo
    echo "Noget gik galt - se fejlen ovenfor."
    echo "Mangler der nøgler, skal de ligge i wanikani_token.txt og jiten_key.txt."
    read -r -p "Tryk retur for at lukke..."
    exit 1
}

echo
echo "== 1/4  Henter dine WaniKani-data =="
"$PY" wkjiten.py export --refresh || fejl

echo
echo "== 2/4  Sender ordlisten til jiten.moe =="
"$PY" wkjiten.py push || fejl

echo
echo "== 3/4  Coverage på dine titler =="
"$PY" wkjiten.py batch || fejl

echo
echo "== 4/4  Fremgang og hvad du kan læse nu =="
"$PY" wkjiten.py status || fejl

echo
echo "Færdig. Tallene er nu opdateret på jiten.moe, og hele tabellen"
echo "ligger også i coverage.csv."
echo
read -r -p "Tryk retur for at lukke..."
