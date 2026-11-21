"""report_engine.py - bewertet gekuerzte EI-JSONs (log_analysis.data, erzeugt von extract_log.py) nach Regeln aus
report_rules.toml. Reine Logik ohne HTML und ohne DB, damit sie auch fuer Graphen/Statistiken nutzbar ist.
Liegt neben build_site.py und hat keine Abhaengigkeit zum Backend.

Regelarten (jeweils unter [kind."Name"], boss-spezifisch unter [boss."<Boss-ID, CM negativ>".kind."Name"]):
  mechanic      Anzahl der Ereignisse des Spielers (Name wie in analysis_filter.toml)
  buff          Uptime in Phase 0 (Full Fight) in % bzw. Ø Stacks; Name wie in buffs_info
  metric        berechnete Werte: dps, breakbar_damage, flanking_percent, resurrects
  consumables   [consumables] ohne Namen: Anzahl verschiedener Consumables (Food/Utility/...)

Schluessel einer Regel:
  critical = X                    auffaellig, wenn Wert >= X   (mechanic: Default 1)
  critical_below = X              auffaellig, wenn Wert <  X
  list_if_not_critical = true     auch unauffaellige Werte auflisten (Mechaniken nur bei Anzahl > 0)
  text_if_listed_critical         Text, wenn auffaellig
  text_if_listed_not_critical     Text, wenn nur informativ gelistet
  link                            Link zur Erklaerung (wird als "Erklärung" angehaengt)
  professions = ["..."]           Regel nur fuer diese Professionen/Elite-Spezialisierungen
Platzhalter in Texten: {value} {count} {threshold} {rule} {player} {account} {character} {time} {times}
Boss-spezifische Regeln ersetzen globale mit gleichem Namen; globale Mechanik-Regeln greifen nur, wenn die
Mechanik im Log vorkommt.
"""
import sys
import tomllib

KINDS = ("mechanic", "buff", "metric", "consumables")
RULE_KEYS = {"critical", "critical_below", "list_if_not_critical", "text_if_listed_critical",
             "text_if_listed_not_critical", "link", "professions"}


# ----------------------------------------------------------------------------- Metriken (Phase 0, Hauptziele)
def _total(pl, key):
    t = (pl.get("dps_targets_total") or [{}])
    return (t[0] or {}).get(key)


def _flanking_percent(pl, slim):
    mains = (slim.get("main_targets_per_phase") or [[]])[0]
    hits = flank = 0
    for t in mains:
        st = ((pl.get("targets") or {}).get(str(t)) or {}).get("stats") or []
        if st:
            hits += st[0].get("connectedDirectDamageCount", 0) or 0
            flank += st[0].get("flankingRate", 0) or 0
    return round(100.0 * flank / hits, 1) if hits else None


METRICS = {
    "dps": lambda pl, slim: _total(pl, "dps"),
    "breakbar_damage": lambda pl, slim: _total(pl, "breakbarDamage"),
    "flanking_percent": _flanking_percent,
    "resurrects": lambda pl, slim: ((pl.get("support") or [{}])[0] or {}).get("resurrects"),
}
METRIC_LABEL = {"dps": "DPS (Hauptziele)", "breakbar_damage": "Breakbar-Schaden",
                "flanking_percent": "Flankenrate %", "resurrects": "Wiederbelebungen"}


# ----------------------------------------------------------------------------- Regeln laden
def _clean_rules(block, where, problems):
    """{kind: {name: rule}} aus einem TOML-Block (global oder boss.<key>)."""
    out = {}
    for kind, val in block.items():
        if kind not in KINDS:
            if kind != "boss":
                problems.append(f"{where}: unbekannte Regelart '{kind}'")
            continue
        entries = {"": val} if kind == "consumables" else val
        for name, rule in entries.items():
            if not isinstance(rule, dict):
                problems.append(f"{where}.{kind}.{name}: Regel muss eine Tabelle sein")
                continue
            bad = sorted(set(rule) - RULE_KEYS)
            if bad:
                problems.append(f"{where}.{kind}.{name}: unbekannte Schluessel {bad}")
            if kind == "metric" and name not in METRICS:
                problems.append(f"{where}.metric.{name}: unbekannte Metrik (erlaubt: {', '.join(METRICS)})")
                continue
            out.setdefault(kind, {})[name] = rule
    return out


def load_rules(path):
    """Rueckgabe: (rules, problems). rules = {"global": {kind: {name: rule}}, "boss": {key: {...}}}"""
    problems = []
    try:
        data = tomllib.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError:
        return {"global": {}, "boss": {}}, [f"{path} nicht gefunden - es werden keine Auffaelligkeiten bewertet"]
    except tomllib.TOMLDecodeError as e:
        return {"global": {}, "boss": {}}, [f"{path}: ungueltiges TOML ({e})"]
    rules = {"global": _clean_rules(data, "global", problems), "boss": {}}
    for key, block in (data.get("boss") or {}).items():
        rules["boss"][str(key)] = _clean_rules(block, f"boss.{key}", problems)
    return rules, problems


# ----------------------------------------------------------------------------- Hilfen
class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _num(v):
    if isinstance(v, float) and v != int(v):
        return f"{v:.1f}"
    return str(int(v)) if isinstance(v, (int, float)) else str(v)


def fmt_ms(ms):
    if ms is None:
        return "?"
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def mech_label(m):
    return m.get("name") or m.get("fullName") or m.get("description") or "?"


