"""Erzeugt aus der gw2logs-SQLite-DB eine statische Website.

Seiten:
  index.html          Log-Uebersicht mit Filter (Spieler / Boss / Modus)
  players.html        Spielerliste
  player-<slug>.html  eine Seite pro Whitelist-Spieler
  report-<hash>.html  Bewertung eines Logs (alle Spieler, Auffaelligkeiten nach report_rules.toml)
  (legal.html und features.html werden NICHT erzeugt - nur verlinkt; liegen manuell im Ausgabeordner)
  style.css, filter.js

Jeder filterbare Container hat die Klasse 'item' plus Klassen wie
  player-<slug>  boss-<short>  mode-normal|mode-cm|mode-lcm
Das JS blendet Items per `hidden` aus, wenn sie die gewaehlte Klasse nicht haben.

Aufruf:  python3 build_site.py [--db gw2logs.sqlite] [--out site]
  --db   Pfad zur SQLite-DB (relativ zum Ausfuehrverzeichnis), Default: db_path aus ./config.toml, sonst gw2logs.sqlite
  --out  Ausgabeordner (relativ zum Ausfuehrverzeichnis, wird angelegt), Default: site
  --rules  Regeldatei fuer die Bewertung, Default: ./report_rules.toml
"""
import argparse, json, re, sqlite3, sys, tomllib
from html import escape as esc
from pathlib import Path

import report_engine

CATS = ["Damage", "Mechanics", "Speed", "Support", "Teamplay"]
GRADES = ["SSS", "SS", "S", "A+", "A", "B+", "B", "C+", "C", "D", "E", "F"]  # Index+1 = Zahl (wie in gw2logs.py)
CAT_SHORT = {"Damage": "DPS", "Mechanics": "Mech", "Speed": "Speed", "Support": "Supp", "Teamplay": "Team"}


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


def ilink(href, text):
    return f'<a href="{esc(href)}">{esc(text)}</a>'


def result_class(l):
    return {1: "result-ok", 0: "result-fail"}.get(l["success"], "")


def page(title, body, scripts=False, nav=True):
    navhtml = ('<nav><a href="index.html">Logs</a><a href="players.html">Spieler</a>'
               '<a href="features.html">Geplante Features</a>'
               '<a href="legal.html">Lizenz &amp; Rechtliches</a></nav>') if nav else ""
    js = '<script src="filter.js"></script>' if scripts else ""
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#272a2f">
<title>{esc(title)}</title>
<link rel="stylesheet" href="style.css">
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
                           ilink(report_name(l), "Bewertung") if l["file_hash"] in reports else "")
        ok = {1: "✔", 0: "✘"}.get(l["success"], "?")
        rows.append(f'<tr class="{classes}"><td>{fmt_time(l["encounter_time"])}</td>'
                    f'<td>{esc(b["name"])}</td><td>{ok}</td><td>{links}</td></tr>')

    table = (f'<table><thead><tr><th>Zeit (UTC)</th><th>Boss</th><th>Kill</th>'
             f'<th>Links</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>')
    return page("GW2 Logs", filters + table, scripts=True)


def build_players(players):
    items = "".join(f'<li class="item player-{player_slug(p["account"])}">'
                    f'<a href="player-{player_slug(p["account"])}.html">{esc(p["account"])}</a></li>'
                    for p in players)
    return page("Spieler", f'<p><a href="index.html">→ Log-Übersicht</a></p><ul>{items}</ul>')


