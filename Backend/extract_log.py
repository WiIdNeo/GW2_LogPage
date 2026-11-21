#!/usr/bin/env python3
"""extract_log.py - verkleinert eine Elite-Insights-JSON auf die Daten, die fuer die Analyse gebraucht werden.

Wird von gw2logs.py importiert, laeuft aber auch allein:
    python extract_log.py log.json                       -> extracted/log.slim.json
    python extract_log.py json_dump/ -o out/             -> alle *.json(.gz) im Ordner (rekursiv)
    python extract_log.py log.json --indent 1 --report   -> lesbar + Groessen pro Abschnitt
    python extract_log.py log.json --whitelist-from config.toml --filter analysis_filter.toml
    python extract_log.py json_dump/ --catalog           -> analysis_filter.toml anlegen bzw. NUR Neues ergaenzen

Konventionen im Ausgabeformat:
  * Alles, was die EI-JSON pro Phase liefert, bleibt eine Liste mit Phasen-Index (wie "phases").
  * Ziele ("targets") werden ueber ihren ORIGINAL-Index aus der EI-JSON adressiert (String-Key "0", "1", ...).
  * Buffs/Zustaende/Consumables sind ueber ihren KLARNAMEN adressiert (aus der buffMap der JSON); IDs und
    weitere Attribute stehen in "buffs_info". Bei gleichem Namen mit verschiedenen IDs: "Name (ID)".
  * Wer einen Buff erzeugt hat ("generated", "incomingBy") wird bewusst NICHT exportiert - dafuer ins Log schauen.
  * Mit Whitelist (Accounts) bleiben nur diese Spieler uebrig; Mechanik-Ereignisse anderer Spieler entfallen.
  * Pro Boss kann eine Filterdatei (TOML) Ziele (NPC-ID) und Mechaniken (Name) auf eine Whitelist begrenzen.
    Schluessel: Boss-ID, bei Challenge Mode negativ (wie in bosses.toml), z. B. [boss."-17194"].
"""
import argparse
import datetime
import gzip
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

# ----------------------------------------------------------------------------- Konfiguration
SCHEMA_VERSION = 2
META_FIELDS = ("eliteInsightsVersion", "gW2Build", "triggerID", "isCM", "success",
               "durationMS", "duration", "timeStartStd", "timeEndStd")
PLAYER_FIELDS = ("account", "name", "group", "profession", "hasCommanderTag", "friendlyNPC", "notInSquad")
TARGET_FIELDS = ("id", "name", "finalHealth", "finalBarrier", "barrierPercent", "healthPercentBurned")
DPS_FIELDS = ("dps", "condiDps", "powerDps", "breakbarDamage")
STATS_FIELDS = ("flankingRate", "connectedDirectDamageCount", "killed", "downed")
SUPPORT_FIELDS = ("resurrects", "resurrectTime")
DROP_VOLUME_KEYS = {"incomingBy", "incomingByExtensionBy"}   # "wer hat es erzeugt" -> nicht exportieren
BUFF_INFO_DROP = {"icon", "descriptions"}      # aus buffMap-Eintraegen entfernt (URLs / lange Texte)
TOTAL_PRIORITIES = {"MAIN"}                    # welche Ziele in "dps_targets_total" einfliessen


# ----------------------------------------------------------------------------- Helfer
def load_json(path):
    """Liest .json und .json.gz (erkennt gzip am Dateikopf, nicht an der Endung)."""
    with open(path, "rb") as f:
        magic = f.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rb") as f:
        return json.loads(f.read().decode("utf-8-sig"))


def pick(d, keys):
    return {k: d[k] for k in keys if k in d}


def orient_of(arr, n_targets, n_phases):
    """EI liefert [target][phase]. Falls die Laengen eher auf [phase][target] hindeuten, wird das erkannt."""
    if not isinstance(arr, list) or len(arr) == n_targets:
        return "tp"
    return "pt" if len(arr) == n_phases else "tp"


def at(arr, t, p, orient):
    try:
        return arr[t][p] if orient == "tp" else arr[p][t]
    except (IndexError, TypeError):
        return None


def any_nonzero(entries):
    return any(any(v for v in e.values()) for e in entries)


