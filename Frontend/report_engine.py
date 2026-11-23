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
  critical_above = X              auffaellig, wenn Wert >  X   (streng groesser, "uebersteigt")
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
  compare = "ratio" (nur mechanic)  Wert = Treffer des Spielers (of) / Anzahl Ereignisse der Mechanik(en) in per
      per = "Apply"  (oder Liste)   Nenner; per_scope = "total" (Default: ALLE Ereignisse der Mechanik im Log, auch mit
                                    NPC als Akteur) oder "player" (nur die des Spielers); min_per = N (Default 1):
                                    ist der Nenner kleiner/0, wird die Regel uebersprungen (keine Division durch 0)
                                    Platzhalter: {value}/{ratio} (2 Nachkommastellen), {count} (Zaehler), {per} (Nenner)
  of = ["A", "B"]                 nur bei mechanic: die Werte mehrerer Mechaniken werden ADDIERT und als Summe bewertet
                                  (player, total und share; {rank}/{rank_bottom} und {players} gibt es dann nicht;
                                  Rollen-Kriterien koennen nur einzelne Mechaniken vergleichen)
  roles = ["Tank"]                Regel nur fuer Spieler mit (mindestens) einer dieser Rollen
  not_roles = ["Tank"]            Regel NICHT fuer Spieler mit einer dieser Rollen
Platzhalter in Texten: {value} {count} {threshold} {rule} {player} {account} {character} {time} {times} {roles}
  zusaetzlich bei total/share: {total} (Gruppensumme) {players} (betroffene Spieler) {player_count} {share}
  Mechanik-Summen beliebiger Mechaniken im Text: {gruppe[Mechanikname]} = Summe ueber ALLE Spieler im Log,
  {eigene[Mechanikname]} = Wert des Spielers (Name wie in analysis_filter.toml; 0 = erfasst aber nicht aufgetreten,
  ? = nicht erfasst bzw. keine Gruppenwerte). Der Name darf kein ] oder } enthalten.
Rang-Platzhalter (wenn Gruppenwerte vorhanden): {rank} (Platz von oben, 1 = meiste) {rank_bottom} (von unten) {share} {total}

Rollen ([role."Name"], boss-spezifisch [boss."<ID>".role."Name"]) - aus dem Vergleich mit ALLEN Spielern im Log:
  match = "any"|"all"             Rolle, wenn mindestens ein / alle Kriterien zutreffen (Default any)
  description = "..."             optional, nur zur Erinnerung
  criteria = [ { source = "mechanic"|"metric", name = "...", <Bedingung(en)> }, ... ]
  Bedingungen eines Kriteriums (alle angegebenen muessen zutreffen):
    top = 3          Platz <= 3 von oben (Wert > 0)       bottom = 3     Platz <= 3 von unten (Nullen zaehlen mit)
    share_min = 30   Anteil an der Gruppensumme >= 30 %    share_max = 5  Anteil <= 5 %
    min_total = 4    Gruppensumme muss >= 4 sein (Default 1)
  value_min = 30 / value_max = 5   absoluter Wert (Anzahl bzw. bei generation: Ø Uptime/Stacks fuer die Gruppe)
  Metriken fuer Rollen: dps, breakbar_damage, resurrects
  source = "generation", name = "Quickness": wie viel Uptime (bzw. Stacks) der Spieler im Schnitt ALLEN Spielern im Log
  gibt (Summe seiner 'generated'-Anteile / Spielerzahl, Phase 0). Erfasst: Alacrity, Quickness, Might, Fury, Protection,
  Stability, Aegis, Resistance, Resolution, Regeneration, Swiftness, Vigor (GENERATION_BUFFS in extract_log.py).
