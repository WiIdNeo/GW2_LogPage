"""Erzeugt aus der gw2logs-SQLite-DB eine statische Website.

Seiten (Ordnerstruktur im Ausgabeordner):
  index.html                       Log-Uebersicht mit Filter (Spieler / Boss / Modus)
  players.html                     Spielerliste
  spieler/player-<slug>.html       eine Seite pro Whitelist-Spieler (mit Verlaufs-Charts je Boss, siehe charts.py/charts.toml)
  bewertungen/report-<hash>.html   Bewertung eines Logs (alle Spieler, Auffaelligkeiten nach report_rules.toml)
  (legal.html, features.html und guides.html werden NICHT erzeugt - nur verlinkt; liegen manuell top-level im Ausgabeordner)
  style.css, filter.js             top-level, werden von allen Seiten genutzt
Ordnernamen: PLAYER_DIR / REPORT_DIR. Seiten in Unterordnern verlinken top-level-Dateien mit root="../".

Jeder filterbare Container hat die Klasse 'item' plus Klassen wie
  player-<slug>  boss-<short>  mode-normal|mode-cm|mode-lcm
Das JS blendet Items per `hidden` aus, wenn sie die gewaehlte Klasse nicht haben.

Aufruf:  python3 build_site.py [--db gw2logs.sqlite] [--out site]
  --db   Pfad zur SQLite-DB (relativ zum Ausfuehrverzeichnis), Default: db_path aus ./config.toml, sonst gw2logs.sqlite
  --out  Ausgabeordner (relativ zum Ausfuehrverzeichnis, wird angelegt), Default: site
  --charts Einstellungen fuer die Verlaufs-Charts (DPS-Faktoren), Default: ./charts.toml
  --rules  Regeldatei fuer die Bewertung, Default: ./report_rules.toml
           Bei Fehlern in der Regeldatei bricht der Build ab (--ignore-rule-problems: trotzdem bauen)
"""
import argparse, json, re, sqlite3, sys, tomllib
from html import escape as esc
from pathlib import Path

import charts
import report_engine

CATS = ["Damage", "Mechanics", "Speed", "Support", "Teamplay"]
GRADES = ["SSS", "SS", "S", "A+", "A", "B+", "B", "C+", "C", "D", "E", "F"]  # Index+1 = Zahl (wie in gw2logs.py)
CAT_SHORT = {"Damage": "DPS", "Mechanics": "Mech", "Speed": "Speed", "Support": "Supp", "Teamplay": "Team"}
PLAYER_DIR = "spieler"        # Unterordner fuer die Spielerseiten
REPORT_DIR = "bewertungen"    # Unterordner fuer die Bewertungen (Reports)


# ---------------------------------------------------------------- Helfer
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def player_slug(account):
    return slug(account)


def boss_slug(b):
    return slug(b["short"] or f"id{abs(b['game_id'])}")


def boss_mode(b):
    """normal | cm | lcm. LCM erkennt man am Wingman-Schluessel mit doppeltem Minus ('--25989')."""
    if str(b["key"]).startswith("--"):
        return "lcm"
    return "cm" if b["is_cm"] else "normal"


def boss_classes(b):
    return f"boss-{boss_slug(b)} mode-{boss_mode(b)}"


def fmt_time(t):
    return t.replace("T", " ")[:16] if t else ""


def grade(g):
    if not g:
        return "–"
    return f'<span class="grade grade-{slug(g.replace("+", "plus"))}">{esc(g)}</span>'


def link(url, text):
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(text)}</a>' if url else ""


def links_html(*items):
    """Links in einem Container, der umbricht (am Handy untereinander, breit nebeneinander)."""
    items = [x for x in items if x]
    return '<div class="links">' + "".join(items) + "</div>" if items else ""


def report_name(l):
    return f"report-{l['file_hash'][:12]}.html"


def player_name(account):
    return f"player-{player_slug(account)}.html"


def report_href(l, root=""):
    """Relativer Link auf die Bewertung. root: '' von top-level-Seiten, '../' aus einem Unterordner."""
    return f"{root}{REPORT_DIR}/{report_name(l)}"


def player_href(account, root=""):
    """Relativer Link auf die Spielerseite. root wie bei report_href."""
    return f"{root}{PLAYER_DIR}/{player_name(account)}"



def ilink(href, text):
    return f'<a href="{esc(href)}">{esc(text)}</a>'


def result_class(l):
    return {1: "result-ok", 0: "result-fail"}.get(l["success"], "")


def page(title, body, scripts=False, nav=True, root=""):
    """root: Praefix fuer top-level-Dateien ('' fuer top-level-Seiten, '../' fuer Seiten in Unterordnern)."""
    navhtml = (f'<nav><a href="{root}index.html">Logs</a><a href="{root}players.html">Spieler</a>'
               f'<a href="{root}guides.html">Allgemeine Hilfen und Guides</a>'
               f'<a href="{root}features.html">Geplante Features</a>'
               f'<a href="{root}legal.html">Lizenz &amp; Rechtliches</a></nav>') if nav else ""
    js = f'<script src="{root}filter.js"></script>' if scripts else ""
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#272a2f">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
{navhtml}
<h1>{esc(title)}</h1>
{body}
{js}
</body>
</html>
"""


def options(pairs, all_label):
    out = [f'<option value="">{esc(all_label)}</option>']
    out += [f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in pairs]
    return "\n".join(out)


# ---------------------------------------------------------------- Daten laden
def load(db):
    players = db.execute("SELECT * FROM player WHERE whitelisted=1 ORDER BY account COLLATE NOCASE").fetchall()
    wl = {p["account"].casefold(): p for p in players}
    bosses = {r["key"]: r for r in db.execute("SELECT * FROM boss")}

    logs = db.execute(
        "SELECT l.*, b.name AS boss_name FROM log l JOIN boss b ON b.key = l.boss_key "
        "WHERE l.process_failed = 0 AND b.enabled = 1 AND l.players_json IS NOT NULL "
        "AND (l.dps_url IS NOT NULL OR l.wm_url IS NOT NULL) "
        "ORDER BY l.encounter_time DESC, l.created_at DESC").fetchall()

    rated = {}
    for r in db.execute("SELECT * FROM log_player"):
        rated[(r["file_hash"], r["account"].casefold())] = r

    entries = []
    for l in logs:
        pj = json.loads(l["players_json"] or "{}")
        plist = []
        for cf, info in pj.items():
            if cf not in wl:
                continue
            lp = rated.get((l["file_hash"], cf))
            plist.append(dict(
                account=wl[cf]["account"],
                character=(lp["character"] if lp and lp["character"] else info.get("character")),
                cls=(lp["class"] if lp and lp["class"] else info.get("class")),
                grades=[lp[c.lower()] if lp and lp["state"] == "rated" else None for c in CATS]))
        if plist:
            entries.append((l, bosses[l["boss_key"]], sorted(plist, key=lambda p: p["account"].casefold())))
    return players, bosses, entries


def analysed_hashes(db):
    """file_hash aller Logs mit gekuerzter Analyse (Tabelle fehlt in alten DBs -> leer)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='log_analysis'").fetchone():
        return set()
    return {r[0] for r in db.execute("SELECT file_hash FROM log_analysis WHERE data IS NOT NULL")}