def build_player(db, p, bosses, entries, reports=frozenset()):
    acc = p["account"]
    ps = player_slug(acc)
    parts = ['<p><a href="players.html">← Spielerübersicht</a> | <a href="index.html">→ Log-Übersicht</a> | '
             f'<a href="index.html?player=player-{ps}">→ Logs von {esc(acc)}</a></p>']

    # Gesamtrating
    ranks = json.loads(p["ranks_json"]) if p["ranks_json"] else {}
    rank_txt = " ".join(f'{esc(str(k))}: {grade(v) if isinstance(v, str) else esc(str(v))}'
                        for k, v in ranks.items()) if isinstance(ranks, dict) else esc(str(ranks))
    parts.append(f'<h2>Rating</h2><p>Gesamt: <b>{esc(p["global_rank"] or "–")}</b> '
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
    parts.append("<h2>Durchschnitt (nur erfolgreiche Logs)</h2><table><tr>"
                 + "".join(f"<th>{CAT_SHORT[c]}</th>" for c in CATS) + "</tr><tr>"
                 + "".join(avg_cell(c) for c in CATS) + "</tr></table>")

    own = [l for l, b, pl in entries if any(x["account"] == acc for x in pl)]
    rated_n = db.execute("SELECT COUNT(*) FROM log_player WHERE account=? AND state='rated'", (acc,)).fetchone()[0]
    if own and not avg:
        from collections import Counter
        st = dict(Counter(l["wm_status"] for l in own))
        parts.append(f'<p><i>Keine Wingman-Bewertungen erfolgreicher Logs vorhanden ({len(own)} Logs, Wingman-Status: {esc(str(st))}).</i></p>')
        print(f"WARNUNG {acc}: {len(own)} Logs, aber 0 bewertete (wm_status: {st}, log_player rated: {rated_n})")

    # Charaktere
    chars = db.execute("SELECT * FROM character WHERE account=? ORDER BY name COLLATE NOCASE", (acc,)).fetchall()
    li = "".join(f'<li>{esc(c["name"])}</li>' for c in chars)
    parts.append(f"<h2>Charaktere</h2><ul>{li or '<li>–</li>'}</ul>")

    # Bossspezifisch
    best = {}
    for r in db.execute("SELECT * FROM player_boss_best WHERE account=?", (acc,)):
        best.setdefault(r["boss_key"], {})[r["category"]] = r["grade"]
    keys = [k for k in set(best) | set(avgb) if k in bosses and bosses[k]["enabled"]]
    keys.sort(key=lambda k: bosses[k]["sort"])
    rows = []
    for k in keys:
        b = bosses[k]
        cells = ""
        for c in CATS:
            a = avgb.get(k, {}).get(c)
            cells += f'<td>Ø {grade(a["avg_grade"]) if a else "–"} / Best {grade(best.get(k, {}).get(c))}</td>'
        rows.append(f'<tr class="item {boss_classes(b)}"><td>{esc(b["name"])}</td>{cells}</tr>')
    boss_opts = []
    seen = set()
    for k in keys:
        s = boss_slug(bosses[k])
        if s not in seen:
            seen.add(s)
            boss_opts.append((f"boss-{s}", re.sub(r" CM$", "", bosses[k]["name"])))
    parts.append(
        '<h2>Bossspezifisch</h2><div id="filters">'
        f'<label>Boss <select id="boss" data-filter>{options(boss_opts, "Alle")}</select></label>'
        f'<label>Modus <select id="mode" data-filter>{options([("mode-normal", "Normal"), ("mode-cm", "CM"), ("mode-lcm", "LCM")], "Alle")}</select></label>'
        '</div><table><thead><tr><th>Boss</th>'
        + "".join(f"<th>{CAT_SHORT[c]}</th>" for c in CATS)
        + f'</tr></thead><tbody>{"".join(rows) or "<tr><td colspan=6>Noch keine Daten</td></tr>"}</tbody></table>')

    # Killproofs (Schluessel-Format von Wingman roh anzeigen, falls nicht in boss-Tabelle)
    kp = db.execute("SELECT * FROM player_kp WHERE account=? ORDER BY boss_key", (acc,)).fetchall()
    if kp:
        krows = "".join(f'<tr><td>{esc(bosses[r["boss_key"]]["name"] if r["boss_key"] in bosses else r["boss_key"])}</td>'
                        f'<td>{r["total"]}</td></tr>' for r in kp)
        parts.append(f"<h2>Killproofs</h2><table><tr><th>Typ</th><th>Anzahl</th></tr>{krows}</table>")

    # Letzte Logs dieses Spielers
    mine = [(l, b) for l, b, pl in entries if any(x["account"] == acc for x in pl)][:20]
    lrows = "".join(
        f'<tr><td>{fmt_time(l["encounter_time"])}</td><td>{esc(b["name"])}</td>'
        '<td>' + links_html(link(l["dps_url"], "dps.report"), link(l["wm_url"], "Wingman"),
                            ilink(f"{report_name(l)}#p-{ps}", "Bewertung") if l["file_hash"] in reports else "")
        + '</td></tr>' for l, b in mine)
    parts.append(f'<h2>Letzte Logs</h2><table><tr><th>Zeit (UTC)</th><th>Boss</th><th>Links</th></tr>'
                 f'{lrows or "<tr><td colspan=3>–</td></tr>"}</table>')
    return page(acc, "\n".join(parts), scripts=True)


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


def build_report(l, b, plist, slim, rules):
    """Eine Seite je Log: alle Whitelist-Spieler des Logs, je Spieler die Auffaelligkeiten laut Regeln."""
    evals = {e["account"].casefold(): e for e in report_engine.evaluate(slim, l["boss_key"], rules)}
    ph = slim.get("phases") or []
    dur = report_engine.fmt_ms(ph[0].get("end")) if ph else "?"
    ok = {1: "✔ Erfolg", 0: "✘ Niederlage"}.get(l["success"], "?")
    shown = [(p, evals[p["account"].casefold()]) for p in plist if p["account"].casefold() in evals]
    head = ['<p><a href="index.html">← Log-Übersicht</a></p>',
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
    return page(f'Bewertung: {b["name"]}', "\n".join(head + parts))


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
    history.replaceState(null, '', location.pathname + (q ? '?' + q : ''));
  }

  selects.forEach(function (s) {
    if (params.has(s.id)) s.value = params.get(s.id);
    s.addEventListener('change', apply);
  });
  apply();
})();
"""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./../Backend/gw2logs.sqlite", help="Pfad zur SQLite-DB (Default: db_path aus ./config.toml, sonst ./gw2logs.sqlite)")
    ap.add_argument("--rules", default="./report_rules.toml", help="Regeldatei fuer die Log-Bewertung")
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
    for pr in problems:
        print(f"WARNUNG Regeln: {pr}")

    out = Path.cwd() / a.out                          # Unterordner im Ausfuehrverzeichnis
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("player-*.html")) + list(out.glob("report-*.html")):
        old.unlink()                                   # entfernte Spieler/Logs loeschen
    (out / "style.css").write_text(CSS, encoding="utf-8")
    (out / "filter.js").write_text(JS, encoding="utf-8")
    n_rep = 0
    for l, b, plist in entries:                        # zuerst: nur erfolgreiche Reports duerfen verlinkt werden
        if l["file_hash"] not in reports:
            continue
        try:
            slim = json.loads(db.execute("SELECT data FROM log_analysis WHERE file_hash=?",
                                         (l["file_hash"],)).fetchone()[0])
            (out / report_name(l)).write_text(build_report(l, b, plist, slim, rules), encoding="utf-8")
            n_rep += 1
        except Exception as e:                         # defektes Log darf den Build nicht stoppen
            print(f"WARNUNG Bewertung {l['file_hash'][:8]}: {e!r}")
            reports.discard(l["file_hash"])
    (out / "index.html").write_text(build_index(entries, players, reports), encoding="utf-8")
    (out / "players.html").write_text(build_players(players), encoding="utf-8")
    for p in players:
        (out / f"player-{player_slug(p['account'])}.html").write_text(
            build_player(db, p, bosses, entries, reports), encoding="utf-8")
    print(f"{len(entries)} Logs, {len(players)} Spieler, {n_rep} Bewertungen -> {out}/")


if __name__ == "__main__":
    main()
