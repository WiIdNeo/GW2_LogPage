"""report_engine.py - bewertet gekuerzte EI-JSONs (log_analysis.data, erzeugt von extract_log.py) nach Regeln aus
report_rules.toml. Reine Logik ohne HTML und ohne DB, damit sie auch fuer Graphen/Statistiken nutzbar ist.
Liegt neben build_site.py und hat keine Abhaengigkeit zum Backend.

Regelarten (jeweils unter [kind."Name"], boss-spezifisch unter [boss."<Boss-ID, CM negativ>".kind."Name"]):
  mechanic      Anzahl der Ereignisse des Spielers (Name wie in analysis_filter.toml)
  buff          Uptime in Phase 0 (Full Fight) in % bzw. Ø Stacks; Name wie in buffs_info
  metric        berechnete Werte: dps, condi_dps, breakbar_damage, flanking_percent, resurrects, stack_dist,
                skill_cast_uptime
  consumables   [consumables] ohne Namen: Anzahl verschiedener Consumables (Food/Utility/...), ohne BLACKLISTED_FOODS

Schluessel einer Regel:
  critical = X                    auffaellig, wenn Wert >= X   (mechanic: Default 1)
  critical_below = X              auffaellig, wenn Wert <  X
  critical_above = X              auffaellig, wenn Wert >  X   (streng groesser, "uebersteigt")
  list_if_not_critical = true     auch unauffaellige Werte auflisten (Mechaniken nur bei Anzahl > 0)
  text_if_listed_critical         Text, wenn auffaellig
  text_if_listed_not_critical     Text, wenn nur informativ gelistet
  link                            Link zur Erklaerung (wird als "Erklärung" angehaengt)
  professions = ["..."]           Regel nur fuer diese Professionen/Elite-Spezialisierungen
  compare = "player"|"total"|"share"   (nur mechanic und metric; Metrik nur dps, condi_dps, breakbar_damage, resurrects)
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
  chart = true                    (mechanic, buff, metric) Wert jedes Logs fuer den Verlaufs-Chart auf der Spielerseite
                                  merken - unabhaengig davon, ob er auffaellig ist. Bei compare = "total" ohne Wirkung.
  roles = ["Tank", "Heal"]        Regel nur fuer Spieler, die ALLE diese Rollen haben (UND-verknuepft)
  not_roles = ["Tank", "Heal"]    Regel NICHT fuer Spieler, die auch nur EINE dieser Rollen haben (ODER-verknuepft, "weder noch")
Platzhalter in Texten: {value} {count} {threshold} {rule} {player} {account} {character} {time} {times} {roles}
  zusaetzlich bei total/share: {total} (Gruppensumme) {players} (betroffene Spieler) {player_count} {share}
  Mechanik-Summen beliebiger Mechaniken im Text: {gruppe[Mechanikname]} = Summe ueber ALLE Spieler im Log,
  {eigene[Mechanikname]} = Wert des Spielers (Name wie in analysis_filter.toml; 0 = erfasst aber nicht aufgetreten,
  ? = nicht erfasst bzw. keine Gruppenwerte). Der Name darf kein ] oder } enthalten.
Rang-Platzhalter (wenn Gruppenwerte vorhanden): {rank} (Platz von oben, 1 = meiste) {rank_bottom} (von unten) {share} {total}

Formeln: critical, critical_below und critical_above duerfen statt einer Zahl ein TEXT mit einer Rechnung sein, die je Spieler
  ausgewertet wird, z. B.  critical_below = "25000 - 25000 * 0.2 * {Mechanic A}"
  Erlaubt: + - * / // % ** ( ), Zahlen, min() max() abs() round() floor() ceil() sqrt(), Vergleiche (< <= > >= == !=),
  and/or/not und  "A if Bedingung else B".  Werte kommen als {Platzhalter} in geschweiften Klammern (Name darf Leerzeichen enthalten):
    {Name} oder {eigene[Name]}   Anzahl der Mechanik beim Spieler (0, wenn nicht aufgetreten)
    {gruppe[Name]}               Summe der Mechanik ueber ALLE Spieler im Log
    {buff[Name]}                 Uptime/Stacks des Buffs beim Spieler (Phase 0), 0 wenn nicht vorhanden
    {metric[dps]}                dps, condi_dps, breakbar_damage, flanking_percent, resurrects, stack_dist des Spielers
                                 (fehlt der Wert: Regel entfaellt)
    {schnitt[skill_cast_uptime]} Durchschnitt der Metrik ueber ALLE Spieler im Log, die einen Wert haben (auch Randoms);
                                 fehlen die Gruppenwerte (alte Logs): Regel entfaellt. Fuer Metriken mit Gruppenwerten:
                                 dps, condi_dps, breakbar_damage, resurrects, skill_cast_uptime
    {spieler}                    Anzahl Spieler im Log
  Bei compare = "total" (Gruppenregel) sind {Name} und {gruppe[Name]} die Gruppensumme und {metric[..]} der Gruppenwert.
  Laesst sich eine Formel nicht berechnen (Division durch 0, fehlender Wert), entfaellt die Regel fuer diesen Spieler.
  Den berechneten Schwellwert zeigt {threshold} im Text.
  roles / not_roles / professions duerfen auch ein einzelner Text sein (wird zur Liste).

Rollen ([role."Name"], boss-spezifisch [boss."<ID>".role."Name"]) - aus dem Vergleich mit ALLEN Spielern im Log:
  match = "any"|"all"             Rolle, wenn mindestens ein / alle Kriterien zutreffen (Default any)
  description = "..."             optional, nur zur Erinnerung
  not_roles = ["Cannon"]          Rolle entfaellt, wenn der Spieler eine dieser Rollen hat (diese muss WEITER OBEN in der Datei stehen)
  criteria = [ { source = "mechanic"|"metric", name = "...", <Bedingung(en)> }, ... ]
             zusaetzlich in einem Kriterium: roles = [..] (hat mindestens eine dieser Rollen) / not_roles = [..] (hat keine davon);
             ohne source/name ist es ein reines Rollen-Kriterium, z. B. { not_roles = ["BoonDPS", "Heal"] }.
             Es zaehlen nur Rollen, die WEITER OBEN definiert sind (globale Rollen stehen vor den Boss-Rollen).
  Bedingungen eines Kriteriums (alle angegebenen muessen zutreffen):
    top = 3          Platz <= 3 von oben (Wert > 0)       bottom = 3     Platz <= 3 von unten (Nullen zaehlen mit)
    share_min = 30   Anteil an der Gruppensumme >= 30 %    share_max = 5  Anteil <= 5 %
    min_total = 4    Gruppensumme muss >= 4 sein (Default 1)
  value_min = 30 / value_max = 5   absoluter Wert (Anzahl bzw. bei generation: Ø Uptime/Stacks fuer die Gruppe)
  source = "target", name = "<NPC-ID>": Wert des Spielers auf dieses Ziel (Phase 0, alle Ziele mit der ID zusammen).
  stat = "dps" (Default) | "cc" (Anzahl ausgeteilter Crowd-Control-Effekte) | "cc_duration" (CC-Dauer in Sekunden).
  Nur value_min / value_max. Fehlt der CC-Wert in den Daten (EI-Version / Log vor Schema 7), trifft es NICHT zu. Kommt das Ziel im Log nicht vor (nicht exportiert: in analysis_filter.toml
  unter targets oder damage_targets aufnehmen), trifft das Kriterium NICHT zu.
  source = "incoming", name = "<Quelle>": Schaden, den der Spieler durch diese Quelle erhalten hat (Phase 0, Name wie
  in incoming_damage, z. B. "Soul Feast"). value_min / value_max = Schaden absolut, share_min / share_max = Anteil
  in % an ALLEM Schaden, den der Spieler erhalten hat. Nur die Top-N-Quellen je Spieler werden exportiert
  (incoming_damage in analysis_filter.toml, Default 10); fehlt die Quelle dort, zaehlt sie als 0. Ohne
  incoming_damage im Log (abgeschaltet / alte Logs) trifft das Kriterium NICHT zu.
  Metriken fuer Rollen: dps, condi_dps, breakbar_damage, resurrects (fehlen die Gruppenwerte - Logs vor Schema 6 -,
  funktionieren nur value_min / value_max)
  source = "generation", name = "Quickness": wie viel Uptime (bzw. Stacks) der Spieler im Schnitt ALLEN Spielern im Log
  gibt (Summe seiner 'generated'-Anteile / Spielerzahl, Phase 0). Erfasst: Alacrity, Quickness, Might, Fury, Protection,
  Stability, Aegis, Resistance, Resolution, Regeneration, Swiftness, Vigor (GENERATION_BUFFS in extract_log.py).
Boss-spezifische Regeln ersetzen globale mit gleichem Namen; globale Mechanik-Regeln greifen nur, wenn die
Mechanik im Log vorkommt.
"""
import ast
import functools
import math
import operator
import re
import sys
import tomllib

