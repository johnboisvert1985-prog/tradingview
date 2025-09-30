# main.py
import os
import sqlite3
import logging
import asyncio
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

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

# Flood control (plus conservateur que Telegram)
TG_MIN_DELAY_SEC = float(os.getenv("TG_MIN_DELAY_SEC", "3.3"))  # délai mini entre messages
TG_PER_MIN_LIMIT = int(os.getenv("TG_PER_MIN_LIMIT", "18"))     # plafond / minute (safe < 20)

# Altseason auto-notify
ALTSEASON_AUTONOTIFY = int(os.getenv("ALTSEASON_AUTONOTIFY", "1"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))          # nb min de symboles avec TP
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))

# Telegram UI (pin + bouton dashboard)
TELEGRAM_PIN_ALTSEASON = int(os.getenv("TELEGRAM_PIN_ALTSEASON", "1"))
TG_BUTTONS = int(os.getenv("TG_BUTTONS", "1"))
TG_BUTTON_TEXT = os.getenv("TG_BUTTON_TEXT", "📊 Ouvrir le Dashboard")
TG_DASHBOARD_URL = os.getenv("TG_DASHBOARD_URL", "https://tradingview-gd03.onrender.com/trades")

# Vector icons & throttle
VECTOR_UP_ICON = "🟩"
VECTOR_DN_ICON = "🟥"
VECTOR_GLOBAL_GAP_SEC = int(os.getenv("VECTOR_GLOBAL_GAP_SEC", "5"))  # max 1 vector / 5s global

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

# Schéma
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
    # réindex de sûreté
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

# Durée lisible (ex: 1h10, 23 min 10 s)
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
# =========================
# Telegram
# =========================
_last_tg_sent: Dict[str, float] = {}
_last_altseason_notify_ts: float = 0.0

# Throttle global
_last_global_send_ts: float = 0.0
_send_times_window = deque()  # timestamps des envois < 60s
_last_vector_flush_ts: float = 0.0

def _create_dashboard_button() -> Optional[dict]:
    if not TG_BUTTONS or not TG_DASHBOARD_URL:
        return None
    return {
        "inline_keyboard": [[
            {"text": TG_BUTTON_TEXT, "url": TG_DASHBOARD_URL}
        ]]
    }

async def _respect_rate_limits():
    """Respecte les limites: délai mini + plafond / minute."""
    global _last_global_send_ts, _send_times_window

    now = time.time()

    # fenêtre glissante 60s
    while _send_times_window and now - _send_times_window[0] > 60:
        _send_times_window.popleft()

    if len(_send_times_window) >= TG_PER_MIN_LIMIT:
        sleep_for = 60 - (now - _send_times_window[0]) + 0.2
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    # délai minimal inter-messages
    delta = now - _last_global_send_ts
    if delta < TG_MIN_DELAY_SEC:
        await asyncio.sleep(TG_MIN_DELAY_SEC - delta)

def _record_sent():
    global _last_global_send_ts, _send_times_window
    ts = time.time()
    _last_global_send_ts = ts
    _send_times_window.append(ts)

async def tg_send_text(text: str, disable_web_page_preview: bool = True, key: Optional[str] = None,
                       reply_markup: Optional[dict] = None, pin: bool = False) -> Dict[str, Any]:
    if not TELEGRAM_ENABLED:
        return {"ok": False, "reason": "telegram disabled"}

    k = key or "default"
    # anti-spam par clé (en plus du global)
    now_ts = time.time()
    last = _last_tg_sent.get(k, 0.0)
    if now_ts - last < TG_MIN_DELAY_SEC:
        logger.warning("Telegram send skipped due to per-key cooldown")
        return {"ok": False, "reason": "cooldown"}
    _last_tg_sent[k] = now_ts

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    await _respect_rate_limits()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            # Gestion 429 — on lit retry_after & on retente 1 fois
            if r.status_code == 429:
                try:
                    j = r.json()
                    ra = float(j.get("parameters", {}).get("retry_after", 30))
                except Exception:
                    ra = 30.0
                logger.warning(f"Telegram 429: retry_after={ra:.1f}s")
                await asyncio.sleep(ra + 0.5)
                # retente une fois
                await _respect_rate_limits()
                r = await client.post(url, json=payload)

            r.raise_for_status()
            data = r.json()
            logger.info(f"Telegram sent: {text[:80]}...")

            _record_sent()

            if pin and TELEGRAM_PIN_ALTSEASON and data.get("ok"):
                try:
                    message_id = data["result"]["message_id"]
                    pin_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
                    await client.post(pin_url, json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "message_id": message_id,
                        "disable_notification": True
                    })
                except Exception as e:
                    logger.warning(f"Pin message failed: {e}")

            return {"ok": True, "result": data}
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return {"ok": False, "reason": str(e)}

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
    except Exception:
        return None

