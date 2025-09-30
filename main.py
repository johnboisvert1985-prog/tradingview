import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import FastAPI, APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from dotenv import load_dotenv

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# =========================
# Logging
# =========================
logger = logging.getLogger("aitrader")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# =========================
# Advanced Config / Env
# =========================
# Basic Config
DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
DB_PATH = os.getenv("DB_PATH", os.path.join(DB_DIR, "data.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg")

# Altseason Auto-Notifications
ALTSEASON_AUTONOTIFY = int(os.getenv("ALTSEASON_AUTONOTIFY", "0"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

# Trading Filters
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.90"))
RR_MIN = float(os.getenv("RR_MIN", "1.0"))
MIN_CONFLUENCE = int(os.getenv("MIN_CONFLUENCE", "0"))
NEAR_SR_ATR = float(os.getenv("NEAR_SR_ATR", "0.0"))

# LLM Integration
LLM_ENABLED = int(os.getenv("LLM_ENABLED", "0"))
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_REASONING = os.getenv("LLM_REASONING", "high")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Telegram Advanced
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "800"))
TELEGRAM_COOLDOWN_SEC = int(os.getenv("TELEGRAM_COOLDOWN_SEC", str(COOLDOWN_SEC)))

# Telegram UI Features
TELEGRAM_PIN_ALTSEASON = int(os.getenv("TELEGRAM_PIN_ALTSEASON", "0"))
TG_BUTTONS = int(os.getenv("TG_BUTTONS", "1"))
TG_BUTTON_TEXT = os.getenv("TG_BUTTON_TEXT", "📊 Ouvrir le Dashboard")
TG_COMPACT = int(os.getenv("TG_COMPACT", "0"))
TG_DASHBOARD_URL = os.getenv("TG_DASHBOARD_URL", "https://trading-alert-system-2.preview.emergentagent.com")
TG_PARSE = os.getenv("TG_PARSE", "HTML")
TG_SHOW_LLM = int(os.getenv("TG_SHOW_LLM", "1"))
TG_SILENT = int(os.getenv("TG_SILENT", "0"))

# Vector icons
VECTOR_UP_ICON = "🟩"
VECTOR_DN_ICON = "🟥"

logger.info(f"DB initialized at {DB_PATH}")
logger.info(f"Telegram enabled: {TELEGRAM_ENABLED}")
logger.info(f"LLM enabled: {LLM_ENABLED}")
logger.info(f"Altseason auto-notify: {ALTSEASON_AUTONOTIFY}")

# =========================
# SQLite Database
# =========================
def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = dict_factory

def db_execute(sql: str, params: tuple = ()):
    cur = DB.cursor()
    cur.execute(sql, params)
    DB.commit()
    return cur

def db_query(sql: str, params: tuple = ()) -> List[dict]:
    cur = DB.cursor()
    cur = cur.execute(sql, params)
    return list(cur.fetchall())

# Initialize database schema
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
    trade_id TEXT,
    rr REAL,
    confluence INTEGER,
    llm_analysis TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now') * 1000)
)
""")

# Create indexes
db_execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_symbol_tf ON events(symbol, tf)")
db_execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)")

# Altseason notifications tracking
db_execute("""
CREATE TABLE IF NOT EXISTS altseason_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score INTEGER,
    label TEXT,
    timestamp INTEGER,
    notified INTEGER DEFAULT 0
)
""")

# =========================
# Pydantic Models
# =========================
class WebhookPayload(BaseModel):
    secret: Optional[str] = None
    type: str
    symbol: str
    tf: Optional[Any] = None
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
    rr: Optional[float] = None
    confluence: Optional[int] = None

class TradeRow(BaseModel):
    trade_id: str
    symbol: str
    tf_label: str
    side: Optional[str]
    entry: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    sl: Optional[float]
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    sl_hit: bool
    row_state: str
    timestamp: int
    confidence: Optional[int] = None
    rr: Optional[float] = None
    llm_analysis: Optional[str] = None

class AltseasonData(BaseModel):
    score: int
    label: str
    window_minutes: int
    signals: Dict[str, Any]
    auto_notify_enabled: bool = False

class WebhookResponse(BaseModel):
    ok: bool
    trade_id: Optional[str] = None
    message: Optional[str] = None
    filtered: Optional[bool] = False
    filter_reason: Optional[str] = None

# =========================
# Utility Functions
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
    try:
        # Check if new columns exist
        cols = {r["name"] for r in db_query("PRAGMA table_info(events)")}
        
        if "tf_label" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN tf_label TEXT")
        if "rr" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN rr REAL")
        if "confluence" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN confluence INTEGER")
        if "llm_analysis" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN llm_analysis TEXT")
        if "created_at" not in cols:
            db_execute("ALTER TABLE events ADD COLUMN created_at INTEGER DEFAULT (strftime('%s', 'now') * 1000)")
    except Exception as e:
        logger.warning(f"Schema update warning: {e}")

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def ms_ago(minutes: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)

def calculate_rr(entry: float, sl: float, tp: float) -> float:
    """Calculate Risk/Reward ratio"""
    try:
        if not entry or not sl or not tp:
            return 0.0
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        return reward / risk if risk > 0 else 0.0
    except:
        return 0.0

def passes_filters(payload: dict) -> tuple[bool, str]:
    """Check if trade passes all configured filters"""
    
    # Confidence filter
    confidence = payload.get("confidence")
    if confidence and CONFIDENCE_MIN > 0:
        conf_ratio = confidence / 100.0
        if conf_ratio < CONFIDENCE_MIN:
            return False, f"Confidence {confidence}% < {CONFIDENCE_MIN*100}%"
    
    # Risk/Reward filter
    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    
    if entry and sl and tp1 and RR_MIN > 0:
        rr = calculate_rr(entry, sl, tp1)
        if rr < RR_MIN:
            return False, f"R/R {rr:.2f} < {RR_MIN}"
    
    # Confluence filter
    confluence = payload.get("confluence", 0)
    if confluence and MIN_CONFLUENCE > 0:
        if confluence < MIN_CONFLUENCE:
            return False, f"Confluence {confluence} < {MIN_CONFLUENCE}"
    
    return True, ""

try:
    ensure_trades_schema()
except Exception as e:
    logger.warning(f"ensure_trades_schema warning: {e}")

# =========================
# LLM Integration
# =========================
async def get_llm_analysis(payload: dict) -> Optional[str]:
    """Get LLM analysis of the trade setup"""
    if not LLM_ENABLED or not OPENAI_API_KEY:
        return None
    
    try:
        prompt = f"""Analyze this trading setup quickly:
Symbol: {payload.get('symbol')}
Side: {payload.get('side')}
Entry: {payload.get('entry')}
TP1: {payload.get('tp1')}, TP2: {payload.get('tp2')}, TP3: {payload.get('tp3')}
SL: {payload.get('sl')}
Confidence: {payload.get('confidence')}%
Note: {payload.get('note', 'N/A')}

Provide a brief 2-sentence analysis of this setup's quality and risk level."""

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100,
            "temperature": 0.7
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("https://api.openai.com/v1/chat/completions", 
                                       headers=headers, json=data)
            
            if response.status_code == 200:
                result = response.json()
                return result["choices"][0]["message"]["content"].strip()
                
    except Exception as e:
        logger.warning(f"LLM analysis error: {e}")
    
    return None

# =========================
# Telegram Functions
# =========================
_last_tg_sent: Dict[str, float] = {}
_last_altseason_notify = 0

async def tg_send_text(text: str, disable_web_page_preview: bool = True, 
                      key: Optional[str] = None, pin_message: bool = False,
                      reply_markup: Optional[dict] = None):
    if not TELEGRAM_ENABLED:
        return {"ok": False, "reason": "telegram disabled"}

    k = key or "default"
    now = datetime.now().timestamp()
    last = _last_tg_sent.get(k, 0)
    cooldown = TELEGRAM_COOLDOWN_SEC if k != "altseason" else ALTSEASON_NOTIFY_MIN_GAP_MIN * 60
    
    if now - last < cooldown:
        logger.warning(f"Telegram send skipped due to cooldown ({k})")
        return {"ok": False, "reason": "cooldown"}
    _last_tg_sent[k] = now

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
        "parse_mode": TG_PARSE,
        "disable_notification": bool(TG_SILENT),
    }
    
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            result = r.json()
            
            # Pin message if requested and successful
            if pin_message and result.get("ok") and TELEGRAM_PIN_ALTSEASON:
                message_id = result["result"]["message_id"]
                pin_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
                await client.post(pin_url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "message_id": message_id,
                    "disable_notification": bool(TG_SILENT)
                })
            
            logger.info(f"Telegram sent: {text[:80]}...")
            return {"ok": True}
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return {"ok": False, "reason": str(e)}