def success_averages(db, acc):
    """Durchschnittsnoten NUR aus erfolgreichen Logs (log.success = 1).
    Rueckgabe: (je Kategorie, je Boss-Key und Kategorie) mit {logs, avg_num, avg_grade}."""
    rows = db.execute(
        "SELECT l.boss_key, lp.damage, lp.mechanics, lp.speed, lp.support, lp.teamplay "
        "FROM log_player lp JOIN log l ON l.file_hash = lp.file_hash "
        "WHERE lp.account = ? AND lp.state = 'rated' AND l.success = 1", (acc,)).fetchall()
    total, per_boss = {}, {}
    for r in rows:
        for cat in CATS:
            g = r[cat.lower()]
            if g in GRADES:
                num = GRADES.index(g) + 1
                total.setdefault(cat, []).append(num)
                per_boss.setdefault(r["boss_key"], {}).setdefault(cat, []).append(num)

    def summ(nums):
        avg = sum(nums) / len(nums)
        return dict(logs=len(nums), avg_num=avg, avg_grade=GRADES[max(1, min(12, int(avg + 0.5))) - 1])
    return ({c: summ(n) for c, n in total.items()},
            {bk: {c: summ(n) for c, n in cats.items()} for bk, cats in per_boss.items()})


# ---------------------------------------------------------------- Seiten
def build_index(entries, players, reports=frozenset()):
    boss_opts, seen = [], set()
    for _, b, _ in sorted(entries, key=lambda e: e[1]["sort"]):
        s = boss_slug(b)
        if s not in seen:
            seen.add(s)
            boss_opts.append((f"boss-{s}", re.sub(r" CM$", "", b["name"])))
    # Bossnamen ohne CM-Suffix, da der Modus separat gefiltert wird
    player_opts = [(f"player-{player_slug(p['account'])}", p["account"]) for p in players]

    filters = f"""<div id="filters">
<label>Spieler <select id="player" data-filter>{options(player_opts, "Alle")}</select></label>
<label>Boss <select id="boss" data-filter>{options(boss_opts, "Alle")}</select></label>
<label>Modus <select id="mode" data-filter>{options([("mode-normal", "Normal"), ("mode-cm", "CM"), ("mode-lcm", "LCM")], "Alle")}</select></label>
<label>Ergebnis <select id="result" data-filter>{options([("result-ok", "Erfolg"), ("result-fail", "Niederlage")], "Alle")}</select></label>
<span id="count"></span>
</div>"""

    rows = []
    for l, b, plist in entries:
        classes = " ".join(x for x in ["item", boss_classes(b), result_class(l)]
                           + [f"player-{player_slug(p['account'])}" for p in plist] if x)
        links = links_html(link(l["dps_url"], "dps.report"), link(l["wm_url"], "Wingman"),
                           link(l["yt_url"], "YouTube"),
                           ilink(report_href(l), "Bewertung") if l["file_hash"] in reports else "")
        ok = {1: "✔", 0: "✘"}.get(l["success"], "?")
        rows.append(f'<tr class="{classes}"><td>{fmt_time(l["encounter_time"])}</td>'
                    f'<td>{esc(b["name"])}</td><td>{ok}</td><td>{links}</td></tr>')

    table = (f'<table><thead><tr><th>Zeit (UTC)</th><th>Boss</th><th>Kill</th>'
             f'<th>Links</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>')
    return page("GW2 Logs", filters + table, scripts=True)


def build_players(players):
    items = "".join(f'<li class="item player-{player_slug(p["account"])}">'
                    f'<a href="{esc(player_href(p["account"]))}">{esc(p["account"])}</a></li>'
                    for p in players)
    return page("Spieler", f'<p><a href="index.html">→ Log-Übersicht</a></p><ul>{items}</ul>')


