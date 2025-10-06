# main.py - AI Trader Pro v2.2 Enhanced
# Professional Trading Dashboard with Advanced Features
# Python 3.8+

import os
import sqlite3
import logging
import logging.handlers
import asyncio
import time
import shutil
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, validator
import httpx

# CONFIGURATION
class Settings:
    DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
    DB_PATH = os.path.join(DB_DIR, "data.db")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    TELEGRAM_PIN_ALTSEASON = int(os.getenv("TELEGRAM_PIN_ALTSEASON", "1"))
    TG_MIN_DELAY_SEC = float(os.getenv("TG_MIN_DELAY_SEC", "10.0"))
    TG_PER_MIN_LIMIT = int(os.getenv("TG_PER_MIN_LIMIT", "10"))
    TG_BUTTONS = int(os.getenv("TG_BUTTONS", "1"))
    TG_BUTTON_TEXT = os.getenv("TG_BUTTON_TEXT", "📊 Ouvrir le Dashboard")
    TG_DASHBOARD_URL = os.getenv("TG_DASHBOARD_URL", "https://tradingview-gd03.onrender.com/trades")
    ALTSEASON_AUTONOTIFY = int(os.getenv("ALTSEASON_AUTONOTIFY", "1"))
    ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))
    ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
    VECTOR_UP_ICON = "🟩"
    VECTOR_DN_ICON = "🟥"
    VECTOR_GLOBAL_GAP_SEC = int(os.getenv("VECTOR_GLOBAL_GAP_SEC", "10"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

settings = Settings()
os.makedirs(settings.DB_DIR, exist_ok=True)

logger = logging.getLogger("aitrader")
logger.setLevel(settings.LOG_LEVEL)
console_handler = logging.StreamHandler()
console_handler.setLevel(settings.LOG_LEVEL)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

try:
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(settings.DB_DIR, 'ai_trader.log'),
        maxBytes=10*1024*1024,
        backupCount=5
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    logger.warning(f"Could not create file handler: {e}")

logger.info(f"AI Trader Pro v2.2 Enhanced - DB: {settings.DB_PATH}")

# PYDANTIC MODELS
class WebhookPayload(BaseModel):
    type: str
    symbol: str
    tf: Optional[str] = None
    tf_label: Optional[str] = None
    time: Optional[int] = None
    side: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    r1: Optional[float] = None
    s1: Optional[float] = None
    lev_reco: Optional[float] = None
    qty_reco: Optional[float] = None
    notional: Optional[float] = None
    confidence: Optional[int] = None
    horizon: Optional[str] = None
    leverage: Optional[str] = None
    note: Optional[str] = None
    price: Optional[float] = None
    direction: Optional[str] = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None

    @validator('type')
    def validate_type(cls, v):
        valid = ['ENTRY', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT', 'SL_HIT', 'CLOSE', 'VECTOR_CANDLE']
        if v not in valid:
            raise ValueError(f'Type invalide: {v}')
        return v

    @validator('side')
    def validate_side(cls, v):
        if v and v.upper() not in ['LONG', 'SHORT']:
            raise ValueError(f'Side invalide: {v}')
        return v.upper() if v else None

# DATABASE
def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

@contextmanager
def get_db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=30.0)
    conn.row_factory = dict_factory
    try:
        yield conn
    finally:
        conn.close()

def db_execute(sql: str, params: tuple = ()):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            return cur
    except sqlite3.Error as e:
        logger.error(f"DB error: {e}")
        raise

def db_query(sql: str, params: tuple = ()) -> List[dict]:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return list(cur.fetchall())
    except sqlite3.Error as e:
        logger.error(f"Query error: {e}")
        return []

def init_database():
    try:
        db_execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tf TEXT,
                tf_label TEXT,
                time INTEGER NOT NULL,
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
                trade_id TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        indices = [
            "CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
            "CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_symbol_tf ON events(symbol, tf)",
            "CREATE INDEX IF NOT EXISTS idx_events_composite ON events(symbol, tf, type, time DESC)"
        ]
        for idx in indices:
            db_execute(idx)
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
        raise

init_database()

# UTILITIES
def tf_to_label(tf: Any) -> str:
    if tf is None:
        return ""
    s = str(tf)
    try:
        n = int(s)
    except:
        return s
    if n < 60:
        return f"{n}m"
    if n == 60:
        return "1h"
    if n % 60 == 0:
        return f"{n//60}h"
    return s

def ensure_trades_schema():
    try:
        cols = {r["name"] for r in db_query("PRAGMA table_info(events)")}
        if "tf_label" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN tf_label TEXT")
        if "created_at" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN created_at INTEGER")
    except:
        pass

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def ms_ago(minutes: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)

def human_duration_verbose(ms: int) -> str:
    if ms <= 0:
        return "0 s"
    s = ms // 1000
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h} h {m} min"
    if m > 0:
        return f"{m} min {sec} s"
    return f"{sec} s"

try:
    ensure_trades_schema()
except:
    pass

# TELEGRAM
_last_tg_sent: Dict[str, float] = {}
_last_altseason_notify_ts: float = 0.0
_last_global_send_ts: float = 0.0
_send_times_window = deque()
_last_vector_flush_ts: float = 0.0

def _create_dashboard_button() -> Optional[dict]:
    if not settings.TG_BUTTONS or not settings.TG_DASHBOARD_URL:
        return None
    return {"inline_keyboard": [[{"text": settings.TG_BUTTON_TEXT, "url": settings.TG_DASHBOARD_URL}]]}

async def _respect_rate_limits():
    global _last_global_send_ts, _send_times_window
    now = time.time()
    while _send_times_window and now - _send_times_window[0] > 60:
        _send_times_window.popleft()
    if len(_send_times_window) >= settings.TG_PER_MIN_LIMIT:
        sleep_for = 60 - (now - _send_times_window[0]) + 0.2
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
    delta = now - _last_global_send_ts
    if delta < settings.TG_MIN_DELAY_SEC:
        await asyncio.sleep(settings.TG_MIN_DELAY_SEC - delta)

def _record_sent():
    global _last_global_send_ts, _send_times_window
    ts = time.time()
    _last_global_send_ts = ts
    _send_times_window.append(ts)

async def tg_send_text(text: str, disable_web_page_preview: bool = True, key: Optional[str] = None, reply_markup: Optional[dict] = None, pin: bool = False) -> Dict[str, Any]:
    if not settings.TELEGRAM_ENABLED:
        return {"ok": False, "reason": "disabled"}
    k = key or "default"
    now_ts = time.time()
    last = _last_tg_sent.get(k, 0.0)
    if now_ts - last < settings.TG_MIN_DELAY_SEC:
        return {"ok": False, "reason": "cooldown"}
    _last_tg_sent[k] = now_ts
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": disable_web_page_preview, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await _respect_rate_limits()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 429:
                retry = 30.0
                try:
                    retry = float(r.json().get("parameters", {}).get("retry_after", 30))
                except:
                    pass
                await asyncio.sleep(retry + 0.5)
                await _respect_rate_limits()
                r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            logger.info(f"TG sent: {text[:50]}...")
            _record_sent()
            if pin and settings.TELEGRAM_PIN_ALTSEASON and data.get("ok"):
                try:
                    mid = data["result"]["message_id"]
                    pin_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/pinChatMessage"
                    await client.post(pin_url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "message_id": mid, "disable_notification": True})
                except:
                    pass
            return {"ok": True, "result": data}
    except Exception as e:
        logger.error(f"TG error: {e}")
        return {"ok": False, "reason": str(e)}

# MESSAGE FORMATTING
def _fmt_tf_label(tf: Any, tf_label: Optional[str]) -> str:
    return (tf_label or tf_to_label(tf) or "").strip()

def _fmt_side(side: Optional[str]) -> Dict[str, str]:
    s = (side or "").upper()
    if s == "LONG":
        return {"emoji": "📈", "label": "LONG"}
    if s == "SHORT":
        return {"emoji": "📉", "label": "SHORT"}
    return {"emoji": "📌", "label": (side or "Position").upper()}

def _calc_rr(entry: Optional[float], sl: Optional[float], tp1: Optional[float]) -> Optional[float]:
    try:
        if entry is None or sl is None or tp1 is None:
            return None
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        return round(reward / risk, 2) if risk > 0 else None
    except:
        return None

def format_vector_message(symbol: str, tf_label: str, direction: str, price: Any, note: Optional[str] = None) -> str:
    icon = settings.VECTOR_UP_ICON if (direction or "").upper() == "UP" else settings.VECTOR_DN_ICON
    note_text = f" — {note}" if note else ""
    return f"{icon} Vector Candle {direction.upper()} | <b>{symbol}</b> <i>{tf_label}</i> @ <code>{price}</code>{note_text}"

def compute_altseason_snapshot() -> dict:
    t24 = ms_ago(24*60)
    row = db_query("""
        SELECT SUM(CASE WHEN side='LONG' THEN 1 ELSE 0 END) AS long_n,
               SUM(CASE WHEN side='SHORT' THEN 1 ELSE 0 END) AS short_n
        FROM events
        WHERE type='ENTRY' AND time>=?
    """, (t24,))
    long_n = (row[0]["long_n"] if row else 0) or 0
    short_n = (row[0]["short_n"] if row else 0) or 0
    
    def _pct(x, y):
        try:
            return 0.0 if y == 0 else 100.0 * float(x or 0) / float(y or 0)
        except:
            return 0.0
    
    A = _pct(long_n, long_n + short_n)
    row = db_query("""
        WITH tp AS (SELECT COUNT(*) AS n FROM events WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?),
             sl AS (SELECT COUNT(*) AS n FROM events WHERE type='SL_HIT' AND time>=?)
        SELECT tp.n AS tp_n, sl.n AS sl_n FROM tp, sl
    """, (t24, t24))
    tp_n = (row[0]["tp_n"] if row else 0) or 0
    sl_n = (row[0]["sl_n"] if row else 0) or 0
    B = _pct(tp_n, tp_n + sl_n)
    
    symbols_with_tp = db_query("SELECT DISTINCT symbol FROM events WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=? ORDER BY symbol", (t24,))
    symbol_list = [r["symbol"] for r in symbols_with_tp]
    sym_gain = len(symbol_list)
    C = float(min(100.0, sym_gain * 2.0))
    
    t90 = ms_ago(90)
    row = db_query("""
        WITH w AS (SELECT SUM(CASE WHEN time>=? THEN 1 ELSE 0 END) AS recent_n,
                          COUNT(*) AS total_n
                   FROM events WHERE type='ENTRY' AND time>=?)
        SELECT recent_n, total_n FROM w
    """, (t90, t24))
    recent_n = (row[0]["recent_n"] if row else 0) or 0
    total_n = (row[0]["total_n"] if row else 0) or 0
    D = _pct(recent_n, total_n)
    
    score = round((A + B + C + D) / 4.0)
    label = "Altseason (forte)" if score >= 75 else ("Altseason (modérée)" if score >= 50 else "Marché neutre/faible")
    
    return {
        "score": int(score),
        "label": label,
        "window_minutes": 24*60,
        "disclaimer": "Score indicatif. Ne constitue pas un conseil.",
        "signals": {"long_ratio": round(A, 1), "tp_vs_sl": round(B, 1), "breadth_symbols": int(sym_gain), "recent_entries_ratio": round(D, 1)},
        "symbols_with_tp": symbol_list
    }