def relevant_targets(phases, n_targets):
    """Ziele, die in irgendeiner Phase als (secondary) Target vorkommen. Ohne Phasen: alle."""
    rel = set()
    for ph in phases:
        for key in ("targets", "secondaryTargets"):
            rel.update(i for i in ph.get(key, []) if isinstance(i, int) and 0 <= i < n_targets)
    return sorted(rel) if rel else list(range(n_targets))


def main_targets_of_phase(ph, n_targets):
    idx = [i for i in ph.get("targets", []) if isinstance(i, int) and 0 <= i < n_targets]
    pri = ph.get("targetPriorities") or {}
    main = [i for i in idx if str(pri.get(str(i), "MAIN")).upper() in TOTAL_PRIORITIES]
    return main or idx


def sum_dicts(dicts):
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return out


def boss_key_of(root):
    """Boss-Schluessel wie in bosses.toml/DB: Boss-ID, bei CM negativ. Funktioniert auch fuer slim-JSONs."""
    meta = root.get("meta") or {}
    tid = root.get("triggerID", meta.get("triggerID"))
    cm = root.get("isCM", meta.get("isCM"))
    return None if tid is None else str(-abs(tid) if cm else abs(tid))


def mech_label(m):
    return m.get("name") or m.get("fullName") or m.get("description") or "?"


# ----------------------------------------------------------------------------- Filterdatei
def load_filters(path):
    """analysis_filter.toml -> {boss_key: {"targets": set[int]|None, "mechanics": set[str]|None}}"""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("boss", {})
    out = {}
    for key, v in data.items():
        out[str(key)] = {"targets": set(v["targets"]) if "targets" in v else None,
                         "mechanics": set(v["mechanics"]) if "mechanics" in v else None}
    return out


def pick_filter(filters, keys):
    for k in keys:
        if k is not None and filters and str(k) in filters:
            return filters[str(k)]
    return {}


