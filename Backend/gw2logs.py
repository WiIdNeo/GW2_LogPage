#!/usr/bin/env python3
"""GW2-Log-Pipeline: EI-Upload (dps.report/Wingman) -> SQLite -> Wingman-Ratings.

Ablauf:  1. Logs im Ordner finden (bereits verarbeitete werden uebersprungen)
         2. pro neuem Log EI-CLI ausfuehren (Upload; JSON nur als Zwischenprodukt -> gekuerzt in log_analysis)
         3. dps.report-Metadaten holen (Boss, Spieler, Charaktere)
         4. Logs per /api/importLogQueued bei Wingman anmelden, auf Import warten,
            dann Wingman-Noten pro Whitelist-Spieler, in Batches, idempotent
         5. Gesamtrating + Killproofs/Charaktere pro Spieler (je einmal am Ende)
"""
import argparse, glob, hashlib, json, logging, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests
import extract_log                      # gekuerzte EI-JSON (liegt neben diesem Script)

GRADES = ["SSS", "SS", "S", "A+", "A", "B+", "B", "C+", "C", "D", "E", "F"]  # Index+1 = Zahl
CATS = ["Damage", "Mechanics", "Speed", "Support", "Teamplay"]
WM = "https://gw2wingman.nevermindcreations.de"
DPS_META = "https://dps.report/getUploadMetadata"
EXTS = (".zevtc", ".evtc", ".evtc.zip")
MAX_ATTEMPTS = 3
log = logging.getLogger("gw2")

