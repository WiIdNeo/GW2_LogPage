#!/usr/bin/env python3
"""raidtool.py - Hilfsskript fuer GW2-Raid-Aufnahmen.

Schritt 1 (optional): zufaellige Musik als Tonspur unter das Video legen
    python3 raidtool.py audio "2026-11-27 20-00-05.mkv"
    -> erzeugt "2026-11-27 20-00-05_audio.mkv" (Video wird nicht neu kodiert)

Schritt 2: Kapitel und Links pro Kampf berechnen
    python3 raidtool.py links "2026-11-27 20-00-05_audio.mkv" --url https://youtu.be/VIDEOID \
        [--json-dir EI_JSON_ORDNER] [--db raids.sqlite]
    -> <video>.chapters.txt (fuer die YouTube-Beschreibung)
       <video>.links.json   (fuer die Website)
       dauerhaft: youtube_links.json neben dem Script (ueberlebt DB-Resets, ins Git legen!)
       optional (--db): log.yt_url in der SQLite-DB (Match ueber Dateiname im Pfad)
    python3 raidtool.py apply --db raids.sqlite   # alle Links aus dem Speicher neu in die DB

Kampfstart pro Log:
  1. EI-JSON (timeStart), falls --json-dir angegeben und eine passende JSON gefunden wird
  2. sonst: Endzeit aus dem Log-Dateinamen minus Kampfdauer aus dem evtc

Voraussetzungen: Python 3.8+, ffmpeg und ffprobe im PATH.
Die Aufnahme braucht OBS-Standard-Dateinamen ("%CCYY-%MM-%DD %hh-%mm-%ss"),
denn daraus wird der Startzeitpunkt des Videos gelesen. In OBS nicht pausieren.
"""
import argparse
import json
import random
import re
import sqlite3
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ---------------- Einstellungen (hier anpassen) ----------------
LOG_DIR = Path("~/arcdps.cbtlogs").expanduser()   # Elternordner, Unterordner = Bosse
MUSIC_DIR = Path("~/Music/raid").expanduser()     # Ordner mit Musikdateien
PRE_ROLL = 10             # Sekunden, die der Link vor dem Pull startet
FALLBACK_DURATION = 180   # Kampfdauer, falls sie nirgends lesbar ist
FADE_SECONDS = 5          # Ausblenden am Ende der Tonspur
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac"}
# ----------------------------------------------------------------


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


# ---------------------------- Audio ----------------------------
def build_playlist(files, total):
    """Zufaellige Reihenfolge, bis die Gesamtlaenge reicht (ohne Wiederholung,
    solange der Ordner gross genug ist)."""
    cache, playlist, acc, pool = {}, [], 0.0, []
    while acc < total:
        if not pool:
            pool = files[:]
            random.shuffle(pool)
        f = pool.pop()
        if f not in cache:
            try:
                cache[f] = probe_duration(f)
            except Exception:
                cache[f] = 0.0
        if cache[f] <= 1:
            files = [x for x in files if x != f]
            if not files:
                sys.exit("Keine lesbaren Audiodateien gefunden.")
            continue
        playlist.append(f)
        acc += cache[f]
    return playlist


def cmd_audio(args):
    video = Path(args.video)
    total = probe_duration(video)
    files = [p for p in MUSIC_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXT]
    if not files:
        sys.exit(f"Keine Audiodateien in {MUSIC_DIR}")
    playlist = build_playlist(files, total)
    n = len(playlist)

    cmd = ["ffmpeg", "-y", "-i", str(video)]
    for f in playlist:
        cmd += ["-i", str(f)]
    parts = [f"[{i}:a:0]aresample=48000,"
             f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}]"
             for i in range(1, n + 1)]
    labels = "".join(f"[a{i}]" for i in range(1, n + 1))
    fade_start = max(0.0, total - FADE_SECONDS)
    graph = (";".join(parts) +
             f";{labels}concat=n={n}:v=0:a=1,atrim=duration={total:.3f},"
             f"afade=t=out:st={fade_start:.3f}:d={FADE_SECONDS}[aout]")
    out = video.with_name(video.stem + "_audio.mkv")
    cmd += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    print(f"{n} Titel, Video {total/60:.1f} min -> {out}")
    subprocess.run(cmd, check=True)