# ----------------------------------------------------------------------------- Extraktion
def extract(root, graph="full", keep_zero=False, source_name=None, whitelist=None, filters=None, filter_keys=()):
    """whitelist: Menge casefold()-Accountnamen oder None (= alle Spieler).
    filters: Ergebnis von load_filters(); filter_keys: weitere Boss-Schluessel (z. B. aus der DB), die vor dem
    aus der JSON abgeleiteten Schluessel probiert werden."""
    warnings = []
    phases = root.get("phases") or []
    n_ph = len(phases)
    raw_targets = root.get("targets") or []
    n_t = len(raw_targets)
    raw_players = root.get("players") or []
    if not raw_targets:
        warnings.append("Keine 'targets' in der JSON")

    ei_key = boss_key_of(root)
    keys = [k for k in (*filter_keys, ei_key) if k]
    flt = pick_filter(filters, keys)
    boss_key = keys[0] if keys else None

    # ---- relevante Ziele (+ optional Whitelist nach NPC-ID)
    rel = relevant_targets(phases, n_t)
    if flt.get("targets"):
        kept = [t for t in range(n_t) if raw_targets[t].get("id") in flt["targets"]]   # Whitelist gilt fuer alle Ziele
        if kept:
            rel = kept
        else:
            warnings.append(f"Ziel-Whitelist {sorted(flt['targets'])} trifft kein Ziel - nicht angewendet")
    rel_set = set(rel)
    used_buffs = set()

    def keep(entries):
        return keep_zero or any_nonzero(entries)

    # ---- Ziele
    targets = []
    for t in rel:
        src = raw_targets[t]
        out = {"index": t, **pick(src, TARGET_FIELDS)}

        buffs = {}
        for b in src.get("buffs") or []:
            per_phase = [{"uptime": e.get("uptime", 0), "presence": e.get("presence", 0)}
                         for e in b.get("buffData") or []]
            if keep(per_phase):
                buffs[str(b["id"])] = per_phase
        out["buffs"] = buffs

        volumes = {}
        for b in src.get("buffVolumes") or []:
            per_phase = [{k: v for k, v in e.items() if k not in DROP_VOLUME_KEYS}
                         for e in b.get("buffVolumeData") or []]
            if keep(per_phase):
                volumes[str(b["id"])] = per_phase
        out["buffVolumes"] = volumes

        used_buffs.update(buffs)
        used_buffs.update(volumes)
        targets.append(out)

    # ---- Spieler
    wl = None if whitelist is None else {a.casefold() for a in whitelist}
    all_chars = {pl["name"] for pl in raw_players if pl.get("name")}
    players = []
    characters = {}
    main_per_phase = [[i for i in main_targets_of_phase(ph, n_t) if i in rel_set] for ph in phases]
    for pl in raw_players:
        if wl is not None and (pl.get("account") or "").casefold() not in wl:
            continue
        out = pick(pl, PLAYER_FIELDS)
        if pl.get("name") and pl.get("account"):
            characters[pl["name"]] = pl["account"]

        out["dps_all"] = [e.get("dps") for e in (pl.get("dpsAll") or [])]

        dps_t = pl.get("dpsTargets") or []
        stats_t = pl.get("statsTargets") or []
        g1s = pl.get("targetDamage1S") or []
        o_dps, o_stats, o_g = (orient_of(a, n_t, n_ph) for a in (dps_t, stats_t, g1s))

        per_target = {}
        for t in rel:
            entry = {"dps": [pick(at(dps_t, t, p, o_dps) or {}, DPS_FIELDS) for p in range(n_ph)],
                     "stats": [pick(at(stats_t, t, p, o_stats) or {}, STATS_FIELDS) for p in range(n_ph)]}
            if graph != "none" and g1s:
                phase_ids = range(n_ph) if graph == "all" else range(min(1, n_ph))
                entry["damage1S"] = {str(p): at(g1s, t, p, o_g) for p in phase_ids}
            per_target[str(t)] = entry
        out["targets"] = per_target

        # Gesamtschaden auf die Hauptziele der jeweiligen Phase (fuer Multi-Target-Kaempfe)
        out["dps_targets_total"] = [
            sum_dicts(pick(at(dps_t, t, p, o_dps) or {}, DPS_FIELDS) for t in main_per_phase[p])
            for p in range(n_ph)]

        out["support"] = [pick(e, SUPPORT_FIELDS) for e in (pl.get("support") or [])]

        uptimes = {}
        for b in pl.get("buffUptimes") or []:
            per_phase = [{"uptime": e.get("uptime", 0), "presence": e.get("presence", 0)}
                         for e in b.get("buffData") or []]
            if keep(per_phase):
                uptimes[str(b["id"])] = per_phase
        out["buffUptimes"] = uptimes
        used_buffs.update(uptimes)

        out["consumables"] = pl.get("consumables") or []
        used_buffs.update(str(c["id"]) for c in out["consumables"] if "id" in c)
        players.append(out)

    if graph != "none" and raw_players and not any(pl.get("targetDamage1S") for pl in raw_players):
        warnings.append("targetDamage1S fehlt in der JSON (EI-Einstellung RawTimelineArrays=true?)")
    if wl is not None and raw_players and not players:
        warnings.append("Kein Spieler der Whitelist im Log")

    # ---- Mechaniken (Whitelist nach Name; Ereignisse nicht-whitelisted Spieler entfallen)
    blocked = (all_chars - set(characters)) if wl is not None else set()
    mech_wl = flt.get("mechanics")
    seen_labels, mechanics = set(), []
    for m in root.get("mechanics") or []:
        label = mech_label(m)
        seen_labels.update({label, m.get("fullName")})
        if mech_wl and label not in mech_wl and m.get("fullName") not in mech_wl:
            continue
        data = m.get("mechanicsData")
        if blocked and isinstance(data, list):
            data = [e for e in data if e.get("actor") not in blocked]
            if not data:
                continue
            m = {**m, "mechanicsData": data}
        mechanics.append(m)
    if mech_wl:
        missing = sorted(x for x in mech_wl if x not in seen_labels)
        if missing:
            warnings.append(f"Mechaniken der Whitelist nicht im Log (Tippfehler?): {', '.join(missing[:8])}")

    # ---- Klarnamen statt IDs
    bmap = root.get("buffMap") or {}
    if not bmap:
        warnings.append("Keine 'buffMap' in der JSON - Buff-Namen fehlen")
    raw_info = {}
    for key, val in bmap.items():
        bid = key[1:] if key[:1] == "b" and key[1:].lstrip("-").isdigit() else key
        if bid in used_buffs:
            raw_info[bid] = val
    unknown = sorted(used_buffs - set(raw_info), key=lambda x: int(x) if x.lstrip("-").isdigit() else 0)

    base = {bid: (raw_info[bid].get("name") or f"unbekannt_{bid}") if bid in raw_info else f"unbekannt_{bid}"
            for bid in used_buffs}
    counts = {}
    for n in base.values():
        counts[n] = counts.get(n, 0) + 1
    # gleicher Name bei verschiedenen IDs: ID zur Unterscheidung anhaengen
    display = {bid: (n if counts[n] == 1 else f"{n} ({bid})") for bid, n in base.items()}
    collisions = sorted(n for n, c in counts.items() if c > 1)
    if collisions:
        warnings.append(f"Mehrdeutige Buff-Namen (ID angehaengt): {', '.join(collisions[:10])}")

    def rename(d):
        return {display.get(k, k): v for k, v in d.items()}

    for t in targets:
        t["buffs"] = rename(t["buffs"])
        t["buffVolumes"] = rename(t["buffVolumes"])
    for pl in players:
        pl["buffUptimes"] = rename(pl["buffUptimes"])
        pl["consumables"] = [{"name": display.get(str(c.get("id")), f"unbekannt_{c.get('id')}"),
                              **{k: v for k, v in c.items() if k != "id"}} for c in pl["consumables"]]

    buffs_info = {display[bid]: {"id": bid, **{k: v for k, v in info.items() if k not in BUFF_INFO_DROP | {"name"}}}
                  for bid, info in raw_info.items()}

    links = root.get("uploadLinks") or []
    slug = links[0].rstrip("/").rsplit("/", 1)[-1] if links else None

    result = {
        "schema": SCHEMA_VERSION,
        "source_file": source_name,
        "boss_key": boss_key,
        "filters_applied": {"whitelist": wl is not None, "targets": bool(flt.get("targets")),
                            "mechanics": bool(mech_wl)},
        "meta": pick(root, META_FIELDS),
        "uploadLinks": links,
        "dps_slug": slug,                 # == letzter Teil von log.dps_url in der DB
        "phases": phases,
        "main_targets_per_phase": main_per_phase,
        "targets": targets,
        "characters": characters,
        "players": players,
        "mechanics": mechanics,
        "buffs_info": buffs_info,
    }
    stats = {"phases": n_ph, "targets_total": n_t, "targets_kept": len(rel), "players": len(players),
             "buffs_used": len(used_buffs), "buffs_without_name": unknown,
             "mechanics": len(mechanics), "warnings": warnings}
    return result, stats


