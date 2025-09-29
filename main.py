# main.py
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import httpx

# =========================
# Logging
# =========================
logger = logging.getLogger("aitrader")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# =========================
# Config / Env
# =========================
DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
DB_PATH = os.path.join(DB_DIR, "data.db")
os.makedirs(DB_DIR, exist_ok=True)
logger.info(f"DB dir OK: {DB_DIR} (using {DB_PATH})")

# Secrets
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_COOLDOWN_SEC = int(os.getenv("TELEGRAM_COOLDOWN_SEC", "15"))

# Vector icons
VECTOR_UP_ICON = "🟩"   # carré vert
VECTOR_DN_ICON = "🟥"   # carré rouge (si DOWN)

# =========================
# SQLite
# =========================
def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = dict_factory
logger.info(f"DB initialized at {DB_PATH}")

def db_execute(sql: str, params: tuple = ()):
    cur = DB.cursor()
    cur.execute(sql, params)
    DB.commit()
    return cur

def db_query(sql: str, params: tuple = ()) -> List[dict]:
    cur = DB.cursor()
    cur = cur.execute(sql, params)
    return list(cur.fetchall())

# Initial schema (souple : n’écrase pas si déjà existant)
db_execute("""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT,
    symbol TEXT,
    tf TEXT,
    tf_label TEXT,
    time INTEGER,
    side TEXT,
    entry REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    r1 REAL,
    s1 REAL,
    lev_reco REAL,
    qty_reco REAL,
    notional REAL,
    confidence INTEGER,
    horizon TEXT,
    leverage TEXT,
    note TEXT,
    price REAL,
    direction TEXT,
    trade_id TEXT
)
""")

# Index utiles
db_execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_symbol_tf ON events(symbol, tf)")

# =========================
# Utils
# =========================
def tf_to_label(tf: Any) -> str:
    if tf is None:
        return ""
    s = str(tf)
    try:
        n = int(s)
    except Exception:
        return s
    if n < 60:
        return f"{n}m"
    if n == 60:
        return "1h"
    if n % 60 == 0:
        return f"{n//60}h"
    return s

def ensure_trades_schema():
    cols = {r["name"] for r in db_query("PRAGMA table_info(events)")}
    if "tf_label" not in cols:
        db_execute("ALTER TABLE events ADD COLUMN tf_label TEXT")
    # Réindex de sûreté
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_symbol_tf ON events(symbol, tf)")

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def ms_ago(minutes: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)

try:
    ensure_trades_schema()
except Exception as e:
    logger.warning(f"ensure_trades_schema warning: {e}")

# =========================
# Telegram
# =========================
_last_tg_sent: Dict[str, float] = {}

async def tg_send_text(text: str, disable_web_page_preview: bool = True, key: Optional[str] = None):
    if not TELEGRAM_ENABLED:
        return {"ok": False, "reason": "telegram disabled"}

    # Cooldown par "key"
    k = key or "default"
    now = datetime.now().timestamp()
    last = _last_tg_sent.get(k, 0)
    if now - last < TELEGRAM_COOLDOWN_SEC:
        logger.warning("Telegram send skipped due to cooldown")
        return {"ok": False, "reason": "cooldown"}
    _last_tg_sent[k] = now

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            logger.info(f"Telegram sent: {text[:80]}...")
            return {"ok": True}
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return {"ok": False, "reason": str(e)}

def format_vector_message(symbol: str, tf_label: str, direction: str, price: Any, note: Optional[str] = None) -> str:
    icon = VECTOR_UP_ICON if (direction or "").upper() == "UP" else VECTOR_DN_ICON
    n = f" — {note}" if note else ""
    return f"{icon} Vector Candle {direction.upper()} | <b>{symbol}</b> <i>{tf_label}</i> @ <code>{price}</code>{n}"

# =========================
# FastAPI
# =========================
app = FastAPI(title="AI Trader", version="1.0")

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("""
    <html><head><meta charset="utf-8"><title>AI Trader</title></head>
    <body style="font-family:system-ui; padding:24px; background:#0b0f14; color:#e6edf3;">
      <h1>AI Trader</h1>
      <p>Endpoints:</p>
      <ul>
        <li><a href="/trades">/trades</a> — Dashboard</li>
        <li><code>POST /tv-webhook</code> — Webhook TradingView</li>
      </ul>
    </body></html>
    """)

