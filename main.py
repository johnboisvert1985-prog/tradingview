import os, sqlite3, threading, json, time, math, re, uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

APP_NAME = "AI Trader"
VERSION  = "2025.09.29"

# -------------------- CONFIG --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")
DB_PATH            = os.getenv("DB_PATH", "/tmp/ai_trader/data.db")

ALT_INTERVAL_SEC   = int(os.getenv("ALT_INTERVAL_SEC", "120"))   # rafraîchissement altseason
TELEGRAM_COOLDOWN  = int(os.getenv("TELEGRAM_COOLDOWN", "3"))    # anti-spam (sec)

# -------------------- DB BOOTSTRAP --------------------
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL;")
_conn.execute("PRAGMA synchronous=NORMAL;")

def db_execute(sql: str, params: tuple = ()) -> int:
    with _db_lock:
        cur = _conn.cursor()
        cur.execute(sql, params)
        _conn.commit()
        return cur.lastrowid

def db_query(sql: str, params: tuple = ()) -> List[tuple]:
    with _db_lock:
        cur = _conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

# Tables minimales
db_execute("""
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  type TEXT NOT NULL,
  symbol TEXT,
  tf TEXT,
  side TEXT,
  entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
  r1 REAL, s1 REAL,
  leverage TEXT,
  note TEXT,
  trade_id TEXT,
  price REAL,
  direction TEXT,
  status TEXT
)""")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at DESC)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events (trade_id)")

db_execute("""
CREATE TABLE IF NOT EXISTS altseason_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  btc_dom REAL,
  btc_7d REAL,
  alts_7d REAL,
  alts_btc_ratio REAL,
  heat REAL,
  phase TEXT
)""")

# -------------------- UTILS --------------------
def now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0): return None
    return (a / b - 1.0) * 100.0

# Mémoire anti-spam Telegram
_last_telegram_time = 0.0

def icon_for_event(event_type: str, direction: Optional[str] = None) -> str:
    et = (event_type or "").upper()
    diru = (direction or "").upper()
    if et == "VECTOR_CANDLE":
        if diru == "UP":   return "🟩"
        if diru == "DOWN": return "🟥"
        return "🟦"
    if et == "TP3_HIT": return "✅3"
    if et == "TP2_HIT": return "✅2"
    if et == "TP1_HIT": return "✅1"
    if et == "SL_HIT":  return "⛔️"
    if et == "ENTRY":   return "📥"
    if et == "CLOSE":   return "📤"
    if et == "AOE_PREMIUM":  return "🟨"
    if et == "AOE_DISCOUNT": return "🟪"
    return "ℹ️"
# -------------------- ALTSEASON ENGINE --------------------
# Modèle simple : calcule une "chaleur" des alts depuis la base d'events
# (approx : % d'events non-BTC/ETH vs BTC sur 7 jours & ratio)
def compute_altseason_snapshot() -> Dict[str, Any]:
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    since = (now - timedelta(days=7)).isoformat()

    rows = db_query("""
        SELECT type, symbol, created_at FROM events
        WHERE created_at >= ? ORDER BY created_at DESC
    """, (since,))

    total = len(rows)
    alts  = 0
    btc   = 0
    for t, sym, created in rows:
        s = (sym or "").upper()
        if s.startswith("BTC"): btc += 1
        # ETH traité comme "semi-majors" → on le retire des alts purs
        elif s.startswith("ETH"): pass
        else: alts += 1

    btc_dom = None
    if total > 0:
        btc_dom = 100.0 * btc / total

    # approximations (juste pour un dashboard macro)
    btc_7d  = safe_float(2*btc - total, 0)  # proxy momentum
    alts_7d = safe_float(2*alts - total, 0)
    ratio   = None
    if btc + alts > 0:
        ratio = alts / max(1, btc)

    # heat -> combinaison normalisée 0..100
    heat_raw = 0.0
    if ratio is not None:
        heat_raw = max(0.0, min(100.0, 50.0 + 30.0*(ratio-1.0) + 20.0*((alts_7d - btc_7d)/max(1.0, total)) ))

    # phase textuelle
    if heat_raw >= 66: phase = "Altseason (forte)"
    elif heat_raw >= 40: phase = "Altseason (modérée)"
    elif heat_raw >= 25: phase = "Neutre / Rotation"
    else: phase = "BTC-dominance"

    snap = {
        "created_at": now_iso(),
        "btc_dom": btc_dom,
        "btc_7d": btc_7d,
        "alts_7d": alts_7d,
        "alts_btc_ratio": ratio,
        "heat": heat_raw,
        "phase": phase
    }
    return snap