# ---------------------------------------------------------------- Killproofs: feste Reihenfolge fuer die Spielerseite
# Eintrag: (Anzeigename, Boss-ID aus bosses.toml, Modi) - Modi: n = Normal, c = CM, l = LCM (Reihenfolge = Anzeige).
# Schluessel in player_kp / boss-Tabelle: '<ID>' normal, '-<ID>' CM, '--<ID>' LCM.
KP_LAYOUT = [
    ("Wing 1", [("Vale Guardian", 15438, "n"), ("Spirit Woods / Spirit Run", 15415, "n"),
                ("Gorseval", 15429, "n"), ("Sabetha", 15375, "n")]),
    ("Wing 2", [("Slothasor", 16123, "n"), ("Bandit Trio", 16088, "n"), ("Matthias Gabrel", 16115, "n")]),
    ("Wing 3", [("Escort / McLeod", 16253, "n"), ("Keep Construct", 16235, "nc"),
                ("Twisted Castle", 16247, "n"), ("Xera", 16246, "n")]),
    ("Wing 4", [("Cairn", 17194, "nc"), ("Mursaat Overseer", 17172, "nc"),
                ("Samarog", 17188, "nc"), ("Deimos", 17154, "nc")]),
    ("Wing 5", [("Soulless Horror", 19767, "nc"), ("River of Souls / Desmina", 19828, "n"),
                ("Statue of Ice (Broken King)", 19691, "n"), ("Statue of Death (Eater of Souls)", 19536, "n"),
                ("Statue of Darkness (Eyes)", 19651, "n"), ("Dhuum", 19450, "nc")]),
    ("Wing 6", [("Conjured Amalgamate", 43974, "nc"), ("Twin Largos", 21105, "nc"), ("Qadim", 20934, "nc")]),
    ("Wing 7", [("Cardinal Adina", 22006, "nc"), ("Cardinal Sabir", 21964, "nc"), ("Qadim the Peerless", 22000, "nc")]),
    ("Wing 8", [("Greer", 26725, "nc"), ("Decima", 26774, "nc"), ("Ura", 26712, "ncl")]),
    ("Icebrood Saga", [("Icebrood Construct", 22154, "n"), ("Fraenir of Jormag", 22492, "n"),
                       ("Kodan Brothers (Voice and Claw)", 22343, "n"), ("Boneskinner", 22521, "n"),
                       ("Whisper of Jormag", 22711, "n")]),
    ("End of Dragons Strikes", [("Aetherblade Hideout", 24033, "nc"), ("Xunlai Jade Junkyard", 23957, "nc"),
                                ("Kaineng Overlook", 24485, "nc"), ("Harvest Temple", 24375, "nc")]),
    ("Core Game Strikes", [("Old Lion's Court", 25413, "nc"), ("Freezie", 21333, "n")]),
    ("Secrets of the Obscure Strikes", [("Cosmic Observatory", 25705, "nc"), ("Temple of Febe", 25989, "ncl")]),
    ("Visions of Eternity Strikes", [("Kela", 27124, "nc"), ("Vloxx", 28106, "ncl")]),
]


def kp_html(kp_rows, bosses):
    """Killproofs in fester Reihenfolge nach Wing/Strike-Gruppe; CM (und LCM) direkt hinter der Normalversion."""
    totals = {str(r["boss_key"]): r["total"] for r in kp_rows}
    used, out = set(), []
    for section, items in KP_LAYOUT:
        rows = []
        for label, bid, modes in items:
            for m in modes:
                key = {"n": str(bid), "c": str(-abs(bid)), "l": f"--{abs(bid)}"}[m]
                used.add(key)
                rows.append(f"<tr><td>{esc(label + {'n': '', 'c': ' CM', 'l': ' LCM'}[m])}</td><td>{totals.get(key, 0) or 0}</td></tr>")
        out.append(f"<h3>{esc(section)}</h3><table><tr><th>Encounter</th><th>Anzahl</th></tr>{''.join(rows)}</table>")
    rest = [(k, v) for k, v in totals.items() if k not in used and v]       # nichts verschlucken, was nicht im Layout steht
    if rest:
        rows = "".join(f'<tr><td>{esc(bosses[k]["name"] if k in bosses else k)}</td><td>{v}</td></tr>' for k, v in rest)
        out.append(f"<h3>Sonstige</h3><table><tr><th>Typ</th><th>Anzahl</th></tr>{rows}</table>")
    return '<h2 id="kp">Killproofs</h2>' + "".join(out)


# ---------------------------------------------------------------- Spielerseite: Feedback, Rollen/Klassen, Verlauf je Boss
def _day(t):
    """'2026-11-20T19:02:..' -> '20.11.'"""
    return f"{t[8:10]}.{t[5:7]}." if t and len(t) >= 10 else "?"


def _pct(n, total):
    return f"{round(100 * n / total)} %" if total else "–"


def _combo(roles, only=None):
    """Rollenkombination als Text (Reihenfolge wie in report_rules.toml). only: nur diese Rollen beruecksichtigen."""
    names = [r for r in roles if only is None or r in only]
    return " + ".join(names) or ("Sonstige" if only is not None else "ohne Rolle")


def roles_classes_html(items, last_n=10):
    """items: [(Rollenkombination, Klasse)] alt -> neu. Je Kombination: Anteil an allen / den letzten N Logs,
    darunter die Klassen mit ihrem Anteil innerhalb dieser Kombination (gesamt und in den letzten N Logs)."""
    from collections import Counter
    if not items:
        return ""
    last = items[-last_n:]
    tot, tot_l = Counter(c for c, _ in items), Counter(c for c, _ in last)
    out = []
    for combo, n in tot.most_common():
        cls, cls_l = Counter(k for c, k in items if c == combo), Counter(k for c, k in last if c == combo)
        rows = "".join(f'<tr><td>{esc(k)}</td><td class="num">{_pct(m, n)}</td>'
                       f'<td class="num">{_pct(cls_l[k], tot_l[combo]) if tot_l[combo] else "–"}</td></tr>'
                       for k, m in cls.most_common())
        out.append(f'<div class="rc"><h4>{esc(combo)} <small>({_pct(n, len(items))} aller Logs, '
                   f'{_pct(tot_l[combo], len(last))} in den letzten {len(last)})</small></h4>'
                   f'<table><thead><tr><th>Klasse</th><th>gesamt</th><th>letzte {len(last)}</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table></div>')
    return f'<div class="rcgrid">{"".join(out)}</div>'


def feedback_html(logs, min_pct, last_n=10):
    """Was war wie oft kritisch? logs: [(log, eval)] alt -> neu. Nur Punkte, deren Quote UEBER min_pct liegt."""
    from collections import Counter
    n, last = len(logs), {l["file_hash"] for l, _ in logs[-last_n:]}
    hits, hits_l = Counter(), Counter()
    for l, ev in logs:
        for key in {f.get("label") or f["name"] or "Consumables" for f in ev.get("findings") or [] if f["critical"]}:
            hits[key] += 1
            if l["file_hash"] in last:
                hits_l[key] += 1
    items = [(k, c) for k, c in hits.most_common() if n and 100 * c / n > min_pct]
    if not items:
        return f'<p class="muted">Nichts, was in mehr als {fmt_pct(min_pct)} der Logs kritisch war.</p>'
    nl = min(n, last_n)
    li = "".join(f'<li><b>{esc(k)}</b>: in {c} von {n} Log{"s" if n != 1 else ""} kritisch ({_pct(c, n)})'
                 + (f', in den letzten {nl}: {hits_l[k]}×' if n > last_n else "") + "</li>" for k, c in items)
    return f'<ul class="fb">{li}</ul>'