def _flag(rule, value, default_crit=None):
    c = rule.get("critical", default_crit if "critical_below" not in rule else None)
    cb = rule.get("critical_below")
    return (c is not None and value >= c) or (cb is not None and value < cb)


def _text(rule, critical, label, value, pl, extra=None):
    tmpl = rule.get("text_if_listed_critical" if critical else "text_if_listed_not_critical")
    thr = rule.get("critical") if rule.get("critical") is not None else rule.get("critical_below")
    ctx = _SafeDict(value=_num(value), count=_num(value), threshold=_num(thr) if thr is not None else "",
                    rule=label, player=pl.get("account", ""), account=pl.get("account", ""),
                    character=pl.get("name", ""), time="", times="")
    ctx.update(extra or {})
    if not tmpl:
        return f"{label}: {_num(value)}"
    try:
        return tmpl.format_map(ctx)
    except (ValueError, IndexError, KeyError):
        return tmpl


def _applies(rule, pl):
    profs = rule.get("professions")
    return not profs or pl.get("profession") in profs


# ----------------------------------------------------------------------------- Auswertung
def evaluate(slim, boss_key, rules):
    """Bewertet ein gekuerztes Log. Rueckgabe: Liste je Spieler:
    {account, character, profession, group, findings:[{kind,name,value,critical,text,link}],
     values:{mechanics:{name:count}, buffs:{name:uptime}, metrics:{name:wert}, consumables:[namen]}}"""
    chars = slim.get("characters") or {}
    events, present = {}, set()
    for m in slim.get("mechanics") or []:
        label = mech_label(m)
        present.add(label)
        for e in m.get("mechanicsData") or []:
            acc = chars.get(e.get("actor"))
            if acc:
                events.setdefault((acc, label), []).append(e.get("time"))

    def merged(kind):
        g = dict((rules["global"].get(kind) or {}))
        b = (rules["boss"].get(str(boss_key)) or {}).get(kind) or {}
        scoped = set(b)
        g.update(b)
        return g, scoped

    out = []
    for pl in slim.get("players") or []:
        acc = pl["account"]
        findings = []

        def add(kind, name, rule, value, critical, label, extra=None):
            findings.append({"kind": kind, "name": name, "value": value, "critical": critical,
                             "text": _text(rule, critical, label, value, pl, extra), "link": rule.get("link")})

        # Mechaniken
        rs, scoped = merged("mechanic")
        for name, rule in rs.items():
            if (name not in present and name not in scoped) or not _applies(rule, pl):
                continue
            times = sorted(t for t in events.get((acc, name), []) if t is not None)
            count = len(times)
            crit = _flag(rule, count, default_crit=1)
            if crit or (rule.get("list_if_not_critical") and count > 0):
                add("mechanic", name, rule, count, crit, name,
                    {"time": fmt_ms(times[0]) if times else "",
                     "times": ", ".join(fmt_ms(t) for t in times[:5]) + (" …" if len(times) > 5 else "")})

        # Buffs (Phase 0)
        uptimes = {}
        for name, per_phase in (pl.get("buffUptimes") or {}).items():
            if per_phase:
                uptimes[name] = per_phase[0].get("uptime", 0)
        rs, _ = merged("buff")
        for name, rule in rs.items():
            if not _applies(rule, pl):
                continue
            v = uptimes.get(name, 0.0)
            crit = _flag(rule, v)
            if crit or rule.get("list_if_not_critical"):
                add("buff", name, rule, v, crit, name)

        # Metriken
        metrics = {k: fn(pl, slim) for k, fn in METRICS.items()}
        rs, _ = merged("metric")
        for name, rule in rs.items():
            v = metrics.get(name)
            if v is None or not _applies(rule, pl):
                continue
            crit = _flag(rule, v)
            if crit or rule.get("list_if_not_critical"):
                add("metric", name, rule, v, crit, METRIC_LABEL.get(name, name))

        # Consumables
        cons = sorted({c.get("name") for c in pl.get("consumables") or [] if c.get("name")})
        rs, _ = merged("consumables")
        rule = rs.get("")
        if rule and _applies(rule, pl):
            crit = _flag(rule, len(cons))
            if crit or rule.get("list_if_not_critical"):
                add("consumables", "", rule, len(cons), crit, "Consumables")

        order = {k: i for i, k in enumerate(KINDS)}
        findings.sort(key=lambda f: (not f["critical"], order[f["kind"]], f["name"]))
        out.append({
            "account": acc, "character": pl.get("name"), "profession": pl.get("profession"), "group": pl.get("group"),
            "findings": findings,
            "values": {
                "mechanics": {n: len(t) for (a, n), t in sorted(events.items()) if a == acc},
                "buffs": dict(sorted(uptimes.items())),
                "metrics": {k: v for k, v in metrics.items() if v is not None},
                "consumables": cons}})
    return out


if __name__ == "__main__":                      # Schnelltest: python report_engine.py slim.json report_rules.toml
    import json
    slim = json.load(open(sys.argv[1], encoding="utf-8"))
    rules, probs = load_rules(sys.argv[2])
    for p in probs:
        print("!", p, file=sys.stderr)
    for r in evaluate(slim, slim.get("boss_key"), rules):
        print(r["account"], r["character"])
        for f in r["findings"]:
            print("  ", "KRIT" if f["critical"] else "info", f["text"])
