"""charts.py - Verlaufs-Charts fuer die Spielerseiten (von build_site.py genutzt, ohne DB und ohne JavaScript).

  load_config(path)                     charts.toml lesen -> (cfg, problems)
  dps_factor(cfg, boss_key, roles, v)   Faktor fuer die normalisierte DPS (None = nicht berechenbar)
  is_excluded(cfg, roles)               Spieler mit diesen Rollen nicht im DPS-Chart (z. B. Heal)
  line_chart(labels, series, ...)       Inline-SVG; Werte in jeder Serie je x-Position (None = Luecke)

Die Charts sind reines SVG mit CSS-Klassen aus style.css (.ch ...). Tooltips ueber <title> (Maus drueber halten).
"""
import math
import tomllib
from html import escape as esc

import report_engine

CFG_KEYS = {"only_success", "chart_max_logs", "exclude_roles", "feedback_min_percent", "overview_roles", "dps_factor", "boss"}
DEFAULT_OVERVIEW_ROLES = ["Heal", "Alac-Support", "Quick-Support", "BoonDPS", "DPS", "Power-DPS", "Condi-DPS"]


# ----------------------------------------------------------------------------- Konfiguration
def _check_factors(tbl, where, problems):
    out = {}
    if not isinstance(tbl, dict):
        problems.append(f"{where}: muss eine Tabelle sein")
        return out
    for role, v in tbl.items():
        if isinstance(v, str):
            try:
                report_engine.compile_expr(v)
            except ValueError as e:
                problems.append(f"{where}.{role}: {e}")
                continue
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            problems.append(f"{where}.{role}: muss eine Zahl > 0 oder eine Formel (Text) sein")
            continue
        out[role] = v
    return out


def load_config(path):
    """Rueckgabe: (cfg, problems). Fehlt die Datei, gelten Defaults (BoonDPS 0.8, Heal ausgeschlossen)."""
    problems = []
    default = {"only_success": True, "chart_max_logs": 30, "exclude_roles": ["Heal"], "feedback_min_percent": 10,
               "overview_roles": DEFAULT_OVERVIEW_ROLES, "global": {"BoonDPS": 0.8}, "boss": {}}
    try:
        data = tomllib.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError:
        return default, [f"{path} nicht gefunden - Standardwerte werden genutzt"]
    except tomllib.TOMLDecodeError as e:
        return default, [f"{path}: ungueltiges TOML ({e})"]
    bad = sorted(set(data) - CFG_KEYS)
    if bad:
        problems.append(f"{path}: unbekannte Schluessel {bad}")
    cfg = {"only_success": data.get("only_success", True), "exclude_roles": data.get("exclude_roles", ["Heal"]),
           "feedback_min_percent": data.get("feedback_min_percent", 10),
           "chart_max_logs": data.get("chart_max_logs", 30),
           "overview_roles": data.get("overview_roles", DEFAULT_OVERVIEW_ROLES),
           "global": _check_factors(data.get("dps_factor", {}), "dps_factor", problems), "boss": {}}
    if not isinstance(cfg["only_success"], bool):
        problems.append("only_success muss true/false sein")
    cm = cfg["chart_max_logs"]
    if isinstance(cm, bool) or not isinstance(cm, int) or cm < 1:
        problems.append("chart_max_logs muss eine ganze Zahl >= 1 sein")
        cfg["chart_max_logs"] = 30
    fm = cfg["feedback_min_percent"]
    if isinstance(fm, bool) or not isinstance(fm, (int, float)) or not 0 <= fm < 100:
        problems.append("feedback_min_percent muss eine Zahl von 0 bis unter 100 sein")
        cfg["feedback_min_percent"] = 10
    for k, dflt in (("exclude_roles", ["Heal"]), ("overview_roles", DEFAULT_OVERVIEW_ROLES)):
        v = cfg[k]
        if isinstance(v, str):
            cfg[k] = [v]
        elif not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            problems.append(f"{k} muss eine Liste von Rollennamen sein")
            cfg[k] = dflt
    for key, blk in (data.get("boss") or {}).items():
        if not isinstance(blk, dict) or set(blk) - {"dps_factor"}:
            problems.append(f"boss.{key}: erlaubt ist nur [boss.\"{key}\".dps_factor]")
            continue
        cfg["boss"][str(key)] = _check_factors(blk.get("dps_factor", {}), f"boss.{key}.dps_factor", problems)
    return cfg, problems