# ---------------------------- Links ----------------------------
LOG_NAME_RE = re.compile(r"(\d{8})-(\d{6})")
VIDEO_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})-(\d{2})-(\d{2})")
URL_ID_RE = re.compile(r"(?:youtu\.be/|v=)([\w-]{11})")


def video_start(video, duration):
    m = VIDEO_NAME_RE.search(video.name)
    if m:
        return datetime(*map(int, m.groups()))
    # Fallback: Aenderungszeit der Datei minus Laenge
    return datetime.fromtimestamp(video.stat().st_mtime) - timedelta(seconds=duration)


def log_duration(path):
    """Kampfdauer in Sekunden aus dem evtc (LOG_START bis LOG_END), sonst None."""
    try:
        if path.suffix.lower() == ".zevtc":
            with zipfile.ZipFile(path) as z:
                data = z.read(z.namelist()[0])
        else:
            data = path.read_bytes()
        n_agents = struct.unpack_from("<I", data, 16)[0]
        off = 20 + n_agents * 96
        n_skills = struct.unpack_from("<I", data, off)[0]
        off += 4 + n_skills * 68
        # is_statechange liegt in jedem 64-Byte-Event an Offset 56
        flags = data[off + 56::64]
        i_start, i_end = flags.find(bytes([22])), flags.rfind(bytes([23]))
        if i_start < 0 or i_end < 0:
            return None
        t_start = struct.unpack_from("<Q", data, off + i_start * 64)[0]
        t_end = struct.unpack_from("<Q", data, off + i_end * 64)[0]
        dur = (t_end - t_start) / 1000.0
        return dur if 1 <= dur <= 3600 else None
    except Exception:
        return None


def ei_json_start(stem, json_dir):
    """Startzeit (lokal, ohne Zeitzone) und Dauer (s) aus einer EI-JSON, sonst (None, None)."""
    if not json_dir:
        return None, None
    for jp in Path(json_dir).rglob(stem + "*.json"):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
            start = datetime.strptime(d["timeStart"][:19], "%Y-%m-%d %H:%M:%S")
            ms = d.get("durationMS")
            return start, (ms / 1000.0 if ms else None)
        except Exception:
            continue
    return None, None


def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


STORE_PATH = Path(__file__).resolve().with_name("youtube_links.json")


def load_store(path):
    """Dauerhafter Link-Speicher (JSON, lebt ausserhalb der DB und ueberlebt Resets).
    Schluessel = Dateiname des Logs, Wert = Video-ID und Sekunden-Offset."""
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_store(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)                       # atomar: nie eine halb geschriebene Datei


def store_link(entry):
    return f"https://youtu.be/{entry['video_id']}?t={entry['t']}"