def build_confidence_line(payload: dict) -> str:
    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    rr = _calc_rr(entry, sl, tp1)
    alt = compute_altseason_snapshot()
    factors = []
    conf = payload.get("confidence")
    if conf is None:
        base = 50
        if rr:
            base += max(min((rr - 1.0) * 10, 20), -10)
            factors.append(f"R/R {rr}")
        momentum = alt["signals"]["recent_entries_ratio"]
        base += max(min((momentum - 50) * 0.3, 15), -15)
        factors.append(f"Momentum {momentum}%")
        breadth = alt["signals"]["breadth_symbols"]
        base += max(min((breadth - 10) * 0.7, 15), -10)
        factors.append(f"Breadth {breadth} sym")
        factors.append(f"Bias LONG {alt['signals']['long_ratio']}%")
        conf = int(max(5, min(95, round(base))))
        payload["confidence"] = conf
    else:
        if rr:
            factors.append(f"R/R {rr}")
        factors.append(f"Momentum {alt['signals']['recent_entries_ratio']}%")
        factors.append(f"Breadth {alt['signals']['breadth_symbols']} sym")
    return f"🧠 Confiance: {conf}% — {', '.join(factors)}"

def format_entry_announcement(payload: dict) -> str:
    symbol = payload.get("symbol", "")
    tf_lbl = _fmt_tf_label(payload.get("tf"), payload.get("tf_label"))
    side_i = _fmt_side(payload.get("side"))
    entry = payload.get("entry")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    tp3 = payload.get("tp3")
    sl = payload.get("sl")
    leverage = payload.get("leverage") or payload.get("lev_reco") or ""
    note = (payload.get("note") or "").strip()
    rr = _calc_rr(entry, sl, tp1)
    rr_text = f" (R/R: {rr:.2f})" if rr else ""
    
    lines = []
    if tp1:
        lines.append(f"🎯 TP1: {tp1}{rr_text}")
    if tp2:
        lines.append(f"🎯 TP2: {tp2}")
    if tp3:
        lines.append(f"🎯 TP3: {tp3}")
    if sl:
        lines.append(f"❌ SL: {sl}")
    
    conf_line = build_confidence_line(payload)
    tip_line = "💡 Astuce: après TP1, placez SL au BE." if tp1 else ""
    entry_text = f"<b>Entry: {entry}</b>" if entry else "Entry: N/A"
    
    msg = [
        "🚨 <b>NOUVELLE POSITION</b>",
        f"📊 {symbol} {tf_lbl}",
        f"{side_i['emoji']} {side_i['label']} | {entry_text}",
        f"⚡ Leverage: {leverage}" if leverage else "",
        "",
        *lines,
        "",
        conf_line,
        tip_line
    ]
    if note:
        msg.append(f"📝 {note}")
    return "\n".join([m for m in msg if m])

def format_event_announcement(etype: str, payload: dict, duration_ms: Optional[int]) -> str:
    symbol = payload.get("symbol", "")
    tf_lbl = _fmt_tf_label(payload.get("tf"), payload.get("tf_label"))
    side_i = _fmt_side(payload.get("side"))
    base = f"{symbol} {tf_lbl}"
    d_txt = f"⏱ Temps écoulé : {human_duration_verbose(duration_ms)}" if duration_ms and duration_ms > 0 else "⏱ Temps écoulé : N/A"
    
    if etype in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        tick = {"TP1_HIT": "TP1", "TP2_HIT": "TP2", "TP3_HIT": "TP3"}[etype]
        price = payload.get("price") or payload.get("tp1") or payload.get("tp2") or payload.get("tp3") or ""
        price_txt = f" @ {price}" if price else ""
        return f"✅ <b>{tick} ATTEINT</b>{price_txt}\n📊 {base}\n{side_i['emoji']} {side_i['label']}\n{d_txt}"
    
    if etype == "SL_HIT":
        price = payload.get("price") or payload.get("sl") or ""
        price_txt = f" @ {price}" if price else ""
        return f"🛑 <b>SL TOUCHÉ</b>{price_txt}\n📊 {base}\n{side_i['emoji']} {side_i['label']}\n{d_txt}"
    
    if etype == "CLOSE":
        note = payload.get("note") or ""
        x = f"📪 <b>TRADE CLÔTURÉ</b>\n📊 {base}\n{side_i['emoji']} {side_i['label']}"
        if note:
            x += f"\n📝 {note}"
        x += f"\n{d_txt}"
        return x
    
    return f"ℹ️ {etype} — {base}\n{d_txt}"

# TRADE ANALYSIS
def _latest_entry_for_trade(trade_id: str) -> Optional[dict]:
    r = db_query("SELECT * FROM events WHERE trade_id=? AND type='ENTRY' ORDER BY time DESC LIMIT 1", (trade_id,))
    return r[0] if r else None

def _has_hit_map(trade_id: str) -> Dict[str, bool]:
    hits = db_query("SELECT type FROM events WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSE') GROUP BY type", (trade_id,))
    return {h["type"]: True for h in hits}

def _first_outcome(trade_id: str) -> Optional[str]:
    rows = db_query("SELECT type FROM events WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT') ORDER BY time ASC LIMIT 1", (trade_id,))
    if not rows:
        return None
    t = rows[0]["type"]
    return "TP" if t in ('TP1_HIT', 'TP2_HIT', 'TP3_HIT') else ("SL" if t == "SL_HIT" else None)

def _cancelled_by_opposite(entry_row: dict) -> bool:
    symbol = entry_row.get("symbol")
    tf = entry_row.get("tf")
    side = (entry_row.get("side") or "").upper()
    t = int(entry_row.get("time") or 0)
    if not symbol or tf is None or side not in ("LONG", "SHORT"):
        return False
    opposite = "SHORT" if side == "LONG" else "LONG"
    r = db_query("SELECT 1 FROM events WHERE type='ENTRY' AND symbol=? AND tf=? AND time>? AND UPPER(COALESCE(side,''))=? LIMIT 1", (symbol, str(tf), t, opposite))
    return bool(r)

def build_trade_rows(limit=300, offset=0):
    base = db_query("SELECT e.trade_id, MAX(e.time) AS t_entry FROM events e WHERE e.type='ENTRY' GROUP BY e.trade_id ORDER BY t_entry DESC LIMIT ? OFFSET ?", (limit, offset))
    rows = []
    for item in base:
        e = _latest_entry_for_trade(item["trade_id"])
        if not e:
            continue
        tf_label = e.get("tf_label") or tf_to_label(e.get("tf"))
        hm = _has_hit_map(e["trade_id"])
        tp1_hit = bool(hm.get("TP1_HIT"))
        tp2_hit = bool(hm.get("TP2_HIT"))
        tp3_hit = bool(hm.get("TP3_HIT"))
        sl_hit = bool(hm.get("SL_HIT"))
        closed = bool(hm.get("CLOSE"))
        cancelled = _cancelled_by_opposite(e) and not (tp1_hit or tp2_hit or tp3_hit or sl_hit)
        
        tp_times = {}
        if tp1_hit or tp2_hit or tp3_hit:
            tp_events = db_query("""
                SELECT type, time FROM events 
                WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT')
                ORDER BY time ASC
            """, (e["trade_id"],))
            for tp_event in tp_events:
                tp_times[tp_event["type"]] = tp_event["time"]
        
        if sl_hit:
            state = "sl"
        elif tp1_hit or tp2_hit or tp3_hit:
            state = "tp"
        elif cancelled or closed:
            state = "cancel"
        else:
            state = "normal"
        
        rows.append({
            "trade_id": e["trade_id"],
            "symbol": e["symbol"],
            "tf": e.get("tf"),
            "tf_label": tf_label,
            "side": e["side"],
            "entry": e["entry"],
            "tp1": e["tp1"],
            "tp2": e["tp2"],
            "tp3": e["tp3"],
            "sl": e["sl"],
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
            "sl_hit": sl_hit,
            "tp1_time": tp_times.get("TP1_HIT"),
            "tp2_time": tp_times.get("TP2_HIT"),
            "tp3_time": tp_times.get("TP3_HIT"),
            "row_state": state,
            "t_entry": item["t_entry"]
        })
    return rows

def count_total_trades() -> int:
    result = db_query("SELECT COUNT(DISTINCT trade_id) AS total FROM events WHERE type='ENTRY'")
    return result[0]["total"] if result else 0

def compute_kpis(rows: List[dict]) -> Dict[str, Any]:
    t24 = ms_ago(24*60)
    total_trades = db_query("SELECT COUNT(DISTINCT trade_id) AS n FROM events WHERE type='ENTRY' AND time>=?", (t24,))[0]["n"] or 0
    tp_hits = db_query("SELECT COUNT(*) AS n FROM events WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?", (t24,))[0]["n"] or 0
    tp_details = db_query("SELECT DISTINCT symbol, type, time FROM events WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=? ORDER BY time DESC", (t24,))
    
    trade_ids = [r["trade_id"] for r in db_query("SELECT DISTINCT trade_id FROM events WHERE type='ENTRY' AND time>=?", (t24,))]
    wins = losses = 0
    for tid in trade_ids:
        o = _first_outcome(tid)
        if o == "TP":
            wins += 1
        elif o == "SL":
            losses += 1
    
    winrate = (wins / max(1, wins + losses)) * 100.0 if wins + losses > 0 else 0.0
    active = sum(1 for r in rows if r["row_state"] == "normal")
    cancelled = sum(1 for r in rows if r["row_state"] == "cancel")
    
    return {
        "total_trades": int(total_trades),
        "active_trades": int(active),
        "tp_hits": int(tp_hits),
        "tp_details": tp_details,
        "winrate": round(winrate, 1),
        "wins": wins,
        "losses": losses,
        "cancelled": cancelled,
        "total_closed": wins + losses
    }

def get_chart_data() -> dict:
    t30d = ms_ago(30*24*60)
    
    daily_data = db_query("""
        WITH RECURSIVE dates(d) AS (
            SELECT date('now', '-29 days')
            UNION ALL
            SELECT date(d, '+1 day') FROM dates WHERE d < date('now')
        ),
        daily_wins AS (
            SELECT date(time/1000, 'unixepoch') as day, COUNT(*) as wins
            FROM events 
            WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?
            GROUP BY day
        ),
        daily_losses AS (
            SELECT date(time/1000, 'unixepoch') as day, COUNT(*) as losses
            FROM events 
            WHERE type='SL_HIT' AND time>=?
            GROUP BY day
        )
        SELECT dates.d as date, 
               COALESCE(daily_wins.wins, 0) as wins,
               COALESCE(daily_losses.losses, 0) as losses
        FROM dates
        LEFT JOIN daily_wins ON dates.d = daily_wins.day
        LEFT JOIN daily_losses ON dates.d = daily_losses.day
        ORDER BY dates.d
    """, (t30d, t30d))
    
    top_cryptos = db_query("""
        SELECT symbol, COUNT(DISTINCT trade_id) as count
        FROM events
        WHERE type='ENTRY' AND time>=?
        GROUP BY symbol
        ORDER BY count DESC
        LIMIT 10
    """, (t30d,))
    
    return {
        "daily": daily_data,
        "top_cryptos": top_cryptos
    }