def fmt_pct(v):
    return f"{charts.fmt_num(v)} %"


def rating_table(avg, best):
    """Wingman-Bewertung eines Bosses: Durchschnitt (nur erfolgreiche Logs) und Bestwert je Kategorie."""
    head = "".join(f"<th>{CAT_SHORT[c]}</th>" for c in CATS)
    a = "".join(f'<td>{grade(avg[c]["avg_grade"]) if c in avg else "–"}'
                + (f' <small class="muted">({avg[c]["logs"]})</small>' if c in avg else "") + "</td>" for c in CATS)
    b = "".join(f"<td>{grade(best.get(c))}</td>" for c in CATS)
    return f'<table class="rt"><tr><th></th>{head}</tr><tr><th>Ø</th>{a}</tr><tr><th>Best</th>{b}</tr></table>'


def boss_sections(acc, entries, bosses, evals, ccfg, reports, avgb, best):
    """Je Boss ein einklappbarer Abschnitt: Wingman-Bewertung, Feedback, Rollen/Klassen, DPS-Chart + Tabelle,
    Tode/Downs und Mechanik-Charts. Rueckgabe: ([(boss, anchor, html)], [(Rollenkombination, Klasse)] aller Logs)."""
    from collections import Counter
    cf, ps = acc.casefold(), player_slug(acc)
    per_boss, overview = {}, []
    for l, b, plist in sorted(entries, key=lambda e: e[0]["encounter_time"] or ""):       # alt -> neu
        ev = (evals.get(l["file_hash"]) or {}).get(cf)
        if not ev or (ccfg["only_success"] and l["success"] != 1):
            continue
        per_boss.setdefault(l["boss_key"], []).append((l, ev))
        roles = [r["name"] for r in ev.get("roles") or []]
        overview.append((_combo(roles, ccfg["overview_roles"]), ev.get("profession") or "?"))
    keys = [k for k in set(per_boss) | set(avgb) | set(best) if k in bosses and bosses[k]["enabled"]]
    out = []
    for key in sorted(keys, key=lambda k: bosses[k]["sort"]):
        b, logs = bosses[key], per_boss.get(key, [])
        anchor = f"v-{boss_slug(b)}-{boss_mode(b)}"
        parts = []
        if key in avgb or key in best:
            parts.append("<h3>Bewertung <small>(Wingman, Ø nur erfolgreiche Logs)</small></h3>"
                         + rating_table(avgb.get(key, {}), best.get(key, {})))
        if not logs:
            parts.append('<p class="muted">Keine ausgewerteten Logs.</p>')
            out.append((b, anchor, f'<details class="boss-sec" id="{anchor}"><summary><b>{esc(b["name"])}</b> '
                                   f'<small>keine ausgewerteten Logs</small></summary>{"".join(parts)}</details>'))
            continue
        combos, rc = Counter(), []
        for l, ev in logs:                                                 # Rollen/Klassen + Feedback: ALLE Logs
            roles = [r["name"] for r in ev.get("roles") or []]
            combos[_combo(roles)] += 1
            rc.append((_combo(roles), ev.get("profession") or "?"))
        win = logs[-ccfg["chart_max_logs"]:]                               # Charts + Tabelle: nur die letzten N
        older = len(logs) - len(win)
        dps_lab, dps_abs, dps_norm, t_abs, t_norm, rows = [], [], [], [], [], []
        mech = {"Tode": {}, "Downs": {}}                                   # Name -> {x-Index: Punkt}
        for i, (l, ev) in enumerate(win):
            roles = [r["name"] for r in ev.get("roles") or []]
            v = ev["values"]
            dps = (v.get("metrics") or {}).get("dps")
            excl = charts.is_excluded(ccfg, roles)
            f = None if excl or dps is None else charts.dps_factor(ccfg, key, roles, v)
            norm = dps / f if f else None
            when = fmt_time(l["encounter_time"])
            if not excl and dps is not None:
                dps_lab.append(_day(l["encounter_time"]))
                dps_abs.append(dps)
                dps_norm.append(norm)
                t_abs.append(f"{when}: {charts.fmt_num(dps)} DPS")
                t_norm.append(f"{when}: {charts.fmt_num(norm)} normalisiert" if norm else None)
            ms = v.get("mechanics") or {}
            mech["Tode"][i] = {"value": ms.get("Dead", 0), "threshold": None}
            mech["Downs"][i] = {"value": ms.get("Downed", 0), "threshold": None}
            for name, pt in (v.get("charts") or {}).items():
                mech.setdefault(name, {})[i] = pt
            links = links_html(link(l["dps_url"], "dps.report"), link(l["wm_url"], "Wingman"),
                               ilink(f"{report_href(l, '../')}#p-{ps}", "Bewertung") if l["file_hash"] in reports else "")
            rows.append(f'<tr><td>{when}</td><td>{esc(", ".join(roles) or "–")}</td>'
                        f'<td>{esc(ev.get("profession") or "?")}</td><td class="num">{charts.fmt_num(dps)}</td>'
                        f'<td class="num">{"nicht im Chart" if excl else charts.fmt_num(f)}</td>'
                        f'<td class="num">{charts.fmt_num(norm) if norm else "–"}</td><td>{links}</td></tr>')

        parts.append("<h3>Feedback</h3>" + feedback_html(logs, ccfg["feedback_min_percent"]))
        parts.append("<h3>Rollen und Klassen</h3>" + roles_classes_html(rc))
        svg = charts.line_chart(dps_lab, [{"cls": "l1", "values": dps_abs, "titles": t_abs},
                                          {"cls": "l2", "values": dps_norm, "titles": t_norm}],
                                height=200, label=f"DPS-Verlauf {b['name']}")
        parts.append("<h3>Schaden</h3>" + (charts.legend([("l1", "absolut"), ("l2", "normalisiert (auf DPS-Rolle)")])
                                         + svg if svg else '<p class="muted">Keine DPS-Logs (nur Heal o. Ä.).</p>'))
        parts.append('<div class="scroll"><table><thead><tr><th>Zeit (UTC)</th><th>Rollen</th><th>Klasse</th>'
                     '<th>DPS</th><th>Faktor</th><th>normalisiert</th><th>Links</th></tr></thead><tbody>'
                     + "".join(reversed(rows)) + "</tbody></table></div>")
        flt = f"../index.html?player=player-{ps}&amp;boss=boss-{boss_slug(b)}&amp;mode=mode-{boss_mode(b)}" \
              + ("&amp;result=result-ok" if ccfg["only_success"] else "")
        parts.append(f'<p class="muted">Charts und Tabelle: '
                     + (f'die letzten {len(win)} Logs' if len(win) > 1 else "1 Log")
                     + (f', {older} ältere' if older else "")
                     + f' – <a href="{flt}">alle Logs dieses Bosses in der Log-Übersicht</a></p>')
        labels = [_day(l["encounter_time"]) for l, _ in win]
        boxes = []
        for name, pts in mech.items():
            vals = [pts[i]["value"] if i in pts else None for i in range(len(win))]
            thr = [pts[i]["threshold"] if i in pts else None for i in range(len(win))]
            tt = [f"{fmt_time(win[i][0]['encounter_time'])}: {charts.fmt_num(vals[i])}" if i in pts else None
                  for i in range(len(win))]
            series = [{"cls": "l1", "values": vals, "titles": tt}]
            if any(t is not None for t in thr):
                series.append({"cls": "thr", "values": thr, "dots": False})
            svg = charts.line_chart(labels, series, height=160, label=f"{name} {b['name']}", width=420)
            if svg:
                boxes.append(f'<div class="chbox"><h4>{esc(name)}</h4>{svg}</div>')
        if boxes:
            parts.append("<h3>Tode, Downs und Mechaniken <small>(gestrichelt: Schwellwert)</small></h3>"
                         f'<div class="chgrid">{"".join(boxes)}</div>')
        out.append((b, anchor, f'<details class="boss-sec" id="{anchor}"><summary><b>{esc(b["name"])}</b> '
                               f'<small>{len(logs)} Log(s) · meist {esc(combos.most_common(1)[0][0])}</small></summary>'
                               + "".join(parts) + "</details>"))
    return out, overview