KINDS = ("mechanic", "buff", "metric", "consumables")
RULE_KEYS = {"critical", "critical_below", "list_if_not_critical", "text_if_listed_critical",
             "text_if_listed_not_critical", "link", "professions", "compare", "min_total", "of", "roles", "not_roles",
             "critical_above", "per", "per_scope", "min_per", "chart"}
COMPARE = ("player", "total", "share", "ratio")
ROLE_CRIT_KEYS = {"source", "name", "top", "bottom", "share_min", "share_max", "min_total", "value_min", "value_max",
                  "roles", "not_roles", "stat"}
TARGET_STATS = {"dps": "Schaden auf", "cc": "CC auf", "cc_duration": "CC-Dauer (s) auf"}   # stat bei source = target
ADDITIVE_METRICS = ("dps", "condi_dps", "breakbar_damage", "resurrects")      # nur diese gibt es als Gruppensumme

# Consumables, die bei [consumables] NICHT mitgezaehlt werden (Name wie in buffs_info, z. B. "Potion of Slaying ...")
BLACKLISTED_FOODS = {
}


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


def _stats_all(pl, key):
    """Wert aus statsAll (Phase 0), z. B. stackDist. None, wenn nicht exportiert (Logs vor Schema 6)."""
    v = ((pl.get("stats_all") or [{}])[0] or {}).get(key)
    return round(v, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


METRICS = {
    "dps": lambda pl, slim: _total(pl, "dps"),
    "condi_dps": lambda pl, slim: _total(pl, "condiDps"),
    "breakbar_damage": lambda pl, slim: _total(pl, "breakbarDamage"),
    "flanking_percent": _flanking_percent,
    "resurrects": lambda pl, slim: ((pl.get("support") or [{}])[0] or {}).get("resurrects"),
    "stack_dist": lambda pl, slim: _stats_all(pl, "stackDist"),
    "skill_cast_uptime": lambda pl, slim: _stats_all(pl, "skillCastUptime"),
}
METRIC_LABEL = {"dps": "DPS (Hauptziele)", "condi_dps": "Condi-DPS (Hauptziele)", "breakbar_damage": "Breakbar-Schaden",
                "flanking_percent": "Flankenrate %", "resurrects": "Wiederbelebungen",
                "stack_dist": "Abstand zum Squadzentrum", "skill_cast_uptime": "Animationsuptime %"}


# ----------------------------------------------------------------------------- Formeln (sicherer Ausdrucks-Auswerter)
THRESH_KEYS = ("critical", "critical_below", "critical_above")
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_TOKEN = re.compile(r"\{([^{}]+)\}")
_TOKEN_KIND = re.compile(r"^(\w+)\[(.+)\]$")
_TOKEN_KINDS = ("eigene", "gruppe", "buff", "metric", "schnitt")


def _check_expr(node, idents):
    t = type(node)
    if t is ast.Constant:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("nur Zahlen erlaubt")
    elif t is ast.Name:
        if node.id not in idents:
            raise ValueError(f"'{node.id}' ist kein gueltiger Wert - Mechaniken und Werte als {{Name}} in geschweiften Klammern schreiben")
    elif t is ast.BinOp and type(node.op) in _BIN:
        _check_expr(node.left, idents); _check_expr(node.right, idents)
    elif t is ast.UnaryOp and type(node.op) in (ast.USub, ast.UAdd, ast.Not):
        _check_expr(node.operand, idents)
    elif t is ast.IfExp:
        for n in (node.test, node.body, node.orelse):
            _check_expr(n, idents)
    elif t is ast.Compare and all(type(o) in _CMP for o in node.ops):
        for n in [node.left, *node.comparators]:
            _check_expr(n, idents)
    elif t is ast.BoolOp:
        for n in node.values:
            _check_expr(n, idents)
    elif t is ast.Call and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        if not node.args:
            raise ValueError(f"{node.func.id}() braucht Argumente")
        for n in node.args:
            _check_expr(n, idents)
    else:
        raise ValueError(f"nicht erlaubtes Element ({t.__name__})")


@functools.lru_cache(maxsize=512)
def compile_expr(src):
    """Text -> (AST, {Bezeichner: Platzhalter}). ValueError bei Syntaxfehlern oder unerlaubten Elementen."""
    toks = {}

    def sub(m):
        ident = f"v{len(toks)}"
        toks[ident] = m.group(1).strip()
        return ident
    for tok in _TOKEN.findall(src):
        km = _TOKEN_KIND.match(tok.strip())
        if km and km.group(1) not in _TOKEN_KINDS:
            raise ValueError(f"unbekannter Platzhalter '{{{tok}}}' (erlaubt: eigene[..], gruppe[..], buff[..], metric[..], schnitt[..])")
        if km and km.group(1) in ("metric", "schnitt") and km.group(2) not in METRICS:
            raise ValueError(f"unbekannte Metrik '{km.group(2)}' (erlaubt: {', '.join(METRICS)})")
    py = _TOKEN.sub(sub, src).strip()
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError:
        raise ValueError(f"Syntaxfehler in '{src}' - Mechaniknamen mit Leerzeichen muessen als {{Name}} in geschweiften Klammern stehen")
    _check_expr(tree.body, set(toks))
    return tree.body, toks


def _eval_node(node, vals):
    t = type(node)
    if t is ast.Constant:
        return node.value
    if t is ast.Name:
        return vals[node.id]
    if t is ast.BinOp:
        l, r = _eval_node(node.left, vals), _eval_node(node.right, vals)
        if isinstance(node.op, ast.Pow) and abs(r) > 100:
            raise ArithmeticError("Exponent zu gross")
        return _BIN[type(node.op)](l, r)
    if t is ast.UnaryOp:
        v = _eval_node(node.operand, vals)
        return -v if isinstance(node.op, ast.USub) else (+v if isinstance(node.op, ast.UAdd) else (not v))
    if t is ast.IfExp:
        return _eval_node(node.body if _eval_node(node.test, vals) else node.orelse, vals)
    if t is ast.Compare:
        left = _eval_node(node.left, vals)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, vals)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if t is ast.BoolOp:
        res = isinstance(node.op, ast.And)
        for v in node.values:
            val = _eval_node(v, vals)
            if isinstance(node.op, ast.And) and not val:
                return val
            if isinstance(node.op, ast.Or) and val:
                return val
            res = val
        return res
    return _FUNCS[node.func.id](*[_eval_node(a, vals) for a in node.args])       # ast.Call