# =========================
# Save Event (insère tf_label)
# =========================
def save_event(payload: dict):
    etype   = payload.get("type")
    symbol  = payload.get("symbol")
    tf      = payload.get("tf")
    tflabel = payload.get("tf_label") or tf_to_label(tf)
    t       = payload.get("time") or now_ms()
    side    = payload.get("side")
    entry   = payload.get("entry")
    sl      = payload.get("sl")
    tp1     = payload.get("tp1")
    tp2     = payload.get("tp2")
    tp3     = payload.get("tp3")
    r1      = payload.get("r1")
    s1      = payload.get("s1")
    lev_reco= payload.get("lev_reco")
    qty_reco= payload.get("qty_reco")
    notional= payload.get("notional")
    confidence = payload.get("confidence")
    horizon = payload.get("horizon")
    leverage= payload.get("leverage")
    note    = payload.get("note")
    price   = payload.get("price")
    direction = payload.get("direction")
    trade_id  = payload.get("trade_id")

    if trade_id is None and etype and symbol and tf:
        trade_id = f"{symbol}_{tf}_{t}"

    db_execute("""
        INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, r1, s1,
                           lev_reco, qty_reco, notional, confidence, horizon, leverage,
                           note, price, direction, trade_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (etype, symbol, str(tf) if tf is not None else None, tflabel, int(t),
          side, entry, sl, tp1, tp2, tp3, r1, s1,
          lev_reco, qty_reco, notional, confidence, horizon, leverage,
          note, price, direction, trade_id))

    logger.info(f"Saved event: type={etype} symbol={symbol} tf={tf} trade_id={trade_id}")
    return trade_id

# =========================
# Webhook
# =========================
@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # Secret check (souple : si non fourni côté TV, on n’échoue pas si WEBHOOK_SECRET vide)
    secret = payload.get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")

    etype = payload.get("type")
    symbol = payload.get("symbol")
    tf = payload.get("tf")

    if not etype or not symbol:
        raise HTTPException(422, "Missing type or symbol")

    # Sauvegarde
    trade_id = save_event(payload)

    # Optionnel : envoyer Telegram selon le type
    # (si tu as déjà ton propre routeur de messages, tu peux ignorer ce bloc)
    try:
        if TELEGRAM_ENABLED:
            key = f"{etype}:{symbol}"
            if etype == "VECTOR_CANDLE":
                txt = format_vector_message(
                    symbol=symbol,
                    tf_label=payload.get("tf_label") or tf_to_label(tf),
                    direction=payload.get("direction") or "",
                    price=payload.get("price"),
                    note=payload.get("note"),
                )
                await tg_send_text(txt, key=key)
            elif etype in {"ENTRY", "CLOSE", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"}:
                # Message simple et neutre pour ne rien casser
                tfl = payload.get("tf_label") or tf_to_label(tf)
                info = []
                if payload.get("entry") is not None:
                    info.append(f"entry <code>{payload['entry']}</code>")
                if payload.get("sl") is not None:
                    info.append(f"SL <code>{payload['sl']}</code>")
                if payload.get("tp1") is not None:
                    info.append(f"TP1 <code>{payload['tp1']}</code>")
                if payload.get("tp2") is not None:
                    info.append(f"TP2 <code>{payload['tp2']}</code>")
                if payload.get("tp3") is not None:
                    info.append(f"TP3 <code>{payload['tp3']}</code>")
                detail = " | ".join(info) if info else ""
                txt = f"<b>{etype}</b> | <b>{symbol}</b> <i>{tfl}</i>" + (f" — {detail}" if detail else "")
                await tg_send_text(txt, key=key)
    except Exception as e:
        logger.warning(f"Telegram send skipped due to cooldown or error: {e}")

    return JSONResponse({"ok": True, "trade_id": trade_id})

# =========================
# Altseason (4 signaux, sans Vectors)
# =========================
def _pct(x, y):
    try:
        x = float(x or 0)
        y = float(y or 0)
        return 0.0 if y == 0 else 100.0 * x / y
    except Exception:
        return 0.0

def compute_altseason_snapshot() -> dict:
    """
    Score 0-100 basé sur 4 signaux sur 24h :
      A) % LONG sur les ENTRY
      B) TP vs SL (on privilégie LONG si side présent)
      C) Breadth: nb de symboles distincts avec un TP hit
      D) Momentum: % d’ENTRY dans les 90 dernières minutes / total 24h
    Vectors EXCLUS.
    """
    t24 = ms_ago(24*60)

    # A: LONG ratio
    row = db_query("""
        SELECT
          SUM(CASE WHEN side='LONG' THEN 1 ELSE 0 END) AS long_n,
          SUM(CASE WHEN side='SHORT' THEN 1 ELSE 0 END) AS short_n
        FROM events
        WHERE type='ENTRY' AND time>=?
    """, (t24,))
    long_n = (row[0]["long_n"] if row else 0) or 0
    short_n = (row[0]["short_n"] if row else 0) or 0
    A = _pct(long_n, long_n + short_n)

    # B: TP vs SL (LONG si possible)
    row = db_query("""
      WITH tp AS (
        SELECT COUNT(*) AS n FROM events
        WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=? AND (side IS NULL OR side='LONG')
      ),
      sl AS (
        SELECT COUNT(*) AS n FROM events
        WHERE type='SL_HIT' AND time>=? AND (side IS NULL OR side='LONG')
      )
      SELECT tp.n AS tp_n, sl.n AS sl_n FROM tp, sl
    """, (t24, t24))
    tp_n = (row[0]["tp_n"] if row else 0) or 0
    sl_n = (row[0]["sl_n"] if row else 0) or 0
    B = _pct(tp_n, tp_n + sl_n)

    # C: Breadth
    row = db_query("""
      SELECT COUNT(DISTINCT symbol) AS sym_gain FROM events
      WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?
    """, (t24,))
    sym_gain = (row[0]["sym_gain"] if row else 0) or 0
    C = float(min(100.0, sym_gain * 2.0))  # 50 symboles -> 100 pts (ajuste si besoin)

    # D: Momentum (90 min / 24h)
    t90 = ms_ago(90)
    row = db_query("""
      WITH w AS (
        SELECT SUM(CASE WHEN time>=? THEN 1 ELSE 0 END) AS recent_n,
               COUNT(*) AS total_n
        FROM events
        WHERE type='ENTRY' AND time>=?
      )
      SELECT recent_n, total_n FROM w
    """, (t90, t24))
    recent_n = (row[0]["recent_n"] if row else 0) or 0
    total_n  = (row[0]["total_n"] if row else 0) or 0
    D = _pct(recent_n, total_n)

    score = round((A + B + C + D)/4.0)
    label = "Altseason (forte)" if score >= 75 else ("Altseason (modérée)" if score >= 50 else "Marché neutre/faible")
    return {
        "score": int(score),
        "label": label,
        "window_minutes": 24*60,
        "signals": {
            "long_ratio": round(A, 1),
            "tp_vs_sl": round(B, 1),
            "breadth_symbols": int(sym_gain),
            "recent_entries_ratio": round(D, 1),
        }
    }

# =========================
# /trades (TP verts, SL rouge)
# =========================
def build_trade_rows(limit=300):
    # Dernières ENTRY par trade_id
    entries = db_query("""
      SELECT e.trade_id, e.symbol, e.tf, COALESCE(e.tf_label,'') AS tf_label,
             e.side, e.entry, e.sl, e.tp1, e.tp2, e.tp3, MAX(e.time) AS t_entry
      FROM events e
      WHERE e.type='ENTRY'
      GROUP BY e.trade_id
      ORDER BY t_entry DESC
      LIMIT ?
    """, (limit,))
    rows = []
    for e in entries:
        trade_id  = e["trade_id"]
        tf_label  = e["tf_label"] or tf_to_label(e["tf"])
        hits = db_query("""
          SELECT type, MAX(time) AS t FROM events
          WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSE')
          GROUP BY type
        """, (trade_id,))
        hit = {r["type"]: True for r in hits}
        rows.append({
            "trade_id": trade_id,
            "symbol": e["symbol"],
            "tf_label": tf_label,
            "side": e["side"],
            "entry": e["entry"],
            "tp1": e["tp1"], "tp2": e["tp2"], "tp3": e["tp3"],
            "sl": e["sl"],
            "tp1_hit": bool(hit.get("TP1_HIT")),
            "tp2_hit": bool(hit.get("TP2_HIT")),
            "tp3_hit": bool(hit.get("TP3_HIT")),
            "sl_hit":  bool(hit.get("SL_HIT")),
        })
    return rows

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    try:
        ensure_trades_schema()
    except Exception:
        pass

    alt = compute_altseason_snapshot()
    rows = build_trade_rows(limit=300)

    css = """
    <style>
      :root { --bg:#0b0f14; --card:#111823; --txt:#e6edf3; --muted:#94a3b8;
              --green:#16a34a; --red:#ef4444; --chip:#1f2937; --chip-b:#334155; }
      body{margin:0;background:var(--bg);color:var(--txt);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif}
      .wrap{max-width:1200px;margin:24px auto;padding:0 16px}
      .altbox{display:flex;justify-content:space-between;align-items:center;background:var(--card);border:1px solid #1f2937;border-radius:16px;padding:16px 18px;margin-bottom:10px}
      .altbox h2{margin:0;font-size:18px}
      .altscore{font-weight:700;font-size:20px}
      .altchips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px 0}
      .chip{background:var(--chip);border:1px solid var(--chip-b);padding:6px 10px;border-radius:999px;font-size:12px;color:var(--muted)}
      table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid #1f2937;border-radius:12px;overflow:hidden}
      thead th{font-size:12px;color:var(--muted);text-align:left;padding:10px;border-bottom:1px solid #1f2937}
      tbody td{padding:10px;border-bottom:1px solid #162032;font-size:14px}
      tbody tr:hover{background:#0e1520}
      .tag{padding:2px 8px;border-radius:999px;border:1px solid #263246;background:#0f172a;color:#cbd5e1;font-size:12px;display:inline-block}
      .hit{background:rgba(22,163,74,.12);border-color:rgba(22,163,74,.45);color:#86efac}
      .sl {background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.5);color:#fca5a5}
      .dim{color:var(--muted)}
      .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace}
    </style>
    """

    alt_html = f"""
      <div class="altbox">
        <div>
          <h2>Indicateurs Altseason</h2>
          <div class="dim" style="font-size:12px">Fenêtre: {alt['window_minutes']} min — 4 signaux (LONG ratio, TP vs SL, breadth, momentum récent)</div>
        </div>
        <div class="altscore">{alt['score']}/100&nbsp; {alt['label']}</div>
      </div>
      <div class="altchips">
        <span class="chip">LONG ratio: {alt['signals']['long_ratio']}%</span>
        <span class="chip">TP vs SL: {alt['signals']['tp_vs_sl']}%</span>
        <span class="chip">Breadth: {alt['signals']['breadth_symbols']} sym.</span>
        <span class="chip">Entrées récentes: {alt['signals']['recent_entries_ratio']}%</span>
      </div>
    """

    def cell_tp(val, hit):
        if val is None:
            return '<span class="dim">—</span>'
        klass = "tag hit" if hit else "tag"
        return f'<span class="{klass}">TP {val}</span>'

    def cell_sl(val, sl_hit):
        if val is None:
            return '<span class="dim">—</span>'
        klass = "tag sl" if sl_hit else "tag"
        return f'<span class="{klass}">SL {val}</span>'

    rows_html = []
    for r in rows:
        rows_html.append(f"""
          <tr>
            <td class="mono">{(r.get('trade_id') or '')}</td>
            <td>{r['symbol']}</td>
            <td><span class="tag">{r['tf_label']}</span></td>
            <td>{r.get('side') or ''}</td>
            <td class="mono">{'' if r.get('entry') is None else r.get('entry')}</td>
            <td>{cell_tp(r.get('tp1'), r.get('tp1_hit'))}</td>
            <td>{cell_tp(r.get('tp2'), r.get('tp2_hit'))}</td>
            <td>{cell_tp(r.get('tp3'), r.get('tp3_hit'))}</td>
            <td>{cell_sl(r.get('sl'), r.get('sl_hit'))}</td>
          </tr>
        """)

    html = f"""<!doctype html>
    <html lang="fr"><head><meta charset="utf-8"><title>Trades</title>{css}</head>
    <body><div class="wrap">
      {alt_html}
      <table>
        <thead>
          <tr>
            <th>Trade ID</th><th>Symbole</th><th>TF</th><th>Side</th><th>Entry</th>
            <th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </div></body></html>"""
    return HTMLResponse(content=html)

# =========================
# Lancement local (optionnel)
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