# ----------------------------------------------------------------------------- Berichte
def section_sizes(result):
    """Zeichenzahl (kompaktes JSON) pro Abschnitt - zeigt, wo die Masse steckt."""
    def size(x):
        return len(json.dumps(x, ensure_ascii=False, separators=(",", ":")))
    sizes = {k: size(v) for k, v in result.items()}
    for pl in result["players"]:
        for k, v in pl.items():
            if k == "targets":
                for entry in v.values():
                    for sk, sv in entry.items():
                        sizes[f"players.targets.{sk}"] = sizes.get(f"players.targets.{sk}", 0) + size(sv)
            else:
                sizes[f"players.{k}"] = sizes.get(f"players.{k}", 0) + size(v)
    for t in result["targets"]:
        for k in ("buffs", "buffVolumes"):
            sizes[f"targets.{k}"] = sizes.get(f"targets.{k}", 0) + size(t[k])
    return sorted(sizes.items(), key=lambda kv: -kv[1])


def build_catalog(files):
    """Sammelt je Boss ALLE Ziele (NPC-ID, Name, ob in einer Phase) und Mechaniken (Name, Beschreibung,
    max. Ereigniszahl). Funktioniert nur mit ausfuehrlichen EI-JSONs (gekuerzte sind bereits gefiltert)."""
    cat = {}
    for f in files:
        try:
            root = load_json(f)
        except Exception as e:
            print(f"FEHLER {f}: {e!r}", file=sys.stderr)
            continue
        key = boss_key_of(root)
        if key is None:
            print(f"uebersprungen (keine triggerID): {f}", file=sys.stderr)
            continue
        c = cat.setdefault(key, {"name": root.get("fightName"), "logs": 0, "targets": {}, "mechanics": {}})
        c["logs"] += 1
        c["name"] = c["name"] or root.get("fightName")
        tg = root.get("targets") or []
        in_phase = set(relevant_targets(root.get("phases") or [], len(tg))) if root.get("phases") else set(range(len(tg)))
        for t, tgt in enumerate(tg):
            e = c["targets"].setdefault(tgt.get("id"), {"name": tgt.get("name"), "in_phases": False})
            e["in_phases"] = e["in_phases"] or t in in_phase
        for m in root.get("mechanics") or []:
            n = len(m.get("mechanicsData") or [])
            e = c["mechanics"].setdefault(mech_label(m), {"description": m.get("description") or "", "events": 0})
            e["events"] = max(e["events"], n)
    return cat


