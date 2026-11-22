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
  compare = "player"|"total"|"share"   (nur mechanic und metric; Metrik nur dps, breakbar_damage, resurrects)
      player  Wert des Spielers (Default)
      total   Summe ueber ALLE Spieler im Log (auch Randoms); Ergebnis erscheint einmal im Abschnitt "Gruppe"
      share   Anteil des Spielers an dieser Summe in %; Ergebnis je Spieler
  min_total = N                   bei share: Regel nur anwenden, wenn die Gruppensumme >= N ist
  of = "Name"                     Quelle, falls der Regelname abweicht - erlaubt mehrere Regeln fuer dieselbe Mechanik,
                                  z. B. [mechanic."Dead (Gruppe)"] mit of = "Dead", compare = "total"
  roles = ["Tank"]                Regel nur fuer Spieler mit (mindestens) einer dieser Rollen
  not_roles = ["Tank"]            Regel NICHT fuer Spieler mit einer dieser Rollen
Platzhalter in Texten: {value} {count} {threshold} {rule} {player} {account} {character} {time} {times} {roles}
  zusaetzlich bei total/share: {total} (Gruppensumme) {players} (betroffene Spieler) {player_count} {share}
  Rang-Platzhalter (wenn Gruppenwerte vorhanden): {rank} (Platz von oben, 1 = meiste) {rank_bottom} (von unten) {share} {total}

Rollen ([role."Name"], boss-spezifisch [boss."<ID>".role."Name"]) - aus dem Vergleich mit ALLEN Spielern im Log:
  match = "any"|"all"             Rolle, wenn mindestens ein / alle Kriterien zutreffen (Default any)
  description = "..."             optional, nur zur Erinnerung
  criteria = [ { source = "mechanic"|"metric", name = "...", <Bedingung(en)> }, ... ]
  Bedingungen eines Kriteriums (alle angegebenen muessen zutreffen):
    top = 3          Platz <= 3 von oben (Wert > 0)       bottom = 3     Platz <= 3 von unten (Nullen zaehlen mit)
    share_min = 30   Anteil an der Gruppensumme >= 30 %    share_max = 5  Anteil <= 5 %
    min_total = 4    Gruppensumme muss >= 4 sein (Default 1)
  Metriken fuer Rollen: dps, breakbar_damage, resurrects