def format_vector_message(symbol: str, tf_label: str, direction: str, price: Any, note: Optional[str] = None) -> str:
    icon = VECTOR_UP_ICON if (direction or "").upper() == "UP" else VECTOR_DN_ICON
    n = f" — {note}" if note else ""
    return f"{icon} Vector Candle {direction.upper()} | <b>{symbol}</b> <i>{tf_label}</i> @ <code>{price}</code>{n}"
# =========================
# Confiance & messages enrichis
# =========================
def compute_altseason_snapshot() -> dict:
    t24 = ms_ago(24*60)

    # A) LONG ratio
    row = db_query("""
        SELECT
          SUM(CASE WHEN side='LONG' THEN 1 ELSE 0 END) AS long_n,
          SUM(CASE WHEN side='SHORT' THEN 1 ELSE 0 END) AS short_n
        FROM events
        WHERE type='ENTRY' AND time>=?
    """, (t24,))
    long_n = (row[0]["long_n"] if row else 0) or 0
    short_n = (row[0]["short_n"] if row else 0) or 0

    def _pct(x, y):
        try:
            x = float(x or 0); y = float(y or 0)
            return 0.0 if y == 0 else 100.0 * x / y
        except Exception:
            return 0.0

    A = _pct(long_n, long_n + short_n)

    # B) TP vs SL (favorise LONG si side présent)
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

    # C) Breadth
    row = db_query("""
      SELECT COUNT(DISTINCT symbol) AS sym_gain FROM events
      WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?
    """, (t24,))
    sym_gain = (row[0]["sym_gain"] if row else 0) or 0
    C = float(min(100.0, sym_gain * 2.0))

    # D) Momentum (90 min / 24h)
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

def build_confidence_line(payload: dict) -> str:
    """
    Génère un texte explicatif dynamique de la confiance basé sur:
    - R/R calculé (si entry, sl, tp1 présents)
    - Altseason snapshot (momentum, breadth, ratio long)
    - Leverage (faible/moyen/élevé)
    """
    entry = payload.get("entry"); sl = payload.get("sl"); tp1 = payload.get("tp1")
    rr = _calc_rr(entry, sl, tp1)
    alt = compute_altseason_snapshot()
    lev = (payload.get("leverage") or payload.get("lev_reco") or "").strip()

    factors = []
    if rr is not None:
        factors.append(f"R/R {rr}")
    factors.append(f"Momentum {alt['signals']['recent_entries_ratio']}%")
    factors.append(f"Breadth {alt['signals']['breadth_symbols']} sym")
    factors.append(f"Bias LONG {alt['signals']['long_ratio']}%")
    if lev:
        try:
            lev_f = float(str(lev).lower().replace("x","").replace("cross","").strip())
            lev_txt = "lev élevé" if lev_f >= 15 else ("lev moyen" if lev_f >= 7 else "lev faible")
        except Exception:
            lev_txt = lev
        factors.append(lev_txt)

    conf = payload.get("confidence")
    # Si la confiance n'est pas fournie, on la déduit grossièrement d'un mix (RR, momentum, breadth)
    if conf is None:
        base = 50
        if rr is not None:
            base += max(min((rr - 1.0) * 10, 20), -10)  # RR 2≈ +10, RR 3≈ +20
        base += max(min((alt["signals"]["recent_entries_ratio"] - 50) * 0.3, 15), -15)
        base += max(min((alt["signals"]["breadth_symbols"] - 10) * 0.7, 15), -10)
        conf = int(max(5, min(95, round(base))))
        payload["confidence"] = conf  # on enrichit pour l'affichage

    return f"🧠 Confiance: {conf}% — basé sur " + ", ".join(factors)

