# I'll generate a comprehensive `main.py` with a richer feature set (admin filters, stats, CSV export, advanced altseason, Telegram handling, etc.)
# The file will be saved as /mnt/data/main_full.py.txt so you can download and deploy it as main.py.

code = r'''# main.py
# FastAPI app pour TradingView -> SQLite -> Admin -> Telegram
# Version étendue (dashboard + stats + export CSV + altseason avancé)
# Compatible Render (DB persistante /data, port binding via PORT), compacte mais complète.

import os
import time
import json
import csv
import io
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import urllib.request
import urllib.parse
import logging

# ============================================================================
# CONFIG / ENV
# ============================================================================
APP_NAME = "aiTrader"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
if not ADMIN_SECRET:
    raise RuntimeError("ADMIN_SECRET manquant. Définissez ADMIN_SECRET dans les variables d'environnement.")

DB_PATH = os.getenv("DB_PATH", "/data/app.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "45"))
EXPORT_MAIN_TXT = os.getenv("EXPORT_MAIN_TXT", "0") == "1"
PIN_ENTRIES = os.getenv("TELEGRAM_PIN_ENTRIES", "0") == "1"   # optionnel, nécessite droits

def _ensure_dir_for(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

_ensure_dir_for(DB_PATH)

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(APP_NAME)

# ============================================================================
# DB UTILS
# ============================================================================
@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()

def db_init():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              type TEXT NOT NULL,
              symbol TEXT,
              tf TEXT,
              tf_label TEXT,
              side TEXT,
              entry REAL,
              sl REAL,
              tp REAL,
              tp1 REAL,
              tp2 REAL,
              tp3 REAL,
              r1 REAL,
              s1 REAL,
              lev_reco REAL,
              qty_reco REAL,
              notional REAL,
              reason TEXT,
              trade_id TEXT,
              payload TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol);")

db_init()

# ============================================================================
# HELPERS
# ============================================================================
def ts_ms_to_str(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ms)

def now_ms() -> int:
    return int(time.time()*1000)

def clamp(n, lo, hi):
    return max(lo, min(hi, n))

# ============================================================================
# TELEGRAM
# ============================================================================
_last_tg_sent_at = 0.0

def _can_send_tg() -> bool:
    global _last_tg_sent_at
    if TELEGRAM_COOLDOWN_SECONDS <= 0:
        return True
    now = time.time()
    if (now - _last_tg_sent_at) >= TELEGRAM_COOLDOWN_SECONDS:
        _last_tg_sent_at = now
        return True
    return False

def _tg_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

def _tg_request(method: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(_tg_api_url(method), data=payload)
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    js = json.loads(raw)
                except Exception:
                    js = {"raw": raw}
                if resp.status == 200:
                    return True, None, js
                else:
                    err = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + i*2)
                err = "HTTP Error 429: Too Many Requests"
                continue
            err = f"HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        time.sleep(1 + i)
    return False, err, None

def tg_send(text: str, pin: bool=False) -> Tuple[bool, Optional[str]]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "telegram-not-configured"
    if not _can_send_tg():
        return False, "rate-limited (cooldown)"
    ok, err, js = _tg_request("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true"
    })
    if ok and pin and PIN_ENTRIES:
        try:
            msg_id = js.get("result", {}).get("message_id")
            if msg_id:
                _tg_request("pinChatMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": msg_id})
        except Exception as e:
            log.warning("pin failed: %s", e)
    return ok, err

# ============================================================================
# DÉDOUBLONNAGE ENVOIS
# ============================================================================
_recent_keys: Dict[str, float] = {}
RECENT_TTL = 60  # s

def dedupe_should_send(p: Dict[str, Any]) -> bool:
    now_s = time.time()
    # purge
    for k, t in list(_recent_keys.items()):
        if now_s - t > RECENT_TTL:
            _recent_keys.pop(k, None)
    key = f"{p.get('type')}|{p.get('trade_id') or p.get('symbol')}|{p.get('time')}"
    if key in _recent_keys:
        return False
    _recent_keys[key] = now_s
    return True

# ============================================================================
# PAYLOAD / SAUVEGARDE
# ============================================================================
def save_event(payload: Dict[str, Any]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO events (
              ts_ms, type, symbol, tf, tf_label, side, entry, sl, tp, tp1, tp2, tp3,
              r1, s1, lev_reco, qty_reco, notional, reason, trade_id, payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(payload.get("time") or 0),
                payload.get("type"),
                payload.get("symbol"),
                payload.get("tf"),
                payload.get("tf_label"),
                payload.get("side"),
                payload.get("entry"),
                payload.get("sl"),
                payload.get("tp"),
                payload.get("tp1"),
                payload.get("tp2"),
                payload.get("tp3"),
                payload.get("r1"),
                payload.get("s1"),
                payload.get("lev_reco"),
                payload.get("qty_reco"),
                payload.get("notional"),
                payload.get("reason"),
                payload.get("trade_id"),
                json.dumps(payload, ensure_ascii=False)
            )
        )

def format_msg(p: Dict[str, Any]) -> str:
    t = p.get("type", "")
    sym = p.get("symbol", "")
    tf = p.get("tf_label") or p.get("tf") or ""
    lines = [f"🔔 {t} — {sym} {tf}"]
    def add(k, title=None):
        if p.get(k) is not None:
            lines.append(f"{(title or k).upper()}: {p[k]}")
    for k in ("side","entry","sl","tp","tp1","tp2","tp3","r1","s1","lev_reco","qty_reco","notional","reason"):
        add(k)
    lines.append(f"TIME: {ts_ms_to_str(int(p.get('time') or 0))}")
    lines.append(f"ID: {p.get('trade_id') or '-'}")
    return "\n".join(lines)

# ============================================================================
# STATS & ALTSEASON
# ============================================================================
def get_counts(since_ms: int) -> Dict[str, int]:
    with db_conn() as conn:
        rows = conn.execute("SELECT type, COUNT(*) c FROM events WHERE ts_ms >= ? GROUP BY type", (since_ms,)).fetchall()
    return {r["type"].upper(): r["c"] for r in rows}

def altseason_signal(window_h: int = 24) -> Dict[str, Any]:
    since = now_ms() - window_h*3600*1000
    counts = get_counts(since)
    tp = counts.get("TP1_HIT",0)+counts.get("TP2_HIT",0)+counts.get("TP3_HIT",0)
    sl = counts.get("SL_HIT",0)
    total = tp+sl
    pct_tp = (tp/total*100) if total else 0.0
    score = tp - sl
    status = "Neutral"
    if pct_tp >= 62 and tp >= 20:
        status = "Altseason (risk-on)"
    elif pct_tp <= 38 and sl >= 20:
        status = "Risk-off"
    return {"window_hours": window_h, "tp_hits": tp, "sl_hits": sl, "pct_tp": round(pct_tp,2), "score": score, "status": status}

def compute_basic_stats(days: int=7) -> Dict[str, Any]:
    since = now_ms() - days*24*3600*1000
    with db_conn() as conn:
        rows = conn.execute("SELECT symbol, tf_label, type FROM events WHERE ts_ms >= ?", (since,)).fetchall()
    tp = sum(1 for r in rows if r["type"].upper() in ("TP1_HIT","TP2_HIT","TP3_HIT"))
    sl = sum(1 for r in rows if r["type"].upper()=="SL_HIT")
    total = tp + sl
    winrate = (tp/total*100) if total else 0.0
    by_symbol = {}
    for r in rows:
        sym = r["symbol"] or "-"
        t = r["type"].upper()
        d = by_symbol.setdefault(sym, {"tp":0,"sl":0})
        if t in ("TP1_HIT","TP2_HIT","TP3_HIT"): d["tp"]+=1
        if t=="SL_HIT": d["sl"]+=1
    for sym, d in by_symbol.items():
        tot = d["tp"]+d["sl"]
        d["winrate"] = round((d["tp"]/tot*100),2) if tot else 0.0
    return {"window_days": days, "tp": tp, "sl": sl, "total": total, "winrate": round(winrate,2), "by_symbol": by_symbol}

# ============================================================================
# FASTAPI
# ============================================================================
app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# ============================================================================
# HTML UI
# ============================================================================
CSS = """
<style>
:root{--bg:#0b0c10;--card:#14161b;--muted:#777;--fg:#eaeef2;--accent:#4f46e5;--ok:#10b981;--ko:#ef4444;}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;color:var(--fg);background:linear-gradient(180deg,#0b0c10,#0f1117);}
a{color:#8ab4ff;text-decoration:none}
h1,h2{margin:0 0 12px}
.page{max-width:1200px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid #222;border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 6px 20px rgba(0,0,0,.35)}
.controls a, .controls button{display:inline-block;margin:0 8px 8px 0;padding:8px 12px;border-radius:10px;background:#1f2430;border:1px solid #2a2f3a;color:#cbd5e1}
.controls .danger{background:#2a1315;border-color:#541216;color:#fecaca}
.controls .muted{background:#1a1d24;color:#9aa4b2}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid #2a2f3a;padding:8px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#171a21}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #2a2f3a;background:#1c2230;color:#cbd5e1}
.badge{display:inline-block;padding:4px 10px;border-radius:999px}
.badge.ok{background:rgba(16,185,129,.15);color:#a7f3d0;border:1px solid rgba(16,185,129,.4)}
.badge.ko{background:rgba(239,68,68,.15);color:#fecaca;border:1px solid rgba(239,68,68,.4)}
small{color:var(--muted)}
input,select{padding:6px 8px;border:1px solid #2a2f3a;border-radius:8px;background:#0f1219;color:#e5e7eb}
pre{white-space:pre-wrap;margin:0}
kbd{background:#222;padding:2px 6px;border-radius:6px;border:1px solid #333}
.header{display:flex;gap:10px;align-items:center;justify-content:space-between}
.nav a{margin-right:10px}
</style>
"""

def nav(secret: str) -> str:
    return f"""
<div class="controls">
  <a href="/?secret={secret}">Home</a>
  <a href="/trades-admin?secret={secret}">Admin</a>
  <a href="/stats?secret={secret}">Stats</a>
  <a href="/altseason?secret={secret}">Altseason</a>
  <a class="muted" href="/export/csv?secret={secret}">Export CSV</a>
  <a class="danger" href="/reset?secret={secret}&confirm=yes&redirect=/?secret={secret}">Reset</a>
</div>
"""

def require_secret(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "bad secret")

# ============================================================================
# ROUTES
# ============================================================================
@app.get("/", response_class=HTMLResponse)
def home(secret: str = Query(...)):
    require_secret(secret)
    sig = altseason_signal(24)
    badge = "ok" if "Altseason" in sig["status"] else ("ko" if sig["status"]=="Risk-off" else "")
    return HTMLResponse(f"""{CSS}
<div class="page">
  <div class="header"><h1>{APP_NAME}</h1><div class="nav">{nav(secret)}</div></div>
  <div class="card">
    <h2>Altseason (24h)</h2>
    <div class="badge {badge}">{sig['status']}</div>
    <div style="margin-top:8px;">TP: <b>{sig['tp_hits']}</b> — SL: <b>{sig['sl_hits']}</b> — %TP: <b>{sig['pct_tp']}%</b> — Score: <b>{sig['score']}</b></div>
    <small>Heuristique rapide, ajustable dans le code.</small>
  </div>
  <div class="card">
    <h2>Accès rapides</h2>
    <ul>
      <li><a href="/trades-admin?secret={secret}">Admin (journal des évènements)</a></li>
      <li><a href="/stats?secret={secret}">Stats</a></li>
      <li><a href="/altseason?secret={secret}">Altseason détaillé</a></li>
      <li><a href="/export/csv?secret={secret}">Exporter en CSV</a></li>
    </ul>
  </div>
</div>
""")

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.post("/tv-webhook", response_class=PlainTextResponse)
async def tv_webhook(request: Request, background: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(400, "invalid json")

    log.info("Webhook payload: %s", json.dumps(payload, ensure_ascii=False))

    if payload.get("secret") != ADMIN_SECRET:
        raise HTTPException(403, "bad secret")

    # save
    save_event(payload)
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             payload.get("type"), payload.get("symbol"), payload.get("tf"), payload.get("trade_id"))

    # telegram
    msg = format_msg(payload)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and msg and dedupe_should_send(payload):
        pin = (payload.get("type") == "ENTRY") and PIN_ENTRIES
        def _bg():
            ok, err = tg_send(msg, pin=pin)
            log.info("TV webhook -> telegram sent=%s err=%s", ok, err)
        background.add_task(_bg)

    return "ok"

# -------------------- ADMIN --------------------
@app.get("/trades-admin", response_class=HTMLResponse)
def trades_admin(
    secret: str = Query(...),
    q: Optional[str] = Query(None, description="filter symbol contains"),
    type_filter: Optional[str] = Query(None),
    side: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    hours: int = Query(72, ge=1, le=720),
    limit: int = Query(500, ge=10, le=5000),
):
    require_secret(secret)
    since = now_ms() - hours*3600*1000
    params: List[Any] = [since]
    where = ["ts_ms >= ?"]
    if q:
        where.append("symbol LIKE ?")
        params.append(f"%{q}%")
    if type_filter:
        where.append("UPPER(type)=UPPER(?)")
        params.append(type_filter)
    if side:
        where.append("UPPER(side)=UPPER(?)")
        params.append(side)
    if tf:
        where.append("(tf_label = ? OR tf = ?)")
        params.extend([tf, tf])
    sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY ts_ms DESC, id DESC LIMIT ?"
    params.append(limit)

    with db_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    html_rows = []
    for r in rows:
        payload = r["payload"]
        try:
            obj = json.loads(payload)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pretty = payload
        html_rows.append(f"""
<tr>
  <td><span class="tag">{r['type']}</span><br><small>{ts_ms_to_str(r['ts_ms'])}</small></td>
  <td>{r['symbol'] or ''}<br><small>{r['tf_label'] or r['tf'] or ''}</small></td>
  <td>ID: <small>{r['trade_id'] or '-'}</small><br>Side: <small>{r['side'] or '-'}</small></td>
  <td>
    entry={r['entry']} sl={r['sl']} tp={r['tp']}<br>
    tp1={r['tp1']} tp2={r['tp2']} tp3={r['tp3']}<br>
    r1={r['r1']} s1={r['s1']}<br>
    lev={r['lev_reco']} qty={r['qty_reco']} notional={r['notional']}
  </td>
  <td><pre>{pretty}</pre></td>
</tr>""")

    html = f"""{CSS}
<div class="page">
  <div class="header"><h1>{APP_NAME} — Admin</h1><div class="nav">{nav(secret)}</div></div>
  <div class="card">
    <form method="get" action="/trades-admin" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <input type="hidden" name="secret" value="{secret}"/>
      <label>Symbole contient <input name="q" value="{q or ''}" placeholder="ex: BTC"/></label>
      <label>Type <input name="type_filter" value="{type_filter or ''}" placeholder="ENTRY/TP1_HIT/..."/></label>
      <label>Side <input name="side" value="{side or ''}" placeholder="LONG/SHORT"/></label>
      <label>TF <input name="tf" value="{tf or ''}" placeholder="15m/1h"/></label>
      <label>Heures <input type="number" name="hours" value="{hours}" min="1" max="720"/></label>
      <label>Limit <input type="number" name="limit" value="{limit}" min="10" max="5000"/></label>
      <button>Filtrer</button>
      <a class="muted" href="/trades-admin?secret={secret}">Reset filtres</a>
    </form>
    <small>DB: {DB_PATH}</small>
  </div>
  <div class="card">
    <h2>Évènements</h2>
    <table>
      <thead><tr><th>Type/Time</th><th>Symbol/TF</th><th>Trade</th><th>Numbers</th><th>Payload</th></tr></thead>
      <tbody>{''.join(html_rows) or '<tr><td colspan="5">Aucun évènement</td></tr>'}</tbody>
    </table>
  </div>
</div>
"""
    return HTMLResponse(html)

# -------------------- STATS --------------------
@app.get("/stats", response_class=HTMLResponse)
def stats(secret: str = Query(...), days: int = Query(7, ge=1, le=90)):
    require_secret(secret)
    s = compute_basic_stats(days)
    by_sym_rows = "".join(
        f"<tr><td>{sym}</td><td>{d['tp']}</td><td>{d['sl']}</td><td>{d['winrate']}%</td></tr>"
        for sym, d in sorted(s["by_symbol"].items(), key=lambda kv: (-kv[1]["winrate"], -(kv[1]["tp"]+kv[1]["sl"])))
    )
    html = f"""{CSS}
<div class="page">
  <div class="header"><h1>{APP_NAME} — Stats</h1><div class="nav">{nav(secret)}</div></div>
  <div class="card">
    <form method="get" action="/stats" style="display:flex;gap:10px;align-items:center">
      <input type="hidden" name="secret" value="{secret}"/>
      <label>Fenêtre (jours) <input type="number" name="days" value="{days}" min="1" max="90"/></label>
      <button>Recalculer</button>
    </form>
  </div>
  <div class="card">
    <h2>Résumé {days} jours</h2>
    <div>TP: <b>{s['tp']}</b> — SL: <b>{s['sl']}</b> — Total: <b>{s['total']}</b> — Winrate: <b>{s['winrate']}%</b></div>
  </div>
  <div class="card">
    <h2>Par symbole</h2>
    <table>
      <thead><tr><th>Symbole</th><th>TP</th><th>SL</th><th>Winrate</th></tr></thead>
      <tbody>{by_sym_rows or '<tr><td colspan=4>Aucune donnée</td></tr>'}</tbody>
    </table>
  </div>
</div>
"""
    return HTMLResponse(html)

# -------------------- ALTSEASON --------------------
@app.get("/altseason", response_class=HTMLResponse)
def altseason(secret: str = Query(...), window_h: int = Query(24, ge=6, le=168)):
    require_secret(secret)
    sig = altseason_signal(window_h)
    badge = "ok" if "Altseason" in sig["status"] else ("ko" if sig["status"]=="Risk-off" else "")
    html = f"""{CSS}
<div class="page">
  <div class="header"><h1>{APP_NAME} — Altseason</h1><div class="nav">{nav(secret)}</div></div>
  <div class="card">
    <form method="get" action="/altseason" style="display:flex;gap:10px;align-items:center">
      <input type="hidden" name="secret" value="{secret}"/>
      <label>Fenêtre (heures) <input type="number" name="window_h" value="{window_h}" min="6" max="168"/></label>
      <button>Calculer</button>
    </form>
  </div>
  <div class="card">
    <h2>Signal</h2>
    <div class="badge {badge}">{sig['status']}</div>
    <div style="margin-top:8px;">TP: <b>{sig['tp_hits']}</b> — SL: <b>{sig['sl_hits']}</b> — %TP: <b>{sig['pct_tp']}%</b> — Score: <b>{sig['score']}</b></div>
    <small>Heuristique: Altseason si %TP ≥ 62% et TP≥20 ; Risk-off si %TP ≤ 38% et SL≥20.</small>
  </div>
</div>
"""
    return HTMLResponse(html)

# -------------------- EXPORT CSV --------------------
@app.get("/export/csv")
def export_csv(secret: str = Query(...), days: int = Query(7, ge=1, le=365)):
    require_secret(secret)
    since = now_ms() - days*24*3600*1000
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ts_ms,type,symbol,tf,tf_label,side,entry,sl,tp,tp1,tp2,tp3,r1,s1,lev_reco,qty_reco,notional,reason,trade_id FROM events WHERE ts_ms>=? ORDER BY ts_ms",
            (since,)
        ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ts","time_iso","type","symbol","tf","tf_label","side","entry","sl","tp","tp1","tp2","tp3","r1","s1","lev_reco","qty_reco","notional","reason","trade_id"])
    for r in rows:
        w.writerow([
            r["ts_ms"],
            ts_ms_to_str(r["ts_ms"]),
            r["type"], r["symbol"], r["tf"], r["tf_label"], r["side"],
            r["entry"], r["sl"], r["tp"], r["tp1"], r["tp2"], r["tp3"],
            r["r1"], r["s1"], r["lev_reco"], r["qty_reco"], r["notional"], r["reason"], r["trade_id"]
        ])
    out.seek(0)
    filename = f"events_last_{days}d.csv"
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

# -------------------- RESET --------------------
@app.get("/reset")
def reset(secret: str = Query(...), confirm: str = Query("no"), redirect: Optional[str] = None):
    require_secret(secret)
    if confirm.lower() != "yes":
        return PlainTextResponse("Ajoutez confirm=yes pour réinitialiser la DB (IRREVERSIBLE).", status_code=400)
    with db_conn() as conn:
        conn.execute("DELETE FROM events;")
        conn.execute("VACUUM;")
    log.info("Database reset done.")
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    return PlainTextResponse("reset ok")

# -------------------- API JSON --------------------
@app.get("/api/events", response_class=JSONResponse)
def api_events(secret: str = Query(...), since_h: int = Query(24, ge=1, le=720)):
    require_secret(secret)
    since = now_ms() - since_h*3600*1000
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM events WHERE ts_ms>=? ORDER BY ts_ms DESC", (since,)).fetchall()
    return JSONResponse([dict(r) for r in rows])

@app.get("/api/stats", response_class=JSONResponse)
def api_stats(secret: str = Query(...), days: int = Query(7, ge=1, le=90)):
    require_secret(secret)
    return JSONResponse(compute_basic_stats(days))

# ============================================================================
# EXPORT DU CODE (facultatif)
# ============================================================================
def optional_export_source():
    if not EXPORT_MAIN_TXT:
        return
    export_path = "/data/export/main.py.txt"
    _ensure_dir_for(export_path)
    try:
        with open(__file__, "r", encoding="utf-8") as src:
            content = src.read()
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("Source exportée -> %s", export_path)
    except Exception as e:
        log.warning("Export source échoué: %s", e)

optional_export_source()

# ============================================================================
# UVICORN ENTRY (LOCAL)
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
'''
with open('/mnt/data/main_full.py.txt','w',encoding='utf-8') as f:
    f.write(code)

'/mnt/data/main_full.py.txt'