Boss-spezifische Regeln ersetzen globale mit gleichem Namen; globale Mechanik-Regeln greifen nur, wenn die
Mechanik im Log vorkommt.
"""
import sys
import tomllib

KINDS = ("mechanic", "buff", "metric", "consumables")
RULE_KEYS = {"critical", "critical_below", "list_if_not_critical", "text_if_listed_critical",
             "text_if_listed_not_critical", "link", "professions", "compare", "min_total", "of", "roles", "not_roles"}
COMPARE = ("player", "total", "share")
ROLE_CRIT_KEYS = {"source", "name", "top", "bottom", "share_min", "share_max", "min_total"}
ADDITIVE_METRICS = ("dps", "breakbar_damage", "resurrects")      # nur diese gibt es als Gruppensumme


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
def _clean_roles(block, where, problems):
    out = {}
    for rname, r in block.items():
        w = f"{where}.role.{rname}"
        if not isinstance(r, dict) or not isinstance(r.get("criteria"), list) or not r["criteria"]:
            problems.append(f"{w}: 'criteria' (Liste) fehlt")
            continue
        bad = sorted(set(r) - {"criteria", "match", "description"})
        if bad:
            problems.append(f"{w}: unbekannte Schluessel {bad}")
        if r.get("match", "any") not in ("any", "all"):
            problems.append(f"{w}: match muss any oder all sein")
            continue
        ok = []
        for i, c in enumerate(r["criteria"]):
            cw = f"{w}.criteria[{i}]"
            if not isinstance(c, dict):
                problems.append(f"{cw}: muss eine Tabelle sein")
                continue
            bad = sorted(set(c) - ROLE_CRIT_KEYS)
            if bad:
                problems.append(f"{cw}: unbekannte Schluessel {bad}")
            if c.get("source") not in ("mechanic", "metric") or not c.get("name"):
                problems.append(f"{cw}: source (mechanic|metric) und name noetig")
                continue
            if c["source"] == "metric" and c["name"] not in ADDITIVE_METRICS:
                problems.append(f"{cw}: Metrik fuer Rollen nur {', '.join(ADDITIVE_METRICS)}")
                continue
            if not any(k in c for k in ("top", "bottom", "share_min", "share_max")):
                problems.append(f"{cw}: mindestens eine Bedingung (top, bottom, share_min, share_max) noetig")
                continue
            ok.append(c)
        if ok:
            out[rname] = {**r, "criteria": ok}
    return out


def _clean_rules(block, where, problems):
    """{kind: {name: rule}} aus einem TOML-Block (global oder boss.<key>)."""
    out = {}
    for kind, val in block.items():
        if kind == "role":
            out["role"] = _clean_roles(val, where, problems)
            continue
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
            src = rule.get("of", name)
            if kind == "metric" and src not in METRICS:
                problems.append(f"{where}.metric.{name}: unbekannte Metrik (erlaubt: {', '.join(METRICS)})")
                continue
            cmp_ = rule.get("compare", "player")
            if cmp_ not in COMPARE:
                problems.append(f"{where}.{kind}.{name}: compare muss {' | '.join(COMPARE)} sein")
                continue
            if cmp_ != "player" and kind not in ("mechanic", "metric"):
                problems.append(f"{where}.{kind}.{name}: compare = {cmp_} gibt es nur bei mechanic und metric")
                continue
            if cmp_ != "player" and kind == "metric" and src not in ADDITIVE_METRICS:
                problems.append(f"{where}.metric.{name}: compare = {cmp_} nur bei {', '.join(ADDITIVE_METRICS)}")
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
                    character=pl.get("name", ""), time="", times="", roles=pl.get("_roles", ""))
    ctx.update(extra or {})
    if not tmpl:
        return f"{label}: {_num(value)}"
    try:
        return tmpl.format_map(ctx)
    except (ValueError, IndexError, KeyError):
        return tmpl


def _applies(rule, pl, role_names=()):
    profs = rule.get("professions")
    if profs and pl.get("profession") not in profs:
        return False
    need, deny = rule.get("roles"), rule.get("not_roles")
    if need and not set(need) & set(role_names):
        return False
    if deny and set(deny) & set(role_names):
        return False
    return True


def _rank_stats(counts, own):
    """counts: Werte ALLER Spieler im Log (anonym), own: Wert dieses Spielers."""
    if not counts:
        return None
    total = sum(counts)
    return {"count": own, "total": total, "n": len(counts),
            "rank": 1 + sum(1 for c in counts if c > own),            # 1 = hoechster Wert
            "rank_bottom": 1 + sum(1 for c in counts if c < own),     # 1 = niedrigster Wert
            "share": round(100.0 * own / total, 1) if total > 0 else None}


def _rank_extra(st):
    if not st:
        return {}
    return {"rank": _num(st["rank"]), "rank_bottom": _num(st["rank_bottom"]), "total": _num(st["total"]),
            "share": _num(st["share"]) if st["share"] is not None else "", "player_count": _num(st["n"])}


def _criterion(c, mstats, xstats):
    """Liefert einen Beleg-Text, wenn das Kriterium zutrifft, sonst None."""
    st = (mstats if c["source"] == "mechanic" else xstats).get(c["name"])
    if not st or st["total"] < c.get("min_total", 1):
        return None
    if "top" in c and not (st["count"] > 0 and st["rank"] <= c["top"]):
        return None
    if "bottom" in c and not st["rank_bottom"] <= c["bottom"]:
        return None
    sh = st["share"]
    if "share_min" in c and (sh is None or sh < c["share_min"]):
        return None
    if "share_max" in c and (sh is None or sh > c["share_max"]):
        return None
    label = c["name"] if c["source"] == "mechanic" else METRIC_LABEL.get(c["name"], c["name"])
    parts = [_num(st["count"])]
    if sh is not None:
        parts.append(f"{_num(sh)} %")
    if "bottom" in c and "top" not in c:
        parts.append(f"Platz {st['rank_bottom']} von unten")
    else:
        parts.append(f"Platz {st['rank']} von {st['n']}")
    return f"{label}: " + ", ".join(parts)


# ----------------------------------------------------------------------------- Auswertung
def evaluate(slim, boss_key, rules):
    """Bewertet ein gekuerztes Log. Rueckgabe: Liste je Spieler:
    {account, character, profession, group, roles:[{name, why:[...]}], findings:[{kind,name,value,critical,text,link}],
     values:{mechanics, buffs, metrics, consumables, mechanic_stats:{name:{count,share,rank,...}}, metric_stats}}"""
    chars = slim.get("characters") or {}
    grp = slim.get("group") or {}
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
        metrics = {k: fn(pl, slim) for k, fn in METRICS.items()}

        # Rang/Anteil im Vergleich mit ALLEN Spielern (aus den anonymen Gruppenwerten)
        mstats = {}
        for name, g in (grp.get("mechanics") or {}).items():
            if g.get("counts"):
                mstats[name] = _rank_stats(g["counts"], len(events.get((acc, name), [])))
        xstats = {}
        for name in ADDITIVE_METRICS:
            vals, own = (grp.get("metric_values") or {}).get(name), metrics.get(name)
            if vals and own is not None:
                xstats[name] = _rank_stats(vals, own)

        # Rollen
        roles = []
        for rname, r in merged("role")[0].items():
            hits = [_criterion(c, mstats, xstats) for c in r["criteria"]]
            if (all(hits) if r.get("match") == "all" else any(hits)):
                roles.append({"name": rname, "why": [h for h in hits if h], "description": r.get("description", "")})
        role_names = [r["name"] for r in roles]
        plx = {**pl, "_roles": ", ".join(role_names)}

        def add(kind, name, rule, value, critical, label, extra=None):
            findings.append({"kind": kind, "name": name, "value": value, "critical": critical,
                             "text": _text(rule, critical, label, value, plx, extra), "link": rule.get("link")})

        # Mechaniken
        rs, scoped = merged("mechanic")
        for name, rule in rs.items():
            cmp_ = rule.get("compare", "player")
            src = rule.get("of", name)
            if cmp_ == "total" or (src not in present and name not in scoped) or not _applies(rule, pl, role_names):
                continue
            times = sorted(t for t in events.get((acc, src), []) if t is not None)
            count = len(times)
            extra = {"time": fmt_ms(times[0]) if times else "",
                     "times": ", ".join(fmt_ms(t) for t in times[:5]) + (" …" if len(times) > 5 else "")}
            extra.update(_rank_extra(mstats.get(src)))
            value = count
            if cmp_ == "share":
                g = (grp.get("mechanics") or {}).get(src)
                if not g or g["count"] <= 0 or g["count"] < rule.get("min_total", 1):
                    continue
                value = round(100.0 * count / g["count"], 1)
                extra.update(total=_num(g["count"]), players=_num(g["players"]), share=_num(value),
                             count=_num(count), player_count=_num(grp.get("player_count", 0)))
            crit = _flag(rule, value, default_crit=1 if cmp_ == "player" else None)
            if crit or (rule.get("list_if_not_critical") and count > 0):
                add("mechanic", name, rule, value, crit, name, extra)

        # Buffs (Phase 0)
        uptimes = {}
        for name, per_phase in (pl.get("buffUptimes") or {}).items():
            if per_phase:
                uptimes[name] = per_phase[0].get("uptime", 0)
        for name, rule in merged("buff")[0].items():
            if not _applies(rule, pl, role_names):
                continue
            v = uptimes.get(name, 0.0)
            crit = _flag(rule, v)
            if crit or rule.get("list_if_not_critical"):
                add("buff", name, rule, v, crit, name)

        # Metriken
        for name, rule in merged("metric")[0].items():
            src = rule.get("of", name)
            cmp_ = rule.get("compare", "player")
            v = metrics.get(src)
            if v is None or cmp_ == "total" or not _applies(rule, pl, role_names):
                continue
            extra = _rank_extra(xstats.get(src))
            if cmp_ == "share":
                tot = (grp.get("metrics") or {}).get(src)
                if not tot or tot <= 0 or tot < rule.get("min_total", 1):
                    continue
                extra.update(total=_num(tot), share=_num(round(100.0 * v / tot, 1)), count=_num(v),
                             player_count=_num(grp.get("player_count", 0)))
                v = round(100.0 * v / tot, 1)
            crit = _flag(rule, v)
            if crit or rule.get("list_if_not_critical"):
                add("metric", name, rule, v, crit, METRIC_LABEL.get(src, name), extra or None)

        # Consumables
        cons = sorted({c.get("name") for c in pl.get("consumables") or [] if c.get("name")})
        rule = merged("consumables")[0].get("")
        if rule and _applies(rule, pl, role_names):
            crit = _flag(rule, len(cons))
            if crit or rule.get("list_if_not_critical"):
                add("consumables", "", rule, len(cons), crit, "Consumables")

        order = {k: i for i, k in enumerate(KINDS)}
        findings.sort(key=lambda f: (not f["critical"], order[f["kind"]], f["name"]))
        out.append({
            "account": acc, "character": pl.get("name"), "profession": pl.get("profession"), "group": pl.get("group"),
            "roles": roles, "findings": findings,
            "values": {
                "mechanics": {n: len(t) for (a, n), t in sorted(events.items()) if a == acc},
                "buffs": dict(sorted(uptimes.items())),
                "metrics": {k: v for k, v in metrics.items() if v is not None},
                "consumables": cons,
                "mechanic_stats": {n: st for n, st in mstats.items() if st and st["count"] > 0},
                "metric_stats": xstats}})
    return out


def evaluate_group(slim, boss_key, rules):
    """Regeln mit compare = "total": Wert = Summe ueber alle Spieler im Log (auch Nicht-Whitelist).
    Rueckgabe: {"findings": [...], "values": {"player_count", "mechanics": {name: {count, players}}, "metrics": {...}}}"""
    grp = slim.get("group") or {}
    values = {"player_count": grp.get("player_count", 0), "mechanics": grp.get("mechanics") or {},
              "metrics": grp.get("metrics") or {}}
    findings = []
    if not grp:
        return {"findings": findings, "values": values}
    for kind in ("mechanic", "metric"):
        g = dict(rules["global"].get(kind) or {})
        b = (rules["boss"].get(str(boss_key)) or {}).get(kind) or {}
        scoped = set(b)
        g.update(b)
        for name, rule in g.items():
            if rule.get("compare") != "total":
                continue
            src = rule.get("of", name)
            if kind == "mechanic":
                info = values["mechanics"].get(src)
                if info is None and name not in scoped:
                    continue                     # globale Regel: nur wenn die Mechanik im Log vorkam
                v = (info or {}).get("count", 0)
                extra = {"players": _num((info or {}).get("players", 0))}
                label, dflt = name, 1
            else:
                v = values["metrics"].get(src)
                if v is None:
                    continue
                extra, label, dflt = {}, METRIC_LABEL.get(src, name), None
            extra.update(total=_num(v), player_count=_num(values["player_count"]))
            crit = _flag(rule, v, default_crit=dflt)
            if crit or (rule.get("list_if_not_critical") and v):
                findings.append({"kind": kind, "name": name, "value": v, "critical": crit, "link": rule.get("link"),
                                 "text": _text(rule, crit, label, v, {}, extra)})
    findings.sort(key=lambda f: (not f["critical"], f["kind"], f["name"]))
    return {"findings": findings, "values": values}


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