# (1) ENTRY — format FR + R/R + temps écoulé
def format_entry_announcement(payload: dict) -> str:
    symbol   = payload.get("symbol", "")
    tf_lbl   = _fmt_tf_label(payload.get("tf"), payload.get("tf_label"))
    side_i   = _fmt_side(payload.get("side"))
    entry    = payload.get("entry")
    tp1      = payload.get("tp1")
    tp2      = payload.get("tp2")
    tp3      = payload.get("tp3")
    sl       = payload.get("sl")
    leverage = payload.get("leverage") or payload.get("lev_reco") or ""
    note     = (payload.get("note") or "").strip()

    rr = _calc_rr(entry, sl, tp1)
    rr_text = f" (R/R: {rr:.2f})" if rr is not None else ""

    lines = []
    if tp1 is not None: lines.append(f"🎯 TP1: {tp1}{rr_text}")
    if tp2 is not None: lines.append(f"🎯 TP2: {tp2}")
    if tp3 is not None: lines.append(f"🎯 TP3: {tp3}")
    if sl  is not None: lines.append(f"❌ SL: {sl}")

    conf_line = build_confidence_line(payload)
    tip_line = "💡 Astuce: après TP1, placez SL au BE." if tp1 is not None else ""

    # Temps écoulé depuis l'entry (0 s au moment de l'entry)
    t_entry = payload.get("time") or now_ms()
    elapsed = max(0, (now_ms() - int(t_entry)))
    elapsed_line = f"⏱ Temps écoulé : {human_duration_verbose(elapsed)}"

    msg = [
        f"📩 {symbol} {tf_lbl}",
        f"{side_i['emoji']} {side_i['label']} Entry: {entry}" if entry is not None else f"{side_i['emoji']} {side_i['label']}",
        f"💡Leverage: {leverage}" if leverage else "",
        *lines,
        conf_line,
        tip_line,
        elapsed_line,
    ]
    if note:
        msg.append(f"📝 {note}")
    return "\n".join([m for m in msg if m])

# (2)(3)(4) TP/SL/CLOSE — avec temps écoulé
def format_event_announcement(etype: str, payload: dict, duration_ms: Optional[int]) -> str:
    symbol = payload.get("symbol", "")
    tf_lbl = _fmt_tf_label(payload.get("tf"), payload.get("tf_label"))
    side_i = _fmt_side(payload.get("side"))
    base   = f"{symbol} {tf_lbl}"
    d_txt  = f"⏱ Temps écoulé : {human_duration_verbose(duration_ms)}" if duration_ms is not None else ""

    if etype in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        tick = {"TP1_HIT": "TP1", "TP2_HIT": "TP2", "TP3_HIT": "TP3"}[etype]
        return f"✅ {tick} atteint — {base}\n{side_i['label'].title()}\n{d_txt}"

    if etype == "SL_HIT":
        return f"🛑 SL touché — {base}\n{side_i['label'].title()}\n{d_txt}"

    if etype == "CLOSE":
        note = payload.get("note") or ""
        x = f"📪 Trade clôturé — {base}\n{side_i['emoji']} {side_i['label']}"
        if note:
            x += f"\n📝 {note}"
        if d_txt:
            x += f"\n{d_txt}"
        return x

    return f"ℹ️ {etype} — {base}" + (f"\n{d_txt}" if d_txt else "")
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
# Save Event
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

def get_entry_time_for_trade(trade_id: Optional[str]) -> Optional[int]:
    if not trade_id:
        return None
    r = db_query("""
        SELECT MIN(time) AS t FROM events
        WHERE trade_id=? AND type='ENTRY'
    """, (trade_id,))
    if r and r[0]["t"] is not None:
        return int(r[0]["t"])
    return None