SCHEMA = """
CREATE TABLE IF NOT EXISTS grade(name TEXT PRIMARY KEY, num INTEGER);
CREATE TABLE IF NOT EXISTS boss(
  key TEXT PRIMARY KEY, game_id INTEGER, name TEXT, short TEXT, type TEXT, wing INTEGER,
  sort INTEGER, is_cm INTEGER, enabled INTEGER DEFAULT 0);   -- key: '17154' normal, '-17154' CM
CREATE TABLE IF NOT EXISTS player(
  account TEXT PRIMARY KEY, whitelisted INTEGER DEFAULT 0,
  global_rank TEXT, global_rank_num REAL, ranks_json TEXT, commander_ratio REAL,
  main_professions_json TEXT, bosses_completed INTEGER,
  best_updated_at TEXT, kp_updated_at TEXT);
CREATE TABLE IF NOT EXISTS character(
  name TEXT PRIMARY KEY, account TEXT, class TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS log(
  file_hash TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime REAL,
  dps_status TEXT, dps_url TEXT,            -- ok | rejected | error | parse_failed | skipped
  wm_status TEXT, wm_id TEXT, wm_url TEXT,  -- pending | ok | rejected | error | n/a
  wm_cands TEXT, wm_attempts INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
  wm_queued INTEGER DEFAULT 0, wm_queued_at TEXT,   -- 0 nicht gemeldet | 1 in Wingman-Queue | 2 in Wingman-DB
  boss_key TEXT, success INTEGER, encounter_time TEXT, players_json TEXT,
  process_failed INTEGER DEFAULT 0, retry INTEGER DEFAULT 0, yt_url TEXT, ei_output TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS log_player(
  file_hash TEXT, account TEXT, character TEXT, class TEXT,
  state TEXT,                               -- rated | absent
  damage TEXT, mechanics TEXT, speed TEXT, support TEXT, teamplay TEXT,
  PRIMARY KEY(file_hash, account));
CREATE TABLE IF NOT EXISTS grade_count(
  account TEXT, boss_key TEXT, category TEXT, grade TEXT, n INTEGER DEFAULT 0,
  PRIMARY KEY(account, boss_key, category, grade));
CREATE TABLE IF NOT EXISTS player_boss_best(
  account TEXT, boss_key TEXT, category TEXT, grade TEXT,
  PRIMARY KEY(account, boss_key, category));
CREATE TABLE IF NOT EXISTS player_kp(
  account TEXT, boss_key TEXT, total INTEGER, by_class_json TEXT,
  PRIMARY KEY(account, boss_key));
CREATE TABLE IF NOT EXISTS log_analysis(
  file_hash TEXT PRIMARY KEY,               -- = log.file_hash
  dps_slug TEXT, boss_key TEXT, duration_ms INTEGER, success INTEGER,
  schema_ver INTEGER, analyzed_at TEXT,
  data TEXT);                               -- gekuerzte EI-JSON (extract_log.py), kompakt

CREATE VIEW IF NOT EXISTS v_avg_boss AS
SELECT account, boss_key, category, logs, avg_num,
  (SELECT name FROM grade WHERE num = MAX(1, MIN(12, CAST(ROUND(avg_num) AS INTEGER)))) AS avg_grade
FROM (SELECT c.account, c.boss_key, c.category, SUM(c.n) AS logs,
             SUM(g.num * c.n) * 1.0 / SUM(c.n) AS avg_num
      FROM grade_count c JOIN grade g ON g.name = c.grade
      GROUP BY c.account, c.boss_key, c.category);
CREATE VIEW IF NOT EXISTS v_avg_player AS
SELECT account, category, logs, avg_num,
  (SELECT name FROM grade WHERE num = MAX(1, MIN(12, CAST(ROUND(avg_num) AS INTEGER)))) AS avg_grade
FROM (SELECT c.account, c.category, SUM(c.n) AS logs,
             SUM(g.num * c.n) * 1.0 / SUM(c.n) AS avg_num
      FROM grade_count c JOIN grade g ON g.name = c.grade
      GROUP BY c.account, c.category);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP
def http(method, url, retries=4, **kw):
    for i in range(retries):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if i == retries - 1:
                raise
            time.sleep(2 ** (i + 1))


# ---------------------------------------------------------------- DB
def reset_db_files(path):
    """Loescht die DB samt WAL/SHM-Dateien (nur mit --reset-db)."""
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        if f.exists():
            f.unlink()
            log.warning("DB-Datei geloescht: %s", f)


def migrate(db):
    """Ergaenzt fehlende Spalten in bestehenden Tabellen (additiv, nie destruktiv)."""
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA)
    for (tbl,) in mem.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        have = {r[1] for r in db.execute(f"PRAGMA table_info({tbl})")}
        for _, col, typ, _, dflt, _ in mem.execute(f"PRAGMA table_info({tbl})"):
            if col not in have:
                db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}" + (f" DEFAULT {dflt}" if dflt is not None else ""))
                log.info("DB-Migration: %s.%s ergaenzt", tbl, col)
    mem.close()


def open_db(path, whitelist, bosses):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)          # nur CREATE ... IF NOT EXISTS: bestehende Daten bleiben erhalten
    migrate(db)
    db.executemany("INSERT OR IGNORE INTO grade VALUES(?,?)", [(g, i + 1) for i, g in enumerate(GRADES)])
    db.execute("UPDATE player SET whitelisted=0")
    for acc in whitelist:
        db.execute("INSERT INTO player(account, whitelisted) VALUES(?,1) "
                   "ON CONFLICT(account) DO UPDATE SET whitelisted=1", (acc,))
    sync_bosses(db, bosses)
    db.commit()
    return db


def load_bosses(path):
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))["boss"]


def sync_bosses(db, entries):
    """bosses.toml -> Tabelle boss (je Modus eine Zeile: ID und -ID fuer CM)."""
    for i, b in enumerate(entries):
        variants = []
        if not b.get("only_cm"):
            variants.append((str(b["id"]), 0, ""))
        if b.get("cm") or b.get("only_cm"):
            variants.append((str(-abs(b["id"])), 1, " CM"))
        for j, (key, cm, suffix) in enumerate(variants):
            db.execute(
                "INSERT INTO boss(key,game_id,name,short,type,wing,sort,is_cm,enabled) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET game_id=excluded.game_id, name=excluded.name, short=excluded.short, "
                "type=excluded.type, wing=excluded.wing, sort=excluded.sort, is_cm=excluded.is_cm, enabled=excluded.enabled",
                (key, b["id"], b["name"] + suffix, b.get("short"), b.get("type"), b.get("wing"),
                 i * 10 + j, cm, int(b.get("enabled", False))))


def save_log(db, d):
    cols = ",".join(d)
    upd = ",".join(f"{k}=excluded.{k}" for k in d if k != "file_hash")
    db.execute(f"INSERT INTO log({cols}) VALUES({','.join(':' + k for k in d)}) "
               f"ON CONFLICT(file_hash) DO UPDATE SET {upd}", d)


# ---------------------------------------------------------------- Dateien
def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


FOLDER_ID = re.compile(r"\((\d+)\)\s*$")


def skip_sets(bosses):
    """IDs deaktivierter Bosse (inkl. aliases). Ordnernamen haben die Form 'Boss Name (ID)'."""
    enabled = {i for b in bosses if b.get("enabled") for i in [b["id"], *b.get("aliases", [])]}
    disabled = {i for b in bosses if not b.get("enabled") for i in [b["id"], *b.get("aliases", [])]}
    return disabled - enabled


def find_new_files(db, root, skip_patterns, skip_ids, limit=None):
    """Liefert Dateien, die verarbeitet werden muessen (neu oder Retry nach Fehler).
    Logs in Ordnern deaktivierter Bosses oder mit Namens-Pattern werden vor dem Upload verworfen."""
    pats = [p.lower() for p in skip_patterns]
    todo = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or not p.name.lower().endswith(EXTS):
            continue
        folder = p.parent.name
        m = FOLDER_ID.search(folder)
        if (m and int(m.group(1)) in skip_ids) or any(x in (folder + "/" + p.name).lower() for x in pats):
            continue
        st = p.stat()
        known = db.execute("SELECT retry, attempts FROM log WHERE path=? AND size=? AND mtime=?",
                           (str(p), st.st_size, st.st_mtime)).fetchone()
        if known is None:
            h = sha1(p)
            known = db.execute("SELECT retry, attempts FROM log WHERE file_hash=?", (h,)).fetchone()
        else:
            h = None
        if known and not (known["retry"] and known["attempts"] < MAX_ATTEMPTS):
            continue
        h = h or sha1(p)
        todo.append(dict(path=p, hash=h, size=st.st_size, mtime=st.st_mtime,
                         attempts=known["attempts"] if known else 0))
        if limit and len(todo) >= limit:
            break
    return todo


# ---------------------------------------------------------------- EI
# Nur Schluessel, die in der offiziellen Settings/sample.conf stehen. Rechen-Einstellungen
# (ParsePhases, Compute*, ParseCombatReplay ...) bleiben bewusst unangetastet (Defaults der
# sample.conf), damit der Wingman-Upload (uploadProcessed) vollstaendige Daten bekommt.
EI_OVERRIDES = {
    "SaveAtOut": "false", "SaveOutHTML": "false", "SaveOutCSV": "false", "SaveOutJSON": "true",
    "IndentJSON": "false", "RawTimelineArrays": "true", "CompressRaw": "false",   # JSON nur Zwischenprodukt; Graph-Daten (targetDamage1S) noetig
    "SaveOutTrace": "false", "ApplicationTraces": "false",
    "UploadToDPSReports": "true", "UploadToWingman": "false", "UploadToRaidar": "false",
    "SendEmbedToWebhook": "false", "SendSimpleMessageToWebhook": "false", "AutoDiscordBatch": "false",
    "ParseMultipleLogs": "false", "SkipFailedTries": "false", "AutoAdd": "false", "AutoParse": "false",
}


def write_conf(ei_cfg, tmpdir):
    lines = {}
    tpl = ei_cfg.get("conf_template")
    if tpl:
        for ln in Path(tpl).read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                lines[k.strip()] = v.strip()
    lines.update(EI_OVERRIDES)
    lines["OutLocation"] = str(tmpdir)
    if ei_cfg.get("dps_user_token"):
        lines["DPSReportUserToken"] = ei_cfg["dps_user_token"]
    out = Path(tmpdir) / "settings.conf"
    out.write_text("\n".join(f"{k}={v}" for k, v in lines.items()), encoding="utf-8")
    return out


def run_ei(ei_cfg, conf, path):
    cmd = list(ei_cfg["command"]) + ["-c", str(conf), str(path)]
    try:
        env = {**os.environ, **{k: str(v) for k, v in ei_cfg.get("env", {}).items()}}
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=ei_cfg.get("timeout", 900), env=env)
        return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def _flag(blob, name):
    m = re.search(rf'"?{name}"?\s*[:=]\s*"?(true|false)', blob, re.I)
    return bool(m) and m.group(1).lower() == "true"


def parse_ei_output(out):
    """Liest die Zeile 'Processed - {...}' der CLI (Felder laut offiziellem README).
    Toleranter Regex-Parser, falls das 'JSON-aehnliche' Objekt kein striktes JSON ist."""
    m = re.search(r"Processed\s*-\s*(.*)", out, re.S)
    if not m:
        return None
    blob = m.group(1)
    link = re.search(r'"?dpsReportLink"?\s*[:=]\s*"?(https?://[^",}\s]+)', blob)
    reason = re.search(r'"?reason"?\s*[:=]\s*"?(\w+)', blob)
    return dict(parsed=_flag(blob, "parsed"), reason=reason.group(1) if reason else None,
                dps_link=link.group(1) if link else None,
                dps_tried=_flag(blob, "dpsReportUploadTentative"), dps_failed=_flag(blob, "dpsReportUploadFailed"),
                wm_tried=_flag(blob, "wingmanUploadTentative"), wm_refused=_flag(blob, "wingmanUploadRefused"),
                wm_failed=_flag(blob, "wingmanUploadFailed"))


def fetch_dps_meta(url):
    try:
        return http("GET", DPS_META, params={"permalink": url}, timeout=30).json()
    except Exception as e:
        log.warning("dps.report-Metadaten nicht abrufbar (%s): %s", url, e)
        return None


def parse_meta(meta):
    enc = meta.get("encounter") or {}
    gid, cm = enc.get("bossId"), bool(enc.get("isCm"))
    key = None if gid is None else str(-abs(gid) if cm else abs(gid))
    players = {}
    for char, p in (meta.get("players") or {}).items():
        acc = p.get("display_name") or p.get("account")
        if not acc:
            continue
        spec = p.get("elite_spec") if isinstance(p.get("elite_spec"), str) else None
        players[acc.casefold()] = {"account": acc, "character": p.get("character_name") or char,
                                   "class": spec or str(p.get("profession") or "")}
    t = meta.get("encounterTime") or meta.get("uploadTime")
    when = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if isinstance(t, (int, float)) else None
    return dict(boss_key=key, game_id=gid, boss_name=enc.get("boss"), is_cm=int(cm),
                success=int(bool(enc.get("success"))) if "success" in enc else None,
                when=when, players=players or None)


_dump_lock = threading.Lock()


def _safe(name):
    return re.sub(r"[^\w.+-]+", "_", name or "unknown").strip("_") or "unknown"


def load_filter(cfg):
    """Boss-Filter (Ziele/Mechaniken) aus [analysis] filter_file, relativ zur config.toml."""
    name = cfg.get("analysis", {}).get("filter_file", "analysis_filter.toml")
    if not name:
        return {}
    p = Path(cfg.get("_dir", ".")) / name
    if not p.exists():
        log.warning("Analyse-Filter %s nicht gefunden - es wird nur nach Whitelist-Spielern gekuerzt", p)
        return {}
    return extract_log.load_filters(p)


def analyze_json(cfg, tmpdir, f, meta, flt, wl):
    """Kuerzt die von EI erzeugte JSON (extract_log.extract) und gibt das Ergebnis zurueck (oder None).
    Die ausfuehrliche JSON wird IMMER aus dem Temp-Ordner geloescht. Laeuft im Worker-Thread, fasst die DB
    nicht an. Optional: gekuerzte JSON nach [analysis] slim_dir (Debugging), Original der ersten
    raw_keep_per_boss Logs je Boss nach raw_dir (zum Ansehen neuer Bosse).
    Achtung Speicher: pro paralleler EI-Instanz wird die komplette JSON einmal geladen."""
    acfg = cfg.get("analysis", {})
    src = f["path"]
    base = src.name
    for e in EXTS:
        if base.lower().endswith(e):
            base = base[:-len(e)]
            break
    found = sorted(Path(tmpdir).glob(glob.escape(base) + "*.json"), key=lambda x: x.stat().st_mtime)
    if not found:
        log.warning("Keine EI-JSON fuer %s gefunden (SaveOutJSON/OutLocation pruefen)", src.name)
        return None
    j = found[-1]
    slim = None
    try:
        if acfg.get("enabled", True):
            label, bkey = "unknown", None
            if meta:
                try:
                    m = parse_meta(meta)
                    bkey = m["boss_key"]
                    label = _safe(m["boss_name"] or m["boss_key"]) + ("_CM" if m["is_cm"] else "")
                except Exception:
                    pass
            root = extract_log.load_json(j)
            slim, st = extract_log.extract(root, graph=acfg.get("graph", "full"), source_name=src.name,
                                           whitelist=wl, filters=flt, filter_keys=[bkey] if bkey else ())
            for w in st["warnings"]:
                log.warning("%s: %s", src.name, w)
            if acfg.get("slim_dir", "json_slim"):
                d = Path(acfg.get("slim_dir", "json_slim")) / label
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{f['hash'][:8]}_{extract_log.out_name(j)}").write_text(
                    json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            keep_raw = int(acfg.get("raw_keep_per_boss", 0))
            if keep_raw > 0 and acfg.get("raw_dir"):
                target = Path(acfg["raw_dir"]) / label
                with _dump_lock:
                    target.mkdir(parents=True, exist_ok=True)
                    if len(list(target.glob("*.json"))) < keep_raw:
                        shutil.copy2(j, target / j.name)
                        log.info("Original-JSON behalten: %s", target / j.name)
    except Exception:
        log.exception("Analyse fehlgeschlagen: %s", src.name)
        slim = None
    finally:
        for x in found:                             # ausfuehrliche JSON nie liegen lassen
            x.unlink(missing_ok=True)
    return slim


def store_analysis(db, file_hash, slim):
    """Schreibt die gekuerzte JSON in log_analysis (nur Hauptthread, NACH ei_store)."""
    ph = slim.get("phases") or []
    ok = (slim.get("meta") or {}).get("success")
    db.execute(
        "INSERT INTO log_analysis(file_hash,dps_slug,boss_key,duration_ms,success,schema_ver,analyzed_at,data) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(file_hash) DO UPDATE SET dps_slug=excluded.dps_slug, "
        "boss_key=excluded.boss_key, duration_ms=excluded.duration_ms, success=excluded.success, "
        "schema_ver=excluded.schema_ver, analyzed_at=excluded.analyzed_at, data=excluded.data",
        (file_hash, slim.get("dps_slug"), slim.get("boss_key"), ph[0].get("end") if ph else None,
         None if ok is None else int(bool(ok)), slim.get("schema"), now(),
         json.dumps(slim, ensure_ascii=False, separators=(",", ":"))))
    db.commit()


def ei_stage(db, cfg, files, debug, jobs=1):
    """EI-Aufrufe laufen in `jobs` parallelen Threads (je ein dotnet-Prozess); der Netzwerkabruf der
    dps.report-Metadaten ebenfalls. ALLE DB-Zugriffe bleiben im Hauptthread (sqlite3-Verbindung ist
    nicht thread-sicher): die Worker liefern nur Ergebnisse, der Hauptthread verbucht sie."""
    wl = {a.casefold() for a in cfg["whitelist"]}
    flt = load_filter(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        conf = write_conf(cfg["ei"], tmp)         # wird von allen Prozessen nur gelesen

        def work(f):
            log.info("EI start: %s", f["path"].name)
            rc, out = run_ei(cfg["ei"], conf, f["path"])
            r = parse_ei_output(out)
            meta = fetch_dps_meta(r["dps_link"]) if r and r["dps_link"] else None
            slim = analyze_json(cfg, tmp, f, meta, flt, wl)
            return f, rc, out, r, meta, slim

        fails = 0
        ex = ThreadPoolExecutor(max_workers=max(1, jobs))
        try:
            futs = [ex.submit(work, f) for f in files]
            for i, fut in enumerate(as_completed(futs), 1):
                f, rc, out, r, meta, slim = fut.result()
                log.info("[%d/%d] EI fertig: %s", i, len(files), f["path"].name)
                if debug:
                    print("----- EI-Ausgabe -----\n" + out + "\n----------------------")
                if r is None and out != "timeout":
                    fails += 1
                    log.error("Keine 'Processed - '-Zeile (rc=%s). Letzte Ausgabe:\n%s", rc, out.strip()[-600:])
                    if fails >= 3:
                        ex.shutdown(wait=False, cancel_futures=True)
                        sys.exit("Abbruch: EI startet nicht (3x ohne Ergebnis). Aufruf/Runtime pruefen (--debug).")
                    continue                                # nicht speichern -> naechster Lauf versucht es neu
                fails = 0
                ei_store(db, wl, f, rc, out, r, meta)
                if slim:                                    # Zeile in log existiert jetzt
                    store_analysis(db, f["hash"], slim)
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            ex.shutdown(wait=True)


def ei_store(db, wl, f, rc, out, r, meta):
    """Verbucht das Ergebnis eines EI-Laufs in der DB (nur Hauptthread!)."""
    dps_url = r["dps_link"] if r else None
    slug = dps_url.rstrip("/").rsplit("/", 1)[-1] if dps_url else None
    retry, wm_status = 0, "pending"
    if r is None:                                   # kein 'Processed - ' -> Absturz/Setup-Fehler
        dps_status, retry = "error", 1
        log.warning("Keine 'Processed - '-Zeile in der EI-Ausgabe (rc=%s). Mit --debug pruefen.", rc)
    elif not r["parsed"]:
        dps_status, wm_status = "parse_failed", "n/a"
        retry = int(r["reason"] == "Fatal")
        log.warning("EI konnte das Log nicht parsen (reason=%s)", r["reason"])
    elif dps_url:
        dps_status = "ok"
    else:
        dps_status, retry = "error", int(r["dps_tried"] or r["dps_failed"])
        if not r["dps_tried"]:
            log.warning("EI hat nicht zu dps.report hochgeladen - UploadToDPSReports in der Config pruefen")
    # Wingman-Upload erfolgt ausschliesslich ueber den dps.report-Link (importLogQueued), nicht ueber EI.
    d = dict(file_hash=f["hash"], path=str(f["path"]), size=f["size"], mtime=f["mtime"],
             dps_url=dps_url, wm_cands=json.dumps([slug] if slug else []), wm_attempts=0,
             attempts=f["attempts"] + 1, ei_output=out[-4000:], created_at=now(),
             dps_status=dps_status, wm_status=wm_status, retry=retry,
             boss_key=None, success=None, encounter_time=None, players_json=None,
             wm_id=None, wm_url=None, process_failed=0, yt_url=None)
    if d["attempts"] >= MAX_ATTEMPTS:
        d["retry"] = 0
        if d["dps_status"] == "error":
            d["dps_status"] = "rejected"
    if meta:
        m = parse_meta(meta)
        had_players = bool(m["players"])
        # Datenschutz: nur Whitelist-Spieler speichern
        m["players"] = {k: v for k, v in (m["players"] or {}).items() if k in wl} or None
        d.update(boss_key=m["boss_key"], success=m["success"], encounter_time=m["when"],
                 players_json=json.dumps(m["players"]) if m["players"] else None)
        if m["boss_key"]:
            row = db.execute("SELECT enabled FROM boss WHERE key=?", (m["boss_key"],)).fetchone()
            if row is None:
                log.warning("Boss %s (%s) fehlt in bosses.toml - wird ignoriert (enabled=false)",
                            m["boss_key"], m["boss_name"])
                db.execute("INSERT INTO boss(key,game_id,name,is_cm,enabled) VALUES(?,?,?,?,0)",
                           (m["boss_key"], m["game_id"], m["boss_name"], m["is_cm"]))
            if row is None or not row["enabled"]:
                d["wm_status"] = "n/a"      # Boss nicht aktiviert
        for p in (m["players"] or {}).values():
            db.execute("INSERT INTO character(name,account,class,last_seen) VALUES(?,?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET account=excluded.account, "
                       "class=excluded.class, last_seen=excluded.last_seen",
                       (p["character"], p["account"], p["class"], m["when"] or now()))
        if had_players and not m["players"]:
            d["wm_status"] = "n/a"      # kein Whitelist-Spieler im Log
    save_log(db, d)
    db.commit()


# ---------------------------------------------------------------- Wingman
def wm_rate(account, ids):
    r = http("POST", f"{WM}/api/ratePerformanceOfPlayerInLogs",
             json={"logs": ids, "playerAccount": account}, timeout=120)
    data = r.json()
    if not isinstance(data, list) or len(data) != len(ids):
        raise RuntimeError(f"Unerwartete Wingman-Antwort fuer {account}: {str(data)[:200]}")
    return data


def store_rating(db, row, acc, info, wm_id, ranks):
    """Schreibt log_player und zaehlt die Noten hoch - alles in einer Transaktion."""
    grades = [ranks.get(c) if ranks and ranks.get(c) in GRADES else None for c in CATS]
    with db:
        cur = db.execute(
            "INSERT OR IGNORE INTO log_player VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["file_hash"], acc, info.get("character"), info.get("class"),
             "rated" if ranks else "absent", *grades))
        if cur.rowcount == 1 and ranks:
            bk = row["boss_key"] or "unknown"
            for cat, g in zip(CATS, grades):
                if g:
                    db.execute("INSERT INTO grade_count VALUES(?,?,?,?,1) ON CONFLICT"
                               "(account,boss_key,category,grade) DO UPDATE SET n=n+1",
                               (acc, bk, cat, g))
        db.execute("UPDATE log SET wm_id=?, wm_url=?, wm_status='ok' WHERE file_hash=?",
                   (wm_id, f"{WM}/log/{wm_id}", row["file_hash"]))


def rate_player(db, acc, hashes, batch_size, pause):
    """Gibt (gefunden, 'Log fehlt bei Wingman') als Mengen von file_hash zurueck."""
    rows = {h: db.execute("SELECT * FROM log WHERE file_hash=?", (h,)).fetchone() for h in hashes}
    found, missing = set(), set(hashes)
    for ci in range(3):                                   # Kandidaten-IDs der Reihe nach
        queue = []
        for h in list(missing):
            r = rows[h]
            cands = ([r["wm_id"]] if r["wm_id"] else []) + json.loads(r["wm_cands"] or "[]")
            if ci < len(cands):
                queue.append((h, cands[ci]))
        for i in range(0, len(queue), batch_size):
            chunk = queue[i:i + batch_size]
            data = wm_rate(acc, [c for _, c in chunk])
            time.sleep(pause)
            for (h, cid), item in zip(chunk, data):
                note = item.get("note", "")
                if "ranks" in item:
                    pj = json.loads(rows[h]["players_json"] or "{}")
                    store_rating(db, rows[h], acc, pj.get(acc.casefold(), {}), cid, item["ranks"])
                elif "Player could not be found" in note:
                    pj = json.loads(rows[h]["players_json"] or "{}")
                    store_rating(db, rows[h], acc, pj.get(acc.casefold(), {}), cid, None)
                elif "Log could not be found" in note:
                    continue                              # naechster Kandidat
                else:
                    log.warning("Unbekannte Wingman-Antwort %s: %s", cid, item)
                    continue
                found.add(h)
                missing.discard(h)
    return found, missing


def wm_id_from_url(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def wm_check(link):
    """Status eines dps.report-Links bei Wingman: {inDB, inQueue, targetURL, ...}."""
    return http("GET", f"{WM}/api/checkLogQueuedOrDB", params={"link": link}, timeout=30).json()


def wm_import(link):
    """Meldet einen dps.report-Link bei Wingman zum Import an (Queue).
    Antwort: {"success": 1, "futureLink": ".../log/<ID>", "note": ...}"""
    return http("GET", f"{WM}/api/importLogQueued", params={"link": link}, timeout=30).json()


def queue_imports(db, pause):
    """Stellt sicher, dass jedes Log mit dps.report-Link bei Wingman in der DB oder Import-Queue liegt.
    Nur dann kann /api/ratePerformanceOfPlayerInLogs das Log finden."""
    rows = db.execute("SELECT file_hash, dps_url, wm_cands FROM log WHERE wm_status='pending' "
                      "AND wm_queued=0 AND dps_url IS NOT NULL").fetchall()
    if rows:
        log.info("Wingman: %d Logs pruefen/zum Import anmelden", len(rows))
    for r in rows:
        state, wid = 0, None
        try:                                            # evtl. schon vorhanden (z. B. EI-Direktupload)
            d = wm_check(r["dps_url"])
            time.sleep(pause)
            if d.get("inDB") or d.get("inQueue"):
                state = 2 if d.get("inDB") else 1
                wid = wm_id_from_url(d["targetURL"]) if d.get("targetURL") else None
        except Exception as e:
            log.warning("checkLogQueuedOrDB fehlgeschlagen (%s): %s", r["dps_url"], e)
        if not state:
            try:
                d = wm_import(r["dps_url"])
                time.sleep(pause)
            except Exception as e:
                log.warning("importLogQueued fehlgeschlagen (%s), naechster Lauf versucht es erneut: %s", r["dps_url"], e)
                continue
            if d.get("success") == 1 and d.get("futureLink"):
                state, wid = 1, wm_id_from_url(d["futureLink"])
            else:
                log.warning("Wingman hat den Import abgelehnt (%s): %s", r["dps_url"], d.get("note") or d)
                db.execute("UPDATE log SET wm_attempts=wm_attempts+1, "
                           "wm_status=CASE WHEN wm_attempts+1>=? THEN 'error' ELSE wm_status END "
                           "WHERE file_hash=?", (MAX_ATTEMPTS, r["file_hash"]))
                db.commit()
                continue
        cands = json.loads(r["wm_cands"] or "[]")
        if wid:
            cands = [wid] + [c for c in cands if c != wid]
        db.execute("UPDATE log SET wm_queued=?, wm_queued_at=?, wm_id=COALESCE(?, wm_id), wm_cands=? WHERE file_hash=?",
                   (state, now(), wid, json.dumps(cands), r["file_hash"]))
        db.commit()


def wait_for_imports(db, wait, poll, pause, stale_hours=24):
    """Wartet bis `wait` Sekunden darauf, dass Logs aus der Wingman-Queue in der DB landen.
    Nicht fertige bleiben 'pending' (ohne Fehlversuch) und werden beim naechsten Lauf erneut geprueft."""
    deadline = time.time() + wait
    while True:
        rows = db.execute("SELECT file_hash, dps_url, wm_queued_at FROM log "
                          "WHERE wm_status='pending' AND wm_queued=1").fetchall()
        if not rows:
            return
        for r in rows:
            try:
                d = wm_check(r["dps_url"])
                time.sleep(pause)
            except Exception as e:
                log.warning("checkLogQueuedOrDB fehlgeschlagen (%s): %s", r["dps_url"], e)
                continue
            if d.get("inDB"):
                db.execute("UPDATE log SET wm_queued=2 WHERE file_hash=?", (r["file_hash"],))
            elif not d.get("inQueue"):
                # weder in DB noch in Queue -> Import ist verloren gegangen, neu anmelden
                db.execute("UPDATE log SET wm_queued=0 WHERE file_hash=?", (r["file_hash"],))
        db.commit()
        left = db.execute("SELECT COUNT(*) FROM log WHERE wm_status='pending' AND wm_queued=1").fetchone()[0]
        if not left or time.time() >= deadline:
            break
        log.info("Wingman-Queue: %d Logs warten noch ...", left)
        time.sleep(poll)
    cutoff = datetime.now(timezone.utc).timestamp() - stale_hours * 3600
    for r in db.execute("SELECT file_hash, wm_queued_at FROM log WHERE wm_status='pending' AND wm_queued=1").fetchall():
        try:
            if datetime.fromisoformat(r["wm_queued_at"]).timestamp() < cutoff:
                log.warning("Log seit >%dh in der Wingman-Queue - als Fehler markiert", stale_hours)
                db.execute("UPDATE log SET wm_status='error' WHERE file_hash=?", (r["file_hash"],))
        except (TypeError, ValueError):
            pass
    db.commit()


def wingman_stage(db, cfg):
    bs, pause = cfg["wingman"]["batch_size"], cfg["wingman"]["pause"]
    queue_imports(db, pause)
    wait_for_imports(db, cfg["wingman"]["wait"], cfg["wingman"]["poll"], pause)
    wl = cfg["whitelist"]
    pending = db.execute("SELECT * FROM log WHERE wm_status='pending' AND wm_queued=2 "
                         "AND dps_status IN ('ok','rejected')").fetchall()
    todo = {}
    for r in pending:
        in_log = json.loads(r["players_json"]) if r["players_json"] else None
        for acc in wl:
            if in_log is not None and acc.casefold() not in in_log:
                continue
            if db.execute("SELECT 1 FROM log_player WHERE file_hash=? AND account=?",
                          (r["file_hash"], acc)).fetchone():
                continue
            todo.setdefault(acc, []).append(r["file_hash"])
    touched, found_all, missing_all, failed_all = set(), set(), set(), set()
    for acc, hashes in todo.items():
        log.info("Wingman: %s - %d Logs", acc, len(hashes))
        try:
            found, missing = rate_player(db, acc, hashes, bs, pause)
        except Exception as e:
            log.error("Wingman-Aufruf fuer %s fehlgeschlagen (naechster Lauf versucht es erneut): %s", acc, e)
            failed_all.update(hashes)
            continue
        found_all |= found
        missing_all |= missing
        if found:
            touched.add(acc)
    # Fehlversuche zaehlen: nur wenn Wingman wirklich geantwortet hat
    for h in missing_all - found_all - failed_all:
        db.execute("UPDATE log SET wm_attempts=wm_attempts+1, "
                   "wm_status=CASE WHEN wm_attempts+1>=? THEN 'error' ELSE wm_status END "
                   "WHERE file_hash=? AND wm_status='pending'", (MAX_ATTEMPTS, h))
    db.commit()
    return touched


def players_stage(db, cfg, touched, force):
    for acc in cfg["whitelist"]:
        row = db.execute("SELECT best_updated_at FROM player WHERE account=?", (acc,)).fetchone()
        if not (force or acc in touched or row["best_updated_at"] is None):
            continue
        try:
            log.info("Wingman Gesamtrating: %s (langsam)", acc)
            best = http("GET", f"{WM}/api/ratePerformanceOfPlayerForCurrentPatch/{acc}", timeout=300).json()
            if best.get("success"):
                with db:
                    db.execute(
                        "UPDATE player SET global_rank=?, global_rank_num=?, ranks_json=?, commander_ratio=?, "
                        "main_professions_json=?, bosses_completed=?, best_updated_at=? WHERE account=?",
                        (best.get("globalRank"), best.get("globalRankNumeric"),
                         json.dumps(best.get("ranksByCategory")), best.get("commanderRatio"),
                         json.dumps(best.get("mainProfessions")), best.get("bossesCompleted"), now(), acc))
                    db.execute("DELETE FROM player_boss_best WHERE account=?", (acc,))
                    for bk, cats in (best.get("bestPerBoss") or {}).items():
                        for cat, g in cats.items():
                            db.execute("INSERT INTO player_boss_best VALUES(?,?,?,?)", (acc, bk, cat, g))
            else:
                log.warning("Kein Gesamtrating fuer %s: %s", acc, str(best)[:200])
            log.info("Wingman KP/Charaktere: %s", acc)
            kp = http("GET", f"{WM}/api/kp", params={"account": acc}, timeout=60).json()
            with db:
                for ch in kp.get("chars") or []:
                    db.execute("INSERT INTO character(name,account,last_seen) VALUES(?,?,?) "
                               "ON CONFLICT(name) DO UPDATE SET account=excluded.account", (ch, acc, now()))
                db.execute("DELETE FROM player_kp WHERE account=?", (acc,))
                for bk, v in (kp.get("kp") or {}).items():
                    by_class = {k: n for k, n in v.items() if k != "total"}
                    db.execute("INSERT INTO player_kp VALUES(?,?,?,?)", (acc, bk, v.get("total"), json.dumps(by_class)))
                db.execute("UPDATE player SET kp_updated_at=? WHERE account=?", (now(), acc))
        except Exception as e:
            log.error("Spielerdaten fuer %s fehlgeschlagen: %s", acc, e)


def backfill_meta(db, cfg):
    """Holt dps.report-Metadaten fuer Logs nach, bei denen der Abruf frueher fehlschlug
    (boss_key/players_json leer) - sonst taucht das Log nie auf der Website auf."""
    wl = {a.casefold() for a in cfg["whitelist"]}
    rows = db.execute("SELECT file_hash, dps_url FROM log WHERE dps_url IS NOT NULL "
                      "AND (boss_key IS NULL OR encounter_time IS NULL)").fetchall()
    if rows:
        log.info("Metadaten nachholen: %d Logs", len(rows))
    for r in rows:
        meta = fetch_dps_meta(r["dps_url"])
        if not meta:
            continue
        m = parse_meta(meta)
        had = bool(m["players"])
        players = {k: v for k, v in (m["players"] or {}).items() if k in wl} or None
        if m["boss_key"] and not db.execute("SELECT 1 FROM boss WHERE key=?", (m["boss_key"],)).fetchone():
            db.execute("INSERT INTO boss(key,game_id,name,is_cm,enabled) VALUES(?,?,?,?,0)",
                       (m["boss_key"], m["game_id"], m["boss_name"], m["is_cm"]))
        db.execute("UPDATE log SET boss_key=?, success=?, encounter_time=?, players_json=?, "
                   "wm_status=CASE WHEN ? AND NOT ? THEN 'n/a' ELSE wm_status END WHERE file_hash=?",
                   (m["boss_key"], m["success"], m["when"], json.dumps(players) if players else None,
                    int(had), int(bool(players)), r["file_hash"]))
        for p in (players or {}).values():
            db.execute("INSERT INTO character(name,account,class,last_seen) VALUES(?,?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET account=excluded.account, class=excluded.class, "
                       "last_seen=excluded.last_seen", (p["character"], p["account"], p["class"], m["when"] or now()))
        db.commit()


def finalize(db):
    db.execute("UPDATE log SET process_failed = CASE WHEN "
               "(dps_status IN ('rejected','parse_failed') OR (dps_status='error' AND attempts>=?)) "
               "AND wm_status IN ('rejected','error','n/a') THEN 1 ELSE 0 END", (MAX_ATTEMPTS,))
    db.commit()
    print("\nZusammenfassung:")
    for q, label in [("SELECT dps_status s, COUNT(*) n FROM log GROUP BY 1", "dps.report"),
                     ("SELECT wm_status s, COUNT(*) n FROM log GROUP BY 1", "Wingman")]:
        print(f"  {label:10}", {r["s"]: r["n"] for r in db.execute(q)})
    print("  failed    ", db.execute("SELECT COUNT(*) FROM log WHERE process_failed=1").fetchone()[0])
    n = db.execute("SELECT COUNT(*) FROM log WHERE wm_status='pending' AND wm_queued<2 AND dps_url IS NOT NULL").fetchone()[0]
    if n:
        print(f"  ! {n} Logs warten noch auf den Wingman-Import (naechster Lauf prueft erneut)")
    n = db.execute("SELECT COUNT(*) FROM log WHERE wm_status='pending' AND dps_url IS NULL").fetchone()[0]
    if n:
        print(f"  ! {n} Logs ohne dps.report-Link - Wingman-Import nicht moeglich")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--log-dir")
    ap.add_argument("--limit", type=int, help="max. Anzahl neuer Logs (zum Testen)")
    ap.add_argument("--dry-run", action="store_true", help="nur neue Dateien auflisten")
    ap.add_argument("--jobs", type=int, help="parallele EI-Prozesse (Default: [ei] jobs aus config.toml, sonst 1)")
    ap.add_argument("--skip-ei", action="store_true")
    ap.add_argument("--skip-wingman", action="store_true")
    ap.add_argument("--refresh-players", action="store_true", help="Gesamtrating+KP fuer alle neu laden")
    ap.add_argument("--requeue-wingman", action="store_true",
                    help="Logs mit wm_status=error/pending erneut bei Wingman anmelden und bewerten")
    ap.add_argument("--reset-db", action="store_true",
                    help="DB komplett neu erzeugen (loescht die bestehende DB!). Ohne Flag wird die DB nur erweitert.")
    ap.add_argument("--debug", action="store_true", help="rohe EI-Ausgabe anzeigen")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = tomllib.loads(Path(a.config).read_text(encoding="utf-8"))
    cfg["_dir"] = Path(a.config).resolve().parent
    cfg.setdefault("wingman", {}).setdefault("batch_size", 40)
    cfg["wingman"].setdefault("pause", 0.5)
    cfg["wingman"].setdefault("wait", 180)     # max. Sekunden Wartezeit auf die Wingman-Import-Queue
    cfg["wingman"].setdefault("poll", 10)      # Abstand der Queue-Abfragen in Sekunden
    root = a.log_dir or cfg["log_dir"]
    bosses_path = Path(a.config).parent / cfg.get("bosses_file", "bosses.toml")
    bosses = load_bosses(bosses_path)
    if a.reset_db:
        reset_db_files(cfg["db_path"])
    db = open_db(cfg["db_path"], cfg["whitelist"], bosses)
    # Altlast: fruehere EI-Direktuploads zu Wingman wurden abgelehnt -> 'rejected'. Jetzt laeuft alles ueber den
    # dps.report-Link, daher diese Logs fuer den Link-Import freigeben.
    n = db.execute("UPDATE log SET wm_status='pending', wm_attempts=0, wm_queued=0 WHERE wm_status='rejected' "
                   "AND dps_url IS NOT NULL AND boss_key IS NOT NULL AND players_json IS NOT NULL "
                   "AND boss_key IN (SELECT key FROM boss WHERE enabled=1)").rowcount
    if n:
        log.info("%d zuvor abgelehnte Logs werden per dps.report-Link neu bei Wingman angemeldet", n)
    db.commit()
    if not a.dry_run:
        backfill_meta(db, cfg)

    if a.requeue_wingman:
        n = db.execute("UPDATE log SET wm_status='pending', wm_attempts=0, wm_queued=0, process_failed=0 "
                       "WHERE wm_status IN ('error','pending') AND dps_url IS NOT NULL").rowcount
        db.commit()
        log.info("%d Logs fuer Wingman zurueckgesetzt", n)

    if not a.skip_ei:
        files = find_new_files(db, root, cfg.get("skip_name_patterns", []), skip_sets(bosses), a.limit)
        log.info("%d neue Logs", len(files))
        if a.dry_run:
            from collections import Counter
            for folder, n in sorted(Counter(f["path"].parent.name for f in files).items()):
                print(f"{n:4}  {folder}")
            return
        ei_stage(db, cfg, files, a.debug, a.jobs or cfg["ei"].get("jobs", 1))
    touched = set() if a.skip_wingman else wingman_stage(db, cfg)
    if not a.skip_wingman:
        players_stage(db, cfg, touched, a.refresh_players)
    finalize(db)


if __name__ == "__main__":
    main()
