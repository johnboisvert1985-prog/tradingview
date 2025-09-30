# main.py
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException, Query
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
    C = float(min(100.0, sym_gain * 2.0))  # 50 symboles -> 100 pts

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
# /trades — Dashboard avancé (TP verts, SL rouge, Cancel/Flip orange + filtres)
# =========================
def _parse_date_to_ms(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None

def _fetch_entries(symbol: Optional[str], tf: Optional[str],
                   since_ms: Optional[int], until_ms: Optional[int],
                   limit: int) -> List[dict]:
    where = ["type='ENTRY'"]
    params: List[Any] = []
    if symbol:
        if "%" in symbol:
            where.append("symbol LIKE ?")
        else:
            where.append("symbol = ?")
        params.append(symbol.upper())
    if tf:
        where.append("(tf = ? OR tf_label = ?)")
        params += [tf, tf]
    if since_ms:
        where.append("time >= ?")
        params.append(int(since_ms))
    if until_ms:
        where.append("time < ?")
        params.append(int(until_ms))

    sql = f"""
      SELECT trade_id, symbol, tf, COALESCE(tf_label,'') AS tf_label,
             side, entry, sl, tp1, tp2, tp3, MAX(time) AS opened_ts
      FROM events
      WHERE {" AND ".join(where)}
      GROUP BY trade_id
      ORDER BY opened_ts DESC
      LIMIT ?
    """
    params.append(limit)
    return db_query(sql, tuple(params))

def _fetch_hits_for_trade(trade_id: str) -> dict:
    rows = db_query("""
        SELECT type, time, COALESCE(note,'') AS note
        FROM events
        WHERE trade_id = ?
          AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSE')
        ORDER BY time ASC
    """, (trade_id,))
    hit = {
        "tp1": False, "tp2": False, "tp3": False,
        "sl": False, "closed": False, "flip_note": False,
        "first_close_ts": None, "first_hit_ts": None
    }
    for r in rows:
        t = r["type"]
        if t == "TP1_HIT":
            hit["tp1"] = True
            hit["first_hit_ts"] = hit["first_hit_ts"] or r["time"]
        elif t == "TP2_HIT":
            hit["tp2"] = True
            hit["first_hit_ts"] = hit["first_hit_ts"] or r["time"]
        elif t == "TP3_HIT":
            hit["tp3"] = True
            hit["first_hit_ts"] = hit["first_hit_ts"] or r["time"]
        elif t == "SL_HIT":
            hit["sl"] = True
            hit["first_hit_ts"] = hit["first_hit_ts"] or r["time"]
        elif t == "CLOSE":
            hit["closed"] = True
            hit["first_close_ts"] = hit["first_close_ts"] or r["time"]
            note = (r.get("note") or "").lower()
            if "flip" in note or "reverse" in note or "direction" in note:
                hit["flip_note"] = True
    return hit

def _detect_flip(symbol: str, tf: str, opened_ts: int, side: Optional[str], this_trade_id: str) -> Optional[int]:
    """
    Flip détecté si un ENTRY plus récent existe pour le même symbol+tf
    avec un side opposé (LONG vs SHORT). Retourne le timestamp du flip si trouvé.
    """
    if not side:
        return None
    opposite = "SHORT" if side.upper() == "LONG" else "LONG"
    row = db_query("""
        SELECT MIN(time) AS t
        FROM events
        WHERE type='ENTRY' AND symbol=? AND (tf=? OR tf_label=?)
          AND time > ? AND UPPER(COALESCE(side,'')) = ?
    """, (symbol, tf, tf, opened_ts, opposite))
    t = (row[0]["t"] if row and row[0]["t"] is not None else None)
    return int(t) if t else None

def _fmt_duration_secs(opened_ts: Optional[int], end_ts: Optional[int]) -> int:
    if not opened_ts or not end_ts:
        return 0
    return max(0, int((end_ts - opened_ts) / 1000))

def _compute_summary(rows: List[dict]) -> dict:
    total = len(rows)
    wins = losses = 0
    tp1n = tp2n = tp3n = 0
    durations: List[int] = []
    for r in rows:
        if r["sl_hit"]:
            losses += 1
            durations.append(_fmt_duration_secs(r["opened_ts"], r["end_ts"] or r["opened_ts"]))
        elif r["tp3_hit"] or r["tp2_hit"] or r["tp1_hit"]:
            wins += 1
            tp1n += int(r["tp1_hit"])
            tp2n += int(r["tp2_hit"])
            tp3n += int(r["tp3_hit"])
            durations.append(_fmt_duration_secs(r["opened_ts"], r["end_ts"] or r["opened_ts"]))
        elif r["opened_ts"] and r["end_ts"]:
            durations.append(_fmt_duration_secs(r["opened_ts"], r["end_ts"]))
    avg = int(sum(durations) / len(durations)) if durations else 0
    winrate = round((wins / total) * 100, 1) if total else 0.0
    return {"total": total, "wins": wins, "losses": losses, "winrate": winrate,
            "tp1": tp1n, "tp2": tp2n, "tp3": tp3n, "avg_time": avg}

def build_trade_rows_v2(symbol: Optional[str], tf: Optional[str],
                        start_date: Optional[str], end_date: Optional[str],
                        limit: int) -> List[dict]:
    since_ms = _parse_date_to_ms(start_date)
    until_ms = _parse_date_to_ms(end_date)
    entries = _fetch_entries(symbol, tf, since_ms, until_ms, limit)
    out: List[dict] = []
    for e in entries:
        trade_id = e["trade_id"]
        tf_label = e["tf_label"] or tf_to_label(e["tf"])
        hits = _fetch_hits_for_trade(trade_id)

        # Statut par défaut
        end_ts = None
        row_class = ""
        outcome_txt = "—"

        # 1) SL ou TP (prioritaires)
        if hits["sl"]:
            row_class = "row-sl"
            outcome_txt = "SL"
            end_ts = hits["first_hit_ts"]
        elif hits["tp3"] or hits["tp2"] or hits["tp1"]:
            if hits["tp3"]:
                outcome_txt = "TP3"
            elif hits["tp2"]:
                outcome_txt = "TP2"
            else:
                outcome_txt = "TP1"
            end_ts = hits["first_hit_ts"]

        # 2) CLOSE / NOTE flip
        elif hits["closed"]:
            row_class = "row-cancel"
            outcome_txt = "Flip" if hits["flip_note"] else "Canceled"
            end_ts = hits["first_close_ts"]

        # 3) ENTRY opposé (flip “silencieux”)
        flip_ts = _detect_flip(e["symbol"], str(e["tf"] or tf_label), e["opened_ts"], e["side"], trade_id)
        if flip_ts and (end_ts is None or flip_ts < end_ts):
            row_class = "row-cancel"
            outcome_txt = "Flip"
            end_ts = flip_ts

        out.append({
            "trade_id": trade_id,
            "symbol": e["symbol"],
            "tf_label": tf_label,
            "side": e["side"],
            "entry": e["entry"],
            "sl": e["sl"],
            "tp1": e["tp1"], "tp2": e["tp2"], "tp3": e["tp3"],
            "opened_ts": e["opened_ts"],
            "end_ts": end_ts,
            "duration": _fmt_duration_secs(e["opened_ts"], end_ts),
            "tp1_hit": hits["tp1"], "tp2_hit": hits["tp2"], "tp3_hit": hits["tp3"],
            "sl_hit": hits["sl"],
            "row_class": row_class, "outcome": outcome_txt
        })
    return out

@app.get("/trades", response_class=HTMLResponse)
async def trades_page(
    symbol: Optional[str] = Query(default=None, description="Ex: BTCUSDT.P (peut contenir %)"),
    tf: Optional[str] = Query(default=None, description="Ex: 15, 60, 1D (tf ou tf_label)"),
    start: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    try:
        ensure_trades_schema()
    except Exception:
        pass

    alt = compute_altseason_snapshot()
    rows = build_trade_rows_v2(symbol, tf, start, end, limit)
    summary = _compute_summary(rows)

    css = """
    <style>
      :root { --bg:#0b0f14; --card:#111823; --txt:#e6edf3; --muted:#94a3b8;
              --green:#16a34a; --red:#ef4444; --orange:#f59e0b;
              --chip:#0f172a; --chip-b:#243041; --line:#1e293b; }
      *{box-sizing:border-box}
      body{margin:0;background:var(--bg);color:var(--txt);font-family:Inter,system-ui,Segoe UI,Roboto,Arial}
      .wrap{max-width:1200px;margin:24px auto;padding:0 16px}
      .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
      h2{margin:0 0 8px 0;font-size:18px}
      .muted{color:var(--muted)}
      .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
      input,button{height:38px;border-radius:10px;border:1px solid var(--line);background:#0d1420;color:var(--txt);padding:0 10px}
      input{min-width:160px}
      button{background:#2563eb;border-color:#1e40af;cursor:pointer}
      button:hover{filter:brightness(1.05)}
      table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
      thead th{font-size:12px;color:var(--muted);text-align:left;padding:10px;border-bottom:1px solid var(--line)}
      tbody td{padding:10px;border-bottom:1px solid #162032;font-size:14px}
      tbody tr:hover{background:#0e1520}
      .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace}
      .tag{padding:2px 8px;border-radius:999px;border:1px solid #263246;background:#0f172a;color:#cbd5e1;font-size:12px;display:inline-block}
      .hit{background:rgba(22,163,74,.12);border-color:rgba(22,163,74,.45);color:#86efac}
      .cell-sl{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.5);color:#fca5a5}
      .row-sl td{background:rgba(239,68,68,.08)}
      .row-cancel td{background:rgba(245,158,11,.08)}
      .kpis{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
      .kpi{background:var(--chip);border:1px solid var(--chip-b);border-radius:10px;padding:6px 10px;font-size:12px;color:#cbd5e1}
      .empty{padding:20px;color:var(--muted)}
    </style>
    """

    # En-tête Altseason
    alt_html = f"""
      <div class="card" style="margin-bottom:12px">
        <h2>Indicateurs Altseason</h2>
        <div class="muted">Fenêtre: {alt['window_minutes']} min — 4 signaux (LONG ratio, TP vs SL, breadth, momentum récent)</div>
        <div class="row" style="margin-top:10px">
          <span class="kpi">Score: {alt['score']}/100</span>
          <span class="kpi">{alt['label']}</span>
          <span class="kpi">LONG ratio: {alt['signals']['long_ratio']}%</span>
          <span class="kpi">TP vs SL: {alt['signals']['tp_vs_sl']}%</span>
          <span class="kpi">Breadth: {alt['signals']['breadth_symbols']} sym.</span>
          <span class="kpi">Entrées récentes: {alt['signals']['recent_entries_ratio']}%</span>
        </div>
      </div>
    """

    # Filtres (GET)
    def _val(v): return "" if v is None else str(v)
    filters_html = f"""
    <div class="card" style="margin-bottom:12px">
      <h2>Trades — Dashboard</h2>
      <div class="muted" style="margin-bottom:10px">Filtrez par symbole / timeframe / date, puis validez.</div>
      <form method="get" class="row" action="/trades">
        <input name="symbol" placeholder="symbol (ex: BTCUSDT.P ou %USDT.P)" value="{_val(symbol)}" />
        <input name="tf" placeholder="tf (ex: 15, 60, 1D)" value="{_val(tf)}" />
        <input name="start" type="date" value="{_val(start)}" />
        <input name="end" type="date" value="{_val(end)}" />
        <input name="limit" type="number" min="1" max="1000" value="{limit}" style="width:90px" />
        <button type="submit">Appliquer</button>
        <a class="kpi" href="/">&larr; Home</a>
      </form>
    </div>
    """

    kpi_html = f"""
    <div class="kpis" style="margin:-6px 0 12px 0">
      <div class="kpi">Total: {summary['total']}</div>
      <div class="kpi">Winrate: {summary['winrate']}%</div>
      <div class="kpi">Wins: {summary['wins']}</div>
      <div class="kpi">Losses: {summary['losses']}</div>
      <div class="kpi">TP1: {summary['tp1']}</div>
      <div class="kpi">TP2: {summary['tp2']}</div>
      <div class="kpi">TP3: {summary['tp3']}</div>
      <div class="kpi">Avg. time: {summary['avg_time']}s</div>
    </div>
    """

    # Helpers cellule
    def cell_tp(val, hit):
        if val is None:
            return '<span class="muted">—</span>'
        klass = "tag hit" if hit else "tag"
        return f'<span class="{klass}">TP {val}</span>'

    def cell_sl(val, sl_hit):
        if val is None:
            return '<span class="muted">—</span>'
        klass = "tag cell-sl" if sl_hit else "tag"
        return f'<span class="{klass}">SL {val}</span>'

    body_rows = []
    if not rows:
        body_rows.append('<tr><td colspan="12" class="empty">No trades yet. Send a webhook to <code>/tv-webhook</code>.</td></tr>')
    else:
        for r in rows:
            body_rows.append(f"""
              <tr class="{r['row_class']}">
                <td class="mono">{r['trade_id']}</td>
                <td>{r['symbol']}</td>
                <td><span class="tag">{r['tf_label']}</span></td>
                <td>{r.get('side') or ''}</td>
                <td class="mono">{'' if r.get('entry') is None else r.get('entry')}</td>
                <td>{cell_tp(r.get('tp1'), r.get('tp1_hit'))}</td>
                <td>{cell_tp(r.get('tp2'), r.get('tp2_hit'))}</td>
                <td>{cell_tp(r.get('tp3'), r.get('tp3_hit'))}</td>
                <td>{cell_sl(r.get('sl'), r.get('sl_hit'))}</td>
                <td>{r['outcome']}</td>
                <td class="mono">{r['duration']}</td>
              </tr>
            """)

    table_html = f"""
    <div class="card">
      <div class="muted" style="margin-bottom:8px">Stats</div>
      {kpi_html}
      <table>
        <thead>
          <tr>
            <th>Trade ID</th>
            <th>Symbol</th>
            <th>TF</th>
            <th>Side</th>
            <th>Entry</th>
            <th>TP1</th>
            <th>TP2</th>
            <th>TP3</th>
            <th>SL</th>
            <th>Outcome</th>
            <th>Duration(s)</th>
          </tr>
        </thead>
        <tbody>
          {''.join(body_rows)}
        </tbody>
      </table>
    </div>
    """

    html = f"""<!doctype html>
    <html lang="fr">
      <head><meta charset="utf-8"><title>Trades</title>{css}</head>
      <body>
        <div class="wrap">
          {alt_html}
          {filters_html}
          {table_html}
        </div>
      </body>
    </html>"""
    return HTMLResponse(content=html)

# =========================
# Lancement local (optionnel)
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)