# =========================
# Webhook
# =========================
@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    secret = payload.get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")

    etype = payload.get("type")
    symbol = payload.get("symbol")
    tf = payload.get("tf")
    if not etype or not symbol:
        raise HTTPException(422, "Missing type or symbol")

    # 1) sauver
    trade_id = save_event(payload)

    # 2) notifier Telegram
    try:
        if TELEGRAM_ENABLED:
            key = payload.get("trade_id") or f"{etype}:{symbol}"

            if etype == "VECTOR_CANDLE":
                # Throttle global des vectors
                global _last_vector_flush_ts
                now_sec = time.time()
                if now_sec - _last_vector_flush_ts < VECTOR_GLOBAL_GAP_SEC:
                    logger.info("Skip VECTOR_CANDLE by global throttle")
                else:
                    _last_vector_flush_ts = now_sec
                    txt = format_vector_message(
                        symbol=symbol,
                        tf_label=payload.get("tf_label") or tf_to_label(tf),
                        direction=(payload.get("direction") or ""),
                        price=payload.get("price"),
                        note=payload.get("note"),
                    )
                    await tg_send_text(txt, key=key)

            elif etype == "ENTRY":
                txt = format_entry_announcement(payload)
                await tg_send_text(txt, key=key)

            elif etype in {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"}:
                hit_time = payload.get("time") or now_ms()
                entry_t  = get_entry_time_for_trade(payload.get("trade_id"))
                duration = (hit_time - entry_t) if entry_t is not None else None
                txt = format_event_announcement(etype, payload, duration)
                await tg_send_text(txt, key=key)

        # 3) altseason auto-notify opportuniste (après un event)
        await maybe_altseason_autonotify()

    except Exception as e:
        logger.warning(f"Telegram send skipped due to cooldown or error: {e}")

    return JSONResponse({"ok": True, "trade_id": trade_id})

async def maybe_altseason_autonotify():
    """Envoie l'alerte altseason auto (épinglée) si seuils OK + cooldown."""
    global _last_altseason_notify_ts
    if not ALTSEASON_AUTONOTIFY or not TELEGRAM_ENABLED:
        return

    alt = compute_altseason_snapshot()
    greens = alt["signals"]["breadth_symbols"]
    nowt = time.time()
    if greens < ALT_GREENS_REQUIRED or alt["score"] < 50:
        return
    if (nowt - _last_altseason_notify_ts) < (ALTSEASON_NOTIFY_MIN_GAP_MIN * 60):
        return

    emoji = "🟢" if alt["score"] >= 75 else "🟡"
    msg = f"""🚨 <b>Alerte Altseason Automatique</b> {emoji}

📊 <b>Score: {alt['score']}/100</b>
📈 Status: <b>{alt['label']}</b>

🔥 <b>Signaux détectés</b>:
• Ratio LONG: {alt['signals']['long_ratio']}%
• TP vs SL: {alt['signals']['tp_vs_sl']}%
• Breadth: {alt['signals']['breadth_symbols']} symboles
• Momentum: {alt['signals']['recent_entries_ratio']}%

⚡ <b>{greens} symboles</b> avec TP atteints (seuil: {ALT_GREENS_REQUIRED})

<i>Notification automatique activée</i>"""

    reply_markup = _create_dashboard_button()
    res = await tg_send_text(msg, key="altseason", reply_markup=reply_markup, pin=True)
    if res.get("ok"):
        _last_altseason_notify_ts = nowt
# =========================
# Helpers /trades : statut, outcome, annulation
# =========================
def _latest_entry_for_trade(trade_id: str) -> Optional[dict]:
    r = db_query("""
      SELECT * FROM events
      WHERE trade_id=? AND type='ENTRY'
      ORDER BY time DESC LIMIT 1
    """, (trade_id,))
    return r[0] if r else None

def _has_hit_map(trade_id: str) -> Dict[str, bool]:
    hits = db_query("""
      SELECT type, MIN(time) AS t FROM events
      WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSE')
      GROUP BY type
    """, (trade_id,))
    return {h["type"]: True for h in hits}

def _first_outcome(trade_id: str) -> Optional[str]:
    rows = db_query("""
      SELECT type, time FROM events
      WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT')
      ORDER BY time ASC LIMIT 1
    """, (trade_id,))
    if not rows: return None
    t = rows[0]["type"]
    return "TP" if t.startswith("TP") else ("SL" if t == "SL_HIT" else None)

def _cancelled_by_opposite(entry_row: dict) -> bool:
    symbol = entry_row.get("symbol"); tf = entry_row.get("tf")
    side = (entry_row.get("side") or "").upper(); t = int(entry_row.get("time") or 0)
    if not symbol or tf is None or side not in ("LONG", "SHORT"): return False
    opposite = "SHORT" if side == "LONG" else "LONG"
    r = db_query("""
      SELECT 1 FROM events
      WHERE type='ENTRY' AND symbol=? AND tf=? AND time>? AND UPPER(COALESCE(side,''))=?
      LIMIT 1
    """, (symbol, str(tf), t, opposite))
    return bool(r)

# =========================
# Build rows + KPIs
# =========================
def build_trade_rows(limit=300):
    base = db_query("""
      SELECT e.trade_id, MAX(e.time) AS t_entry
      FROM events e
      WHERE e.type='ENTRY'
      GROUP BY e.trade_id
      ORDER BY t_entry DESC
      LIMIT ?
    """, (limit,))
    rows: List[dict] = []
    for item in base:
        e = _latest_entry_for_trade(item["trade_id"])
        if not e: continue

        tf_label = (e.get("tf_label") or tf_to_label(e.get("tf")))
        hm = _has_hit_map(e["trade_id"])
        tp1_hit = bool(hm.get("TP1_HIT")); tp2_hit = bool(hm.get("TP2_HIT")); tp3_hit = bool(hm.get("TP3_HIT"))
        sl_hit  = bool(hm.get("SL_HIT"));  closed  = bool(hm.get("CLOSE"))

        cancelled = _cancelled_by_opposite(e) and not (tp1_hit or tp2_hit or tp3_hit or sl_hit)
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
            "tf_label": tf_label,
            "side": e["side"],
            "entry": e["entry"],
            "tp1": e["tp1"], "tp2": e["tp2"], "tp3": e["tp3"],
            "sl": e["sl"],
            "tp1_hit": tp1_hit, "tp2_hit": tp2_hit, "tp3_hit": tp3_hit,
            "sl_hit": sl_hit,
            "row_state": state,
            "t_entry": item["t_entry"],
        })
    return rows