class _Env:
    """Werte, die in Formeln als {Platzhalter} zur Verfuegung stehen."""
    def __init__(self, own, group, buffs, metrics, players, metric_values=None):
        self.own, self.group, self.buffs, self.metrics, self.players = own, group, buffs, metrics, players
        self.metric_values = metric_values or {}           # Werte ALLER Spieler je Metrik (anonym) -> {schnitt[..]}

    def value(self, tok):
        if tok == "spieler":
            return self.players or 0
        km = _TOKEN_KIND.match(tok)
        kind, name = km.groups() if km else ("eigene", tok)
        if kind == "metric":
            return self.metrics.get(name)                  # None -> Regel entfaellt
        if kind == "schnitt":                              # Summe aller Spieler mit Wert / deren Anzahl
            vals = [v for v in self.metric_values.get(name) or [] if v is not None]
            return sum(vals) / len(vals) if vals else None
        return {"eigene": self.own, "gruppe": self.group, "buff": self.buffs}[kind].get(name, 0)


def _eval_expr(src, env):
    """Berechnet eine Formel; None, wenn sie sich nicht berechnen laesst (fehlender Wert, Division durch 0 ...)."""
    body, toks = compile_expr(src)
    vals = {ident: env.value(tok) for ident, tok in toks.items()}
    if any(v is None for v in vals.values()):
        return None
    try:
        r = _eval_node(body, vals)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return float(r) if isinstance(r, (int, float)) and r == r else None