Boss-spezifische Regeln ersetzen globale mit gleichem Namen; globale Mechanik-Regeln greifen nur, wenn die
Mechanik im Log vorkommt.
"""
import sys
import tomllib

KINDS = ("mechanic", "buff", "metric", "consumables")
RULE_KEYS = {"critical", "critical_below", "list_if_not_critical", "text_if_listed_critical",
             "text_if_listed_not_critical", "link", "professions", "compare", "min_total", "of", "roles", "not_roles",
             "critical_above", "per", "per_scope", "min_per"}
COMPARE = ("player", "total", "share", "ratio")
ROLE_CRIT_KEYS = {"source", "name", "top", "bottom", "share_min", "share_max", "min_total", "value_min", "value_max"}
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
            if c.get("source") not in ("mechanic", "metric", "generation") or not c.get("name"):
                problems.append(f"{cw}: source (mechanic|metric|generation) und name noetig")
                continue
            if c["source"] == "metric" and c["name"] not in ADDITIVE_METRICS:
                problems.append(f"{cw}: Metrik fuer Rollen nur {', '.join(ADDITIVE_METRICS)}")
                continue
            if not any(k in c for k in ("top", "bottom", "share_min", "share_max", "value_min", "value_max")):
                problems.append(f"{cw}: mindestens eine Bedingung (top, bottom, share_min, share_max, value_min, value_max) noetig")
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
            if kind == "mechanic" and isinstance(src, list):
                if not src or not all(isinstance(x, str) for x in src):
                    problems.append(f"{where}.mechanic.{name}: 'of' muss eine nicht leere Liste von Namen sein")
                    continue
            elif not isinstance(src, str):
                problems.append(f"{where}.{kind}.{name}: 'of' muss ein Name sein (Liste nur bei mechanic)")
                continue
            if kind == "metric" and src not in METRICS:
                problems.append(f"{where}.metric.{name}: unbekannte Metrik (erlaubt: {', '.join(METRICS)})")
                continue
            cmp_ = rule.get("compare", "player")
            if cmp_ == "ratio":
                per = rule.get("per")
                if kind != "mechanic":
                    problems.append(f"{where}.{kind}.{name}: compare = ratio gibt es nur bei mechanic")
                    continue
                if not (isinstance(per, str) or (isinstance(per, list) and per and all(isinstance(x, str) for x in per))):
                    problems.append(f"{where}.mechanic.{name}: compare = ratio braucht 'per' (Name oder Liste von Namen)")
                    continue
                if rule.get("per_scope", "total") not in ("total", "player"):
                    problems.append(f"{where}.mechanic.{name}: per_scope muss total oder player sein")
                    continue
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


class _Counts(dict):
    """Fuer {gruppe[Name]} / {eigene[Name]}. tracked: True = alle Mechaniken erfasst, Menge = nur diese, False = unbekannt."""
    def __init__(self, data, tracked):
        super().__init__(data or {})
        self._tracked = tracked

    def __missing__(self, key):
        return 0 if (self._tracked is True or (isinstance(self._tracked, (set, frozenset)) and key in self._tracked)) else "?"


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
    explicit = any(k in rule for k in ("critical_below", "critical_above"))
    c = rule.get("critical", default_crit if not explicit else None)
    cb, ca = rule.get("critical_below"), rule.get("critical_above")
    return ((c is not None and value >= c) or (cb is not None and value < cb)
            or (ca is not None and value > ca))


def _text(rule, critical, label, value, pl, extra=None):
    tmpl = rule.get("text_if_listed_critical" if critical else "text_if_listed_not_critical")
    thr = rule.get("critical") if rule.get("critical") is not None else rule.get("critical_below")
    ctx = _SafeDict(value=_num(value), count=_num(value), threshold=_num(thr) if thr is not None else "",
                    rule=label, player=pl.get("account", ""), account=pl.get("account", ""),
                    character=pl.get("name", ""), time="", times="", roles=pl.get("_roles", ""))
    ctx.update(extra or {})
    tr = pl.get("_tracked", False)
    ctx["gruppe"] = _Counts(pl.get("_group"), tr if pl.get("_group") is not None else False)
    ctx["eigene"] = _Counts(pl.get("_own"), tr)
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


def _criterion(c, mstats, xstats, gstats):
    """Liefert einen Beleg-Text, wenn das Kriterium zutrifft, sonst None."""
    st = {"mechanic": mstats, "metric": xstats, "generation": gstats}[c["source"]].get(c["name"])
    if not st or st["total"] < c.get("min_total", 1):
        return None
    if "top" in c and not (st["count"] > 0 and st["rank"] <= c["top"]):
        return None
    if "bottom" in c and not st["rank_bottom"] <= c["bottom"]:
        return None
    if "value_min" in c and not st["count"] >= c["value_min"]:
        return None
    if "value_max" in c and not st["count"] <= c["value_max"]:
        return None
    sh = st["share"]
    if "share_min" in c and (sh is None or sh < c["share_min"]):
        return None
    if "share_max" in c and (sh is None or sh > c["share_max"]):
        return None
    label = {"mechanic": c["name"], "generation": f"{c['name']} an die Gruppe"}.get(
        c["source"], METRIC_LABEL.get(c["name"], c["name"]))
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

    tracked = False                                  # welche Mechaniken die Gruppenwerte abdecken
    if "tracked" in grp:
        tracked = True if grp["tracked"] is None else frozenset(grp["tracked"])

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

        gstats = {}                                  # Boon-Erzeugung: Ø Uptime/Stacks, die der Spieler der Gruppe gibt
        for name, vals in (grp.get("generation") or {}).items():
            own = (pl.get("generates") or {}).get(name)
            if vals and own is not None:
                gstats[name] = _rank_stats(vals, own)

        # Rollen
        roles = []
        for rname, r in merged("role")[0].items():
            hits = [_criterion(c, mstats, xstats, gstats) for c in r["criteria"]]
            if (all(hits) if r.get("match") == "all" else any(hits)):
                roles.append({"name": rname, "why": [h for h in hits if h], "description": r.get("description", "")})
        role_names = [r["name"] for r in roles]
        plx = {**pl, "_roles": ", ".join(role_names), "_tracked": tracked,
               "_group": {n: g["count"] for n, g in (grp.get("mechanics") or {}).items()} if grp else None,
               "_own": {n: len(t) for (a, n), t in events.items() if a == acc}}

        def add(kind, name, rule, value, critical, label, extra=None):
            findings.append({"kind": kind, "name": name, "value": value, "critical": critical,
                             "text": _text(rule, critical, label, value, plx, extra), "link": rule.get("link")})

        # Mechaniken
        rs, scoped = merged("mechanic")
        for name, rule in rs.items():
            cmp_ = rule.get("compare", "player")
            src = rule.get("of", name)
            srcs = [src] if isinstance(src, str) else list(src)
            if cmp_ == "total" or (not any(x in present for x in srcs) and name not in scoped) \
                    or not _applies(rule, pl, role_names):
                continue
            times = sorted(t for x in srcs for t in events.get((acc, x), []) if t is not None)
            count = len(times)
            extra = {"time": fmt_ms(times[0]) if times else "",
                     "times": ", ".join(fmt_ms(t) for t in times[:5]) + (" …" if len(times) > 5 else "")}
            gs = [(grp.get("mechanics") or {}).get(x) for x in srcs]
            gtotal = sum(g["count"] for g in gs if g)
            if len(srcs) == 1:
                extra.update(_rank_extra(mstats.get(srcs[0])))
            else:                                   # Summe mehrerer Mechaniken: kein Rang, aber Gesamtsumme/Anteil
                extra.update(rank="", rank_bottom="", players="",
                             total=_num(gtotal) if gtotal else "",
                             share=_num(round(100.0 * count / gtotal, 1)) if gtotal > 0 else "")
            value = count
            if cmp_ == "ratio":
                pers = [rule["per"]] if isinstance(rule["per"], str) else list(rule["per"])
                if rule.get("per_scope", "total") == "player":
                    denom = sum(len(events.get((acc, x), [])) for x in pers)
                else:
                    denom = 0
                    for x in pers:
                        g = (grp.get("mechanics") or {}).get(x) or {}
                        denom += g.get("events", g.get("count", 0))
                if denom <= 0 or denom < rule.get("min_per", 1):
                    continue                          # kein Nenner (z. B. 0 Applies) -> Regel entfaellt
                value = count / denom
                extra.update(value=f"{value:.2f}", ratio=f"{value:.2f}", count=_num(count), per=_num(denom))
            if cmp_ == "share":
                if gtotal <= 0 or gtotal < rule.get("min_total", 1):
                    continue
                value = round(100.0 * count / gtotal, 1)
                extra.update(total=_num(gtotal), share=_num(value), count=_num(count),
                             player_count=_num(grp.get("player_count", 0)))
                if len(srcs) == 1:
                    extra["players"] = _num(gs[0]["players"])
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
                "generation": {n: st["count"] for n, st in gstats.items() if st["count"] > 0},
                "generation_stats": {n: st for n, st in gstats.items() if st["count"] > 0},
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
                srcs = [src] if isinstance(src, str) else list(src)
                infos = [values["mechanics"].get(x) for x in srcs]
                if not any(infos) and name not in scoped:
                    continue                     # globale Regel: nur wenn die Mechanik im Log vorkam
                v = sum(i["count"] for i in infos if i)
                extra = {"players": _num(infos[0]["players"] if infos[0] else 0) if len(srcs) == 1 else ""}
                label, dflt = name, 1
            else:
                v = values["metrics"].get(src)
                if v is None:
                    continue
                extra, label, dflt = {}, METRIC_LABEL.get(src, name), None
            extra.update(total=_num(v), player_count=_num(values["player_count"]))
            crit = _flag(rule, v, default_crit=dflt)
            if crit or (rule.get("list_if_not_critical") and v):
                gx = {"_group": {n: i["count"] for n, i in values["mechanics"].items()},
                      "_tracked": (True if grp.get("tracked") is None else frozenset(grp["tracked"]))
                      if "tracked" in grp else False}
                findings.append({"kind": kind, "name": name, "value": v, "critical": crit, "link": rule.get("link"),
                                 "text": _text(rule, crit, label, v, gx, extra)})
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
