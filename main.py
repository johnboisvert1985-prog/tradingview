# main.py  — BLOC 1/5
import os
import re
import time
import math
import json
import uuid
import hmac
import base64
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

# -------------------------
# Config
# -------------------------
APP_SECRET = os.getenv("TV_WEBHOOK_SECRET", "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DB_PATH = os.getenv("DB_PATH", "/tmp/ai_trader/data.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# -------------------------
# App
# -------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# -------------------------
# SQLite connex + Row factory
# -------------------------
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row

def _exec(q: str, params: Tuple = ()):
    cur = DB.execute(q, params)
    DB.commit()
    return cur

def _query(q: str, params: Tuple = ()) -> List[sqlite3.Row]:
    cur = DB.execute(q, params)
    return cur.fetchall()

# -------------------------
# Migrations idempotentes
# -------------------------
def migrate_events_table():
    _exec("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time INTEGER,
        type TEXT,
        symbol TEXT,
        tf TEXT,
        trade_id TEXT,
        -- colonnes qui peuvent manquer, on les ajoutera après si besoin
        tf_label TEXT,
        direction TEXT,
        price REAL,
        note TEXT,
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
        leverage TEXT
    );
    """)

    def ensure_column(name: str, decl: str):
        cols = {r["name"] for r in _query("PRAGMA table_info(events)")}
        if name not in cols:
            log.warning("DB: adding missing column %s", name)
            _exec(f"ALTER TABLE events ADD COLUMN {name} {decl};")

    ensure_column("tf_label", "TEXT")
    ensure_column("direction", "TEXT")
    ensure_column("price", "REAL")
    ensure_column("note", "TEXT")
    ensure_column("side", "TEXT")
    ensure_column("entry", "REAL")
    ensure_column("sl", "REAL")
    ensure_column("tp1", "REAL")
    ensure_column("tp2", "REAL")
    ensure_column("tp3", "REAL")
    ensure_column("r1", "REAL")
    ensure_column("s1", "REAL")
    ensure_column("lev_reco", "REAL")
    ensure_column("qty_reco", "REAL")
    ensure_column("notional", "REAL")
    ensure_column("confidence", "INTEGER")
    ensure_column("horizon", "TEXT")
    ensure_column("leverage", "TEXT")

    # Index utiles
    _exec("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id);")
    _exec("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time);")
    _exec("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);")
    _exec("CREATE INDEX IF NOT EXISTS idx_events_symbol_time ON events(symbol, time);")

migrate_events_table()

# -------------------------
# Helpers DB publics
# -------------------------
def db_execute(query: str, params: Tuple = ()) -> int:
    cur = _exec(query, params)
    return cur.lastrowid if cur.lastrowid is not None else 0

def db_query(query: str, params: Tuple = ()) -> List[sqlite3.Row]:
    return _query(query, params)

# -------------------------
# Utils
# -------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def as_utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None: return None
        return float(v)
    except Exception:
        return None

def safe_int(v: Any) -> Optional[int]:
    try:
        if v is None: return None
        return int(v)
    except Exception:
        return None
# main.py — BLOC 2/5

import urllib.parse
import urllib.request

# -------------------------
# Telegram (optionnel)
# -------------------------
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram send skipped (no token/chat id).")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning("Telegram send non-200: %s", resp.status)
    except Exception as e:
        log.warning("Telegram send exception: %s", e)

# Mapping du petit carré : UP -> 🟩, DOWN -> 🟥, neutre -> 🟪
def vector_square(direction: Optional[str]) -> str:
    if (direction or "").upper() == "UP":
        return "🟩"
    if (direction or "").upper() == "DOWN":
        return "🟥"
    return "🟪"

# -------------------------
# Sauvegarde d'un événement
# -------------------------
def save_event(payload: Dict[str, Any]) -> str:
    """
    Normalise et enregistre un événement dans la table events.
    Retourne le trade_id utilisé.
    """
    etype = (payload.get("type") or "").upper()
    symbol = payload.get("symbol")
    tf = str(payload.get("tf") or "")
    tf_label = str(payload.get("tf_label") or f"{tf}m" if tf else "")
    direction = payload.get("direction")
    price = safe_float(payload.get("price"))
    note = payload.get("note")
    t_ms = safe_int(payload.get("time")) or now_ms()
    trade_id = payload.get("trade_id")

    # Champs trade/info si ENTRY
    side = payload.get("side")
    entry = safe_float(payload.get("entry"))
    sl = safe_float(payload.get("sl"))
    tp1 = safe_float(payload.get("tp1"))
    tp2 = safe_float(payload.get("tp2"))
    tp3 = safe_float(payload.get("tp3"))
    r1 = safe_float(payload.get("r1"))
    s1 = safe_float(payload.get("s1"))
    lev_reco = safe_float(payload.get("lev_reco"))
    qty_reco = safe_float(payload.get("qty_reco"))
    notional = safe_float(payload.get("notional"))
    confidence = safe_int(payload.get("confidence"))
    horizon = payload.get("horizon")
    leverage = payload.get("leverage")

    # Auto trade_id si absent
    if not trade_id:
        base = f"{symbol}_{tf}_{t_ms}"
        # certains types (VECTOR_CANDLE) n'ont pas de time réaliste -> garantie unicité
        trade_id = base if etype == "ENTRY" else f"{symbol}_{tf}_{now_ms()}"

    db_execute("""
        INSERT INTO events(
            time,type,symbol,tf,trade_id,tf_label,direction,price,note,
            side,entry,sl,tp1,tp2,tp3,r1,s1,lev_reco,qty_reco,notional,confidence,horizon,leverage
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        t_ms, etype, symbol, tf, trade_id, tf_label, direction, price, note,
        side, entry, sl, tp1, tp2, tp3, r1, s1, lev_reco, qty_reco, notional, confidence, horizon, leverage
    ))

    # Telegram (exemples compacts)
    if etype == "VECTOR_CANDLE":
        sq = vector_square(direction)
        send_telegram(f"{sq} Vector Candle — <b>{symbol}</b> {tf_label}\n"
                      f"Prix: {price if price is not None else '-'}")

    elif etype == "ENTRY":
        send_telegram(
            f"🟦 ENTRY — <b>{symbol}</b> {tf_label} ({side or '-'})\n"
            f"Entry: {entry or '-'} | SL: {sl or '-'} | TP1/TP2/TP3: {tp1 or '-'} / {tp2 or '-'} / {tp3 or '-'}"
        )
    elif etype in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        send_telegram(f"🟩 {etype.replace('_',' ')} — <b>{symbol}</b> {tf_label}")
    elif etype == "SL_HIT":
        send_telegram(f"🟥 Stop Loss — <b>{symbol}</b> {tf_label}")
    elif etype.startswith("AOE_"):
        send_telegram(f"🟨 {etype} — <b>{symbol}</b> {tf_label}")

    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s", etype, symbol, tf, trade_id)
    return trade_id