def _resolve(rule, env):
    """Ersetzt Formeln in critical/critical_below/critical_above durch Zahlen. None = nicht berechenbar -> Regel entfaellt."""
    out = rule
    for k in THRESH_KEYS:
        v = rule.get(k)
        if isinstance(v, str):
            r = _eval_expr(v, env)
            if r is None:
                return None
            if out is rule:
                out = dict(rule)
            out[k] = r
    return out


# ----------------------------------------------------------------------------- Regeln laden
def _clean_roles(block, where, problems):
    out = {}
    for rname, r in block.items():
        w = f"{where}.role.{rname}"
        if not isinstance(r, dict) or not isinstance(r.get("criteria"), list) or not r["criteria"]:
            problems.append(f"{w}: 'criteria' (Liste) fehlt")
            continue
        bad = sorted(set(r) - {"criteria", "match", "description", "not_roles"})
        if bad:
            problems.append(f"{w}: unbekannte Schluessel {bad}")
        nr = r.get("not_roles", [])
        if isinstance(nr, str):
            r = {**r, "not_roles": [nr]}
            nr = r["not_roles"]
        if not (isinstance(nr, list) and all(isinstance(x, str) for x in nr)):
            problems.append(f"{w}: not_roles muss eine Liste von Rollennamen sein")
            continue
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
            c = dict(c)
            for k in ("roles", "not_roles"):
                if isinstance(c.get(k), str):
                    c[k] = [c[k]]
            if any(k in c and not (isinstance(c[k], list) and all(isinstance(x, str) for x in c[k])) for k in ("roles", "not_roles")):
                problems.append(f"{cw}: roles/not_roles muessen eine Liste von Rollennamen sein")
                continue
            if "source" not in c and "name" not in c:       # reines Rollen-Kriterium: nur roles / not_roles
                if c.get("roles") or c.get("not_roles"):
                    ok.append(c)
                else:
                    problems.append(f"{cw}: source + name (mit Bedingung) oder roles/not_roles noetig")
                continue
            if c.get("source") == "target":
                tid = str(c.get("name", "")).strip()
                if not tid.lstrip("-").isdigit():
                    problems.append(f"{cw}: bei source = target ist name die NPC-ID des Ziels (Zahl)")
                    continue
                bad = [k for k in ("top", "bottom", "share_min", "share_max", "min_total") if k in c]
                if bad:
                    problems.append(f"{cw}: bei source = target gibt es nur value_min / value_max (nicht {bad})")
                    continue
                if c.get("stat", "dps") not in TARGET_STATS:
                    problems.append(f"{cw}: stat muss {' | '.join(TARGET_STATS)} sein")
                    continue
                c["name"] = tid
            elif "stat" in c:
                problems.append(f"{cw}: stat gibt es nur bei source = target")
                continue
            if c.get("source") == "incoming":
                if not isinstance(c.get("name"), str) or not c["name"].strip():
                    problems.append(f"{cw}: bei source = incoming ist name der Name der Schadensquelle (Text)")
                    continue
                bad = [k for k in ("top", "bottom", "min_total") if k in c]
                if bad:
                    problems.append(f"{cw}: bei source = incoming gibt es nur value_min/value_max/share_min/share_max (nicht {bad})")
                    continue
            if c.get("source") not in ("mechanic", "metric", "generation", "target", "incoming") or not c.get("name"):
                problems.append(f"{cw}: source (mechanic|metric|generation|target|incoming) und name noetig")
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
            rule = dict(rule)
            ok = True
            for k in ("roles", "not_roles", "professions"):
                if isinstance(rule.get(k), str):
                    rule[k] = [rule[k]]                     # "Tank" -> ["Tank"]
                elif k in rule and not (isinstance(rule[k], list) and all(isinstance(x, str) for x in rule[k])):
                    problems.append(f"{where}.{kind}.{name}: {k} muss eine Liste von Namen sein")
                    ok = False
            for k in THRESH_KEYS:
                v = rule.get(k)
                if k not in rule:
                    continue
                if isinstance(v, str):
                    try:
                        compile_expr(v)
                    except ValueError as e:
                        problems.append(f"{where}.{kind}.{name}.{k}: {e}")
                        ok = False
                elif isinstance(v, bool) or not isinstance(v, (int, float)):
                    problems.append(f"{where}.{kind}.{name}.{k}: muss eine Zahl oder eine Formel (Text) sein")
                    ok = False
            if "chart" in rule and (not isinstance(rule["chart"], bool) or kind == "consumables"):
                problems.append(f"{where}.{kind}.{name}: chart muss true/false sein (nicht bei consumables)")
                ok = False
            if not ok:
                continue
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