_q = lambda s: json.dumps(s, ensure_ascii=False)               # gueltiger TOML-String
_one = lambda s: " ".join(str(s or "").split())

CATALOG_HEADER = """# Filter fuer extract_log.py / gw2logs.py - je Boss eine Whitelist fuer Ziele (NPC-ID) und Mechaniken (Name).
# Schluessel: Boss-ID, bei Challenge Mode negativ.  Fehlt 'targets' bzw. 'mechanics' bei einem Boss, wird dort nicht gefiltert.
# NICHT GEWUENSCHTES BITTE AUSKOMMENTIEREN (#), NICHT LOESCHEN: Beim naechsten 'python extract_log.py <Ordner> --catalog'
# werden nur Eintraege ergaenzt, die weder aktiv noch auskommentiert in dieser Datei stehen.
"""


def _target_line(tid, e):
    return f"  {tid},  # {_one(e['name'])}" + ("" if e["in_phases"] else "  [in keiner Phase]")


def _mech_line(name, e):
    return f"  {_q(name)},  # max {e['events']} Ereignisse | {_one(e['description'])[:90]}"


def _sorted_targets(d):
    return sorted(d.items(), key=lambda kv: (kv[0] is None, kv[0] if isinstance(kv[0], int) else 0))


def _sorted_mechs(d):
    return sorted(d.items(), key=lambda kv: (-kv[1]["events"], kv[0]))


def render_boss(key, c, marker=None):
    out = [f"# {_one(c['name']) or '?'}  ({c['logs']} Log(s))", f'[boss."{key}"]']
    for sec, items, line in (("targets", _sorted_targets(c["targets"]), _target_line),
                             ("mechanics", _sorted_mechs(c["mechanics"]), _mech_line)):
        out.append(f"{sec} = [")
        if marker:
            out.append(f"  # --- {marker} ---")
        out += [line(k, v) for k, v in items]
        out.append("]")
    return out + [""]


_RE_HEAD = re.compile(r'^\s*(#\s*)?\[boss\."(-?\d+)"\]')
_RE_ARR = re.compile(r'^\s*(#\s*)?(targets|mechanics)\s*=\s*\[(.*)$')
_RE_CLOSE = re.compile(r'^\s*(#\s*)?\]')
_RE_INT = re.compile(r'^\s*(?:#\s*)*(-?\d+)\s*,')
_RE_STR = re.compile(r'^\s*(?:#\s*)*("(?:[^"\\]|\\.)*")\s*,')


def scan_filter_text(lines):
    """Liest eine (ggf. teilweise auskommentierte) Filterdatei zeilenweise.
    Rueckgabe: bosses (alle Header, auch auskommentierte), known[boss][sec] (aktive UND auskommentierte Eintraege),
    arrays[(boss, sec)] = {active, oneline, close (Zeilenindex der schliessenden Klammer)}."""
    bosses, known, arrays = set(), {}, {}
    cur = sec = None
    for i, line in enumerate(lines):
        m = _RE_HEAD.match(line)
        if m:
            cur, sec = m.group(2), None
            bosses.add(cur)
            known.setdefault(cur, {"targets": set(), "mechanics": set()})
            continue
        if cur is None:
            continue
        m = _RE_ARR.match(line)
        if m and sec is None:
            name, rest = m.group(2), m.group(3).split("#")[0]
            one = "]" in rest
            arrays[(cur, name)] = {"active": not m.group(1), "oneline": one, "close": None}
            if one:                                  # Eintraege einer einzeiligen Liste ebenfalls als bekannt werten
                for g in re.findall(r'-?\d+' if name == "targets" else r'"(?:[^"\\]|\\.)*"', rest):
                    known[cur][name].add(int(g) if name == "targets" else json.loads(g))
            else:
                sec = name
            continue
        if sec:
            if _RE_CLOSE.match(line):
                arrays[(cur, sec)]["close"] = i
                sec = None
                continue
            mi = (_RE_INT if sec == "targets" else _RE_STR).match(line)
            if mi:
                known[cur][sec].add(int(mi.group(1)) if sec == "targets" else json.loads(mi.group(1)))
    return bosses, known, arrays