def get_entry_time_for_trade(trade_id: Optional[str]) -> Optional[int]:
    if not trade_id:
        return None
    r = db_query("SELECT MIN(time) AS t FROM events WHERE trade_id=? AND type='ENTRY'", (trade_id,))
    if r and r[0]["t"]:
        return int(r[0]["t"])
    return None

def save_event(payload: WebhookPayload) -> str:
    try:
        trade_id = payload.trade_id
        if not trade_id and payload.type and payload.symbol and payload.tf:
            t = payload.time or now_ms()
            trade_id = f"{payload.symbol}_{payload.tf}_{t}"
        
        db_execute("""
            INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, r1, s1, lev_reco, qty_reco, notional, confidence, horizon, leverage, note, price, direction, trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload.type,
            payload.symbol,
            str(payload.tf) if payload.tf else None,
            payload.tf_label or tf_to_label(payload.tf),
            int(payload.time or now_ms()),
            payload.side,
            payload.entry,
            payload.sl,
            payload.tp1,
            payload.tp2,
            payload.tp3,
            payload.r1,
            payload.s1,
            payload.lev_reco,
            payload.qty_reco,
            payload.notional,
            payload.confidence,
            payload.horizon,
            payload.leverage,
            payload.note,
            payload.price,
            payload.direction,
            trade_id
        ))
        logger.info(f"Event saved: {payload.type} {payload.symbol}")
        return trade_id
    except Exception as e:
        logger.error(f"Save failed: {e}")
        raise

# HTML COMPONENTS
def generate_sidebar_html(active_page: str, kpi: dict) -> str:
    new_trades_badge = f'<span class="new-badge" id="newTradesBadge" style="display:none">0</span>' if active_page == 'dashboard' else ''
    
    return f'''
    <aside class="sidebar" id="sidebar">
        <div class="sidebar-overlay" id="sidebarOverlay"></div>
        <div class="sidebar-content">
            <div class="logo">
                <div class="logo-icon">⚡</div>
                <div class="logo-text"><h2>AI Trader</h2><p>Professional</p></div>
            </div>
            <nav>
                <div class="nav-item {'active' if active_page == 'dashboard' else ''}" onclick="window.location.href='/trades'">
                    <span>📊</span><span>Dashboard</span>{new_trades_badge}
                </div>
                <div class="nav-item {'active' if active_page == 'positions' else ''}" onclick="window.location.href='/positions'">
                    <span>📈</span><span>Positions</span><span class="nav-badge">{kpi.get('active_trades', 0)}</span>
                </div>
                <div class="nav-item {'active' if active_page == 'history' else ''}" onclick="window.location.href='/history'">
                    <span>📜</span><span>Historique</span>
                </div>
                <div class="nav-item {'active' if active_page == 'analytics' else ''}" onclick="window.location.href='/analytics'">
                    <span>📊</span><span>Analytics</span>
                </div>
            </nav>
            <div class="ml-status">
                <div class="ml-status-header"><h4><span class="status-dot"></span> Performance</h4></div>
                <div class="ml-metric"><span class="label">Win Rate</span><span class="value">{kpi.get('winrate', 0)}%</span></div>
                <div class="ml-metric"><span class="label">Wins/Losses</span><span class="value">{kpi.get('wins', 0)}/{kpi.get('losses', 0)}</span></div>
                <div class="ml-metric"><span class="label">TP Atteints</span><span class="value">{kpi.get('tp_hits', 0)}</span></div>
            </div>
            <div class="user-profile">
                <div class="avatar">TP</div>
                <div class="user-info"><div class="name">Trader Pro</div><div class="status"><span class="status-dot"></span> En ligne</div></div>
                <div style="margin-left:auto">⚙️</div>
            </div>
        </div>
    </aside>
    <button class="menu-toggle" id="menuToggle" onclick="toggleSidebar()">☰</button>
    '''

def get_base_css() -> str:
    return """
:root{--bg:#050a12;--sidebar:#0a0f1a;--panel:rgba(15,23,38,0.8);--card:rgba(20,30,48,0.6);--border:rgba(99,102,241,0.12);--txt:#e2e8f0;--muted:#64748b;--accent:#6366f1;--accent2:#8b5cf6;--success:#10b981;--danger:#ef4444;--warning:#f59e0b;--info:#06b6d4;--purple:#a855f7;--glow:rgba(99,102,241,0.25)}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#050a12;color:var(--txt);font-family:'Inter',system-ui,sans-serif;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(circle at 15% 25%, rgba(99,102,241,0.08) 0%, transparent 45%),radial-gradient(circle at 85% 75%, rgba(139,92,246,0.06) 0%, transparent 45%);pointer-events:none}
.app{display:flex;min-height:100vh;position:relative;z-index:1}
.sidebar{width:300px;background:linear-gradient(180deg, rgba(10,15,26,0.98) 0%, rgba(10,15,26,0.95) 100%);backdrop-filter:blur(40px);border-right:1px solid var(--border);position:fixed;height:100vh;z-index:100;box-shadow:4px 0 40px rgba(0,0,0,0.5);transition:transform 0.3s}
.sidebar-content{padding:28px 20px;display:flex;flex-direction:column;height:100%}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:-1}
.logo{display:flex;align-items:center;gap:14px;margin-bottom:36px;padding-bottom:24px;border-bottom:1px solid var(--border)}
.logo-icon{width:48px;height:48px;background:linear-gradient(135deg, var(--accent), var(--purple));border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:28px;box-shadow:0 8px 32px var(--glow);position:relative}
.logo-icon::before{content:'';position:absolute;inset:-3px;background:inherit;border-radius:16px;filter:blur(16px);opacity:0.6;z-index:-1}
.logo-text h2{font-size:22px;font-weight:900;background:linear-gradient(135deg, var(--accent), var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.5px}
.logo-text p{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:1px}
.nav-item{display:flex;align-items:center;gap:14px;padding:13px 18px;border-radius:14px;color:var(--muted);cursor:pointer;transition:all 0.3s;margin-bottom:6px;font-size:14px;font-weight:600;position:relative}
.nav-item::before{content:'';position:absolute;left:0;top:0;width:3px;height:100%;background:var(--accent);transform:scaleY(0);transition:transform 0.3s}
.nav-item:hover, .nav-item.active{background:rgba(99,102,241,0.12);color:var(--accent);transform:translateX(6px)}
.nav-item.active::before{transform:scaleY(1)}
.nav-badge, .new-badge{margin-left:auto;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:800}
.nav-badge{background:rgba(239,68,68,0.15);color:var(--danger)}
.new-badge{background:var(--accent);color:white;animation:pulse 2s infinite}
.ml-status{background:linear-gradient(135deg, rgba(99,102,241,0.1), rgba(139,92,246,0.1));border:1px solid rgba(99,102,241,0.2);border-radius:14px;padding:16px;margin:20px 0}
.ml-status-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.ml-status-header h4{font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--success);box-shadow:0 0 12px var(--success);animation:pulse 2s infinite}
.ml-metric{display:flex;justify-content:space-between;font-size:12px;margin:8px 0}
.ml-metric .label{color:var(--muted)}
.ml-metric .value{font-weight:700;color:var(--success)}
.user-profile{margin-top:auto;padding-top:24px;border-top:1px solid var(--border);display:flex;align-items:center;gap:14px;padding:20px 16px;border-radius:14px;background:rgba(30,35,48,0.4);cursor:pointer;transition:all 0.3s}
.user-profile:hover{background:rgba(30,35,48,0.6);transform:translateY(-2px)}
.avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg, var(--accent), var(--purple));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;box-shadow:0 4px 16px var(--glow)}
.user-info{flex:1}
.user-info .name{font-size:14px;font-weight:700;margin-bottom:2px}
.user-info .status{font-size:11px;color:var(--success);display:flex;align-items:center;gap:6px}
.main{flex:1;margin-left:300px;padding:32px 40px;max-width:100%}
.panel{background:var(--card);backdrop-filter:blur(30px);border:1px solid var(--border);border-radius:20px;padding:32px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:10px;font-size:12px;font-weight:800;backdrop-filter:blur(10px)}
.badge-long{background:rgba(16,185,129,0.15);color:var(--success);border:1px solid rgba(16,185,129,0.35)}
.badge-short{background:rgba(239,68,68,0.15);color:var(--danger);border:1px solid rgba(239,68,68,0.35)}
.badge-tp{background:rgba(16,185,129,0.15);color:var(--success);border:1px solid rgba(16,185,129,0.35)}
.badge-pending{background:rgba(100,116,139,0.15);color:var(--muted);border:1px solid rgba(100,116,139,0.35)}
.badge-sl{background:rgba(239,68,68,0.15);color:var(--danger);border:1px solid rgba(239,68,68,0.35)}
.badge-cancel{background:rgba(100,116,139,0.15);color:var(--muted);border:1px solid rgba(100,116,139,0.35)}
.badge-tf{background:rgba(6,182,212,0.15);color:var(--info);border:1px solid rgba(6,182,212,0.35)}
table{width:100%;border-collapse:collapse;display:table}
thead{display:table-header-group}
tbody{display:table-row-group}
thead tr, tbody tr{display:grid;grid-template-columns:140px 120px 90px 100px 100px 100px 100px 100px 100px 100px 1fr;border-bottom:1px solid rgba(99,102,241,0.05)}
thead th, tbody td{padding:16px 12px;text-align:left;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center}
thead th{font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase;background:rgba(15,23,38,0.3);border-bottom:1px solid var(--border)}
tbody tr{cursor:pointer;transition:all 0.3s}
tbody tr:hover{background:rgba(99,102,241,0.08)}
.trade-row{position:relative}
.trade-row::before{content:'';position:absolute;left:0;top:0;width:4px;height:100%}
.trade-row.tp::before{background:var(--success);box-shadow:0 0 16px var(--success)}
.trade-row.sl::before{background:var(--danger);box-shadow:0 0 16px var(--danger)}
.trade-row.normal::before{background:var(--info);box-shadow:0 0 16px var(--info)}
.btn{padding:12px 24px;border:none;border-radius:12px;font-weight:700;cursor:pointer;transition:all 0.3s;font-size:14px}
.btn-danger{background:var(--danger);color:white}
.btn-danger:hover{background:#dc2626;transform:translateY(-2px);box-shadow:0 8px 24px rgba(239,68,68,0.4)}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
.chart-container{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:32px;margin-bottom:20px;min-height:300px}
.pagination{display:flex;justify-content:center;align-items:center;gap:10px;margin-top:20px}
.pagination button{padding:8px 16px;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--txt);cursor:pointer;transition:all 0.3s}
.pagination button:hover:not(:disabled){background:var(--accent);color:white}
.pagination button:disabled{opacity:0.5;cursor:not-allowed}
.pagination span{color:var(--muted);font-size:14px}
.error-toast{position:fixed;top:80px;right:20px;background:var(--danger);color:white;padding:16px 24px;border-radius:12px;box-shadow:0 8px 32px rgba(239,68,68,0.4);z-index:1000;animation:slideIn 0.3s;max-width:400px}
.error-toast.success{background:var(--success);box-shadow:0 8px 32px rgba(16,185,129,0.4)}
@keyframes slideIn{from{transform:translateX(400px);opacity:0}to{transform:translateX(0);opacity:1}}
.tooltip{position:relative;display:inline-block}
.tooltip .tooltiptext{visibility:hidden;background:var(--panel);color:var(--txt);text-align:left;border-radius:8px;padding:10px;position:absolute;z-index:1;bottom:125%;left:50%;margin-left:-100px;width:200px;opacity:0;transition:opacity 0.3s;border:1px solid var(--border);font-size:12px;line-height:1.4}
.tooltip:hover .tooltiptext{visibility:visible;opacity:1}
.menu-toggle{display:none;position:fixed;top:20px;left:20px;z-index:101;width:48px;height:48px;background:var(--accent);border:none;border-radius:12px;color:white;font-size:24px;cursor:pointer;box-shadow:0 4px 20px var(--glow)}
@media(max-width:1200px){
.sidebar{transform:translateX(-100%)}
.sidebar.open{transform:translateX(0)}
.sidebar.open .sidebar-overlay{display:block}
.main{margin-left:0;padding:24px}
.menu-toggle{display:flex;align-items:center;justify-content:center}
table{font-size:11px}
thead th, tbody td{padding:12px 8px}
thead tr, tbody tr{grid-template-columns:120px 100px 80px 90px 90px 90px 90px 90px 90px 90px 1fr}
}
"""

# FASTAPI APP
app = FastAPI(title="AI Trader Pro Enhanced", version="2.2")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": str(exc), "type": "server_error"})

@app.post("/api/reset-database")
async def reset_database(req: Request):
    try:
        payload = await req.json()
        if payload.get("secret") != settings.WEBHOOK_SECRET:
            raise HTTPException(403, "Secret invalide")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(settings.DB_DIR, f"backup_{timestamp}.db")
        try:
            shutil.copy2(settings.DB_PATH, backup)
            logger.info(f"Backup: {backup}")
        except Exception as e:
            logger.warning(f"Backup failed: {e}")
            backup = "Backup non créé"
        db_execute("DELETE FROM events")
        logger.warning("Database reset!")
        return {"ok": True, "message": "Reset OK", "backup": backup, "timestamp": timestamp}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/trades-data")
async def get_trades_data(page: int = 1, per_page: int = 50):
    try:
        offset = (page - 1) * per_page
        rows = build_trade_rows(limit=per_page, offset=offset)
        total = count_total_trades()
        total_pages = (total + per_page - 1) // per_page
        
        return {
            "ok": True,
            "trades": rows,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages
            }
        }
    except Exception as e:
        logger.error(f"Error getting trades: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/api/charts-data")
async def get_charts_data():
    try:
        data = get_chart_data()
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"Error getting chart data: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/api/export-csv")
async def export_csv():
    rows = build_trade_rows(limit=10000)
    csv = "symbol,tf,side,entry,exit,result,date\n"
    for r in rows:
        if r['row_state'] in ('tp', 'sl'):
            result = 'WIN' if r['row_state'] == 'tp' else 'LOSS'
            exit_price = r.get('tp1') if r.get('tp1_hit') else r.get('sl')
            date = datetime.fromtimestamp(r.get('t_entry', 0) / 1000).isoformat()
            csv += f"{r['symbol']},{r['tf_label']},{r['side']},{r['entry']},{exit_price},{result},{date}\n"
    return Response(content=csv, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("""<!doctype html><html><head><meta charset="utf-8"><title>AI Trader Pro</title>