def _chart_point(rule, value, default_crit=None):
    """Wert + (aufgeloester) Schwellwert fuer den Verlaufs-Chart."""
    thr = next((rule[k] for k in THRESH_KEYS if isinstance(rule.get(k), (int, float))
                and not isinstance(rule.get(k), bool)), None)
    if thr is None and not any(k in rule for k in THRESH_KEYS):
        thr = default_crit
    return {"value": value, "threshold": thr}


def eval_formula(src, own=None, buffs=None, metrics=None):
    """Formel ausserhalb einer Regel berechnen (z. B. DPS-Faktoren in charts.toml). {Name}/{eigene[..]} = eigene
    Mechanik-Anzahl, {buff[..]}, {metric[..]}; Gruppenwerte gibt es hier nicht. None = nicht berechenbar."""
    return _eval_expr(src, _Env(own or {}, {}, buffs or {}, metrics or {}, 0))


def _text(rule, critical, label, value, pl, extra=None):
    tmpl = rule.get("text_if_listed_critical" if critical else "text_if_listed_not_critical")
    thr = next((rule[k] for k in ("critical", "critical_below", "critical_above") if rule.get(k) is not None), None)
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
    if need and not set(need) <= set(role_names):           # roles: UND - der Spieler braucht ALLE genannten Rollen
        return False
    if deny and set(deny) & set(role_names):                # not_roles: ODER (weder noch) - keine einzige darf zutreffen
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