def persist_altseason_snapshot():
    s = compute_altseason_snapshot()
    db_execute("""
    INSERT INTO altseason_snapshots (created_at, btc_dom, btc_7d, alts_7d, alts_btc_ratio, heat, phase)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (s["created_at"], s["btc_dom"], s["btc_7d"], s["alts_7d"], s["alts_btc_ratio"], s["heat"], s["phase"]))

def latest_altseason_snapshot() -> Dict[str, Any]:
    row = db_query("""
        SELECT created_at, btc_dom, btc_7d, alts_7d, alts_btc_ratio, heat, phase
        FROM altseason_snapshots ORDER BY id DESC LIMIT 1
    """)
    if not row:
        s = compute_altseason_snapshot()
        return s
    (created_at, btc_dom, btc_7d, alts_7d, ratio, heat, phase) = row[0]
    return {
        "created_at": created_at,
        "btc_dom": btc_dom,
        "btc_7d": btc_7d,
        "alts_7d": alts_7d,
        "alts_btc_ratio": ratio,
        "heat": heat,
        "phase": phase
    }

# -------------------- TELEGRAM --------------------
async def telegram_send(text: str):
    global _last_telegram_time
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    # Anti-spam simple
    nowt = time.time()
    if nowt - _last_telegram_time < TELEGRAM_COOLDOWN:
        return
    _last_telegram_time = nowt

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except Exception as e:
        # on loggue sans planter
        print(f"[tg] send error: {e}")

def fmt_price(x: Optional[float]) -> str:
    if x is None: return "-"
    if x == 0: return "0"
    # format auto
    if x >= 100: return f"{x:,.2f}"
    if x >= 1:   return f"{x:,.3f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")

def telegram_text_for_event(ev: Dict[str, Any]) -> str:
    et = ev.get("type", "")
    sym = ev.get("symbol", "")
    tf  = ev.get("tf", "")
    side= ev.get("side") or ""
    price = ev.get("price")
    direction = ev.get("direction")
    icon = icon_for_event(et, direction)

    # lignes additionnelles
    if et == "ENTRY":
        return (f"{icon} <b>ENTRY</b> — <b>{sym}</b> {tf}\n"
                f"side: <b>{side or '-'}</b>\n"
                f"entry: <code>{fmt_price(ev.get('entry'))}</code> • "
                f"SL: <code>{fmt_price(ev.get('sl'))}</code>\n"
                f"TP1/2/3: <code>{fmt_price(ev.get('tp1'))}</code> / "
                f"<code>{fmt_price(ev.get('tp2'))}</code> / <code>{fmt_price(ev.get('tp3'))}</code>")
    if et in {"TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT"}:
        return f"{icon} <b>{et}</b> — <b>{sym}</b> {tf} (entry {fmt_price(ev.get('entry'))}) → <code>{fmt_price(ev.get('tp') or ev.get('price'))}</code>"
    if et == "VECTOR_CANDLE":
        txtdir = "UP" if (direction or "").upper()=="UP" else ("DOWN" if (direction or "").upper()=="DOWN" else "")
        return f"{icon} <b>Vector Candle {txtdir}</b> — <b>{sym}</b> {tf} @ <code>{fmt_price(price)}</code>"
    if et in {"AOE_PREMIUM","AOE_DISCOUNT"}:
        tag = "Premium" if et=="AOE_PREMIUM" else "Discount"
        return f"{icon} <b>AOE {tag}</b> — <b>{sym}</b> {tf}"
    if et == "CLOSE":
        return f"{icon} <b>CLOSE</b> — <b>{sym}</b> {tf} ({ev.get('reason') or '-'})"
    # default
    return f"{icon} <b>{et}</b> — <b>{sym}</b> {tf}"

# -------------------- SAVE EVENT --------------------
def save_event(event: Dict[str, Any]) -> None:
    created = event.get("created_at") or now_iso()
    type_   = (event.get("type") or "").upper()
    symbol  = event.get("symbol")
    tf      = event.get("tf")
    side    = event.get("side")
    entry   = safe_float(event.get("entry"))
    sl      = safe_float(event.get("sl"))
    tp1     = safe_float(event.get("tp1"))
    tp2     = safe_float(event.get("tp2"))
    tp3     = safe_float(event.get("tp3"))
    r1      = safe_float(event.get("r1"))
    s1      = safe_float(event.get("s1"))
    leverage= event.get("leverage")
    note    = event.get("note")
    trade_id= event.get("trade_id")
    price   = safe_float(event.get("price"))
    direction = event.get("direction")
    status  = event.get("status")

    db_execute("""
    INSERT INTO events (created_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, r1, s1, leverage, note, trade_id, price, direction, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (created, type_, symbol, tf, side, entry, sl, tp1, tp2, tp3, r1, s1, leverage, note, trade_id, price, direction, status))
app = FastAPI(title=APP_NAME, version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "invalid json"}, status_code=400)

    # normalisation
    t = (payload.get("type") or "").upper()
    symbol = payload.get("symbol")
    tf     = payload.get("tf") or payload.get("tf_label")
    event = dict(payload)
    event["type"] = t
    # trade_id fallback
    if not event.get("trade_id"):
        event["trade_id"] = f"{symbol}_{tf}_{int(time.time()*1000)}"

    # Sauvegarde + Telegram
    save_event(event)
    try:
        await telegram_send(telegram_text_for_event(event))
    except Exception:
        pass

    return JSONResponse({"ok": True})