<style>body{font-family:system-ui;padding:40px;background:#0b0f14;color:#e6edf3}h1{color:#6366f1}a{color:#8b5cf6}</style></head>
<body><h1>AI Trader Pro v2.2</h1><p>Système opérationnel</p><h2>Endpoints:</h2><ul>
<li><a href="/trades">📊 Dashboard</a></li><li><a href="/positions">📈 Positions</a></li>
<li><a href="/history">📜 Historique</a></li><li><a href="/analytics">📊 Analytics</a></li>
<li><a href="/health">🏥 Health</a></li><li><a href="/api/export-csv">📥 Export CSV</a></li></ul></body></html>""")

@app.get("/health")
async def health_check():
    try:
        db_query("SELECT 1")
        db_status = "ok"
        db_records = db_query("SELECT COUNT(*) as cnt FROM events")[0]["cnt"]
    except Exception as e:
        db_status = f"error: {e}"
        db_records = 0
    return {"status": "healthy" if db_status == "ok" else "degraded", "database": db_status, "total_events": db_records, "telegram": settings.TELEGRAM_ENABLED, "timestamp": datetime.now(timezone.utc).isoformat(), "version": "2.2"}

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload_dict = await req.json()
    except Exception as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    
    secret = payload_dict.get("secret")
    if settings.WEBHOOK_SECRET and secret != settings.WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    
    try:
        payload = WebhookPayload(**payload_dict)
    except Exception as e:
        raise HTTPException(422, f"Validation error: {e}")
    
    trade_id = save_event(payload)
    
    try:
        if settings.TELEGRAM_ENABLED:
            key = payload.trade_id or f"{payload.type}:{payload.symbol}"
            reply_markup = _create_dashboard_button()
            
            if payload.type == "VECTOR_CANDLE":
                global _last_vector_flush_ts
                now_sec = time.time()
                if now_sec - _last_vector_flush_ts >= settings.VECTOR_GLOBAL_GAP_SEC:
                    _last_vector_flush_ts = now_sec
                    txt = format_vector_message(payload.symbol, payload.tf_label or tf_to_label(payload.tf), payload.direction or "", payload.price, payload.note)
                    await tg_send_text(txt, key=key, reply_markup=reply_markup)
            
            elif payload.type == "ENTRY":
                txt = format_entry_announcement(payload.dict())
                await tg_send_text(txt, key=key, reply_markup=reply_markup)
            
            elif payload.type in {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"}:
                hit_time = payload.time or now_ms()
                entry_t = get_entry_time_for_trade(payload.trade_id)
                
                if not entry_t and payload.symbol and payload.tf:
                    symbol = payload.symbol
                    tf = str(payload.tf)
                    side = payload.side
                    query = """
                        SELECT time FROM events
                        WHERE symbol=? AND tf=? AND type='ENTRY'
                    """
                    params = [symbol, tf]
                    if side:
                        query += " AND side=?"
                        params.append(side)
                    query += " ORDER BY time DESC LIMIT 1"
                    r = db_query(query, tuple(params))
                    
                    if r and r[0].get("time"):
                        entry_t = int(r[0]["time"])
                        logger.info(f"Found ENTRY by symbol+tf+side: {entry_t}")
                    else:
                        r = db_query("""
                            SELECT time FROM events
                            WHERE symbol=? AND tf=? AND type='ENTRY'
                            ORDER BY time DESC LIMIT 1
                        """, (symbol, tf))
                        if r and r[0].get("time"):
                            entry_t = int(r[0]["time"])
                            logger.info(f"Found ENTRY by symbol+tf: {entry_t}")
                        else:
                            symbol_variants = [
                                symbol,
                                symbol.replace('.P', ''),
                                symbol.replace('.PERP', ''),
                                symbol + '.P',
                                symbol + '.PERP'
                            ]
                            for sym_var in symbol_variants:
                                r = db_query("""
                                    SELECT time FROM events
                                    WHERE symbol=? AND tf=? AND type='ENTRY'
                                    ORDER BY time DESC LIMIT 1
                                """, (sym_var, tf))
                                if r and r[0].get("time"):
                                    entry_t = int(r[0]["time"])
                                    logger.info(f"Found ENTRY with symbol variant '{sym_var}': {entry_t}")
                                    break
                    
                    if not entry_t:
                        logger.error(f"NO ENTRY FOUND for {symbol} tf={tf} side={side}")
                
                duration = (hit_time - entry_t) if entry_t else None
                txt = format_event_announcement(payload.type, payload.dict(), duration)
                await tg_send_text(txt, key=key, reply_markup=reply_markup)
            
            await maybe_altseason_autonotify()
    except Exception as e:
        logger.warning(f"TG skip: {e}")
    
    return JSONResponse({"ok": True, "trade_id": trade_id})

async def maybe_altseason_autonotify():
    global _last_altseason_notify_ts
    if not settings.ALTSEASON_AUTONOTIFY or not settings.TELEGRAM_ENABLED:
        return
    
    alt = compute_altseason_snapshot()
    greens = alt["signals"]["breadth_symbols"]
    nowt = time.time()
    
    if greens < settings.ALT_GREENS_REQUIRED or alt["score"] < 50:
        return
    
    if (nowt - _last_altseason_notify_ts) < (settings.ALTSEASON_NOTIFY_MIN_GAP_MIN * 60):
        return
    
    emoji = "🟢" if alt["score"] >= 75 else "🟡"
    symbols_list = ", ".join(alt["symbols_with_tp"][:15])
    if len(alt["symbols_with_tp"]) > 15:
        symbols_list += f" +{len(alt['symbols_with_tp'])-15} autres"
    
    msg = f"""🚨 <b>Alerte Altseason</b> {emoji}

📊 Score: <b>{alt['score']}/100</b>
📈 Status: <b>{alt['label']}</b>

🔥 Signaux:
- LONG: {alt['signals']['long_ratio']}%
- TP/SL: {alt['signals']['tp_vs_sl']}%
- Breadth: {alt['signals']['breadth_symbols']} sym
- Momentum: {alt['signals']['recent_entries_ratio']}%

⚡ <b>{greens} symboles</b> avec TP:
{symbols_list}

<i>{alt['disclaimer']}</i>"""
    
    reply_markup = _create_dashboard_button()
    res = await tg_send_text(msg, key="altseason", reply_markup=reply_markup, pin=True)
    if res.get("ok"):
        _last_altseason_notify_ts = nowt

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = build_trade_rows(limit=50)
    kpi = compute_kpis(rows)
    alt = compute_altseason_snapshot()
    total_trades = count_total_trades()
    
    table_rows = ""
    for idx, r in enumerate(rows, start=1):
        state_class = r["row_state"]
        side_badge = f'<span class="badge badge-{r["side"].lower() if r["side"] else "pending"}">{r["side"] or "N/A"}</span>'
        tf_badge = f'<span class="badge badge-tf">{r["tf_label"]}</span>'
        
        status_html = ""
        if r["tp1_hit"]:
            status_html += '<span class="badge badge-tp">TP1 ✓</span> '
        if r["tp2_hit"]:
            status_html += '<span class="badge badge-tp">TP2 ✓</span> '
        if r["tp3_hit"]:
            status_html += '<span class="badge badge-tp">TP3 ✓</span> '
        if r["sl_hit"]:
            status_html += '<span class="badge badge-sl">SL ✗</span>'
        if not status_html:
            status_html = '<span class="badge badge-pending">En cours</span>'
        
        entry_val = f"{r['entry']:.4f}" if r["entry"] else "N/A"
        
        if r["tp1"]:
            if r["tp1_hit"]:
                tp1_time = datetime.fromtimestamp(r.get("tp1_time", 0) / 1000).strftime("%H:%M:%S") if r.get("tp1_time") else ""
                tooltip = f' title="Atteint à {tp1_time}"' if tp1_time else ''
                tp1_val = f'<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"{tooltip}>{r["tp1"]:.4f} ✓</span>'
            else:
                tp1_val = f'<span style="opacity:0.6">{r["tp1"]:.4f}</span>'
        else:
            tp1_val = "N/A"
            
        if r["tp2"]:
            if r["tp2_hit"]:
                tp2_time = datetime.fromtimestamp(r.get("tp2_time", 0) / 1000).strftime("%H:%M:%S") if r.get("tp2_time") else ""
                tooltip = f' title="Atteint à {tp2_time}"' if tp2_time else ''
                tp2_val = f'<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"{tooltip}>{r["tp2"]:.4f} ✓</span>'
            else:
                tp2_val = f'<span style="opacity:0.6">{r["tp2"]:.4f}</span>'
        else:
            tp2_val = "N/A"
            
        if r["tp3"]:
            if r["tp3_hit"]:
                tp3_time = datetime.fromtimestamp(r.get("tp3_time", 0) / 1000).strftime("%H:%M:%S") if r.get("tp3_time") else ""
                tooltip = f' title="Atteint à {tp3_time}"' if tp3_time else ''
                tp3_val = f'<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"{tooltip}>{r["tp3"]:.4f} ✓</span>'
            else:
                tp3_val = f'<span style="opacity:0.6">{r["tp3"]:.4f}</span>'
        else:
            tp3_val = "N/A"
        
        sl_val = f"{r['sl']:.4f}" if r["sl"] else "N/A"
        
        pl_html = "N/A"
        if r["entry"] and r["row_state"] in ("tp", "sl"):
            try:
                entry_price = float(r["entry"])
                if r["sl_hit"] and r["sl"]:
                    exit_price = float(r["sl"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    pl_color = "var(--danger)"
                    pl_html = f'<span style="color:{pl_color};font-weight:700">{pl_pct:.2f}%</span>'
                elif r["tp1_hit"] and r["tp1"]:
                    exit_price = float(r["tp1"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    pl_color = "var(--success)"
                    pl_html = f'<span style="color:{pl_color};font-weight:700">+{pl_pct:.2f}%</span>'
            except:
                pass
        elif r["row_state"] == "normal" and r["entry"]:
            try:
                entry_price = float(r["entry"])
                current_price = entry_price
                if r["tp1"]:
                    current_price = (float(r["tp1"]) + entry_price) / 2
                pl_pct = ((current_price - entry_price) / entry_price) * 100
                if r["side"] == "SHORT":
                    pl_pct = -pl_pct
                pl_color = "var(--success)" if pl_pct >= 0 else "var(--danger)"
                pl_sign = "+" if pl_pct >= 0 else ""
                pl_html = f'<span style="color:{pl_color};font-weight:700;opacity:0.7" class="tooltip" title="P&L estimé en cours">{pl_sign}{pl_pct:.2f}%*</span>'
            except:
                pass
        
        date_str = datetime.fromtimestamp(r["t_entry"] / 1000).strftime("%Y-%m-%d %H:%M") if r.get("t_entry") else "N/A"
        
        table_rows += f'''
        <tr class="trade-row {state_class}" data-symbol="{r["symbol"]}" data-side="{r["side"]}" data-tf="{r["tf_label"]}" data-date="{r.get('t_entry', 0)}">
            <td>{date_str}</td>
            <td><strong>{r["symbol"]}</strong></td>
            <td>{tf_badge}</td>
            <td>{side_badge}</td>
            <td><strong style="color:var(--info)">{entry_val}</strong></td>
            <td>{tp1_val}</td>
            <td>{tp2_val}</td>
            <td>{tp3_val}</td>
            <td>{sl_val}</td>
            <td>{pl_html}</td>
            <td>{status_html}</td>
        </tr>'''
    
    html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - AI Trader Pro</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>{get_base_css()}
.search-bar{{padding:12px 20px;border-radius:12px;border:1px solid var(--border);background:var(--card);color:var(--txt);font-size:14px;width:100%;max-width:400px;margin-bottom:20px}}
.search-bar:focus{{outline:none;border-color:var(--accent)}}
.theme-toggle{{position:fixed;bottom:20px;right:20px;width:50px;height:50px;border-radius:50%;background:var(--accent);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:24px;box-shadow:0 4px 20px var(--glow);z-index:999;transition:all 0.3s}}
.theme-toggle:hover{{transform:scale(1.1)}}
body.light-mode{{--bg:#f0f4f8;--sidebar:#ffffff;--panel:rgba(255,255,255,0.9);--card:rgba(255,255,255,0.8);--border:rgba(99,102,241,0.2);--txt:#1a202c;--muted:#64748b}}
.sortable{{cursor:pointer;user-select:none;position:relative}}
.sortable:hover{{color:var(--accent)}}
.sortable::after{{content:'⇅';margin-left:8px;opacity:0.3}}
.sortable.asc::after{{content:'↑';opacity:1}}
.sortable.desc::after{{content:'↓';opacity:1}}
    </style>
</head>
<body>
    <button class="theme-toggle" onclick="toggleTheme()" title="Changer le thème">🌓</button>
    <div class="app">
        {generate_sidebar_html("dashboard", kpi)}
        <main class="main">
            <header style="margin-bottom:32px">
                <h1 style="font-size:36px;font-weight:900;margin-bottom:8px;background:linear-gradient(135deg,var(--accent),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Dashboard Trading</h1>
                <p style="color:var(--muted)">Vue d'ensemble de vos positions et performances</p>
            </header>
            
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:32px">
                <div class="panel tooltip" style="background:linear-gradient(135deg,rgba(16,185,129,0.1),rgba(6,182,212,0.1))">
                    <div style="font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:700">TRADES (24H)</div>
                    <div style="font-size:32px;font-weight:900;color:var(--success)">{kpi['total_trades']}</div>
                    <span class="tooltiptext">Nombre total de trades ouverts dans les dernières 24 heures</span>
                </div>
                <div class="panel tooltip" style="background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1))">
                    <div style="font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:700">POSITIONS ACTIVES</div>
                    <div style="font-size:32px;font-weight:900;color:var(--accent)">{kpi['active_trades']}</div>
                    <span class="tooltiptext">Positions ouvertes en attente de TP ou SL</span>
                </div>
                <div class="panel tooltip" style="background:linear-gradient(135deg,rgba(245,158,11,0.1),rgba(251,191,36,0.1))">
                    <div style="font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:700">TP ATTEINTS</div>
                    <div style="font-size:32px;font-weight:900;color:var(--warning)">{kpi['tp_hits']}</div>
                    <span class="tooltiptext">Nombre de Take Profit atteints (TP1, TP2, TP3)</span>
                </div>
                <div class="panel tooltip" style="background:linear-gradient(135deg,rgba(168,85,247,0.1),rgba(217,70,239,0.1))">
                    <div style="font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:700">WIN RATE</div>
                    <div style="font-size:32px;font-weight:900;color:var(--purple)">{kpi['winrate']}%</div>
                    <span class="tooltiptext">Pourcentage de trades gagnants (TP) vs perdants (SL)</span>
                </div>
            </div>

            <div class="panel tooltip" style="margin-bottom:24px">
                <h2 style="font-size:20px;font-weight:800;margin-bottom:16px">🌊 Altseason Index</h2>
                <div style="display:flex;align-items:center;gap:24px;margin-bottom:16px">
                    <div style="flex:1">
                        <div style="font-size:48px;font-weight:900;color:var(--accent)">{alt['score']}/100</div>
                        <div style="color:var(--muted);font-size:14px">{alt['label']}</div>
                    </div>
                    <div style="flex:2">
                        <div style="background:rgba(100,116,139,0.1);height:20px;border-radius:10px;overflow:hidden">
                            <div style="height:100%;background:linear-gradient(90deg,var(--success),var(--accent));width:{alt['score']}%;transition:width 0.3s"></div>
                        </div>
                    </div>
                </div>
                <div style="font-size:12px;color:var(--muted);font-style:italic">{alt['disclaimer']}</div>
                <span class="tooltiptext">Score composite basé sur: ratio LONG/SHORT, ratio TP/SL, nombre de cryptos en gain, et momentum d'entrées récentes</span>
            </div>

            <div class="chart-container">
                <h3 style="margin-bottom:20px;font-weight:800">📊 Performance (30 derniers jours)</h3>
                <canvas id="performanceChart"></canvas>
            </div>

            <div class="panel">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:15px">
                    <div>
                        <h2 style="font-size:20px;font-weight:800">📊 Tous les Trades</h2>
                        <div style="font-size:12px;color:var(--muted);margin-top:8px">
                            <span style="color:var(--success);font-weight:600">✓ = TP atteint</span> • 
                            <span style="opacity:0.6">Prix grisé = Non atteint</span> • 
                            <span style="cursor:help" title="Survolez un TP atteint pour voir l'heure">Survolez pour l'heure</span>
                        </div>
                    </div>
                    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                        <button onclick="resetDatabase()" class="btn btn-danger" style="display:flex;align-items:center;gap:8px">
                            🗑️ Reset Database
                        </button>
                        <select id="filterSelect" style="padding:10px 16px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--txt);font-size:14px;cursor:pointer">
                            <option value="all">Tous les trades ({total_trades})</option>
                            <option value="tp">Avec TP atteints</option>
                            <option value="sl">Avec SL touchés</option>
                            <option value="active">En cours ({kpi['active_trades']})</option>
                        </select>
                        <input type="text" id="searchInput" class="search-bar" placeholder="🔍 Rechercher une crypto...">
                    </div>
                </div>
                <div style="overflow-x:auto">
                    <table id="tradesTable">
                        <thead>
                            <tr>
                                <th class="sortable tooltip" data-column="date">Date<span class="tooltiptext">Date et heure d'ouverture du trade</span></th>
                                <th class="sortable tooltip" data-column="crypto">Crypto<span class="tooltiptext">Symbole de la cryptomonnaie</span></th>
                                <th class="sortable tooltip" data-column="tf">TimeFrame<span class="tooltiptext">Unité de temps du graphique (15m, 1h, 4h, etc.)</span></th>
                                <th class="sortable tooltip" data-column="side">Status<span class="tooltiptext">Direction du trade (LONG = achat, SHORT = vente)</span></th>
                                <th class="tooltip">Entry<span class="tooltiptext">Prix d'entrée du trade</span></th>
                                <th class="tooltip">TP1<span class="tooltiptext">Premier objectif de profit (Take Profit 1)</span></th>
                                <th class="tooltip">TP2<span class="tooltiptext">Deuxième objectif de profit (Take Profit 2)</span></th>
                                <th class="tooltip">TP3<span class="tooltiptext">Troisième objectif de profit (Take Profit 3)</span></th>
                                <th class="tooltip">SL<span class="tooltiptext">Stop Loss - Prix de protection contre les pertes</span></th>
                                <th class="sortable tooltip" data-column="pl">P&L<span class="tooltiptext">Profit & Loss - Gain ou perte en pourcentage. * = estimation en cours</span></th>
                                <th class="tooltip">Validation<span class="tooltiptext">État actuel du trade (TP atteint, SL touché, ou en cours)</span></th>
                            </tr>
                        </thead>
                        <tbody>{table_rows}</tbody>
                    </table>
                </div>
                <div class="pagination">
                    <button onclick="changePage(1)" id="firstPage">⏮ Premier</button>
                    <button onclick="changePage(currentPage - 1)" id="prevPage">◀ Précédent</button>
                    <span id="pageInfo">Page <span id="currentPageNum">1</span> sur <span id="totalPages">1</span></span>
                    <button onclick="changePage(currentPage + 1)" id="nextPage">Suivant ▶</button>
                    <button onclick="changePage(totalPagesCount)" id="lastPage">Dernier ⏭</button>
                </div>
            </div>
        </main>
    </div>

    <script>
    let sortColumn = '';
    let sortDirection = 'asc';
    let currentPage = 1;
    let totalPagesCount = Math.ceil({total_trades} / 50);
    let lastTradeCount = {total_trades};

    function toggleSidebar() {{
        document.getElementById('sidebar').classList.toggle('open');
    }}

    document.getElementById('sidebarOverlay')?.addEventListener('click', toggleSidebar);

    async function changePage(page) {{
        if (page < 1 || page > totalPagesCount) return;
        currentPage = page;
        
        try {{
            const response = await fetch(`/api/trades-data?page=${{page}}&per_page=50`);
            const data = await response.json();
            
            if (!data.ok) throw new Error(data.error);
            
            updateTable(data.trades);
            updatePagination(data.pagination);
        }} catch (error) {{
            showError('Erreur de chargement des trades: ' + error.message);
        }}
    }}

    function updateTable(trades) {{
        const tbody = document.querySelector('#tradesTable tbody');
        tbody.innerHTML = trades.map((r, idx) => {{
            const state_class = r.row_state;
            const side_badge = `<span class="badge badge-${{r.side ? r.side.toLowerCase() : 'pending'}}">${{r.side || 'N/A'}}</span>`;
            const tf_badge = `<span class="badge badge-tf">${{r.tf_label}}</span>`;
            
            let status_html = "";
            if (r.tp1_hit) status_html += '<span class="badge badge-tp">TP1 ✓</span> ';
            if (r.tp2_hit) status_html += '<span class="badge badge-tp">TP2 ✓</span> ';
            if (r.tp3_hit) status_html += '<span class="badge badge-tp">TP3 ✓</span> ';
            if (r.sl_hit) status_html += '<span class="badge badge-sl">SL ✗</span>';
            if (!status_html) status_html = '<span class="badge badge-pending">En cours</span>';
            
            const entry_val = r.entry ? r.entry.toFixed(4) : "N/A";
            
            let tp1_val = "N/A";
            if (r.tp1) {{
                if (r.tp1_hit) {{
                    const tp1_time = r.tp1_time ? new Date(r.tp1_time).toLocaleTimeString('fr-FR') : '';
                    const tooltip = tp1_time ? ` title="Atteint à ${{tp1_time}}"` : '';
                    tp1_val = `<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"${{tooltip}}>${{r.tp1.toFixed(4)}} ✓</span>`;
                }} else {{
                    tp1_val = `<span style="opacity:0.6">${{r.tp1.toFixed(4)}}</span>`;
                }}
            }}
            
            let tp2_val = "N/A";
            if (r.tp2) {{
                if (r.tp2_hit) {{
                    const tp2_time = r.tp2_time ? new Date(r.tp2_time).toLocaleTimeString('fr-FR') : '';
                    const tooltip = tp2_time ? ` title="Atteint à ${{tp2_time}}"` : '';
                    tp2_val = `<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"${{tooltip}}>${{r.tp2.toFixed(4)}} ✓</span>`;
                }} else {{
                    tp2_val = `<span style="opacity:0.6">${{r.tp2.toFixed(4)}}</span>`;
                }}
            }}
            
            let tp3_val = "N/A";
            if (r.tp3) {{
                if (r.tp3_hit) {{
                    const tp3_time = r.tp3_time ? new Date(r.tp3_time).toLocaleTimeString('fr-FR') : '';
                    const tooltip = tp3_time ? ` title="Atteint à ${{tp3_time}}"` : '';
                    tp3_val = `<span style="color:var(--success);font-weight:900;background:rgba(16,185,129,0.1);padding:4px 8px;border-radius:6px;cursor:help" class="tp-hit"${{tooltip}}>${{r.tp3.toFixed(4)}} ✓</span>`;
                }} else {{
                    tp3_val = `<span style="opacity:0.6">${{r.tp3.toFixed(4)}}</span>`;
                }}
            }}
            
            const sl_val = r.sl ? r.sl.toFixed(4) : "N/A";
            
            let pl_html = "N/A";
            if (r.entry && ['tp', 'sl'].includes(r.row_state)) {{
                const entry_price = r.entry;
                if (r.sl_hit && r.sl) {{
                    let pl_pct = ((r.sl - entry_price) / entry_price) * 100;
                    if (r.side === "SHORT") pl_pct = -pl_pct;
                    pl_html = `<span style="color:var(--danger);font-weight:700">${{pl_pct.toFixed(2)}}%</span>`;
                }} else if (r.tp1_hit && r.tp1) {{
                    let pl_pct = ((r.tp1 - entry_price) / entry_price) * 100;
                    if (r.side === "SHORT") pl_pct = -pl_pct;
                    pl_html = `<span style="color:var(--success);font-weight:700">+${{pl_pct.toFixed(2)}}%</span>`;
                }}
            }}
            
            const date_str = new Date(r.t_entry).toLocaleString('fr-FR');
            
            return `<tr class="trade-row ${{state_class}}" data-symbol="${{r.symbol}}" data-side="${{r.side}}" data-tf="${{r.tf_label}}" data-date="${{r.t_entry}}">
                <td>${{date_str}}</td>
                <td><strong>${{r.symbol}}</strong></td>
                <td>${{tf_badge}}</td>
                <td>${{side_badge}}</td>
                <td><strong style="color:var(--info)">${{entry_val}}</strong></td>
                <td>${{tp1_val}}</td>
                <td>${{tp2_val}}</td>
                <td>${{tp3_val}}</td>
                <td>${{sl_val}}</td>
                <td>${{pl_html}}</td>
                <td>${{status_html}}</td>
            </tr>`;
        }}).join('');
    }}

    function updatePagination(pagination) {{
        currentPage = pagination.page;
        totalPagesCount = pagination.total_pages;
        
        document.getElementById('currentPageNum').textContent = pagination.page;
        document.getElementById('totalPages').textContent = pagination.total_pages;
        
        document.getElementById('firstPage').disabled = pagination.page === 1;
        document.getElementById('prevPage').disabled = pagination.page === 1;
        document.getElementById('nextPage').disabled = pagination.page === pagination.total_pages;
        document.getElementById('lastPage').disabled = pagination.page === pagination.total_pages;
    }}

    function showError(message) {{
        const toast = document.createElement('div');
        toast.className = 'error-toast';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 5000);
    }}

    function showSuccess(message) {{
        const toast = document.createElement('div');
        toast.className = 'error-toast success';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 3000);
    }}

    async function resetDatabase() {{
        if (!confirm('⚠️ ATTENTION : Cela va supprimer TOUS les trades. Une sauvegarde sera créée. Continuer ?')) {{
            return;
        }}
        
        const secret = prompt('Entrez votre webhook secret pour confirmer :');
        if (!secret) return;
        
        try {{
            const response = await fetch('/api/reset-database', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{secret: secret}})
            }});
            
            const data = await response.json();
            
            if (data.ok) {{
                showSuccess('✅ Base réinitialisée ! Backup: ' + data.backup);
                setTimeout(() => location.reload(), 2000);
            }} else {{
                showError('❌ Erreur: ' + (data.error || 'Secret invalide'));
            }}
        }} catch (error) {{
            showError('❌ Erreur: ' + error.message);
        }}
    }}

    document.querySelectorAll('.sortable').forEach(header => {{
        header.addEventListener('click', function() {{
            const column = this.dataset.column;
            const tbody = document.querySelector('#tradesTable tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            
            if (sortColumn === column) {{
                sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
            }} else {{
                sortColumn = column;
                sortDirection = 'asc';
            }}
            
            document.querySelectorAll('.sortable').forEach(h => h.className = 'sortable tooltip');
            this.className = 'sortable tooltip ' + sortDirection;
            
            rows.sort((a, b) => {{
                let aVal, bVal;
                if (column === 'date') {{
                    aVal = parseInt(a.dataset.date);
                    bVal = parseInt(b.dataset.date);
                }} else if (column === 'crypto') {{
                    aVal = a.dataset.symbol;
                    bVal = b.dataset.symbol;
                }} else if (column === 'tf') {{
                    aVal = a.dataset.tf;
                    bVal = b.dataset.tf;
                }} else if (column === 'side') {{
                    aVal = a.dataset.side;
                    bVal = b.dataset.side;
                }} else if (column === 'pl') {{
                    aVal = parseFloat(a.cells[9].textContent.replace('%', '').replace('+', '').replace('*', '')) || 0;
                    bVal = parseFloat(b.cells[9].textContent.replace('%', '').replace('+', '').replace('*', '')) || 0;
                }}
                
                if (sortDirection === 'asc') {{
                    return aVal > bVal ? 1 : -1;
                }} else {{
                    return aVal < bVal ? 1 : -1;
                }}
            }});
            
            rows.forEach(row => tbody.appendChild(row));
        }});
    }});

    document.getElementById('searchInput').addEventListener('input', function(e) {{
        const searchTerm = e.target.value.toLowerCase();
        const rows = document.querySelectorAll('#tradesTable tbody tr');
        
        rows.forEach(row => {{
            const symbol = row.dataset.symbol.toLowerCase();
            if (symbol.includes(searchTerm)) {{
                row.style.display = '';
            }} else {{
                row.style.display = 'none';
            }}
        }});
    }});

    document.getElementById('filterSelect').addEventListener('change', function(e) {{
        const filterValue = e.target.value;
        const rows = document.querySelectorAll('#tradesTable tbody tr');
        
        rows.forEach(row => {{
            const rowClass = row.className;
            let shouldShow = true;
            
            if (filterValue === 'tp') {{
                shouldShow = rowClass.includes('trade-row tp');
            }} else if (filterValue === 'sl') {{
                shouldShow = rowClass.includes('trade-row sl');
            }} else if (filterValue === 'active') {{
                shouldShow = rowClass.includes('trade-row normal');
            }}
            
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const symbol = row.dataset.symbol.toLowerCase();
            
            if (shouldShow && (!searchTerm || symbol.includes(searchTerm))) {{
                row.style.display = '';
            }} else {{
                row.style.display = 'none';
            }}
        }});
    }});

    function toggleTheme() {{
        document.body.classList.toggle('light-mode');
        localStorage.setItem('theme', document.body.classList.contains('light-mode') ? 'light' : 'dark');
    }}

    if (localStorage.getItem('theme') === 'light') {{
        document.body.classList.add('light-mode');
    }}

    function updateFilterCounts() {{
        const rows = document.querySelectorAll('#tradesTable tbody tr');
        let tpCount = 0;
        let slCount = 0;
        let activeCount = 0;
        
        rows.forEach(row => {{
            const rowClass = row.className;
            if (rowClass.includes('trade-row tp')) tpCount++;
            if (rowClass.includes('trade-row sl')) slCount++;
            if (rowClass.includes('trade-row normal')) activeCount++;
        }});
        
        const filterSelect = document.getElementById('filterSelect');
        filterSelect.innerHTML = `
            <option value="all">Tous les trades (${{rows.length}})</option>
            <option value="tp">✅ Avec TP atteints (${{tpCount}})</option>
            <option value="sl">🛑 Avec SL touchés (${{slCount}})</option>
            <option value="active">⏳ En cours (${{activeCount}})</option>
        `;
    }}
    
    updateFilterCounts();

    async function loadCharts() {{
        try {{
            const response = await fetch('/api/charts-data');
            const result = await response.json();
            
            if (!result.ok) throw new Error(result.error);
            
            const data = result.data;
            const ctx = document.getElementById('performanceChart').getContext('2d');
            
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: data.daily.map(d => d.date),
                    datasets: [
                        {{
                            label: 'Wins',
                            data: data.daily.map(d => d.wins),
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.1)',
                            tension: 0.4
                        }},
                        {{
                            label: 'Losses',
                            data: data.daily.map(d => d.losses),
                            borderColor: '#ef4444',
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            tension: 0.4
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {{
                        legend: {{
                            labels: {{ color: '#e2e8f0' }}
                        }}
                    }},
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            ticks: {{ color: '#64748b' }},
                            grid: {{ color: 'rgba(99, 102, 241, 0.1)' }}
                        }},
                        x: {{
                            ticks: {{ color: '#64748b' }},
                            grid: {{ color: 'rgba(99, 102, 241, 0.1)' }}
                        }}
                    }}
                }}
            }});
        }} catch (error) {{
            console.error('Erreur chargement graphiques:', error);
            showError('Impossible de charger les graphiques');
        }}
    }}

    loadCharts();

    setInterval(async function() {{
        try {{
            const response = await fetch('/api/trades-data?page=1&per_page=1');
            const data = await response.json();
            
            if (data.ok && data.pagination.total > lastTradeCount) {{
                const newCount = data.pagination.total - lastTradeCount;
                lastTradeCount = data.pagination.total;
                
                const badge = document.getElementById('newTradesBadge');
                if (badge) {{
                    badge.textContent = newCount;
                    badge.style.display = 'inline-flex';
                }}
                
                showSuccess(`${{newCount}} nouveau(x) trade(s) !`);
                
                if (currentPage === 1) {{
                    changePage(1);
                }}
            }}
        }} catch (e) {{
            console.log('Vérification des nouveaux trades échouée:', e);
        }}
    }}, 30000);
    </script>
</body>
</html>'''
    return HTMLResponse(html)

@app.get("/positions", response_class=HTMLResponse)
async def positions_page():
    rows = [r for r in build_trade_rows(limit=1000) if r["row_state"] == "normal"]
    kpi = compute_kpis(rows)
    
    table_rows = ""
    for idx, r in enumerate(rows, start=1):
        side_badge = f'<span class="badge badge-{r["side"].lower() if r["side"] else "pending"}">{r["side"] or "N/A"}</span>'
        tf_badge = f'<span class="badge badge-tf">{r["tf_label"]}</span>'
        
        entry_val = f"{r['entry']:.4f}" if r["entry"] else "N/A"
        tp1_val = f"{r['tp1']:.4f}" if r["tp1"] else "N/A"
        tp2_val = f"{r['tp2']:.4f}" if r["tp2"] else "N/A"
        tp3_val = f"{r['tp3']:.4f}" if r["tp3"] else "N/A"
        sl_val = f"{r['sl']:.4f}" if r["sl"] else "N/A"
        
        date_str = datetime.fromtimestamp(r["t_entry"] / 1000).strftime("%Y-%m-%d %H:%M") if r.get("t_entry") else "N/A"
        
        table_rows += f'''
        <tr class="trade-row normal">
            <td>{date_str}</td>
            <td><strong>{r["symbol"]}</strong></td>
            <td>{tf_badge}</td>
            <td>{side_badge}</td>
            <td><strong style="color:var(--info)">{entry_val}</strong></td>
            <td>{tp1_val}</td>
            <td>{tp2_val}</td>
            <td>{tp3_val}</td>
            <td>{sl_val}</td>
        </tr>'''
    
    html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Positions Actives - AI Trader Pro</title>
    <style>{get_base_css()}</style>
</head>
<body>
    <button class="theme-toggle" onclick="toggleTheme()">🌓</button>
    <div class="app">
        {generate_sidebar_html("positions", kpi)}
        <main class="main">
            <header style="margin-bottom:32px">
                <h1 style="font-size:36px;font-weight:900;margin-bottom:8px">📈 Positions Actives</h1>
                <p style="color:var(--muted)">{len(rows)} position(s) en cours</p>
            </header>
            
            <div class="panel">
                <div style="overflow-x:auto">
                    <table>
                        <thead>
                            <tr>
                                <th>Date</th>
                                <th>Crypto</th>
                                <th>TimeFrame</th>
                                <th>Status</th>
                                <th>Entry</th>
                                <th>TP1</th>
                                <th>TP2</th>
                                <th>TP3</th>
                                <th>SL</th>
                            </tr>
                        </thead>
                        <tbody>{table_rows if table_rows else '<tr><td colspan="9" style="text-align:center;padding:40px;color:var(--muted)">Aucune position active</td></tr>'}</tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>
    <script>
    function toggleTheme() {{
        document.body.classList.toggle('light-mode');
        localStorage.setItem('theme', document.body.classList.contains('light-mode') ? 'light' : 'dark');
    }}
    if (localStorage.getItem('theme') === 'light') {{
        document.body.classList.add('light-mode');
    }}
    function toggleSidebar() {{
        document.getElementById('sidebar').classList.toggle('open');
    }}
    document.getElementById('sidebarOverlay')?.addEventListener('click', toggleSidebar);
    </script>
</body>
</html>'''
    return HTMLResponse(html)

@app.get("/history", response_class=HTMLResponse)
async def history_page():
    rows = [r for r in build_trade_rows(limit=1000) if r["row_state"] in ("tp", "sl", "cancel")]
    kpi = compute_kpis(rows)
    
    table_rows = ""
    for idx, r in enumerate(rows, start=1):
        result = "WIN" if r["row_state"] == "tp" else ("LOSS" if r["row_state"] == "sl" else "ANNULÉ")
        result_class = r["row_state"]
        side_badge = f'<span class="badge badge-{r["side"].lower() if r["side"] else "pending"}">{r["side"] or "N/A"}</span>'
        tf_badge = f'<span class="badge badge-tf">{r["tf_label"]}</span>'
        result_badge = f'<span class="badge badge-{result_class}">{result}</span>'
        
        entry_val = f"{r['entry']:.4f}" if r["entry"] else "N/A"
        tp1_val = f"{r['tp1']:.4f}" if r["tp1"] else "N/A"
        tp2_val = f"{r['tp2']:.4f}" if r["tp2"] else "N/A"
        tp3_val = f"{r['tp3']:.4f}" if r["tp3"] else "N/A"
        sl_val = f"{r['sl']:.4f}" if r["sl"] else "N/A"
        
        pl_html = "N/A"
        if r["entry"] and r["row_state"] in ("tp", "sl"):
            try:
                entry_price = float(r["entry"])
                if r["sl_hit"] and r["sl"]:
                    exit_price = float(r["sl"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    pl_color = "var(--danger)"
                    pl_html = f'<span style="color:{pl_color};font-weight:700">{pl_pct:.2f}%</span>'
                elif r["tp1_hit"] and r["tp1"]:
                    exit_price = float(r["tp1"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    pl_color = "var(--success)"
                    pl_html = f'<span style="color:{pl_color};font-weight:700">+{pl_pct:.2f}%</span>'
            except:
                pass
        
        date_str = datetime.fromtimestamp(r["t_entry"] / 1000).strftime("%Y-%m-%d %H:%M") if r.get("t_entry") else "N/A"
        
        table_rows += f'''
        <tr class="trade-row {result_class}">
            <td>{date_str}</td>
            <td><strong>{r["symbol"]}</strong></td>
            <td>{tf_badge}</td>
            <td>{side_badge}</td>
            <td><strong style="color:var(--info)">{entry_val}</strong></td>
            <td>{tp1_val}</td>
            <td>{tp2_val}</td>
            <td>{tp3_val}</td>
            <td>{sl_val}</td>
            <td>{pl_html}</td>
            <td>{result_badge}</td>
        </tr>'''
    
    html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Historique - AI Trader Pro</title>
    <style>{get_base_css()}</style>
</head>
<body>
    <button class="theme-toggle" onclick="toggleTheme()">🌓</button>
    <div class="app">
        {generate_sidebar_html("history", kpi)}
        <main class="main">
            <header style="margin-bottom:32px">
                <h1 style="font-size:36px;font-weight:900;margin-bottom:8px">📜 Historique</h1>
                <p style="color:var(--muted)">{len(rows)} trade(s) terminé(s)</p>
            </header>
            
            <div class="panel">
                <div style="overflow-x:auto">
                    <table>
                        <thead>
                            <tr>
                                <th>Date</th>
                                <th>Crypto</th>
                                <th>TimeFrame</th>
                                <th>Status</th>
                                <th>Entry</th>
                                <th>TP1</th>
                                <th>TP2</th>
                                <th>TP3</th>
                                <th>SL</th>
                                <th>P&L</th>
                                <th>Résultat</th>
                            </tr>
                        </thead>
                        <tbody>{table_rows if table_rows else '<tr><td colspan="11" style="text-align:center;padding:40px;color:var(--muted)">Aucun historique</td></tr>'}</tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>
    <script>
    function toggleTheme() {{
        document.body.classList.toggle('light-mode');
        localStorage.setItem('theme', document.body.classList.contains('light-mode') ? 'light' : 'dark');
    }}
    if (localStorage.getItem('theme') === 'light') {{
        document.body.classList.add('light-mode');
    }}
    function toggleSidebar() {{
        document.getElementById('sidebar').classList.toggle('open');
    }}
    document.getElementById('sidebarOverlay')?.addEventListener('click', toggleSidebar);
    </script>
</body>
</html>'''
    return HTMLResponse(html)

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page():
    rows = build_trade_rows(limit=10000)
    kpi = compute_kpis(rows)
    alt = compute_altseason_snapshot()
    
    crypto_stats = {}
    for r in rows:
        symbol = r["symbol"]
        if symbol not in crypto_stats:
            crypto_stats[symbol] = {"total": 0, "wins": 0, "losses": 0, "pending": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0, "total_pl": 0.0, "trades": []}
        
        crypto_stats[symbol]["total"] += 1
        crypto_stats[symbol]["trades"].append(r)
        
        if r["row_state"] == "tp":
            crypto_stats[symbol]["wins"] += 1
        elif r["row_state"] == "sl":
            crypto_stats[symbol]["losses"] += 1
        elif r["row_state"] == "normal":
            crypto_stats[symbol]["pending"] += 1
        
        if r["tp1_hit"]:
            crypto_stats[symbol]["tp1"] += 1
        if r["tp2_hit"]:
            crypto_stats[symbol]["tp2"] += 1
        if r["tp3_hit"]:
            crypto_stats[symbol]["tp3"] += 1
        if r["sl_hit"]:
            crypto_stats[symbol]["sl"] += 1
        
        if r["entry"] and r["row_state"] in ("tp", "sl"):
            try:
                entry_price = float(r["entry"])
                if r["sl_hit"] and r["sl"]:
                    exit_price = float(r["sl"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    crypto_stats[symbol]["total_pl"] += pl_pct
                elif r["tp1_hit"] and r["tp1"]:
                    exit_price = float(r["tp1"])
                    pl_pct = ((exit_price - entry_price) / entry_price) * 100
                    if r["side"] == "SHORT":
                        pl_pct = -pl_pct
                    crypto_stats[symbol]["total_pl"] += pl_pct
            except:
                pass
    
    for symbol in crypto_stats:
        stats = crypto_stats[symbol]
        total_closed = stats["wins"] + stats["losses"]
        stats["winrate"] = (stats["wins"] / total_closed * 100) if total_closed > 0 else 0
    
    sorted_cryptos = sorted(crypto_stats.items(), key=lambda x: x[1]["total"], reverse=True)[:20]
    
    crypto_rows = ""
    for idx, (symbol, stats) in enumerate(sorted_cryptos, start=1):
        winrate_color = "var(--success)" if stats["winrate"] >= 50 else "var(--danger)"
        pl_color = "var(--success)" if stats["total_pl"] >= 0 else "var(--danger)"
        pl_sign = "+" if stats["total_pl"] >= 0 else ""
        
        crypto_rows += f'''
        <tr class="trade-row">
            <td><strong>#{idx}</strong></td>
            <td><strong>{symbol}</strong></td>
            <td>{stats["total"]}</td>
            <td style="color:var(--success)">{stats["wins"]}</td>
            <td style="color:var(--danger)">{stats["losses"]}</td>
            <td style="color:var(--info)">{stats["pending"]}</td>
            <td style="color:{winrate_color};font-weight:700">{stats["winrate"]:.1f}%</td>
            <td style="color:{pl_color};font-weight:700">{pl_sign}{stats["total_pl"]:.2f}%</td>
            <td><span class="badge badge-tp">{stats["tp1"]}</span> <span class="badge badge-tp">{stats["tp2"]}</span> <span class="badge badge-tp">{stats["tp3"]}</span></td>
            <td><span class="badge badge-sl">{stats["sl"]}</span></td>
        </tr>'''
    
    html = f'''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Analytics - AI Trader Pro</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>{get_base_css()}</style>
</head>
<body>
    <button class="theme-toggle" onclick="toggleTheme()">🌓</button>
    <div class="app">
        {generate_sidebar_html("analytics", kpi)}
        <main class="main">
            <header style="margin-bottom:32px">
                <h1 style="font-size:36px;font-weight:900;margin-bottom:8px">📊 Analytics</h1>
                <p style="color:var(--muted)">Analyse détaillée de vos performances</p>
            </header>

            <div class="chart-container">
                <h3 style="margin-bottom:20px;font-weight:800">Performance Metrics Globales</h3>
                <div style="display:flex;gap:10px;margin:10px 0;align-items:center">
                    <div style="flex:0 0 100px;font-size:13px;font-weight:600">Win Rate</div>
                    <div style="flex:1;height:32px;background:rgba(100,116,139,0.1);border-radius:8px;position:relative;overflow:hidden">
                        <div style="height:100%;background:linear-gradient(90deg,var(--success),var(--accent));border-radius:8px;transition:width 0.3s;width:{kpi['winrate']}%"></div>
                    </div>
                    <div style="min-width:60px;text-align:right;font-weight:700;font-size:14px">{kpi['winrate']}%</div>
                </div>
                <div style="display:flex;gap:10px;margin:10px 0;align-items:center">
                    <div style="flex:0 0 100px;font-size:13px;font-weight:600">LONG Ratio</div>
                    <div style="flex:1;height:32px;background:rgba(100,116,139,0.1);border-radius:8px;position:relative;overflow:hidden">
                        <div style="height:100%;background:linear-gradient(90deg,var(--success),var(--accent));border-radius:8px;transition:width 0.3s;width:{alt['signals']['long_ratio']}%"></div>
                    </div>
                    <div style="min-width:60px;text-align:right;font-weight:700;font-size:14px">{alt['signals']['long_ratio']}%</div>
                </div>
                <div style="display:flex;gap:10px;margin:10px 0;align-items:center">
                    <div style="flex:0 0 100px;font-size:13px;font-weight:600">TP vs SL</div>
                    <div style="flex:1;height:32px;background:rgba(100,116,139,0.1);border-radius:8px;position:relative;overflow:hidden">
                        <div style="height:100%;background:linear-gradient(90deg,var(--success),var(--accent));border-radius:8px;transition:width 0.3s;width:{alt['signals']['tp_vs_sl']}%"></div>
                    </div>
                    <div style="min-width:60px;text-align:right;font-weight:700;font-size:14px">{alt['signals']['tp_vs_sl']}%</div>
                </div>
                <div style="display:flex;gap:10px;margin:10px 0;align-items:center">
                    <div style="flex:0 0 100px;font-size:13px;font-weight:600">Altseason</div>
                    <div style="flex:1;height:32px;background:rgba(100,116,139,0.1);border-radius:8px;position:relative;overflow:hidden">
                        <div style="height:100%;background:linear-gradient(90deg,var(--success),var(--accent));border-radius:8px;transition:width 0.3s;width:{alt['score']}%"></div>
                    </div>
                    <div style="min-width:60px;text-align:right;font-weight:700;font-size:14px">{alt['score']}/100</div>
                </div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:32px">
                <div class="panel">
                    <h3 style="margin-bottom:16px;font-weight:800">Trades (24h)</h3>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">Total:</span> <strong>{kpi['total_trades']}</strong></div>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">Actifs:</span> <strong style="color:var(--accent)">{kpi['active_trades']}</strong></div>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">Clôturés:</span> <strong>{kpi['total_closed']}</strong></div>
                </div>
                <div class="panel">
                    <h3 style="margin-bottom:16px;font-weight:800">Résultats</h3>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">Wins:</span> <strong style="color:var(--success)">{kpi['wins']}</strong></div>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">Losses:</span> <strong style="color:var(--danger)">{kpi['losses']}</strong></div>
                    <div style="font-size:14px;margin:8px 0"><span style="color:var(--muted)">TP Atteints:</span> <strong>{kpi['tp_hits']}</strong></div>
                </div>
            </div>

            <div class="chart-container">
                <h3 style="margin-bottom:20px;font-weight:800">📈 Top 10 Cryptos par Volume</h3>
                <canvas id="cryptoChart"></canvas>
            </div>

            <div class="panel">
                <h2 style="font-size:20px;font-weight:800;margin-bottom:20px">📈 Statistiques par Crypto (Top 20)</h2>
                <div style="overflow-x:auto">
                    <table>
                        <thead>
                            <tr>
                                <th>Rang</th>
                                <th>Crypto</th>
                                <th>Total Trades</th>
                                <th>Wins</th>
                                <th>Losses</th>
                                <th>En cours</th>
                                <th>Win Rate</th>
                                <th>P&L Total</th>
                                <th>TP (1/2/3)</th>
                                <th>SL</th>
                            </tr>
                        </thead>
                        <tbody>{crypto_rows if crypto_rows else '<tr><td colspan="10" style="text-align:center;padding:40px;color:var(--muted)">Aucune donnée</td></tr>'}</tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>
    <script>
    function toggleTheme() {{
        document.body.classList.toggle('light-mode');
        localStorage.setItem('theme', document.body.classList.contains('light-mode') ? 'light' : 'dark');
    }}
    if (localStorage.getItem('theme') === 'light') {{
        document.body.classList.add('light-mode');
    }}
    function toggleSidebar() {{
        document.getElementById('sidebar').classList.toggle('open');
    }}
    document.getElementById('sidebarOverlay')?.addEventListener('click', toggleSidebar);
    
    async function loadCryptoChart() {{
        try {{
            const response = await fetch('/api/charts-data');
            const result = await response.json();
            
            if (!result.ok) throw new Error(result.error);
            
            const data = result.data.top_cryptos;
            const ctx = document.getElementById('cryptoChart').getContext('2d');
            
            new Chart(ctx, {{
                type: 'bar',
                data: {{
                    labels: data.map(d => d.symbol),
                    datasets: [{{
                        label: 'Nombre de trades',
                        data: data.map(d => d.count),
                        backgroundColor: 'rgba(99, 102, 241, 0.6)',
                        borderColor: '#6366f1',
                        borderWidth: 2
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            ticks: {{ color: '#64748b' }},
                            grid: {{ color: 'rgba(99, 102, 241, 0.1)' }}
                        }},
                        x: {{
                            ticks: {{ color: '#64748b' }},
                            grid: {{ display: false }}
                        }}
                    }}
                }}
            }});
        }} catch (error) {{
            console.error('Erreur chargement graphique:', error);
        }}
    }}
    
    loadCryptoChart();
    </script>
</body>
</html>'''
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting AI Trader Pro v2.2...")
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False, log_level="info")