def create_dashboard_button():
    """Create inline keyboard button for dashboard"""
    if not TG_BUTTONS:
        return None
    
    return {
        "inline_keyboard": [[
            {
                "text": TG_BUTTON_TEXT,
                "url": TG_DASHBOARD_URL
            }
        ]]
    }

def format_vector_message(symbol: str, tf_label: str, direction: str, price: Any, note: Optional[str] = None) -> str:
    icon = VECTOR_UP_ICON if (direction or "").upper() == "UP" else VECTOR_DN_ICON
    n = f" — {note}" if note else ""
    return f"{icon} Vector Candle {direction.upper()} | <b>{symbol}</b> <i>{tf_label}</i> @ <code>{price}</code>{n}"

def format_entry_announcement(payload: dict, llm_analysis: Optional[str] = None) -> str:
    symbol = payload.get("symbol", "")
    tf_lbl = payload.get("tf_label") or tf_to_label(payload.get("tf")) or ""
    side = (payload.get("side") or "").upper()
    entry = payload.get("entry")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    tp3 = payload.get("tp3")
    sl = payload.get("sl")
    leverage = payload.get("leverage") or payload.get("lev_reco") or ""
    conf = payload.get("confidence")
    note = (payload.get("note") or "").strip()
    rr = payload.get("rr")

    side_emoji = "📈" if side == "LONG" else ("📉" if side == "SHORT" else "📌")
    side_label = side if side else "Position"

    lines = []
    if tp1 is not None: 
        rr_text = f" (R/R: {rr:.2f})" if rr and rr > 0 else ""
        lines.append(f"🎯 TP1: {tp1}{rr_text}")
    if tp2 is not None: lines.append(f"🎯 TP2: {tp2}")
    if tp3 is not None: lines.append(f"🎯 TP3: {tp3}")
    if sl is not None: lines.append(f"❌ SL: {sl}")

    conf_line = ""
    if conf is not None:
        expl = note if note else f"Le setup {side_label} a un risque acceptable si le contexte le confirme."
        conf_line = f"🧠 Confiance: {conf}% — {expl}"

    llm_line = ""
    if llm_analysis and TG_SHOW_LLM:
        llm_line = f"🤖 IA: {llm_analysis}"

    tip_line = "💡 Astuce: après TP1, placez SL au BE." if tp1 is not None else ""

    msg = [
        f"📩 {symbol} {tf_lbl}",
        f"{side_emoji} {side_label} Entry: {entry}" if entry is not None else f"{side_emoji} {side_label}",
        f"💡Leverage: {leverage}" if leverage else "",
        *lines,
        conf_line,
        llm_line,
        tip_line,
    ]
    return "\n".join([m for m in msg if m])