def merge_catalog(cat, path, dry_run=False):
    """Ergaenzt NUR neue Ziele/Mechaniken in einer bestehenden Filterdatei (Aktive und Auskommentierte gelten
    als bekannt). Bestehender Inhalt bleibt unveraendert; vorher wird <datei>.bak angelegt.
    Neue Eintraege werden aktiv und mit Datumsmarke eingefuegt. Fehlt ein Array (= 'nicht filtern') oder ist es
    auskommentiert, wird dort nichts ergaenzt. Rueckgabe: Liste von Meldungen."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    bosses, known, arrays = scan_filter_text(lines)
    marker = f"neu am {datetime.date.today().isoformat()}"
    inserts, tail, msgs = [], [], []
    for key in sorted(cat, key=lambda k: (abs(int(k)), int(k))):
        c = cat[key]
        label = f"{_one(c['name']) or '?'} [{key}]"
        if key not in bosses:
            tail += render_boss(key, c, marker)
            msgs.append(f"{label}: neuer Boss ({len(c['targets'])} Ziele, {len(c['mechanics'])} Mechaniken)")
            continue
        for sec, items, line in (("targets", _sorted_targets(c["targets"]), _target_line),
                                 ("mechanics", _sorted_mechs(c["mechanics"]), _mech_line)):
            new = [(k, v) for k, v in items if k not in known[key][sec]]
            if not new:
                continue
            arr = arrays.get((key, sec))
            if not arr or not arr["active"]:
                msgs.append(f"{label}: {len(new)} neue {sec} nicht ergaenzt (kein aktiver Filter in der Datei)")
                continue
            if arr["oneline"] or arr["close"] is None:
                msgs.append(f"{label}: {len(new)} neue {sec} bitte manuell ergaenzen (einzeilige Liste): "
                            + ", ".join(str(k) if sec == "targets" else _q(k) for k, _ in new))
                continue
            inserts.append((arr["close"], [f"  # --- {marker} ---"] + [line(k, v) for k, v in new]))
            msgs.append(f"{label}: +{len(new)} {sec}: " + ", ".join(str(k) if sec == "targets" else _q(k) for k, _ in new[:8])
                        + (" ..." if len(new) > 8 else ""))
    if dry_run:
        return msgs
    for idx, new_lines in sorted(inserts, key=lambda x: -x[0]):
        lines[idx:idx] = new_lines
    if tail:
        if lines and lines[-1] != "":
            lines.append("")
        lines += tail
    if inserts or tail:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        path.write_text("\n".join(lines), encoding="utf-8")
    return msgs


def write_catalog(cat, path, dry_run=False):
    """Neue Datei anlegen oder - wenn sie existiert - nur Neues ergaenzen. Rueckgabe: Meldungen."""
    path = Path(path)
    if path.exists():
        return merge_catalog(cat, path, dry_run)
    if not dry_run:
        lines = [CATALOG_HEADER]
        for key in sorted(cat, key=lambda k: (abs(int(k)), int(k))):
            lines += render_boss(key, cat[key])
        path.write_text("\n".join(lines), encoding="utf-8")
    return [f"{path} neu angelegt ({len(cat)} Boss(e))"]


# ----------------------------------------------------------------------------- CLI
def collect_inputs(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(x for x in p.rglob("*") if x.is_file()
                            and (x.name.endswith(".json") or x.name.endswith(".json.gz"))
                            and not x.name.endswith(".slim.json"))
        else:
            files.append(p)
    return files


def out_name(p):
    n = Path(p).name
    for suf in (".json.gz", ".json"):
        if n.endswith(suf):
            n = n[:-len(suf)]
            break
    return n + ".slim.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="EI-JSON-Dateien und/oder Ordner")
    ap.add_argument("-o", "--out-dir", default="extracted", help="Zielordner (Default: ./extracted)")
    ap.add_argument("--indent", type=int, default=None, help="eingerueckte Ausgabe (zum Lesen)")
    ap.add_argument("--graph", choices=["full", "all", "none"], default="full",
                    help="targetDamage1S: nur Phase 0 (full, Default), alle Phasen oder gar nicht")
    ap.add_argument("--keep-zero", action="store_true", help="Buffs/Volumes nur mit Nullwerten behalten")
    ap.add_argument("--whitelist", help="Accounts, kommagetrennt (nur diese Spieler bleiben)")
    ap.add_argument("--whitelist-from", metavar="CONFIG.toml", help="Whitelist aus config.toml uebernehmen")
    ap.add_argument("--filter", metavar="FILE.toml", help="Boss-Filter (Ziele/Mechaniken), z. B. analysis_filter.toml")
    ap.add_argument("--report", action="store_true", help="Groesse je Abschnitt ausgeben")
    ap.add_argument("--catalog", nargs="?", const="analysis_filter.toml", metavar="FILTER.toml",
                    help="statt zu extrahieren: Ziele/Mechaniken je Boss in die Filterdatei schreiben "
                         "(Default analysis_filter.toml). Existiert sie, werden nur NEUE Eintraege ergaenzt")
    ap.add_argument("--dry-run", action="store_true", help="mit --catalog: nur anzeigen, was ergaenzt wuerde")
    a = ap.parse_args()

    files = collect_inputs(a.inputs)
    if not files:
        sys.exit("Keine JSON-Dateien gefunden.")

    if a.catalog:
        cat = build_catalog(files)
        msgs = write_catalog(cat, a.catalog, a.dry_run)
        print(f"{len(files)} Datei(en), {len(cat)} Boss(e) -> {a.catalog}" + (" (Trockenlauf)" if a.dry_run else ""))
        for key, c in sorted(cat.items(), key=lambda kv: abs(int(kv[0]))):
            print(f"  {key:>8}  {c['name'] or '?':32} Logs {c['logs']:4}  Ziele {len(c['targets']):2}  Mechaniken {len(c['mechanics']):3}")
        for m in msgs:
            print("  " + m)
        return

    whitelist = None
    if a.whitelist:
        whitelist = [x.strip() for x in a.whitelist.split(",") if x.strip()]
    elif a.whitelist_from:
        whitelist = tomllib.loads(Path(a.whitelist_from).read_text(encoding="utf-8")).get("whitelist")
    filters = load_filters(a.filter) if a.filter else None

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        try:
            root = load_json(f)
            result, st = extract(root, a.graph, a.keep_zero, f.name, whitelist, filters)
        except Exception as e:                       # eine kaputte Datei soll den Rest nicht stoppen
            print(f"FEHLER {f}: {e!r}", file=sys.stderr)
            continue
        target = out_dir / out_name(f)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=a.indent,
                      separators=None if a.indent else (",", ":"))
        kb_in, kb_out = f.stat().st_size / 1024, target.stat().st_size / 1024
        print(f"{f.name}: {kb_in:,.0f} KB -> {kb_out:,.0f} KB | Phasen {st['phases']}, "
              f"Ziele {st['targets_kept']}/{st['targets_total']}, Spieler {st['players']}, "
              f"Buffs {st['buffs_used']}, Mechaniken {st['mechanics']}")
        if st["buffs_without_name"]:
            print(f"  ! {len(st['buffs_without_name'])} Buff-IDs ohne Namen in buffMap: "
                  f"{', '.join(st['buffs_without_name'][:15])}", file=sys.stderr)
        for w in st["warnings"]:
            print(f"  ! {w}", file=sys.stderr)
        if a.report:
            for name, n in section_sizes(result)[:14]:
                print(f"    {n:>10,}  {name}")


if __name__ == "__main__":
    main()
