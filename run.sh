#!/usr/bin/env bash
# Komplettlauf:
#   ./run_all.sh "/pfad/zum/2026-11-27 20-00-05.mkv" https://youtu.be/VIDEOID   # nach dem Raid
#   ./run_all.sh                                                                 # ohne neues Video (z. B. nach DB-Reset)
# Ablage: im Projekt-Hauptordner (neben Backend/ und Frontend/); raidtool.py liegt in Backend/.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ $# -ne 0 ] && [ $# -ne 2 ]; then
  echo "Aufruf: $0 [<Videodatei> <YouTube-Link>]" >&2
  exit 1
fi
if [ $# -eq 2 ]; then
  VIDEO="$(readlink -f "$1")"
  URL="$2"
  [ -f "$VIDEO" ] || { echo "Videodatei nicht gefunden: $VIDEO" >&2; exit 1; }
fi

echo "== 1/4 Logs verarbeiten (EI, dps.report, Wingman)"
cd Backend
python3 gw2logs.py --jobs 8

echo "== 2/4 YouTube-Links"
# DB- und Log-Pfad aus der config.toml lesen, damit nichts doppelt gepflegt wird
DB="$(python3 -c "import tomllib,os;print(os.path.expanduser(tomllib.load(open('config.toml','rb'))['db_path']))")"
LOGDIR="$(python3 -c "import tomllib,os;print(os.path.expanduser(tomllib.load(open('config.toml','rb'))['log_dir']))")"
if [ $# -eq 2 ]; then
  # neues Video: Links berechnen, in youtube_links.json speichern UND in die DB schreiben
  python3 videotool.py links "$VIDEO" --url "$URL" --db "$DB" --log-dir "$LOGDIR"
else
  # kein neues Video: alle gespeicherten Links nur neu in die DB schreiben
  python3 videotool.py apply --db "$DB"
fi

echo "== 3/4 Website bauen"
cd ../Frontend
python3 build_site.py

echo "== 4/4 Git"
cd ..
git add .
if git diff --cached --quiet; then
  echo "Keine Aenderungen - nichts zu committen."
else
  git commit -m "$(date +%F)"
  git push
fi

if [ -n "${VIDEO:-}" ]; then
  echo "Fertig. Kapitel fuer die YouTube-Beschreibung: ${VIDEO%.*}.chapters.txt"
else
  echo "Fertig."
fi