def apply_db(db_path, store):
    """Schreibt alle Links aus dem Speicher in log.yt_url (Match: Pfad endet auf den Dateinamen)."""
    con = sqlite3.connect(db_path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(log)")]
    if "yt_url" not in cols or "path" not in cols:
        con.close()
        sys.exit("Tabelle log mit den Spalten path/yt_url nicht gefunden.")
    changed = matched = 0
    for fname, entry in store.items():
        link = store_link(entry)
        row = con.execute("SELECT COUNT(*) FROM log WHERE substr(path, -length(?)) = ?",
                          (fname, fname)).fetchone()
        matched += 1 if row[0] else 0
        changed += con.execute(
            "UPDATE log SET yt_url=? WHERE substr(path, -length(?)) = ? AND IFNULL(yt_url,'') != ?",
            (link, fname, fname, link)).rowcount
    con.commit()
    con.close()
    print(f"DB: {changed} Links gesetzt/aktualisiert; {matched} von {len(store)} Speicher-Eintraegen "
          "haben ein Log in der DB (der Rest ist nicht schlimm: Log noch nicht importiert oder entfernt).")


def cmd_apply(args):
    apply_db(args.db, load_store(args.store))


def cmd_links(args):
    video = Path(args.video)
    total = probe_duration(video)
    vstart = video_start(video, total)
    vend = vstart + timedelta(seconds=total)
    log_dir = Path(args.log_dir).expanduser() if args.log_dir else LOG_DIR

    m = URL_ID_RE.search(args.url) if args.url else None
    vid = m.group(1) if m else (args.url or None)

    fights = []
    for p in log_dir.rglob("*"):
        if p.suffix.lower() not in (".zevtc", ".evtc"):
            continue
        m = LOG_NAME_RE.search(p.stem)
        if not m:
            continue
        end = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        if not (vstart - timedelta(seconds=30) <= end <= vend + timedelta(seconds=60)):
            continue
        dur = log_duration(p)
        ei_start, ei_dur = ei_json_start(p.stem, args.json_dir)
        if ei_start:
            start, src = ei_start, "ei_json"
        else:
            dur_used = dur if dur is not None else FALLBACK_DURATION
            start, src = end - timedelta(seconds=dur_used), ("log" if dur is not None else "fallback")
        fights.append({"file": p.name, "boss": p.parent.name, "start": start + timedelta(seconds=args.shift),
                       "end": end, "source": src})
    fights.sort(key=lambda f: f["start"])
    if not fights:
        sys.exit(f"Keine Logs im Zeitraum {vstart} - {vend} unter {log_dir} gefunden.")

    totals = Counter(f["boss"] for f in fights)
    seen = Counter()
    chapters, links = ["0:00 Start"], []
    for f in fights:
        offset = max(0, (f["start"] - vstart).total_seconds() - PRE_ROLL)
        seen[f["boss"]] += 1
        label = f["boss"] if totals[f["boss"]] == 1 else f"{f['boss']} (Versuch {seen[f['boss']]})"
        chapters.append(f"{fmt_time(offset)} {label}")
        links.append({
            "file": f["file"], "boss": f["boss"], "attempt": seen[f["boss"]],
            "start": f["start"].isoformat(), "start_source": f["source"],
            "offset_s": int(offset), "video_id": vid,
            "link": f"https://youtu.be/{vid}?t={int(offset)}" if vid else None,
        })

    base = video.with_suffix("")
    Path(f"{base}.chapters.txt").write_text("\n".join(chapters) + "\n", encoding="utf-8")
    Path(f"{base}.links.json").write_text(
        json.dumps(links, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n".join(chapters))
    print(f"\nGeschrieben: {base}.chapters.txt, {base}.links.json")
    if any(l["start_source"] == "fallback" for l in links):
        print("Hinweis: Bei manchen Logs war die Dauer nicht lesbar (Fallback genutzt).")
    if vid:
        store = load_store(args.store)
        for l in links:
            store[l["file"]] = {"video_id": vid, "t": l["offset_s"], "boss": l["boss"],
                                "start": l["start"]}
        save_store(args.store, store)
        print(f"Link-Speicher: {args.store} ({len(store)} Eintraege)")
    else:
        store = load_store(args.store)
    if args.db:
        apply_db(args.db, store)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audio", help="Zufaellige Musik als Tonspur hinzufuegen")
    a.add_argument("video")
    a.set_defaults(func=cmd_audio)
    l = sub.add_parser("links", help="Kapitel und Links berechnen")
    l.add_argument("video")
    l.add_argument("--url", help="YouTube-Link oder Video-ID")
    l.add_argument("--shift", type=float, default=0.0,
                   help="Globale Korrektur in Sekunden (+ = Links spaeter), zum Kalibrieren")
    l.add_argument("--log-dir", help="Log-Ordner (ueberschreibt LOG_DIR im Script)")
    l.add_argument("--json-dir", help="Ordner mit EI-JSONs (rekursiv); nutzt dort timeStart")
    l.add_argument("--db", help="SQLite-Datei; schreibt log.yt_url (und Hilfstabelle fight_links)")
    l.add_argument("--store", default=str(STORE_PATH), help="Dauerhafter Link-Speicher (JSON)")
    l.set_defaults(func=cmd_links)
    p = sub.add_parser("apply", help="Alle gespeicherten Links in die DB schreiben (z. B. nach DB-Reset)")
    p.add_argument("--db", required=True)
    p.add_argument("--store", default=str(STORE_PATH))
    p.set_defaults(func=cmd_apply)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