def build_player(db, p, bosses, entries, reports=frozenset(), evals=None, ccfg=None):
    acc = p["account"]
    ps = player_slug(acc)
    ccfg = ccfg or charts.load_config("")[0]
    parts = ['<p><a href="../players.html">← Spielerübersicht</a> | <a href="../index.html">→ Log-Übersicht</a> | '
             f'<a href="../index.html?player=player-{ps}">→ Logs von {esc(acc)}</a></p>']

    # Gesamtrating
    ranks = json.loads(p["ranks_json"]) if p["ranks_json"] else {}
    rank_txt = " ".join(f'{esc(str(k))}: {grade(v) if isinstance(v, str) else esc(str(v))}'
                        for k, v in ranks.items()) if isinstance(ranks, dict) else esc(str(ranks))
    parts.append(f'<h2 id="rating">Rating</h2><p>Gesamt: <b>{esc(p["global_rank"] or "–")}</b> '
                 '<br>'
                 f'Kategorien: {rank_txt or "–"}<br>'
                 f'Bosse abgeschlossen: {p["bosses_completed"] if p["bosses_completed"] is not None else "–"}</p>')

    # Durchschnitt Spieler
    avg, avgb = success_averages(db, acc)
    def avg_cell(c):
        if c not in avg:
            return "<td>–</td>"
        r = avg[c]
        return f'<td>{grade(r["avg_grade"])} ({r["logs"]} Logs, Ø {r["avg_num"]:.2f})</td>'
    parts.append('<h2 id="schnitt">Durchschnitt (nur erfolgreiche Logs)</h2><table><tr>'
                 + "".join(f"<th>{CAT_SHORT[c]}</th>" for c in CATS) + "</tr><tr>"
                 + "".join(avg_cell(c) for c in CATS) + "</tr></table>")

    own = [l for l, b, pl in entries if any(x["account"] == acc for x in pl)]
    rated_n = db.execute("SELECT COUNT(*) FROM log_player WHERE account=? AND state='rated'", (acc,)).fetchone()[0]
    if own and not avg:
        from collections import Counter
        st = dict(Counter(l["wm_status"] for l in own))
        parts.append(f'<p><i>Keine Wingman-Bewertungen erfolgreicher Logs vorhanden ({len(own)} Logs, Wingman-Status: {esc(str(st))}).</i></p>')
        print(f"WARNUNG {acc}: {len(own)} Logs, aber 0 bewertete (wm_status: {st}, log_player rated: {rated_n})")

    # Verlauf je Boss vorbereiten (liefert auch die Rollen/Klassen fuer die Gesamtuebersicht)
    best = {}
    for r in db.execute("SELECT * FROM player_boss_best WHERE account=?", (acc,)):
        best.setdefault(r["boss_key"], {})[r["category"]] = r["grade"]
    secs, overview = boss_sections(acc, entries, bosses, evals or {}, ccfg, reports, avgb, best)
    if overview:
        parts.append('<h2 id="rollen">Rollen und Klassen über alle Bosse</h2>' + roles_classes_html(overview))

    # Charaktere
    chars = db.execute("SELECT * FROM character WHERE account=? ORDER BY name COLLATE NOCASE", (acc,)).fetchall()
    li = "".join(f'<li>{esc(c["name"])}</li>' for c in chars)
    parts.append(f'<h2 id="charaktere">Charaktere</h2><ul>{li or "<li>–</li>"}</ul>')

    # Killproofs (Schluessel-Format von Wingman roh anzeigen, falls nicht in boss-Tabelle)
    kp = db.execute("SELECT * FROM player_kp WHERE account=? ORDER BY boss_key", (acc,)).fetchall()
    if kp:
        parts.append(kp_html(kp, bosses))

    # Je Boss: Bewertung, Feedback, Rollen/Klassen, Charts - standardmaessig eingeklappt
    if secs:
        parts.append('<h2 id="verlauf">Bosse</h2>'
                     '<p><button type="button" data-details="open">Alle aufklappen</button> '
                     '<button type="button" data-details="close">Alle zuklappen</button></p>'
                     + "".join(h for _, _, h in secs))
    toc = [("rating", "Rating"), ("schnitt", "Durchschnitt")]
    if overview:
        toc.append(("rollen", "Rollen und Klassen"))
    toc.append(("charaktere", "Charaktere"))
    if kp:
        toc.append(("kp", "Killproofs"))
    toc_html = "".join(f'<li><a href="#{a}">{t}</a></li>' for a, t in toc)
    if secs:
        toc_html += ('<li><a href="#verlauf">Bosse</a><ul class="toc-b">'
                     + "".join(f'<li><a href="#{a}">{esc(b["name"])}</a></li>' for b, a, _ in secs) + "</ul></li>")
    parts.insert(1, f'<nav class="toc-p" aria-label="Inhalt"><b>Inhalt</b><ul>{toc_html}</ul></nav>')
    return page(acc, "\n".join(parts), scripts=True, root="../")