# -------------------------
# Altseason (4 signaux, sans VECTOR)
# -------------------------
def compute_altseason_snapshot(window_minutes: int = 120) -> Dict[str, Any]:
    """
    Score 0..100 basé sur 4 familles d’événements (hors VECTOR_CANDLE) dans la fenêtre.
    1) Momentum entries : nombre d'ENTRY (alts) pondéré
    2) Quality : TP hits vs SL hits (ratio)
    3) AOE balance : PREMIUM - DISCOUNT
    4) Breadth : nombre de symboles uniques actifs (hors majors)
    """
    now_ = now_ms()
    since = now_ - window_minutes * 60 * 1000

    ev = db_query("""
        SELECT * FROM events
        WHERE time >= ? AND type != 'VECTOR_CANDLE'
        ORDER BY time DESC
    """, (since,))

    def is_major(symbol: str) -> bool:
        s = (symbol or "").upper()
        majors = ("BTC", "ETH", "USDT", "USDC", "USD.P", "USD", "SOLUSD", "BTCUSD", "ETHUSD")
        return any(s.startswith(m) for m in majors)

    # 1) Entries momentum
    entries = [r for r in ev if r["type"] == "ENTRY" and not is_major(r["symbol"])]
    n_entries = len(entries)
    score_entries = min(40, n_entries * 2)  # 20 entries -> 40 pts

    # 2) Quality : TP hits vs SL
    tp_hits = [r for r in ev if r["type"] in ("TP1_HIT","TP2_HIT","TP3_HIT")]
    sl_hits = [r for r in ev if r["type"] == "SL_HIT"]
    q = (len(tp_hits) / max(1, len(tp_hits) + len(sl_hits)))
    score_quality = int(30 * q)  # max 30 lorsque aucun SL

    # 3) AOE balance
    aoe_prem = sum(1 for r in ev if r["type"] == "AOE_PREMIUM")
    aoe_disc = sum(1 for r in ev if r["type"] == "AOE_DISCOUNT")
    bal = aoe_prem - aoe_disc
    score_aoe = max(0, min(20, 10 + bal))  # centre à 10, ±10

    # 4) Breadth (largeur du marché)
    symbols = {r["symbol"] for r in ev if not is_major(r["symbol"])}
    breadth = len(symbols)
    score_breadth = min(10, breadth // 5)  # 50 alts actives -> 10

    score = max(0, min(100, score_entries + score_quality + score_aoe + score_breadth))

    label = "faible"
    if score >= 75:
        label = "forte"
    elif score >= 45:
        label = "modérée"

    return {
        "window_min": window_minutes,
        "score": score,
        "label": label,
        "entries": n_entries,
        "tp_hits": len(tp_hits),
        "sl_hits": len(sl_hits),
        "aoe_premium": aoe_prem,
        "aoe_discount": aoe_disc,
        "breadth": breadth,
    }
# main.py — BLOC 3/5

def get_trade_status(trade_id: str) -> Dict[str, bool]:
    hits = db_query("""
        SELECT type FROM events
        WHERE trade_id = ?
          AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT')
    """, (trade_id,))
    kinds = {r["type"] for r in hits}
    return {
        "tp1": "TP1_HIT" in kinds,
        "tp2": "TP2_HIT" in kinds,
        "tp3": "TP3_HIT" in kinds,
        "sl":  "SL_HIT" in kinds,
    }

def latest_entry_per_trade(limit: int = 200) -> List[sqlite3.Row]:
    # On prend les derniers ENTRY puis on complète avec leur état
    rows = db_query("""
        SELECT e.*
        FROM events e
        WHERE e.type = 'ENTRY'
        ORDER BY e.time DESC
        LIMIT ?
    """, (limit,))
    return rows

def build_trade_rows(limit: int = 200) -> List[Dict[str, Any]]:
    rows = latest_entry_per_trade(limit=limit)
    out = []
    for r in rows:
        st = get_trade_status(r["trade_id"])
        out.append({
            "time": r["time"],
            "time_iso": as_utc_iso(r["time"]),
            "symbol": r["symbol"],
            "tf_label": r["tf_label"] or r["tf"] or "",
            "side": r["side"] or "-",
            "entry": r["entry"],
            "sl": r["sl"],
            "tp1": r["tp1"],
            "tp2": r["tp2"],
            "tp3": r["tp3"],
            "confidence": r["confidence"],
            "horizon": r["horizon"] or "",
            "leverage": r["leverage"] or "",
            "trade_id": r["trade_id"],
            "hit": st,  # dict booleans
        })
    return out

def render_trades_page() -> str:
    snap = compute_altseason_snapshot(window_minutes=120)
    rows = build_trade_rows()

    css = """
    <style>
    :root {
      --bg: #0b0e14; --card:#111827; --muted:#6b7280; --text:#e5e7eb;
      --green:#16a34a; --red:#ef4444; --amber:#f59e0b; --border:#1f2937;
      --chip:#0f172a; --chip2:#111827;
    }
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,Ubuntu}
    .wrap{max-width:1300px;margin:32px auto;padding:0 16px}
    .hstack{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
    .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
    h1{font-size:22px;margin:0 0 8px 0}
    .muted{color:var(--muted)}
    .altgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
    .pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#0b1220;border:1px solid var(--border);font-weight:600}
    .pill.green{background:rgba(22,163,74,.1);border-color:#134e2a;color:#34d399}
    .pill.red{background:rgba(239,68,68,.08);border-color:#7f1d1d;color:#fca5a5}
    .pill.amber{background:rgba(245,158,11,.08);border-color:#78350f;color:#fcd34d}
    .alt-score{font-size:28px;font-weight:800}
    table{width:100%;border-collapse:collapse;margin-top:16px}
    th,td{padding:10px;border-bottom:1px solid var(--border);text-align:left}
    th{color:#aeb3bb;font-weight:700;background:#0e1525;position:sticky;top:0}
    .mono{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace}
    .tag{padding:2px 8px;border-radius:999px;background:var(--chip);border:1px solid var(--border);color:#b6beca;font-size:12px}
    .hit{background:rgba(22,163,74,.12);border:1px solid #14532d;color:#34d399;padding:2px 8px;border-radius:8px;display:inline-block}
    .miss{background:#0b1220;border:1px dashed #293241;color:#94a3b8;padding:2px 8px;border-radius:8px;display:inline-block}
    .slhit{background:rgba(239,68,68,.12);border:1px solid #7f1d1d;color:#fca5a5;padding:2px 8px;border-radius:8px;display:inline-block}
    .small{font-size:12px;color:#9aa3af}
    .right{text-align:right}
    .center{text-align:center}
    .nowrap{white-space:nowrap}
    </style>
    """

    # Label Altseason
    alt_label = snap["label"]
    if alt_label == "forte":
        alt_pill = '<span class="pill green">Altseason forte</span>'
    elif alt_label == "modérée":
        alt_pill = '<span class="pill amber">Altseason modérée</span>'
    else:
        alt_pill = '<span class="pill red">Altseason faible</span>'

    alt_card = f"""
    <div class="card">
      <div class="hstack" style="justify-content:space-between">
        <div>
          <h1>Chaleur Altseason (0–100)</h1>
          <div class="small">Fenêtre: {snap["window_min"]} min – sans signaux Vector</div>
        </div>
        <div class="alt-score">{snap["score"]}/100</div>
      </div>
      <div style="margin-top:10px">{alt_pill}</div>
      <div class="altgrid" style="margin-top:12px">
        <div class="card">
          <div class="small muted">Entries alts</div>
          <div style="font-weight:700">{snap["entries"]}</div>
        </div>
        <div class="card">
          <div class="small muted">TP hits</div>
          <div style="font-weight:700">{snap["tp_hits"]}</div>
        </div>
        <div class="card">
          <div class="small muted">SL hits</div>
          <div style="font-weight:700">{snap["sl_hits"]}</div>
        </div>
        <div class="card">
          <div class="small muted">AOE premium / discount</div>
          <div style="font-weight:700">{snap["aoe_premium"]} / {snap["aoe_discount"]}</div>
        </div>
      </div>
    </div>
    """

    # Table trades
    head = """
    <table>
      <thead>
        <tr>
          <th>Heure (UTC)</th>
          <th>Symbole</th>
          <th>TF</th>
          <th>Side</th>
          <th class="right">Entry</th>
          <th class="right">SL</th>
          <th class="center">TP1</th>
          <th class="center">TP2</th>
          <th class="center">TP3</th>
          <th>Conf.</th>
          <th>Horizon</th>
          <th>Lev.</th>
          <th>Trade ID</th>
        </tr>
      </thead>
      <tbody>
    """

    body_rows = []
    for r in rows:
        hit = r["hit"]
        tp1_cls = "hit" if hit["tp1"] else ("slhit" if hit["sl"] else "miss")
        tp2_cls = "hit" if hit["tp2"] else ("slhit" if hit["sl"] else "miss")
        tp3_cls = "hit" if hit["tp3"] else ("slhit" if hit["sl"] else "miss")

        body_rows.append(f"""
        <tr>
          <td class="mono nowrap">{r["time_iso"]}</td>
          <td class="mono">{r["symbol"]}</td>
          <td>{r["tf_label"]}</td>
          <td><span class="tag">{r["side"]}</span></td>
          <td class="right mono">{r["entry"] if r["entry"] is not None else "-"}</td>
          <td class="right mono">{r["sl"] if r["sl"] is not None else "-"}</td>
          <td class="center"><span class="{tp1_cls}">TP1</span></td>
          <td class="center"><span class="{tp2_cls}">TP2</span></td>
          <td class="center"><span class="{tp3_cls}">TP3</span></td>
          <td class="center">{r["confidence"] if r["confidence"] is not None else "-"}</td>
          <td class="center">{r["horizon"] or "-"}</td>
          <td class="center">{r["leverage"] or "-"}</td>
          <td class="mono small">{r["trade_id"]}</td>
        </tr>
        """)

    tail = """
      </tbody>
    </table>
    """

    html = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width,initial-scale=1" />
      <title>Trades</title>
      {css}
    </head>
    <body>
      <div class="wrap">
        {alt_card}
        <div class="card" style="margin-top:16px">
          <div class="hstack" style="justify-content:space-between">
            <h1>Derniers trades</h1>
            <div class="small muted">TP verts quand atteints • SL en rouge</div>
          </div>
          {head}
          {''.join(body_rows)}
          {tail}
        </div>
      </div>
    </body>
    </html>
    """
    return html
# main.py — BLOC 4/5

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    try:
        return HTMLResponse(render_trades_page())
    except Exception as e:
        log.exception("trades_page error")
        raise HTTPException(status_code=500, detail=str(e))

def _check_secret(payload: Dict[str, Any]):
    sec = payload.get("secret")
    if APP_SECRET and sec != APP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden (secret)")

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    payload = await req.json()
    log.info("Webhook payload: %s", payload)
    _check_secret(payload)

    # normalisation légère
    # assure tf_label si pas fourni
    if not payload.get("tf_label"):
        tf = str(payload.get("tf") or "")
        payload["tf_label"] = f"{tf}m" if tf else ""

    trade_id = save_event(payload)
    return {"ok": True, "trade_id": trade_id}

@app.get("/api/altseason")
def api_altseason():
    return compute_altseason_snapshot(window_minutes=120)

@app.get("/api/trades")
def api_trades():
    return build_trade_rows()
# main.py — BLOC 5/5

def sanity_ping_db():
    # Vérifie qu'on peut requêter tf_label sans erreur "no such column"
    try:
        _query("SELECT tf_label FROM events LIMIT 1")
    except Exception as e:
        log.exception("DB sanity check failed")
        raise

sanity_ping_db()

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