def _incoming_criterion(c, inc):
    """source = incoming: erhaltener Schaden durch eine Quelle, Anteil am eigenen Gesamtschaden. None = trifft nicht zu."""
    if not isinstance(inc, dict) or "sources" not in inc:
        return None
    dmg = sum(x.get("damage") or 0 for x in inc.get("sources") or [] if x.get("name") == c["name"])
    total = inc.get("total") or 0
    share = round(100.0 * dmg / total, 1) if total else 0.0
    if ("value_min" in c and not dmg >= c["value_min"]) or ("value_max" in c and not dmg <= c["value_max"]) \
            or ("share_min" in c and not share >= c["share_min"]) or ("share_max" in c and not share <= c["share_max"]):
        return None
    return f"Schaden durch {c['name']}: {_num(dmg)} ({_num(share)} % des erhaltenen Schadens)"


def _target_criterion(c, tstats):
    """source = target: eigener Wert (dps / cc / cc_duration) auf alle Ziele mit dieser NPC-ID. None = trifft nicht zu."""
    t = (tstats or {}).get(c["name"])
    stat = c.get("stat", "dps")
    v = t.get(stat) if t else None
    if v is None:                                   # Ziel nicht im Log bzw. Wert nicht erfasst
        return None
    if ("value_min" in c and not v >= c["value_min"]) or ("value_max" in c and not v <= c["value_max"]):
        return None
    return f"{TARGET_STATS[stat]} {t['label']}: {_num(v)}"


