#!/usr/bin/env python3
"""extract_log.py - verkleinert eine Elite-Insights-JSON auf die Daten, die fuer die Analyse gebraucht werden.

Wird von gw2logs.py importiert, laeuft aber auch allein:
    python extract_log.py log.json                       -> extracted/log.slim.json
    python extract_log.py json_dump/ -o out/             -> alle *.json(.gz) im Ordner (rekursiv)
    python extract_log.py log.json --indent 1 --report   -> lesbar + Groessen pro Abschnitt
    python extract_log.py log.json --whitelist-from config.toml --filter analysis_filter.toml
    python extract_log.py json_dump/ --catalog           -> analysis_filter.toml anlegen bzw. NUR Neues ergaenzen
    python extract_log.py /pfad/arcdps.cbtlogs --catalog -> parst die Logs selbst (EI-CLI aus config.toml), siehe --help

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
import collections
import datetime
import gzip
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

# ----------------------------------------------------------------------------- Konfiguration
SCHEMA_VERSION = 4
META_FIELDS = ("eliteInsightsVersion", "gW2Build", "triggerID", "isCM", "success",
               "durationMS", "duration", "timeStartStd", "timeEndStd")
PLAYER_FIELDS = ("account", "name", "group", "profession", "hasCommanderTag", "friendlyNPC", "notInSquad")
TARGET_FIELDS = ("id", "name", "finalHealth", "finalBarrier", "barrierPercent", "healthPercentBurned")
DPS_FIELDS = ("dps", "condiDps", "powerDps", "breakbarDamage")
STATS_FIELDS = ("flankingRate", "connectedDirectDamageCount", "killed", "downed")
SUPPORT_FIELDS = ("resurrects", "resurrectTime")
DROP_VOLUME_KEYS = {"incomingBy", "incomingByExtensionBy"}   # "wer hat es erzeugt" -> nicht exportieren
BUFF_INFO_DROP = {"icon", "descriptions"}      # aus buffMap-Eintraegen entfernt (URLs / lange Texte)
GENERATION_BUFFS = ("Alacrity", "Quickness", "Might", "Fury", "Protection", "Stability", "Aegis",
                    "Resistance", "Resolution", "Regeneration", "Swiftness", "Vigor")   # fuer Rollen "wer gibt was"
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

    # ---- Gruppenwerte ueber ALLE Spieler (auch Nicht-Whitelist). Nur Zahlen, keine Namen.
    real = [pl for pl in raw_players if not pl.get("friendlyNPC")]
    owner = {pl["name"]: i for i, pl in enumerate(real) if pl.get("name")}
    mech_wl_g = flt.get("mechanics")
    owners_by_mech, events_by_mech = {}, {}
    for m in root.get("mechanics") or []:
        label = mech_label(m)
        if mech_wl_g and label not in mech_wl_g and m.get("fullName") not in mech_wl_g:
            continue
        owners_by_mech.setdefault(label, []).extend(
            owner[e.get("actor")] for e in (m.get("mechanicsData") or []) if e.get("actor") in owner)
        events_by_mech[label] = events_by_mech.get(label, 0) + len(m.get("mechanicsData") or [])
    g_mech = {}
    for n, o in owners_by_mech.items():
        if o or events_by_mech.get(n):          # counts: Wert JEDES Spielers (inkl. 0), absteigend, ohne Namen -> Rang/Anteil
            c = collections.Counter(o)          # events: ALLE Ereignisse der Mechanik, auch mit NPC als Akteur
            g_mech[n] = {"count": len(o), "players": len(c), "events": events_by_mech.get(n, 0),
                         "counts": sorted((c.get(i, 0) for i in range(len(real))), reverse=True)}
    metric_values = {"dps": [], "breakbar_damage": [], "resurrects": []}
    for pl in real:
        dps_t = pl.get("dpsTargets") or []
        o_g0 = orient_of(dps_t, n_t, n_ph)
        tot = sum_dicts(pick(at(dps_t, t, 0, o_g0) or {}, DPS_FIELDS) for t in (main_per_phase[0] if main_per_phase else []))
        metric_values["dps"].append(tot.get("dps", 0))
        metric_values["breakbar_damage"].append(tot.get("breakbarDamage", 0))
        metric_values["resurrects"].append(((pl.get("support") or [{}])[0] or {}).get("resurrects", 0) or 0)
    g_metrics = {k: sum(v) for k, v in metric_values.items()}
    # ---- Boon-Erzeugung: wie viel Uptime (bzw. Stacks) erzeugt ein Spieler im Schnitt fuer ALLE Spieler im Log.
    # Summe der 'generated'-Anteile jedes Spielers ueber alle Empfaenger (Phase 0), geteilt durch die Spielerzahl.
    gen_ids = {}
    for key, val in (root.get("buffMap") or {}).items():
        bid = key[1:] if key[:1] == "b" and key[1:].lstrip("-").isdigit() else key
        if val.get("name") in GENERATION_BUFFS:
            gen_ids[bid] = val["name"]
    gen_sum = {}
    if gen_ids and real:
        for rcp in real:
            for b in rcp.get("buffUptimes") or []:
                nm, bd = gen_ids.get(str(b.get("id"))), (b.get("buffData") or [None])[0]
                if nm and bd:
                    for src, pct in (bd.get("generated") or {}).items():
                        if src in owner and pct:
                            d = gen_sum.setdefault(nm, {})
                            d[owner[src]] = d.get(owner[src], 0.0) + pct
    gen_val, gen_lists = {}, {}
    for nm, d in gen_sum.items():
        vals = [round(d.get(i, 0.0) / len(real), 1) for i in range(len(real))]
        gen_lists[nm] = sorted(vals, reverse=True)
        for i, v in enumerate(vals):
            gen_val.setdefault(i, {})[nm] = v
    real_idx = {id(pl): i for i, pl in enumerate(real)}
    group = {"player_count": len(real), "mechanics": g_mech, "metrics": g_metrics, "generation": gen_lists,
             "tracked": sorted(mech_wl_g) if mech_wl_g else None,     # None = alle Mechaniken erfasst
             "metric_values": {k: sorted(v, reverse=True) for k, v in metric_values.items()}}
    for pl in raw_players:
        if wl is not None and (pl.get("account") or "").casefold() not in wl:
            continue
        out = pick(pl, PLAYER_FIELDS)
        if pl.get("name") and pl.get("account"):
            characters[pl["name"]] = pl["account"]

        out["generates"] = gen_val.get(real_idx.get(id(pl)), {})      # Ø Uptime/Stacks, die der Spieler der Gruppe gibt
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
        "group": group,
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


# ----------------------------------------------------------------------------- Katalog direkt aus Logs
LOG_EXTS = (".zevtc", ".evtc", ".evtc.zip")
CATALOG_CONF = {                                # EI-Einstellungen fuers Katalogisieren: nur JSON, kein Upload
    "SaveAtOut": "false", "SaveOutJSON": "true", "SaveOutHTML": "false", "SaveOutCSV": "false",
    "SaveOutTrace": "false", "ApplicationTraces": "false", "IndentJSON": "false", "CompressRaw": "false",
    "RawTimelineArrays": "false", "ParseCombatReplay": "false", "ParsePhases": "true", "ComputeMechanics": "true",
    "UploadToDPSReports": "false", "UploadToWingman": "false", "UploadToRaidar": "false",
    "SendEmbedToWebhook": "false", "SendSimpleMessageToWebhook": "false", "AutoDiscordBatch": "false",
    "ParseMultipleLogs": "false", "SkipFailedTries": "false", "AutoAdd": "false", "AutoParse": "false",
}


def split_inputs(paths, skip_patterns=()):
    """Trennt Eingaben in (EI-Logs, JSON-Dateien). Ordner werden rekursiv durchsucht."""
    logs, jsons = [], []
    for p in map(Path, paths):
        cands = sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
        for x in cands:
            n = x.name.lower()
            if any(sp.lower() in str(x).lower() for sp in skip_patterns):
                continue
            if n.endswith(LOG_EXTS):
                logs.append(x)
            elif (n.endswith(".json") or n.endswith(".json.gz")) and not n.endswith(".slim.json"):
                jsons.append(x)
    return logs, jsons


def load_ei_settings(config_path, ei_command):
    """EI-Kommando, Umgebung, Timeout, Skip-Muster und aktivierte Boss-IDs aus config.toml (oder --ei-command)."""
    cp = Path(config_path) if config_path else Path("config.toml")
    cfg = tomllib.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
    ei = cfg.get("ei", {})
    command = shlex.split(ei_command) if ei_command else ei.get("command")
    enabled = None
    bf = cfg.get("bosses_file")
    if bf and (cp.parent / bf).exists():
        enabled = set()
        for b in tomllib.loads((cp.parent / bf).read_text(encoding="utf-8")).get("boss", []):
            if b.get("enabled"):
                enabled.update([abs(b["id"]), *map(abs, b.get("aliases", []))])
    return {"command": command, "env": ei.get("env", {}), "timeout": ei.get("timeout", 900),
            "skip": cfg.get("skip_name_patterns", ["golem"]), "enabled_ids": enabled}


def filter_enabled_bosses(logs, enabled_ids):
    """Behaelt nur Logs aus Ordnern 'Boss (ID)' mit aktiviertem Boss; Ordner ohne erkennbare ID bleiben."""
    if enabled_ids is None:
        return logs
    keep = []
    for p in logs:
        m = re.search(r"\((\d+)\)\s*$", p.parent.name)
        if not m or int(m.group(1)) in enabled_ids:
            keep.append(p)
    return keep


def sample_even(items, n):
    """Gleichmaessig ueber die (nach Zeit sortierte) Liste verteilte Stichprobe."""
    if n <= 0 or len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def merge_cat(acc, other):
    for key, c in other.items():
        a = acc.setdefault(key, {"name": c["name"], "logs": 0, "targets": {}, "mechanics": {}})
        a["logs"] += c["logs"]
        a["name"] = a["name"] or c["name"]
        for tid, e in c["targets"].items():
            t = a["targets"].setdefault(tid, {"name": e["name"], "in_phases": False})
            t["in_phases"] = t["in_phases"] or e["in_phases"]
        for name, e in c["mechanics"].items():
            m = a["mechanics"].setdefault(name, {"description": e["description"], "events": 0})
            m["events"] = max(m["events"], e["events"])


def _sig(p):
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"


def catalog_from_logs(logs, ei, filter_path, batch=20, per_boss=0, state_path="catalog_state.json",
                      dry_run=False, redo=False):
    """Parst Logs in Paketen mit der EI-CLI (JSON nur im Temp-Ordner), ergaenzt nach JEDEM Paket die
    Filterdatei und merkt sich fertige Logs in state_path, damit ein Abbruch/Neustart dort weitermacht."""
    state = set()
    if state_path and Path(state_path).exists() and not redo:
        state = set(json.loads(Path(state_path).read_text(encoding="utf-8")))
    if per_boss > 0:
        groups = {}
        for p in sorted(logs, key=lambda x: (x.parent.name, x.stat().st_mtime)):
            groups.setdefault(p.parent, []).append(p)
        logs = [x for g in groups.values() for x in sample_even(g, per_boss)]
    todo = [p for p in logs if _sig(p) not in state]
    print(f"{len(logs)} Log(s) gefunden, {len(todo)} noch nicht katalogisiert")
    if not todo:
        return {}
    path = Path(filter_path)
    if path.exists() and not dry_run:                     # Sicherung einmal pro Lauf (nicht pro Paket)
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    env = {**os.environ, **{k: str(v) for k, v in ei["env"].items()}}
    acc = {}
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "json"
            out.mkdir()
            conf = Path(tmp) / "catalog.conf"
            conf.write_text("\n".join(f"{k}={v}" for k, v in {**CATALOG_CONF, "OutLocation": str(out)}.items()),
                            encoding="utf-8")
            cmd = [*ei["command"], "-c", str(conf), *map(str, chunk)]
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env,
                                    timeout=ei["timeout"] * len(chunk))
            except subprocess.TimeoutExpired:
                print(f"  ! Zeitueberschreitung im Paket ab Log {i + 1} - uebersprungen", file=sys.stderr)
                continue
            except FileNotFoundError:
                sys.exit(f"EI-Kommando nicht gefunden: {ei['command']}")
            jsons = sorted(out.glob("*.json"))
            if not jsons:
                tail = (cp.stderr or cp.stdout or "").strip()[-400:]
                print(f"  ! Paket ab Log {i + 1}: keine JSON erzeugt (Exit {cp.returncode}). {tail}", file=sys.stderr)
            cat = build_catalog(jsons)
        merge_cat(acc, cat)
        added = []
        if cat and not dry_run:
            added = write_catalog(cat, path)
        if not dry_run:
            state.update(_sig(p) for p in chunk)
            if state_path:
                Path(state_path).write_text(json.dumps(sorted(state)), encoding="utf-8")
        print(f"[{min(i + batch, len(todo))}/{len(todo)}] {len(jsons)} JSON verarbeitet")
        for m in added:
            print("  " + m)
    return acc


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
    ap.add_argument("--dry-run", action="store_true", help="mit --catalog: nur anzeigen, was ergaenzt wuerde "
                                                           "(bei Logs wird trotzdem geparst, aber nichts gespeichert)")
    ap.add_argument("--config", metavar="config.toml", help="fuer --catalog mit Logs: EI-Kommando/Bossliste (Default ./config.toml)")
    ap.add_argument("--ei-command", metavar="CMD", help='EI-Kommando statt config.toml, z. B. "dotnet /pfad/GuildWars2EliteInsights-CLI.dll"')
    ap.add_argument("--batch", type=int, default=20, help="Logs pro EI-Aufruf bei --catalog (Default 20)")
    ap.add_argument("--per-boss", type=int, default=0, metavar="N",
                    help="bei --catalog: hoechstens N gleichmaessig verteilte Logs je Ordner (Default 0 = alle)")
    ap.add_argument("--all-bosses", action="store_true", help="bei --catalog auch Bosse mit enabled=false in bosses.toml")
    ap.add_argument("--state", default="catalog_state.json", help="Merkdatei bereits katalogisierter Logs")
    ap.add_argument("--redo", action="store_true", help="bei --catalog: Merkdatei ignorieren, alles neu parsen")
    a = ap.parse_args()

    missing = [x for x in a.inputs if not Path(x).exists()]
    for x in missing:
        print(f"! Pfad nicht gefunden: {x}", file=sys.stderr)
    if missing:
        print("  Tipp: Pfade mit Leerzeichen (z. B. \"Guild Wars 2\") muessen in Anfuehrungszeichen stehen.", file=sys.stderr)

    if a.catalog:
        ei = load_ei_settings(a.config, a.ei_command)
        logs, jsons = split_inputs(a.inputs, ei["skip"])
        if not a.all_bosses:
            logs = filter_enabled_bosses(logs, ei["enabled_ids"])
        if not logs and not jsons:
            sys.exit("Weder EI-Logs (.zevtc/.evtc) noch JSON-Dateien gefunden.")
        cat, msgs = {}, []
        if jsons:
            cat = build_catalog(jsons)
            msgs = write_catalog(cat, a.catalog, a.dry_run)
        if logs:
            if not ei["command"]:
                sys.exit("Kein EI-Kommando: [ei] command in config.toml setzen oder --ei-command angeben.")
            acc = catalog_from_logs(logs, ei, a.catalog, a.batch, a.per_boss, a.state, a.dry_run, a.redo)
            merge_cat(cat, acc)
            if a.dry_run and acc:
                msgs += write_catalog(acc, a.catalog, True)
        files = jsons or logs
        print(f"{len(logs)} Log(s), {len(jsons)} JSON(s), {len(cat)} Boss(e) -> {a.catalog}" + (" (Trockenlauf)" if a.dry_run else ""))
        for key, c in sorted(cat.items(), key=lambda kv: abs(int(kv[0]))):
            print(f"  {key:>8}  {c['name'] or '?':32} Logs {c['logs']:4}  Ziele {len(c['targets']):2}  Mechaniken {len(c['mechanics']):3}")
        for m in msgs:
            print("  " + m)
        return

    files = collect_inputs(a.inputs)
    if not files:
        sys.exit("Keine JSON-Dateien gefunden.")

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