def kv_list(pairs):
    """Eine Zeile pro Eintrag: Name links, Wert rechts; auf breiten Schirmen mehrspaltig."""
    if not pairs:
        return "<p class=\"muted\">–</p>"
    return '<ul class="kv">' + "".join(
        f'<li><span class="k">{esc(k)}</span>' + (f'<span class="v">{esc(v)}</span>' if v != "" else "") + "</li>"
        for k, v in pairs) + "</ul>"


def stat_value(shown, st):
    """'2 · 66.7 % · Platz 1/3' - Wert, Anteil an der Gruppensumme und Rang unter allen Spielern."""
    if not st or st.get("total", 0) <= 0:                # Summe 0: Rang waere bedeutungslos
        return str(shown)
    parts = [str(shown)]
    if st.get("share") is not None:
        parts.append(f"{st['share']:g} %")
    parts.append(f"Platz {st['rank']}/{st['n']}")
    return " · ".join(parts)


def roles_html(roles):
    if not roles:
        return ""
    return '<ul class="roles">' + "".join(
        f'<li><b>{esc(r["name"])}</b>'
        + (f' <span class="muted">({esc("; ".join(r["why"]))})</span>' if r.get("why") else "") + "</li>"
        for r in roles) + "</ul>"


def findings_html(fl):
    if not fl:
        return '<p class="muted"><i>Keine Auffälligkeiten.</i></p>'
    li = "".join(
        f'<li class="{"crit" if f["critical"] else "info"}">{"⚠ " if f["critical"] else "ℹ "}{esc(f["text"])}'
        + (f'<br><a href="{esc(f["link"])}" target="_blank" rel="noopener">Erklärung</a>' if f.get("link") else "")
        + "</li>" for f in fl)
    return f'<ul class="findings">{li}</ul>'


def group_section(slim, boss_key, rules):
    """Abschnitt 'Gruppe': Regeln mit compare = "total" und Gruppensummen (ueber alle Spieler im Log)."""
    g = report_engine.evaluate_group(slim, boss_key, rules)
    v = g["values"]
    if not slim.get("group"):
        return ""                                       # aeltere Analyse ohne Gruppenwerte
    crit = sum(1 for f in g["findings"] if f["critical"])
    num = report_engine._num
    values = ("<h3>Mechaniken <small>(Summe, betroffene Spieler)</small></h3>"
              + kv_list([(n, f"{i['count']} ({i['players']})") for n, i in sorted(v["mechanics"].items())])
              + "<h3>Metriken (Summe)</h3>"
              + kv_list([(report_engine.METRIC_LABEL.get(k, k), num(x)) for k, x in v["metrics"].items()]))
    return ('<section id="gruppe"><h2>Gruppe'
            + (f' <span class="crit badge">{crit} kritisch</span>' if crit else "")
            + f'<small>{v["player_count"]} Spieler im Log</small></h2>{findings_html(g["findings"])}'
            f'<details><summary>Alle Werte</summary>{values}</details></section>')