def _criterion(c, mstats, xstats, gstats, found=frozenset(), tstats=None, incoming=None):
    """Liefert einen Beleg-Text, wenn das Kriterium zutrifft, sonst None. found = bereits vergebene Rollen des Spielers."""
    if c.get("roles") and not set(c["roles"]) & found:
        return None
    if c.get("not_roles") and set(c["not_roles"]) & found:
        return None
    if "source" not in c:                                   # reines Rollen-Kriterium
        parts = []
        if c.get("roles"):
            parts.append("hat " + ", ".join(sorted(set(c["roles"]) & found)))
        if c.get("not_roles"):
            parts.append("hat keine der Rollen " + ", ".join(c["not_roles"]))
        return "; ".join(parts)
    if c["source"] == "incoming":
        return _incoming_criterion(c, incoming)
    if c["source"] == "target":
        return _target_criterion(c, tstats)
    st = {"mechanic": mstats, "metric": xstats, "generation": gstats, "target": tstats or {}}[c["source"]].get(c["name"])
    if st and st.get("partial"):                           # nur eigener Wert bekannt (keine Gruppenwerte)
        if any(k in c for k in ("top", "bottom", "share_min", "share_max", "min_total")):
            return None
        if ("value_min" in c and not st["count"] >= c["value_min"]) or ("value_max" in c and not st["count"] <= c["value_max"]):
            return None
        return f"{st.get('label') or METRIC_LABEL.get(c['name'], c['name'])}: {_num(st['count'])}"
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
     values:{mechanics, buffs, metrics, consumables, mechanic_stats:{name:{count,share,rank,...}}, metric_stats,
             charts:{Regelname: {value, threshold}}}}"""
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
        charts = {}                                  # Regelname -> {value, threshold} fuer Verlaufs-Charts
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

        # Rollen (fehlen Gruppenwerte einer Metrik, z. B. condi_dps in alten Logs: nur eigener Wert fuer value_min/max)
        rxstats = dict(xstats)
        for name in ADDITIVE_METRICS:
            if name not in rxstats and metrics.get(name) is not None:
                rxstats[name] = {"count": metrics[name], "partial": True}
        tstats = {}                                  # je Ziel-NPC-ID (Phase 0, nur eigener Wert): dps, cc, cc_duration
        for t in slim.get("targets") or []:
            tid = str(t.get("id"))
            e = (pl.get("targets") or {}).get(str(t.get("index"))) or {}
            d = ((e.get("dps") or [{}])[0] or {}).get("dps") or 0
            sx = (e.get("stats") or [{}])[0] or {}
            st = tstats.setdefault(tid, {"dps": 0, "cc": 0, "cc_duration": 0, "label": t.get("name") or tid})
            st["dps"] += d
            for k, src, f in (("cc", "appliedCrowdControl", 1), ("cc_duration", "appliedCrowdControlDuration", 1000)):
                if st[k] is not None and isinstance(sx.get(src), (int, float)):
                    st[k] += sx[src] / f
                else:
                    st[k] = None                     # nicht erfasst -> Kriterium trifft nicht zu
        roles = []
        for rname, r in merged("role")[0].items():
            if set(r.get("not_roles") or []) & {x["name"] for x in roles}:
                continue                              # Rolle entfaellt, wenn der Spieler eine der not_roles (weiter oben definiert) hat
            hits = [_criterion(c, mstats, rxstats, gstats, {x["name"] for x in roles}, tstats, pl.get("incoming_damage"))
                    for c in r["criteria"]]
            if (all(hits) if r.get("match") == "all" else any(hits)):
                roles.append({"name": rname, "why": [h for h in hits if h], "description": r.get("description", "")})
        role_names = [r["name"] for r in roles]
        plx = {**pl, "_roles": ", ".join(role_names), "_tracked": tracked,
               "_group": {n: g["count"] for n, g in (grp.get("mechanics") or {}).items()} if grp else None,
               "_own": {n: len(t) for (a, n), t in events.items() if a == acc}}

        def add(kind, name, rule, value, critical, label, extra=None):
            findings.append({"kind": kind, "name": name, "value": value, "critical": critical, "label": label,
                             "text": _text(rule, critical, label, value, plx, extra), "link": rule.get("link")})

        uptimes = {}                                 # Buff-Uptimes Phase 0 (auch fuer Formeln: {buff[Name]})
        for name, per_phase in (pl.get("buffUptimes") or {}).items():
            if per_phase:
                uptimes[name] = per_phase[0].get("uptime", 0)
        env = _Env(plx["_own"], plx["_group"] or {}, uptimes, {k: v for k, v in metrics.items() if v is not None},
                   grp.get("player_count", 0), grp.get("metric_values"))

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
            rule = _resolve(rule, env)
            if rule is None:
                continue
            if rule.get("chart"):
                charts[name] = _chart_point(rule, value, 1 if cmp_ == "player" else None)
            crit = _flag(rule, value, default_crit=1 if cmp_ == "player" else None)
            if crit or (rule.get("list_if_not_critical") and count > 0):
                add("mechanic", name, rule, value, crit, name, extra)

        # Buffs (Phase 0)
        for name, rule in merged("buff")[0].items():
            if not _applies(rule, pl, role_names):
                continue
            v = uptimes.get(name, 0.0)
            rule = _resolve(rule, env)
            if rule is None:
                continue
            if rule.get("chart"):
                charts[name] = _chart_point(rule, v)
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
            rule = _resolve(rule, env)
            if rule is None:
                continue
            if rule.get("chart"):
                charts[name] = _chart_point(rule, v)
            crit = _flag(rule, v)
            if crit or rule.get("list_if_not_critical"):
                add("metric", name, rule, v, crit, METRIC_LABEL.get(src, name), extra or None)

        # Consumables
        cons = sorted({c.get("name") for c in pl.get("consumables") or []
                       if c.get("name") and c.get("name") not in BLACKLISTED_FOODS})
        rule = merged("consumables")[0].get("")
        rule = _resolve(rule, env) if rule and _applies(rule, pl, role_names) else None
        if rule:
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
                "metric_stats": xstats,
                "charts": charts}})
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
    gcounts = {n: i["count"] for n, i in values["mechanics"].items()}
    env = _Env(gcounts, gcounts, {}, {k: v for k, v in values["metrics"].items() if v is not None}, values["player_count"],
               grp.get("metric_values"))
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
            rule = _resolve(rule, env)
            if rule is None:
                continue
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