# --- Simple scheduler Altseason (loop best-effort, non bloquant) ---
def _altseason_loop():
    while True:
        try:
            persist_altseason_snapshot()
        except Exception as e:
            print(f"[alt] snapshot error: {e}")
        time.sleep(ALT_INTERVAL_SEC)

def start_altseason_thread():
    th = threading.Thread(target=_altseason_loop, daemon=True)
    th.start()

start_altseason_thread()
BASE_CSS = """
:root{
  --bg:#0b1220; --card:#111a2b; --text:#e2e8f0; --muted:#94a3b8; --accent:#22c55e; --warn:#f59e0b; --err:#ef4444; --info:#38bdf8; --border:#1e293b;
}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 Inter,system-ui,Segoe UI,Roboto,Arial}
.container{max-width:1200px;margin:24px auto;padding:0 16px}
.hdr{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px}
.title{font-size:20px;font-weight:700}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin-bottom:16px;box-shadow:0 4px 16px rgba(0,0,0,.25)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.kpi{display:flex;flex-direction:column;gap:4px}
.kpi .lbl{color:var(--muted);font-size:12px}
.kpi .val{font-size:18px;font-weight:700}
.badge{padding:2px 8px;border-radius:999px;font-weight:700;font-size:12px}
.badge.alt{background:rgba(34,197,94,.12);color:var(--accent);border:1px solid rgba(34,197,94,.3)}
.badge.btc{background:rgba(56,189,248,.12);color:var(--info);border:1px solid rgba(56,189,248,.3)}
.badge.warn{background:rgba(245,158,11,.12);color:var(--warn);border:1px solid rgba(245,158,11,.3)}
.progress{height:10px;background:#0f172a;border-radius:999px;overflow:hidden;border:1px solid var(--border)}
.progress>span{display:block;height:100%;background:linear-gradient(90deg,#22c55e,#38bdf8)}
.table{width:100%;border-collapse:collapse}
.table th,.table td{padding:10px;border-bottom:1px solid var(--border);text-align:left}
.table th{color:#93a4bd;font-weight:700;font-size:12px;letter-spacing:.02em;text-transform:uppercase}
.row{transition:.15s}
.row:hover{background:#0e172a}
.sym{font-weight:700}
.tag{font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:999px;color:var(--muted)}
.pill{padding:2px 8px;border-radius:8px;border:1px solid var(--border)}
.pill.hit{background:rgba(34,197,94,.12);color:var(--accent);border-color:rgba(34,197,94,.3);font-weight:700}
.pill.miss{background:rgba(239,68,68,.12);color:var(--err);border-color:rgba(239,68,68,.3)}
.pill.wait{color:#cbd5e1}
.small{color:var(--muted);font-size:12px}
.footer{opacity:.6;text-align:center;margin:24px 0}
"""

def render_altseason_card(snap: Dict[str, Any]) -> str:
    def n(x, d=2):
        if x is None: return "-"
        return f"{x:.{d}f}"
    heat = snap.get("heat") or 0
    heat_pct = max(0, min(100, int(heat)))
    badge_cls = "alt" if heat >= 66 else ("warn" if heat >= 25 else "btc")
    return f"""
<div class="card">
  <div class="hdr">
    <div class="title">Indicateurs Altseason</div>
    <span class="badge {badge_cls}">{snap.get('phase','-')}</span>
  </div>
  <div class="grid">
    <div class="card" style="grid-column: span 6;">
      <div class="kpi"><span class="lbl">Dominance BTC (proxy 7j)</span><span class="val">{n(snap.get('btc_dom'))}%</span></div>
      <div class="small">Part d’activité BTC parmi les signaux (7 jours).</div>
    </div>
    <div class="card" style="grid-column: span 6;">
      <div class="kpi"><span class="lbl">Ratio Alts/BTC</span><span class="val">{n(snap.get('alts_btc_ratio'))}</span></div>
      <div class="small">>1 favorise une rotation vers les alts.</div>
    </div>
    <div class="card" style="grid-column: span 12;">
      <div class="kpi"><span class="lbl">Chaleur Altseason (0-100)</span><span class="val">{n(heat,0)}/100</span></div>
      <div class="progress"><span style="width:{heat_pct}%"></span></div>
      <div class="small">Synthèse : momentum relatif des alts vs BTC, activité récente & ratio.</div>
    </div>
  </div>
</div>
"""