def compute_kpis(rows: List[dict]) -> Dict[str, Any]:
    t24 = ms_ago(24*60)

    total_trades = db_query(
        "SELECT COUNT(DISTINCT trade_id) AS n FROM events WHERE type='ENTRY' AND time>=?", (t24,)
    )[0]["n"] or 0
    tp_hits = db_query(
        "SELECT COUNT(*) AS n FROM events WHERE type IN ('TP1_HIT','TP2_HIT','TP3_HIT') AND time>=?", (t24,)
    )[0]["n"] or 0

    trade_ids = [r["trade_id"] for r in db_query(
        "SELECT DISTINCT trade_id FROM events WHERE type='ENTRY' AND time>=?", (t24,)
    )]
    wins = 0; losses = 0
    for tid in trade_ids:
        o = _first_outcome(tid)
        if o == "TP": wins += 1
        elif o == "SL": losses += 1
    winrate = (wins / max(1, (wins + losses))) * 100.0

    active = sum(1 for r in rows if r["row_state"] == "normal")

    return {
        "total_trades": int(total_trades),
        "active_trades": int(active),
        "tp_hits": int(tp_hits),
        "winrate": round(winrate, 1),
    }

# =========================
# /trades — UI
# =========================
@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    try:
        ensure_trades_schema()
    except Exception:
        pass

    alt = compute_altseason_snapshot()
    rows = build_trade_rows(limit=300)
    kpi = compute_kpis(rows)

    css = """
    <style>
      :root{
        --bg:#0b0f14; --panel:#0f1622; --card:#121b2a; --border:#1f2a3a; --txt:#e6edf3; --muted:#a7b3c6;
        --green:#22c55e; --red:#ef4444; --amber:#f59e0b;
        --chip:#0f172a; --chip-b:#253143;
      }
      *{box-sizing:border-box}
      body{margin:0;background:var(--bg);color:var(--txt);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif}
      .wrap{max-width:1200px;margin:24px auto;padding:0 16px}

      .grid{display:grid;gap:16px}
      .g-2{grid-template-columns:1fr 1fr}
      .g-4{grid-template-columns:repeat(4,1fr)}
      @media(max-width:1000px){.g-4{grid-template-columns:repeat(2,1fr)}}

      .panel{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:16px 18px}
      .muted{color:var(--muted)}
      .kpi{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px 16px}
      .kpi .t{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px}
      .kpi .v{margin-top:6px;font-size:22px;font-weight:700}
      .score{display:flex;align-items:center;justify-content:center;width:120px;height:120px;border-radius:999px;background:#f6c453;color:#000;font-size:28px;font-weight:800;margin:0 auto;border:6px solid #7a4c00}
      .score small{display:block;font-size:11px;font-weight:600}

      table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}
      thead th{font-size:12px;color:var(--muted);text-align:left;padding:10px;border-bottom:1px solid var(--border)}
      tbody td{padding:10px;border-bottom:1px solid #162032;font-size:14px;vertical-align:middle}
      tbody tr:hover{background:#0e1520}
      .accent{width:4px}
      .row-tp .accent{background:var(--green)}
      .row-sl .accent{background:var(--red)}
      .row-cancel .accent{background:var(--amber)}
      .row-normal .accent{background:transparent}

      .pill{padding:4px 10px;border-radius:999px;border:1px solid var(--chip-b);background:var(--chip);color:#cbd5e1;font-size:12px;display:inline-flex;gap:6px;align-items:center}
      .pill.ok{background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.45);color:#86efac}
      .pill.sl{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.5);color:#fca5a5}
      .pill.side-long{background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.45);color:#86efac}
      .pill.side-short{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.5);color:#fca5a5}
      .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace}
      .actions a{opacity:.9;text-decoration:none;margin-right:8px}
    </style>
    """

    alt_html = f"""
      <div class="panel">
        <div class="grid g-2" style="align-items:center">
          <div>
            <h2 style="margin:0 0 4px 0">Indicateurs Altseason</h2>
            <div class="muted">Fenêtre: {alt['window_minutes']} min — 4 signaux clés</div>
          </div>
          <div class="score">{alt['score']}<small>/ 100</small></div>
        </div>
        <div class="grid g-4" style="margin-top:14px">
          <div class="kpi"><div class="t">📈 LONG Ratio</div><div class="v">{alt['signals']['long_ratio']}%</div></div>
          <div class="kpi"><div class="t">🎯 TP vs SL</div><div class="v">{alt['signals']['tp_vs_sl']}%</div></div>
          <div class="kpi"><div class="t">🪄 Breadth</div><div class="v">{alt['signals']['breadth_symbols']} sym</div></div>
          <div class="kpi"><div class="t">⚡ Momentum</div><div class="v">{alt['signals']['recent_entries_ratio']}%</div></div>
        </div>
      </div>
    """

    mini_html = f"""
      <div class="grid g-4" style="margin-top:16px">
        <div class="kpi"><div class="t">📊 Total Trades</div><div class="v">{kpi['total_trades']}</div></div>
        <div class="kpi"><div class="t">⚡ Trades Actifs</div><div class="v">{kpi['active_trades']}</div></div>
        <div class="kpi"><div class="t">✅ TP Atteints</div><div class="v">{kpi['tp_hits']}</div></div>
        <div class="kpi"><div class="t">🎯 Win Rate</div><div class="v">{kpi['winrate']}%</div></div>
      </div>
    """

    def tp_cell(val, hit):
        if val is None:
            return '<span class="pill muted">—</span>'
        klass = "pill ok" if hit else "pill"
        icon = "✅" if hit else "🎯"
        return f'<span class="{klass}">{icon}&nbsp;{val}</span>'

    def sl_cell(val, sl_hit):
        if val is None:
            return '<span class="pill muted">—</span>'
        klass = "pill sl" if sl_hit else "pill"
        icon = "⛔" if sl_hit else "❌"
        return f'<span class="{klass}">{icon}&nbsp;{val}</span>'

    def side_cell(side):
        s = (side or "").upper()
        if s == "LONG":
            return '<span class="pill side-long">✅ LONG</span>'
        if s == "SHORT":
            return '<span class="pill side-short">🚫 SHORT</span>'
        return '<span class="pill">—</span>'

    def row_class(state: str) -> str:
        return {
            "tp": "row-tp",
            "sl": "row-sl",
            "cancel": "row-cancel",
            "normal": "row-normal",
        }.get(state or "normal", "row-normal")

    body_rows = []
    for r in rows:
        body_rows.append(f"""
          <tr class="{row_class(r.get('row_state'))}">
            <td class="accent"></td>
            <td>{r['symbol']}</td>
            <td><span class="pill">{r['tf_label']}</span></td>
            <td>{side_cell(r.get('side'))}</td>
            <td class="mono">{'' if r.get('entry') is None else r.get('entry')}</td>
            <td>{tp_cell(r.get('tp1'), r.get('tp1_hit'))}</td>
            <td>{tp_cell(r.get('tp2'), r.get('tp2_hit'))}</td>
            <td>{tp_cell(r.get('tp3'), r.get('tp3_hit'))}</td>
            <td>{sl_cell(r.get('sl'), r.get('sl_hit'))}</td>
            <td class="actions"><a href="#" title="Edit">🖊️</a><a href="#" title="Delete">🗑️</a></td>
          </tr>
        """)

    html = f"""<!doctype html>
    <html lang="fr"><head><meta charset="utf-8"><title>Trades</title>{css}</head>
    <body>
      <div class="wrap">
        {alt_html}
        {mini_html}
        <div class="panel" style="margin-top:16px">
          <h3 style="margin:0 0 10px 0">Historique des Trades</h3>
          <table>
            <thead>
              <tr>
                <th class="accent"></th>
                <th>Symbole</th>
                <th>TF</th>
                <th>Side</th>
                <th>Entry</th>
                <th>TP1</th>
                <th>TP2</th>
                <th>TP3</th>
                <th>SL</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {''.join(body_rows) if body_rows else '<tr><td class="accent"></td><td colspan="9" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </body></html>"""
    return HTMLResponse(content=html)

# =========================
# Lancement local (optionnel)
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