def build_report(l, b, plist, slim, rules, evals=None):
    """Eine Seite je Log: alle Whitelist-Spieler des Logs, je Spieler die Auffaelligkeiten laut Regeln."""
    if evals is None:
        evals = {e["account"].casefold(): e for e in report_engine.evaluate(slim, l["boss_key"], rules)}
    ph = slim.get("phases") or []
    dur = report_engine.fmt_ms(ph[0].get("end")) if ph else "?"
    ok = {1: "✔ Erfolg", 0: "✘ Niederlage"}.get(l["success"], "?")
    shown = [(p, evals[p["account"].casefold()]) for p in plist if p["account"].casefold() in evals]
    head = ['<p><a href="../index.html">← Log-Übersicht</a></p>',
            f'<p>{fmt_time(l["encounter_time"])} UTC<br>{ok}<br>Dauer {dur}</p>',
            links_html(link(l["dps_url"], "dps.report"), link(l["wm_url"], "Wingman")),
            '<p class="muted">Springe zu</p><div class="toc">'
            + ('<a href="#gruppe">Gruppe</a>' if slim.get("group") else "")
            + "".join(f'<a href="#p-{player_slug(p["account"])}">{esc(p["account"])}</a>' for p, _ in shown) + "</div>"]
    parts = [group_section(slim, l["boss_key"], rules)]
    for p, ev in shown:
        fl = ev["findings"]
        crit = sum(1 for f in fl if f["critical"])
        body = findings_html(fl)
        v = ev["values"]
        num = report_engine._num
        ms, xs = v.get("mechanic_stats") or {}, v.get("metric_stats") or {}
        values = ("<h3>Mechaniken <small>(Anzahl · Anteil an allen Spielern · Platz)</small></h3>"
                  + kv_list([(n, stat_value(c, ms.get(n))) for n, c in v["mechanics"].items()])
                  + "<h3>Metriken</h3>" + kv_list([(report_engine.METRIC_LABEL.get(k, k), stat_value(num(x), xs.get(k)))
                                                    for k, x in v["metrics"].items()])
                  + ("<h3>Boons an die Gruppe <small>(Ø Uptime/Stacks · Anteil · Platz)</small></h3>"
                     + kv_list([(n, stat_value(num(x), (v.get("generation_stats") or {}).get(n)))
                                for n, x in (v.get("generation") or {}).items()]) if v.get("generation") else "")
                  + "<h3>Buffs <small>(Uptime % bzw. Ø Stacks)</small></h3>"
                  + kv_list([(n, num(x)) for n, x in v["buffs"].items()])
                  + "<h3>Consumables</h3>" + kv_list([(c, "") for c in v["consumables"]]))
        sub = " – ".join(x for x in [ev.get("character"), ev.get("profession")] if x)
        parts.append(f'<section id="p-{player_slug(p["account"])}"><h2>{esc(p["account"])}'
                     + (f' <span class="crit badge">{crit} kritisch</span>' if crit else "")
                     + f'<small>{esc(sub)}</small></h2>{roles_html(ev.get("roles"))}{body}<details><summary>Alle Werte</summary>{values}</details></section>')
    return page(f'Bewertung: {b["name"]}', "\n".join(head + parts), root="../")


CSS = """:root{color-scheme:dark;--bg:#272a2f;--fg:#e6e8eb;--muted:#a0a6ae;--line:#454a52;--head:#31353b;
--link:#7cb7ff;--visited:#b9a4ff;--crit:#ff7b7b}
html{background:var(--bg)}
body{font-family:sans-serif;line-height:1.45;margin:1rem auto;max-width:1100px;padding:0 1rem;background:var(--bg);color:var(--fg)}
a{color:var(--link)}
a:visited{color:var(--visited)}
nav{display:flex;flex-wrap:wrap;gap:.3rem 1.2rem;margin-bottom:1rem}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{background:var(--head)}
#filters{margin:1rem 0;display:flex;gap:1rem;flex-wrap:wrap;align-items:center}
select{background:var(--head);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 6px}
[hidden]{display:none !important}
.grade{font-weight:bold}
.pl{white-space:nowrap}
.muted{color:var(--muted)}
.badge{font-size:.6em;font-weight:normal;margin-left:.4rem}
.roles{list-style:none;padding:0;margin:.3rem 0 .6rem}
.crit{color:var(--crit)}
.info{color:var(--muted)}
.links{display:flex;flex-wrap:wrap;gap:.15rem 1rem}
.toc{display:flex;flex-wrap:wrap;gap:.3rem 1rem;margin-bottom:1rem}
.findings{padding-left:1.2rem}
.findings li{margin:.35rem 0}
section{margin:1.5rem 0;padding-top:.5rem;border-top:1px solid var(--line)}
h2 small{display:block;font-weight:normal;color:var(--muted);font-size:.7em}
h3{margin:1rem 0 .3rem;font-size:1rem}
h3 small{font-weight:normal;color:var(--muted)}
details{margin:.5rem 0}
summary{cursor:pointer}
.kv{list-style:none;padding:0;margin:.2rem 0;column-width:17rem;column-gap:2rem}
.kv li{display:flex;justify-content:space-between;gap:.8rem;border-bottom:1px solid var(--line);padding:2px 0;break-inside:avoid}
.kv .v{white-space:nowrap;font-variant-numeric:tabular-nums}
@media (max-width:600px){.links{flex-direction:column}}
:root{--c1:#7cb7ff;--c2:#ffb86b}
.toc-p{border:1px solid var(--line);border-radius:4px;padding:.4rem .8rem;margin:.8rem 0;display:block}
.toc-p ul{margin:.2rem 0;padding-left:1.2rem}.toc-b{columns:14rem;font-size:.9em}
.boss-sec{border:1px solid var(--line);border-radius:4px;padding:.3rem .8rem}
.boss-sec[open]{padding-bottom:.8rem}.boss-sec summary small{color:var(--muted);margin-left:.4rem}
button{background:var(--head);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 8px;cursor:pointer}
.ch{display:block;width:100%;height:auto;max-width:760px}
.ch .g{stroke:var(--line);stroke-width:1}.ch text{fill:var(--muted);font-size:11px;font-family:sans-serif}
.ch polyline{fill:none;stroke-width:2}.ch circle{stroke:none}
.ch .l1 polyline{stroke:var(--c1)}.ch .l1 circle{fill:var(--c1)}
.ch .l2 polyline{stroke:var(--c2)}.ch .l2 circle{fill:var(--c2)}
.ch .thr polyline{stroke:var(--crit);stroke-dasharray:5 4;stroke-width:1.5}
.legend{display:flex;gap:1.2rem;font-size:.9em;color:var(--muted)}
.sw{display:inline-block;width:1.4em;height:3px;vertical-align:middle;margin-right:.4em}
.sw.l1{background:var(--c1)}.sw.l2{background:var(--c2)}
.chgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:.6rem 1.2rem}
.chbox h4{margin:.3rem 0 0;font-weight:normal}
.fb{margin:.3rem 0 .6rem;padding-left:1.2rem}.fb li{margin:.15rem 0}
.rcgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:.4rem 1.2rem}
.rc h4{margin:.4rem 0 .2rem}.rc h4 small,h2 small{font-weight:normal;color:var(--muted)}
.rt th:first-child{width:3rem}
.scroll{overflow-x:auto}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
"""