def tp_pill(label: str, state: str) -> str:
    # state: hit | wait | miss
    cls = "pill " + ("hit" if state=="hit" else ("miss" if state=="miss" else "wait"))
    return f'<span class="{cls}">{label}</span>'

def render_trades_table(rows: List[Dict[str, Any]]) -> str:
    # Colonnes : Time, Symbol, TF, Side, Entry, SL, TP1..TP3, Status
    trs = []
    for r in rows:
        trade_id = r.get("trade_id")
        # calc état TP
        tp1_hit = len(db_query("SELECT 1 FROM events WHERE trade_id=? AND type='TP1_HIT' LIMIT 1", (trade_id,)))>0
        tp2_hit = len(db_query("SELECT 1 FROM events WHERE trade_id=? AND type='TP2_HIT' LIMIT 1", (trade_id,)))>0
        tp3_hit = len(db_query("SELECT 1 FROM events WHERE trade_id=? AND type='TP3_HIT' LIMIT 1", (trade_id,)))>0
        sl_hit  = len(db_query("SELECT 1 FROM events WHERE trade_id=? AND type='SL_HIT'  LIMIT 1", (trade_id,)))>0

        tp1 = tp_pill(f"TP1 {fmt_price(r.get('tp1'))}", "hit" if tp1_hit else ("miss" if sl_hit else "wait"))
        tp2 = tp_pill(f"TP2 {fmt_price(r.get('tp2'))}", "hit" if tp2_hit else ("miss" if sl_hit else "wait"))
        tp3 = tp_pill(f"TP3 {fmt_price(r.get('tp3'))}", "hit" if tp3_hit else ("miss" if sl_hit else "wait"))

        status = "SL hit" if sl_hit else ("TP3" if tp3_hit else ("TP2" if tp2_hit else ("TP1" if tp1_hit else "En cours")))
        trs.append(f"""
<tr class="row">
  <td class="small">{r.get('created_at','-')}</td>
  <td class="sym">{r.get('symbol','-')}</td>
  <td><span class="tag">{r.get('tf','-')}</span></td>
  <td>{r.get('side','-')}</td>
  <td>{fmt_price(r.get('entry'))}</td>
  <td>{fmt_price(r.get('sl'))}</td>
  <td>{tp1}</td>
  <td>{tp2}</td>
  <td>{tp3}</td>
  <td><span class="tag">{status}</span></td>
</tr>""")
    return f"""
<div class="card">
  <div class="hdr"><div class="title">Trades récents</div></div>
  <div style="overflow:auto">
    <table class="table">
      <thead>
        <tr>
          <th>Heure</th><th>Symbole</th><th>TF</th><th>Side</th>
          <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Status</th>
        </tr>
      </thead>
      <tbody>
        {''.join(trs) or '<tr><td colspan="10" class="small">Aucun trade enregistré.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""

def html_page(title: str, content_html: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="container">
    <div class="hdr">
      <div class="title">{APP_NAME} — {title}</div>
      <div class="small">v{VERSION}</div>
    </div>
    {content_html}
    <div class="footer">© {datetime.utcnow().year} — Dashboard généré par {APP_NAME}</div>
  </div>
</body>
</html>"""
def latest_entries(limit: int = 50) -> List[Dict[str, Any]]:
    # On prend les derniers ENTRY, rejoints avec leur info
    rows = db_query("""
    SELECT created_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id
    FROM events
    WHERE type='ENTRY'
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))
    out = []
    for (created_at, type_, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id) in rows:
        out.append(dict(
            created_at=created_at, type=type_, symbol=symbol, tf=tf, side=side,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, trade_id=trade_id
        ))
    return out

@app.get("/", response_class=HTMLResponse)
async def home():
    snap = latest_altseason_snapshot()
    entries = latest_entries(25)
    html = render_altseason_card(snap) + render_trades_table(entries)
    return HTMLResponse(html_page("Accueil", html))

@app.get("/trades", response_class=HTMLResponse)
async def trades():
    snap = latest_altseason_snapshot()
    entries = latest_entries(100)
    html = render_altseason_card(snap) + render_trades_table(entries)
    return HTMLResponse(html_page("Trades", html))

# Health
@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION}

# -------------- MAIN --------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, workers=1)