# =========================
# Business Logic
# =========================
def save_event(payload: dict, llm_analysis: Optional[str] = None):
    etype = payload.get("type")
    symbol = payload.get("symbol")
    tf = payload.get("tf")
    tflabel = payload.get("tf_label") or tf_to_label(tf)
    t = payload.get("time") or now_ms()
    side = payload.get("side")
    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    tp3 = payload.get("tp3")
    r1 = payload.get("r1")
    s1 = payload.get("s1")
    lev_reco = payload.get("lev_reco")
    qty_reco = payload.get("qty_reco")
    notional = payload.get("notional")
    confidence = payload.get("confidence")
    horizon = payload.get("horizon")
    leverage = payload.get("leverage")
    note = payload.get("note")
    price = payload.get("price")
    direction = payload.get("direction")
    trade_id = payload.get("trade_id")
    rr = payload.get("rr") or (calculate_rr(entry, sl, tp1) if entry and sl and tp1 else None)
    confluence = payload.get("confluence")

    if trade_id is None and etype and symbol and tf:
        trade_id = f"{symbol}_{tf}_{t}"

    db_execute("""
        INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, r1, s1,
                           lev_reco, qty_reco, notional, confidence, horizon, leverage,
                           note, price, direction, trade_id, rr, confluence, llm_analysis)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (etype, symbol, str(tf) if tf is not None else None, tflabel, int(t),
          side, entry, sl, tp1, tp2, tp3, r1, s1,
          lev_reco, qty_reco, notional, confidence, horizon, leverage,
          note, price, direction, trade_id, rr, confluence, llm_analysis))

    logger.info(f"Saved event: type={etype} symbol={symbol} tf={tf} trade_id={trade_id}")
    return trade_id

def _pct(x, y):
    try:
        x = float(x or 0)
        y = float(y or 0)
        return 0.0 if y == 0 else 100.0 * x / y
    except Exception:
        return 0.0

def compute_altseason_snapshot() -> AltseasonData:
    """Enhanced Altseason calculation with auto-notify support"""
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
    if score >= 75:
        label = "Altseason (forte)"
    elif score >= 50:
        label = "Altseason (modérée)"
    else:
        label = "Marché neutre/faible"

    return AltseasonData(
        score=int(score),
        label=label,
        window_minutes=24*60,
        signals={
            "long_ratio": round(A, 1),
            "tp_vs_sl": round(B, 1),
            "breadth_symbols": int(sym_gain),
            "recent_entries_ratio": round(D, 1),
            "greens_count": int(sym_gain)
        },
        auto_notify_enabled=bool(ALTSEASON_AUTONOTIFY)
    )

def _latest_entry_for_trade(trade_id: str) -> Optional[dict]:
    rows = db_query("""
      SELECT * FROM events
      WHERE trade_id=? AND type='ENTRY'
      ORDER BY time DESC LIMIT 1
    """, (trade_id,))
    return rows[0] if rows else None

def _has_hit(trade_id: str) -> Dict[str, bool]:
    hits = db_query("""
      SELECT type, MAX(time) AS t FROM events
      WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSE')
      GROUP BY type
    """, (trade_id,))
    return {r["type"]: True for r in hits}

def _is_cancelled_due_to_opposite_after(entry_row: dict) -> bool:
    """Marque 'annulé' (orange) si APRES cette ENTRY,
    on a une autre ENTRY sur le même symbole+tf avec side opposé."""
    symbol = entry_row.get("symbol")
    tf = entry_row.get("tf")
    side = (entry_row.get("side") or "").upper()
    t = entry_row.get("time") or 0
    if not symbol or tf is None or not side:
        return False
    opposite = "SHORT" if side == "LONG" else ("LONG" if side == "SHORT" else "")
    if not opposite:
        return False
    r = db_query("""
      SELECT 1 FROM events
      WHERE type='ENTRY' AND symbol=? AND tf=? AND time>? AND UPPER(COALESCE(side,''))=?
      LIMIT 1
    """, (symbol, str(tf), int(t), opposite))
    return bool(r)

def build_trade_rows(limit=300) -> List[TradeRow]:
    entries = db_query("""
      SELECT e.trade_id, MAX(e.time) AS t_entry
      FROM events e
      WHERE e.type='ENTRY'
      GROUP BY e.trade_id
      ORDER BY t_entry DESC
      LIMIT ?
    """, (limit,))

    rows: List[TradeRow] = []
    for x in entries:
        e = _latest_entry_for_trade(x["trade_id"])
        if not e:
            continue

        tf_label = (e.get("tf_label") or tf_to_label(e.get("tf")))
        hit = _has_hit(e["trade_id"])
        tp1_hit, tp2_hit, tp3_hit = bool(hit.get("TP1_HIT")), bool(hit.get("TP2_HIT")), bool(hit.get("TP3_HIT"))
        sl_hit = bool(hit.get("SL_HIT"))
        closed = bool(hit.get("CLOSE"))

        cancelled = _is_cancelled_due_to_opposite_after(e) and not (tp1_hit or tp2_hit or tp3_hit or sl_hit)

        # Statut de ligne pour couleurs de fond:
        row_state = "normal"
        if sl_hit:
            row_state = "sl"
        elif tp1_hit or tp2_hit or tp3_hit:
            row_state = "tp"
        elif cancelled or closed:
            row_state = "cancel"

        rows.append(TradeRow(
            trade_id=e["trade_id"],
            symbol=e["symbol"],
            tf_label=tf_label,
            side=e["side"],
            entry=e["entry"],
            tp1=e["tp1"],
            tp2=e["tp2"],
            tp3=e["tp3"],
            sl=e["sl"],
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            tp3_hit=tp3_hit,
            sl_hit=sl_hit,
            row_state=row_state,
            timestamp=e.get("time", 0),
            confidence=e.get("confidence"),
            rr=e.get("rr"),
            llm_analysis=e.get("llm_analysis")
        ))
    return rows

# =========================
# Background Tasks
# =========================
async def altseason_auto_notifier():
    """Background task for automatic altseason notifications"""
    global _last_altseason_notify
    
    if not ALTSEASON_AUTONOTIFY or not TELEGRAM_ENABLED:
        return
    
    try:
        altseason = compute_altseason_snapshot()
        now = datetime.now().timestamp()
        
        # Check if we need to notify
        greens_count = altseason.signals.get("greens_count", 0)
        should_notify = (
            greens_count >= ALT_GREENS_REQUIRED and
            altseason.score >= 50 and
            (now - _last_altseason_notify) >= (ALTSEASON_NOTIFY_MIN_GAP_MIN * 60)
        )
        
        if should_notify:
            # Format altseason message
            emoji = "🟢" if altseason.score >= 75 else "🟡" if altseason.score >= 50 else "🔴"
            msg = f"""🚨 <b>Alerte Altseason Automatique</b> {emoji}

📊 <b>Score: {altseason.score}/100</b>
📈 Status: <b>{altseason.label}</b>

🔥 Signaux détectés:
• Ratio LONG: {altseason.signals['long_ratio']}%
• TP vs SL: {altseason.signals['tp_vs_sl']}%
• Breadth: {altseason.signals['breadth_symbols']} symboles
• Momentum: {altseason.signals['recent_entries_ratio']}%

⚡ <b>{greens_count} symboles</b> avec TP atteints (seuil: {ALT_GREENS_REQUIRED})

<i>Notification automatique activée</i>"""

            reply_markup = create_dashboard_button()
            
            result = await tg_send_text(
                msg, 
                key="altseason", 
                pin_message=bool(TELEGRAM_PIN_ALTSEASON),
                reply_markup=reply_markup
            )
            
            if result.get("ok"):
                _last_altseason_notify = now
                # Save notification to DB
                db_execute("""
                    INSERT INTO altseason_notifications (score, label, timestamp, notified)
                    VALUES (?, ?, ?, 1)
                """, (altseason.score, altseason.label, int(now * 1000)))
                
                logger.info(f"Auto altseason notification sent: score={altseason.score}")
    
    except Exception as e:
        logger.error(f"Altseason auto-notifier error: {e}")

# =========================
# FastAPI App
# =========================
app = FastAPI(title="AI Trader Pro", version="3.0")

# Create API router
api_router = APIRouter(prefix="/api")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# API Routes
# =========================

@api_router.get("/", response_model=dict)
async def api_root():
    return {
        "message": "AI Trader Pro API",
        "version": "3.0",
        "features": {
            "telegram_enabled": TELEGRAM_ENABLED,
            "llm_enabled": LLM_ENABLED,
            "altseason_auto_notify": bool(ALTSEASON_AUTONOTIFY),
            "advanced_filters": True,
            "telegram_buttons": bool(TG_BUTTONS)
        },
        "database": "SQLite",
        "status": "operational"
    }

@api_router.post("/tv-webhook", response_model=WebhookResponse)
async def tv_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """Enhanced TradingView webhook endpoint with filters and LLM"""
    # Verify secret if configured
    if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if not payload.type or not payload.symbol:
        raise HTTPException(status_code=422, detail="Missing required fields: type or symbol")

    payload_dict = payload.model_dump()
    
    # Apply filters for ENTRY events
    if payload.type == "ENTRY":
        passes, reason = passes_filters(payload_dict)
        if not passes:
            logger.info(f"Trade filtered out: {payload.symbol} - {reason}")
            return WebhookResponse(ok=True, filtered=True, filter_reason=reason, 
                                 message="Trade filtered by criteria")
        
        # Calculate R/R if not provided
        if payload.entry and payload.sl and payload.tp1 and not payload.rr:
            payload_dict["rr"] = calculate_rr(payload.entry, payload.sl, payload.tp1)
    
    # Get LLM analysis for ENTRY events
    llm_analysis = None
    if payload.type == "ENTRY" and LLM_ENABLED:
        llm_analysis = await get_llm_analysis(payload_dict)
    
    # Save event to database
    trade_id = save_event(payload_dict, llm_analysis)

    # Send Telegram notifications
    try:
        if TELEGRAM_ENABLED:
            key = f"{payload.type}:{payload.symbol}"
            reply_markup = create_dashboard_button() if TG_BUTTONS else None

            if payload.type == "VECTOR_CANDLE":
                txt = format_vector_message(
                    symbol=payload.symbol,
                    tf_label=payload.tf_label or tf_to_label(payload.tf),
                    direction=(payload.direction or ""),
                    price=payload.price,
                    note=payload.note,
                )
                await tg_send_text(txt, key=key, reply_markup=reply_markup)

            elif payload.type == "ENTRY":
                txt = format_entry_announcement(payload_dict, llm_analysis)
                await tg_send_text(txt, key=payload.trade_id or key, reply_markup=reply_markup)

            elif payload.type in {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"}:
                symbol = payload.symbol
                tf_lbl = payload.tf_label or tf_to_label(payload.tf)
                side = (payload.side or "").upper()
                price = payload.price

                side_emoji = "📈" if side == "LONG" else ("📉" if side == "SHORT" else "📌")
                side_label = side if side else "Position"
                base = f"{symbol} {tf_lbl}"
                price_text = f" @ {price}" if price else ""

                if payload.type in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
                    tick = {"TP1_HIT": "TP1", "TP2_HIT": "TP2", "TP3_HIT": "TP3"}[payload.type]
                    txt = f"✅ {tick} atteint — {base}\n{side_emoji} {side_label}{price_text}"
                elif payload.type == "SL_HIT":
                    txt = f"🛑 SL touché — {base}\n{side_emoji} {side_label}{price_text}"
                elif payload.type == "CLOSE":
                    note = payload.note or ""
                    txt = f"📪 Trade clôturé — {base}\n{side_emoji} {side_label}" + (f"\n📝 {note}" if note else "")

                await tg_send_text(txt, key=payload.trade_id or key, reply_markup=reply_markup)

        # Trigger altseason check in background
        background_tasks.add_task(altseason_auto_notifier)

    except Exception as e:
        logger.warning(f"Telegram notification error: {e}")

    return WebhookResponse(ok=True, trade_id=trade_id, message="Event processed successfully")

@api_router.get("/trades", response_model=List[TradeRow])
async def get_trades(limit: int = 300):
    """Get recent trades with enhanced data"""
    try:
        ensure_trades_schema()
    except Exception:
        pass
    return build_trade_rows(limit=limit)

@api_router.get("/altseason", response_model=AltseasonData)
async def get_altseason():
    """Get current altseason indicators with auto-notify status"""
    return compute_altseason_snapshot()

@api_router.post("/altseason/notify")
async def trigger_altseason_notify():
    """Manually trigger altseason notification"""
    if not TELEGRAM_ENABLED:
        return {"ok": False, "reason": "Telegram not enabled"}
    
    try:
        await altseason_auto_notifier()
        return {"ok": True, "message": "Altseason notification triggered"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@api_router.get("/config")
async def get_config():
    """Get current configuration"""
    return {
        "filters": {
            "confidence_min": CONFIDENCE_MIN,
            "rr_min": RR_MIN,
            "min_confluence": MIN_CONFLUENCE,
            "near_sr_atr": NEAR_SR_ATR
        },
        "altseason": {
            "auto_notify": bool(ALTSEASON_AUTONOTIFY),
            "min_gap_minutes": ALTSEASON_NOTIFY_MIN_GAP_MIN,
            "poll_seconds": ALTSEASON_POLL_SECONDS,
            "greens_required": ALT_GREENS_REQUIRED
        },
        "llm": {
            "enabled": bool(LLM_ENABLED),
            "model": LLM_MODEL,
            "reasoning": LLM_REASONING
        },
        "telegram": {
            "enabled": TELEGRAM_ENABLED,
            "buttons": bool(TG_BUTTONS),
            "dashboard_url": TG_DASHBOARD_URL,
            "pin_altseason": bool(TELEGRAM_PIN_ALTSEASON),
            "show_llm": bool(TG_SHOW_LLM),
            "compact": bool(TG_COMPACT)
        }
    }

@api_router.get("/stats")
async def get_stats():
    """Get enhanced trading statistics"""
    t24 = ms_ago(24*60)
    
    # Total events
    total_events = db_query("SELECT COUNT(*) as count FROM events WHERE time>=?", (t24,))
    total = total_events[0]["count"] if total_events else 0
    
    # Entry counts by side
    side_stats = db_query("""
        SELECT 
            side,
            COUNT(*) as count,
            AVG(confidence) as avg_confidence,
            AVG(rr) as avg_rr
        FROM events 
        WHERE type='ENTRY' AND time>=?
        GROUP BY side
    """, (t24,))
    
    # TP/SL hit counts
    outcome_stats = db_query("""
        SELECT 
            type,
            COUNT(*) as count
        FROM events 
        WHERE type IN ('TP1_HIT', 'TP2_HIT', 'TP3_HIT', 'SL_HIT') AND time>=?
        GROUP BY type
    """, (t24,))
    
    # Active symbols
    symbols = db_query("""
        SELECT COUNT(DISTINCT symbol) as count 
        FROM events 
        WHERE time>=?
    """, (t24,))
    
    # High confidence trades
    high_conf = db_query("""
        SELECT COUNT(*) as count 
        FROM events 
        WHERE type='ENTRY' AND time>=? AND confidence >= ?
    """, (t24, CONFIDENCE_MIN * 100))
    
    return {
        "total_events_24h": total,
        "side_distribution": {
            row["side"] or "UNKNOWN": {
                "count": row["count"],
                "avg_confidence": round(row["avg_confidence"] or 0, 1),
                "avg_rr": round(row["avg_rr"] or 0, 2)
            } for row in side_stats
        },
        "outcomes": {row["type"]: row["count"] for row in outcome_stats},
        "active_symbols": symbols[0]["count"] if symbols else 0,
        "high_confidence_trades": high_conf[0]["count"] if high_conf else 0,
        "filters_active": {
            "confidence_min": CONFIDENCE_MIN,
            "rr_min": RR_MIN
        },
        "timestamp": now_ms()
    }

# Include the router
app.include_router(api_router)

# Root endpoint for basic info
@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>AI Trader Pro v3.0</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: system-ui; padding: 24px; background: #0b0f14; color: #e6edf3; }}
            .feature {{ background: #1c2533; padding: 12px; margin: 8px 0; border-radius: 8px; }}
            .enabled {{ border-left: 4px solid #22c55e; }}
            .disabled {{ border-left: 4px solid #ef4444; }}
        </style>
    </head>
    <body>
        <h1>🚀 AI Trader Pro v3.0</h1>
        <p>Professional Trading Dashboard & Advanced Webhook System</p>
        
        <h3>🔧 Fonctionnalités Avancées:</h3>
        <div class="feature {'enabled' if TELEGRAM_ENABLED else 'disabled'}">
            📱 Telegram: {'✅ Activé' if TELEGRAM_ENABLED else '❌ Désactivé'}
        </div>
        <div class="feature {'enabled' if LLM_ENABLED else 'disabled'}">
            🤖 LLM Analysis: {'✅ Activé (' + LLM_MODEL + ')' if LLM_ENABLED else '❌ Désactivé'}
        </div>
        <div class="feature {'enabled' if ALTSEASON_AUTONOTIFY else 'disabled'}">
            📊 Auto Altseason: {'✅ Activé' if ALTSEASON_AUTONOTIFY else '❌ Désactivé'}
        </div>
        <div class="feature {'enabled' if TG_BUTTONS else 'disabled'}">
            🔘 Telegram Buttons: {'✅ Activé' if TG_BUTTONS else '❌ Désactivé'}
        </div>
        
        <h3>📡 API Endpoints:</h3>
        <ul>
            <li><a href="/api" style="color:#58a6ff;">/api</a> — API Info & Features</li>
            <li><a href="/api/trades" style="color:#58a6ff;">/api/trades</a> — Get Trades Data</li>
            <li><a href="/api/altseason" style="color:#58a6ff;">/api/altseason</a> — Altseason Indicators</li>
            <li><a href="/api/stats" style="color:#58a6ff;">/api/stats</a> — Enhanced Statistics</li>
            <li><a href="/api/config" style="color:#58a6ff;">/api/config</a> — Current Configuration</li>
            <li><code>POST /api/tv-webhook</code> — TradingView Webhook</li>
            <li><code>POST /api/altseason/notify</code> — Manual Altseason Notification</li>
        </ul>
        
        <p>
            <a href="{TG_DASHBOARD_URL}" style="color:#2da44e; font-weight:bold;">🎯 Open Modern Dashboard</a>
        </p>
        
        <h3>⚙️ Configuration Actuelle:</h3>
        <pre style="background:#1c2533; padding:12px; border-radius:8px; font-size:12px;">
Filtres: Confiance ≥{CONFIDENCE_MIN*100}%, R/R ≥{RR_MIN}
Altseason: Seuil {ALT_GREENS_REQUIRED} verts, Notification {ALTSEASON_NOTIFY_MIN_GAP_MIN}min
Cooldown: {TELEGRAM_COOLDOWN_SEC}s général, {ALTSEASON_NOTIFY_MIN_GAP_MIN*60}s altseason
        </pre>
    </body>
    </html>
    """)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    if DB:
        DB.close()
    logger.info("Application shutdown complete")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=False)