def factor_table(cfg, boss_key):
    """Globale Faktoren + Boss-Abschnitt (CM ohne eigenen Abschnitt -> Normalmodus)."""
    key = str(boss_key)
    blk = cfg["boss"].get(key)
    if blk is None and key.startswith("-"):
        blk = cfg["boss"].get(key.lstrip("-"))
    return {**cfg["global"], **(blk or {})}


def dps_factor(cfg, boss_key, roles, values):
    """Produkt der Faktoren aller Rollen des Spielers (+ "*"). values = evaluate()[..]["values"]."""
    tbl = factor_table(cfg, boss_key)
    f = 1.0
    for r in [*roles, "*"]:
        if r not in tbl:
            continue
        v = tbl[r]
        if isinstance(v, str):
            v = report_engine.eval_formula(v, own=values.get("mechanics"), buffs=values.get("buffs"),
                                           metrics=values.get("metrics"))
            if v is None:
                return None
        f *= v
    return f if f > 0 else None


def is_excluded(cfg, roles):
    return bool(set(roles) & set(cfg["exclude_roles"]))


# ----------------------------------------------------------------------------- Zahlen
def fmt_num(v):
    """Deutsche Schreibweise: 23.456 bzw. 0,42."""
    if v is None:
        return "–"
    if abs(v) >= 100 or float(v).is_integer():
        return f"{v:,.0f}".replace(",", ".")
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _nice_step(raw):
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


# ----------------------------------------------------------------------------- SVG
PAD_L, PAD_R, PAD_T, PAD_B = 50, 22, 8, 20


def line_chart(labels, series, height=180, label="Chart", width=640):
    """labels: x-Beschriftung je Log (z. B. '20.11.'), series: [{"cls": "l1", "values": [...], "titles": [...],
    "dots": True}] - Werte je x-Position, None = keine Daten. Gibt '' zurueck, wenn es nichts zu zeichnen gibt."""
    n = len(labels)
    vals = [v for s in series for v in s["values"] if v is not None]
    if not n or not vals:
        return ""
    step = _nice_step(max(max(vals), 0) / 4) if max(vals) > 0 else 1
    ymax = step * max(1, math.ceil(max(vals) / step - 1e-9))
    W = width
    ih, iw = height - PAD_T - PAD_B, W - PAD_L - PAD_R

    def x(i):
        return PAD_L + (iw / 2 if n == 1 else i * iw / (n - 1))

    def y(v):
        return PAD_T + ih - (v / ymax) * ih

    out = [f'<svg class="ch" viewBox="0 0 {W} {height}" role="img" aria-label="{esc(label)}">']
    t = 0.0
    while t <= ymax + 1e-9:                                    # waagerechte Hilfslinien + y-Beschriftung
        yy = round(y(t), 1)
        out.append(f'<line class="g" x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy}" y2="{yy}"/>'
                   f'<text x="{PAD_L - 4}" y="{yy + 4}" text-anchor="end">{fmt_num(t)}</text>')
        t += step
    shown, last_x, prev = [], -999, None                      # x-Beschriftung: nur bei neuem Datum, mit Abstand
    for i, lb in enumerate(labels):
        if lb != prev and x(i) - last_x >= 46:
            shown.append(i)
            last_x = x(i)
        prev = lb
    for i in shown:
        out.append(f'<text x="{round(x(i), 1)}" y="{height - 5}" text-anchor="middle">{esc(labels[i])}</text>')
    for s in series:
        out.append(f'<g class="{s["cls"]}">')
        seg = []
        for i, v in enumerate(s["values"] + [None]):          # Linie an Luecken unterbrechen
            if v is None:
                if len(seg) > 1:
                    out.append('<polyline points="' + " ".join(f"{a},{b}" for a, b in seg) + '"/>')
                seg = []
            else:
                seg.append((round(x(i), 1), round(y(v), 1)))
        if s.get("dots", True):
            titles = s.get("titles") or [None] * n
            for i, v in enumerate(s["values"]):
                if v is not None:
                    tt = f"<title>{esc(titles[i])}</title>" if titles[i] else ""
                    out.append(f'<circle cx="{round(x(i), 1)}" cy="{round(y(v), 1)}" r="3">{tt}</circle>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def legend(items):
    """items: [(cls, Text)]"""
    return '<div class="legend">' + "".join(f'<span><i class="sw {c}"></i>{esc(t)}</span>' for c, t in items) + "</div>"
