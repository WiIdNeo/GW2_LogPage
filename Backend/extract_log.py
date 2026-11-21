#!/usr/bin/env python3
"""extract_log.py - verkleinert eine Elite-Insights-JSON auf die Daten, die fuer die Analyse gebraucht werden.

Unabhaengig von gw2logs.py. Aufruf:
    python extract_log.py log.json                 -> extracted/log.slim.json
    python extract_log.py json_dump/ -o out/       -> alle *.json(.gz) im Ordner (rekursiv)
    python extract_log.py log.json --indent 2      -> lesbare Ausgabe zum Pruefen
    python extract_log.py log.json --graph all     -> targetDamage1S fuer ALLE Phasen (Default: nur Phase 0)
    python extract_log.py log.json --keep-zero     -> Buffs/Volumes mit nur Nullwerten NICHT entfernen

Konventionen im Ausgabeformat:
  * Alles, was in der EI-JSON pro Phase vorliegt, bleibt eine Liste mit Phasen-Index (wie "phases").
  * Pro Ziel ("target") wird ueber den ORIGINAL-Index aus der EI-JSON adressiert (String-Key "0", "1", ...).
  * Buffs/Zustaende/Consumables sind ueber ihren KLARNAMEN adressiert (aus der buffMap der JSON); IDs und
    weitere Attribute stehen in "buffs_info". Bei gleichem Namen mit verschiedenen IDs wird "Name (ID)" verwendet.
  * Spieler und Ziele werden ueber Charakternamen referenziert (mechanics.actor, generated, incomingBy);
    die Zuordnung Charakter -> Account steht in "characters".
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

# ----------------------------------------------------------------------------- Konfiguration
META_FIELDS = ("eliteInsightsVersion", "gW2Build", "triggerID", "isCM", "success",
               "durationMS", "duration", "timeStartStd", "timeEndStd")
PLAYER_FIELDS = ("account", "name", "group", "profession", "hasCommanderTag", "friendlyNPC", "notInSquad")
TARGET_FIELDS = ("id", "name", "finalHealth", "finalBarrier", "barrierPercent", "healthPercentBurned")
DPS_FIELDS = ("dps", "condiDps", "powerDps", "breakbarDamage")
STATS_FIELDS = ("flankingRate", "connectedDirectDamageCount", "killed", "downed")
SUPPORT_FIELDS = ("resurrects", "resurrectTime")
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


def nonzero_map(m):
    return {k: v for k, v in (m or {}).items() if v}


def is_zero(v):
    if isinstance(v, dict):
        return not any(v.values())
    return not v


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


# ----------------------------------------------------------------------------- Extraktion
def extract(root, graph="full", keep_zero=False, source_name=None):
    warnings = []
    phases = root.get("phases") or []
    n_ph = len(phases)
    raw_targets = root.get("targets") or []
    n_t = len(raw_targets)
    raw_players = root.get("players") or []
    if not raw_targets:
        warnings.append("Keine 'targets' in der JSON")
    rel = relevant_targets(phases, n_t)
    used_buffs = set()

    def keep(buff_entries):
        return keep_zero or not all(is_zero(e) for e in buff_entries)

    # ---- Ziele
    targets = []
    for t in rel:
        src = raw_targets[t]
        out = {"index": t, **pick(src, TARGET_FIELDS)}

        buffs = {}
        for b in src.get("buffs") or []:
            per_phase = []
            for e in b.get("buffData") or []:
                item = {"uptime": e.get("uptime", 0), "presence": e.get("presence", 0)}
                gen = nonzero_map(e.get("generated"))
                if gen:
                    item["generated"] = gen
                per_phase.append(item)
            if keep([{"u": x["uptime"], "p": x["presence"], "g": x.get("generated")} for x in per_phase]):
                buffs[str(b["id"])] = per_phase
        out["buffs"] = buffs

        volumes = {}
        for b in src.get("buffVolumes") or []:
            per_phase = []
            for e in b.get("buffVolumeData") or []:
                per_phase.append({k: (nonzero_map(v) if isinstance(v, dict) else v) for k, v in e.items()})
            if keep([{k: v for k, v in x.items()} for x in per_phase]):
                volumes[str(b["id"])] = per_phase
        out["buffVolumes"] = volumes

        used_buffs.update(buffs)
        used_buffs.update(volumes)
        targets.append(out)

    # ---- Spieler
    players = []
    characters = {}
    main_per_phase = [main_targets_of_phase(ph, n_t) for ph in phases]
    for pl in raw_players:
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
        totals = []
        for p in range(n_ph):
            totals.append(sum_dicts(pick(at(dps_t, t, p, o_dps) or {}, DPS_FIELDS) for t in main_per_phase[p]))
        out["dps_targets_total"] = totals

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

    # ---- Buff-Namen (nur verwendete IDs) -> IDs durch Klarnamen ersetzen
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
    # gleicher Name bei verschiedenen IDs (z. B. mehrere Food-/Skill-Buffs): ID zur Unterscheidung anhaengen
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

    # Zusatzinfos zu den Buffs (ID, stacking, ...), Schluessel = Klarname
    buffs_info = {display[bid]: {"id": bid, **{k: v for k, v in info.items() if k not in BUFF_INFO_DROP | {"name"}}}
                  for bid, info in raw_info.items()}

    links = root.get("uploadLinks") or []
    slug = links[0].rstrip("/").rsplit("/", 1)[-1] if links else None

    result = {
        "schema": 1,
        "source_file": source_name,
        "meta": pick(root, META_FIELDS),
        "uploadLinks": links,
        "dps_slug": slug,                 # == letzter Teil von log.dps_url in der DB
        "phases": phases,
        "main_targets_per_phase": main_per_phase,
        "targets": targets,
        "characters": characters,
        "players": players,
        "mechanics": root.get("mechanics") or [],
        "buffs_info": buffs_info,
    }
    stats = {"phases": n_ph, "targets_total": n_t, "targets_kept": len(rel), "players": len(players),
             "buffs_used": len(used_buffs), "buffs_without_name": unknown,
             "mechanics": len(result["mechanics"]), "warnings": warnings}
    return result, stats


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
    n = p.name
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
    a = ap.parse_args()

    files = collect_inputs(a.inputs)
    if not files:
        sys.exit("Keine JSON-Dateien gefunden.")
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        try:
            root = load_json(f)
            result, st = extract(root, a.graph, a.keep_zero, f.name)
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


if __name__ == "__main__":
    main()