JS = """// Generischer Filter: <select data-filter id="x"> -> value = CSS-Klasse, die ein .item haben muss.
// Zustand wird ueber ?id=wert in der URL gehalten (statisch, funktioniert auf GitHub Pages).
(function () {
  var selects = Array.prototype.slice.call(document.querySelectorAll('select[data-filter]'));
  var items = Array.prototype.slice.call(document.querySelectorAll('.item'));
  var count = document.getElementById('count');
  var params = new URLSearchParams(location.search);

  function apply() {
    var active = selects.map(function (s) { return s.value; }).filter(Boolean);
    var shown = 0;
    items.forEach(function (el) {
      var ok = active.every(function (c) { return el.classList.contains(c); });
      el.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + ' / ' + items.length + ' Einträge';
    var p = new URLSearchParams();
    selects.forEach(function (s) { if (s.value) p.set(s.id, s.value); });
    var q = p.toString();
    history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);   // Sprungziel behalten
  }

  selects.forEach(function (s) {
    if (params.has(s.id)) s.value = params.get(s.id);
    s.addEventListener('change', apply);
  });
  apply();
})();

// Spielerseite: Sprunglinks oeffnen eingeklappte Abschnitte; Knoepfe "Alle auf-/zuklappen".
(function () {
  function openId(id) {
    var el = id && document.getElementById(id);
    if (el && el.tagName === 'DETAILS') el.open = true;
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (a) openId(decodeURIComponent(a.getAttribute('href').slice(1)));
    var b = e.target.closest && e.target.closest('button[data-details]');
    if (b) Array.prototype.forEach.call(document.querySelectorAll('details.boss-sec'), function (d) {
      d.open = b.getAttribute('data-details') === 'open';
    });
  });
  openId(decodeURIComponent(location.hash.slice(1)));
})();
"""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./../Backend/gw2logs.sqlite", help="Pfad zur SQLite-DB (Default: db_path aus ./config.toml, sonst ./gw2logs.sqlite)")
    ap.add_argument("--rules", default="./report_rules.toml", help="Regeldatei fuer die Log-Bewertung")
    ap.add_argument("--charts", default="./charts.toml", help="Einstellungen fuer die Verlaufs-Charts (DPS-Faktoren)")
    ap.add_argument("--ignore-rule-problems", action="store_true",
                    help="trotz fehlerhafter Regeln weiterbauen (fehlerhafte Regeln werden ignoriert)")
    ap.add_argument("--out", default="./../docs", help="Ausgabeordner, relativ zum Ausfuehrverzeichnis (Default: ./site)")
    a = ap.parse_args()

    db_arg = a.db
    if not db_arg:
        cfg = Path.cwd() / "config.toml"
        db_arg = (tomllib.loads(cfg.read_text(encoding="utf-8")).get("db_path") if cfg.exists() else None) \
            or "gw2logs.sqlite"
    db_path = (Path.cwd() / db_arg).resolve()
    if not db_path.is_file():
        sys.exit(f"DB nicht gefunden: {db_path}\n(Ausfuehrverzeichnis: {Path.cwd()}; "
                 "mit --db anderen Pfad angeben)")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)    # read-only: legt nie eine leere DB an
    db.row_factory = sqlite3.Row
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='player'").fetchone():
        sys.exit(f"{db_path} enthaelt keine Tabelle 'player' - falsche oder leere Datei?")
    players, bosses, entries = load(db)
    reports = analysed_hashes(db)
    rules_path = Path.cwd() / a.rules
    rules, problems = report_engine.load_rules(rules_path)
    ccfg, cprob = charts.load_config(Path.cwd() / a.charts)
    problems += [f"charts: {x}" for x in cprob]
    for pr in problems:
        print(f"{'WARNUNG' if a.ignore_rule_problems else 'FEHLER'} Regeln: {pr}", file=sys.stderr)
    if problems and not a.ignore_rule_problems:          # kaputte Regeln nicht still verwerfen
        sys.exit(f"Abbruch: {len(problems)} Problem(e) in {rules_path} - nichts geschrieben "
                 "(mit --ignore-rule-problems trotzdem bauen)")

    out = Path.cwd() / a.out                          # Unterordner im Ausfuehrverzeichnis
    player_dir, report_dir = out / PLAYER_DIR, out / REPORT_DIR
    for d in (out, player_dir, report_dir):
        d.mkdir(parents=True, exist_ok=True)
    for old in (list(player_dir.glob("player-*.html")) + list(report_dir.glob("report-*.html"))
                + list(out.glob("player-*.html")) + list(out.glob("report-*.html"))):
        old.unlink()                                   # entfernte Spieler/Logs (und Dateien aus der alten flachen Struktur) loeschen
    (out / "style.css").write_text(CSS, encoding="utf-8")
    (out / "filter.js").write_text(JS, encoding="utf-8")
    n_rep = 0
    evals = {}
    for l, b, plist in entries:                        # zuerst: nur erfolgreiche Reports duerfen verlinkt werden
        if l["file_hash"] not in reports:
            continue
        try:
            slim = json.loads(db.execute("SELECT data FROM log_analysis WHERE file_hash=?",
                                         (l["file_hash"],)).fetchone()[0])
            ev = {e["account"].casefold(): e for e in report_engine.evaluate(slim, l["boss_key"], rules)}
            (report_dir / report_name(l)).write_text(build_report(l, b, plist, slim, rules, ev), encoding="utf-8")
            evals[l["file_hash"]] = ev                 # fuer die Verlaufs-Charts der Spielerseiten
            n_rep += 1
        except Exception as e:                         # defektes Log darf den Build nicht stoppen
            print(f"WARNUNG Bewertung {l['file_hash'][:8]}: {e!r}")
            reports.discard(l["file_hash"])
    (out / "index.html").write_text(build_index(entries, players, reports), encoding="utf-8")
    (out / "players.html").write_text(build_players(players), encoding="utf-8")
    for p in players:
        (player_dir / player_name(p["account"])).write_text(
            build_player(db, p, bosses, entries, reports, evals, ccfg), encoding="utf-8")
    print(f"{len(entries)} Logs, {len(players)} Spieler, {n_rep} Bewertungen -> {out}/")


if __name__ == "__main__":
    main()
