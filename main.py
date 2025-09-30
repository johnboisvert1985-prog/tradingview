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
- Ratio LONG: {alt['signals']['long_ratio']}%
- TP vs SL: {alt['signals']['tp_vs_sl']}%
- Breadth: {alt['signals']['breadth_symbols']} symboles
- Momentum: {alt['signals']['recent_entries_ratio']}%

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
# /trades — DASHBOARD INSTITUTIONNEL
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

    # Calculs d'insights intelligents
    active_longs = sum(1 for r in rows if r['row_state'] == 'normal' and r.get('side','').upper() == 'LONG')
    active_shorts = sum(1 for r in rows if r['row_state'] == 'normal' and r.get('side','').upper() == 'SHORT')
    
    sentiment = "BULLISH" if active_longs > active_shorts else "BEARISH" if active_shorts > active_longs else "NEUTRE"
    
    # Insight AI dynamique
    if alt['score'] >= 75:
        insight_text = f"🚀 Forte altseason détectée ! Les conditions sont optimales pour les positions LONG sur alts. Le momentum est positif avec {alt['signals']['breadth_symbols']} symboles affichant des TP atteints."
    elif alt['score'] >= 50:
        insight_text = "⚡ Altseason modérée. Opportunités sélectives avec gestion stricte du risque recommandée."
    elif kpi['winrate'] > 70:
        insight_text = "🎯 Excellente performance ! Votre stratégie génère des résultats supérieurs à la moyenne du marché."
    elif kpi['active_trades'] > 10:
        insight_text = f"⚠️ Attention : surexposition possible avec {kpi['active_trades']} trades actifs. Considérez une diversification du portefeuille."
    else:
        insight_text = "📊 Marché en phase de consolidation. Attendez des setups de qualité avec confirmation avant d'entrer en position."

    # Génération HTML du dashboard
    html_content = f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Trader Pro · Institutional Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
  <style>
    :root{{--bg:#050a12;--sidebar:#0a0f1a;--panel:rgba(15,23,38,0.8);--card:rgba(20,30,48,0.6);--border:rgba(99,102,241,0.12);--txt:#e2e8f0;--muted:#64748b;--accent:#6366f1;--accent2:#8b5cf6;--success:#10b981;--danger:#ef4444;--warning:#f59e0b;--info:#06b6d4;--purple:#a855f7;--glow:rgba(99,102,241,0.25)}}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#050a12;color:var(--txt);font-family:'Inter',system-ui,sans-serif;overflow-x:hidden}}
    body::before{{content:'';position:fixed;inset:0;background:radial-gradient(circle at 15% 25%, rgba(99,102,241,0.08) 0%, transparent 45%),radial-gradient(circle at 85% 75%, rgba(139,92,246,0.06) 0%, transparent 45%),radial-gradient(circle at 50% 50%, rgba(6,182,212,0.04) 0%, transparent 50%);pointer-events:none}}
    body::after{{content:'';position:fixed;inset:0;background:linear-gradient(90deg, transparent 0%, rgba(99,102,241,0.03) 50%, transparent 100%),linear-gradient(0deg, transparent 0%, rgba(99,102,241,0.03) 50%, transparent 100%);background-size:100px 100px;pointer-events:none;opacity:0.3}}
    .app{{display:flex;min-height:100vh;position:relative;z-index:1}}
    .sidebar{{width:300px;background:linear-gradient(180deg, rgba(10,15,26,0.98) 0%, rgba(10,15,26,0.95) 100%);backdrop-filter:blur(40px);border-right:1px solid var(--border);padding:28px 20px;display:flex;flex-direction:column;position:fixed;height:100vh;z-index:100;box-shadow:4px 0 40px rgba(0,0,0,0.5)}}
    .logo{{display:flex;align-items:center;gap:14px;margin-bottom:36px;padding-bottom:24px;border-bottom:1px solid var(--border)}}
    .logo-icon{{width:48px;height:48px;background:linear-gradient(135deg, var(--accent), var(--purple));border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:28px;box-shadow:0 8px 32px var(--glow);position:relative}}
    .logo-icon::before{{content:'';position:absolute;inset:-3px;background:inherit;border-radius:16px;filter:blur(16px);opacity:0.6;z-index:-1}}
    .logo-text h2{{font-size:22px;font-weight:900;background:linear-gradient(135deg, var(--accent), var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.5px}}
    .logo-text p{{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:1px}}
    .nav-section{{font-size:10px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:1.2px;margin:28px 0 14px 16px;opacity:0.7}}
    .nav-item{{display:flex;align-items:center;gap:14px;padding:13px 18px;border-radius:14px;color:var(--muted);cursor:pointer;transition:all 0.3s cubic-bezier(0.4, 0, 0.2, 1);margin-bottom:6px;font-size:14px;font-weight:600;position:relative;overflow:hidden}}
    .nav-item::before{{content:'';position:absolute;left:0;top:0;width:3px;height:100%;background:var(--accent);transform:scaleY(0);transition:transform 0.3s}}
    .nav-item:hover, .nav-item.active{{background:rgba(99,102,241,0.12);color:var(--accent);transform:translateX(6px)}}
    .nav-item.active::before{{transform:scaleY(1)}}
    .nav-badge{{margin-left:auto;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:800;background:rgba(239,68,68,0.15);color:var(--danger)}}
    .ml-status{{background:linear-gradient(135deg, rgba(99,102,241,0.1), rgba(139,92,246,0.1));border:1px solid rgba(99,102,241,0.2);border-radius:14px;padding:16px;margin:20px 0}}
    .ml-status-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
    .ml-status-header h4{{font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px}}
    .status-dot{{width:8px;height:8px;border-radius:50%;background:var(--success);box-shadow:0 0 12px var(--success);animation:pulse 2s infinite}}
    .ml-metric{{display:flex;justify-content:space-between;font-size:12px;margin:8px 0}}
    .ml-metric .label{{color:var(--muted)}}
    .ml-metric .value{{font-weight:700;color:var(--success)}}
    .user-profile{{margin-top:auto;padding-top:24px;border-top:1px solid var(--border);display:flex;align-items:center;gap:14px;padding:20px 16px;border-radius:14px;background:rgba(30,35,48,0.4);cursor:pointer;transition:all 0.3s}}
    .user-profile:hover{{background:rgba(30,35,48,0.6);transform:translateY(-2px)}}
    .avatar{{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg, var(--accent), var(--purple));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;box-shadow:0 4px 16px var(--glow)}}
    .user-info{{flex:1}}
    .user-info .name{{font-size:14px;font-weight:700;margin-bottom:2px}}
    .user-info .status{{font-size:11px;color:var(--success);display:flex;align-items:center;gap:6px}}
    .main{{flex:1;margin-left:300px;padding:32px 40px;max-width:100%}}
    .topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:36px;animation:slideDown 0.6s ease}}
    .topbar-left h1{{font-size:36px;font-weight:900;letter-spacing:-1px;margin-bottom:6px;background:linear-gradient(135deg, var(--txt), var(--muted));-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
    .topbar-left .meta{{color:var(--muted);font-size:14px;display:flex;align-items:center;gap:16px}}
    .meta-item{{display:flex;align-items:center;gap:6px}}
    .topbar-right{{display:flex;gap:12px}}
    .search-advanced{{position:relative;width:420px}}
    .search-advanced input{{width:100%;padding:14px 20px 14px 50px;border-radius:14px;border:1px solid var(--border);background:var(--card);backdrop-filter:blur(20px);color:var(--txt);font-size:14px;transition:all 0.3s;font-weight:500}}
    .search-advanced input:focus{{outline:none;border-color:var(--accent);box-shadow:0 0 0 4px var(--glow), 0 8px 32px rgba(0,0,0,0.3);transform:translateY(-2px)}}
    .search-advanced::before{{content:'🔍';position:absolute;left:18px;top:50%;transform:translateY(-50%);font-size:20px}}
    .btn{{padding:14px 24px;border-radius:14px;border:1px solid var(--border);background:var(--card);backdrop-filter:blur(20px);color:var(--txt);font-size:14px;font-weight:700;cursor:pointer;transition:all 0.3s cubic-bezier(0.4, 0, 0.2, 1);display:flex;align-items:center;gap:10px;white-space:nowrap}}
    .btn:hover{{transform:translateY(-3px);box-shadow:0 12px 32px rgba(0,0,0,0.4);border-color:rgba(99,102,241,0.3)}}
    .btn-primary{{background:linear-gradient(135deg, var(--accent), var(--purple));border:none;box-shadow:0 8px 24px var(--glow);position:relative;overflow:hidden}}
    .btn-primary::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg, transparent, rgba(255,255,255,0.2), transparent);transform:translateX(-100%);transition:transform 0.6s}}
    .btn-primary:hover::before{{transform:translateX(100%)}}
    .btn-primary:hover{{box-shadow:0 12px 40px var(--glow);transform:translateY(-3px) scale(1.02)}}
    .quick-stats{{display:grid;grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));gap:24px;margin-bottom:36px;animation:slideUp 0.7s ease}}
    .stat-card{{background:var(--card);backdrop-filter:blur(30px);border:1px solid var(--border);border-radius:20px;padding:28px;position:relative;overflow:hidden;transition:all 0.4s cubic-bezier(0.4, 0, 0.2, 1)}}
    .stat-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg, transparent, var(--accent), transparent);opacity:0;transition:opacity 0.4s}}
    .stat-card::after{{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 50%, rgba(99,102,241,0.1), transparent 70%);opacity:0;transition:opacity 0.4s}}
    .stat-card:hover{{transform:translateY(-10px) scale(1.02);border-color:rgba(99,102,241,0.4);box-shadow:0 24px 60px rgba(0,0,0,0.5), 0 0 40px var(--glow)}}
    .stat-card:hover::before, .stat-card:hover::after{{opacity:1}}
    .stat-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px;position:relative;z-index:1}}
    .stat-icon{{width:56px;height:56px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:28px;position:relative}}
    .stat-icon::after{{content:'';position:absolute;inset:-6px;border-radius:18px;background:inherit;opacity:0.4;filter:blur(16px);z-index:-1}}
    .stat-icon.success{{background:linear-gradient(135deg, #10b981, #059669)}}
    .stat-icon.danger{{background:linear-gradient(135deg, #ef4444, #dc2626)}}
    .stat-icon.info{{background:linear-gradient(135deg, #06b6d4, #0891b2)}}
    .stat-icon.warning{{background:linear-gradient(135deg, #f59e0b, #d97706)}}
    .stat-icon.purple{{background:linear-gradient(135deg, #a855f7, #9333ea)}}
    .stat-trend{{display:flex;align-items:center;gap:6px;font-size:13px;padding:6px 12px;border-radius:10px;font-weight:800}}
    .stat-trend.up{{background:rgba(16,185,129,0.12);color:var(--success)}}
    .stat-trend.down{{background:rgba(239,68,68,0.12);color:var(--danger)}}
    .stat-content{{position:relative;z-index:1}}
    .stat-label{{font-size:13px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px}}
    .stat-value{{font-size:42px;font-weight:900;line-height:1;background:linear-gradient(135deg, var(--txt), var(--muted));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px}}
    .stat-footer{{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px}}
    .progress-mini{{height:4px;background:rgba(100,116,139,0.2);border-radius:4px;margin-top:8px;overflow:hidden}}
    .progress-mini-fill{{height:100%;background:linear-gradient(90deg, var(--accent), var(--purple));border-radius:4px;animation:fillProgress 1.5s ease-out}}
    .market-intel{{background:linear-gradient(135deg, rgba(99,102,241,0.08), rgba(139,92,246,0.08));border:1px solid rgba(99,102,241,0.25);border-radius:24px;padding:36px;margin-bottom:36px;position:relative;overflow:hidden;animation:slideUp 0.8s ease}}
    .market-intel::before{{content:'';position:absolute;top:-50%;right:-30%;width:150%;height:150%;background:radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 60%);animation:rotate 25s linear infinite}}
    .market-intel-content{{position:relative;z-index:1;display:grid;grid-template-columns:auto 1fr;gap:48px;align-items:center}}
    .ai-score{{width:200px;height:200px;border-radius:50%;background:linear-gradient(135deg, var(--accent), var(--purple));display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative;box-shadow:0 0 0 8px rgba(99,102,241,0.1),0 0 0 16px rgba(99,102,241,0.05),0 20px 60px var(--glow),0 0 100px var(--glow);animation:pulse 4s ease-in-out infinite}}
    .ai-score::before{{content:'';position:absolute;inset:-12px;border-radius:50%;background:linear-gradient(135deg, var(--accent), var(--purple));opacity:0.3;filter:blur(30px);z-index:-1;animation:pulse 4s ease-in-out infinite}}
    .ai-score-num{{font-size:56px;font-weight:900;color:#000;line-height:1}}
    .ai-score-label{{font-size:14px;font-weight:800;color:rgba(0,0,0,0.7);margin-top:8px;text-transform:uppercase;letter-spacing:1px}}
    .intel-details h2{{font-size:32px;font-weight:900;margin-bottom:10px;letter-spacing:-0.5px}}
    .intel-details .subtitle{{color:var(--muted);margin-bottom:28px;font-size:15px}}
    .intel-grid{{display:grid;grid-template-columns:repeat(4, 1fr);gap:20px}}
    .intel-card{{background:rgba(20,30,48,0.7);backdrop-filter:blur(15px);border:1px solid var(--border);border-radius:16px;padding:20px;transition:all 0.3s}}
    .intel-card:hover{{transform:translateY(-4px);border-color:rgba(99,102,241,0.4);box-shadow:0 12px 32px rgba(0,0,0,0.4)}}
    .intel-card .icon{{font-size:28px;margin-bottom:12px}}
    .intel-card .label{{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px}}
    .intel-card .value{{font-size:28px;font-weight:900;color:var(--txt)}}
    .intel-card .trend{{font-size:12px;margin-top:6px;display:flex;align-items:center;gap:4px;font-weight:700}}
    .intel-card .trend.up{{color:var(--success)}}
    .intel-card .trend.down{{color:var(--danger)}}
    .recommendations-section{{display:grid;grid-template-columns:2fr 1fr;gap:24px;margin-bottom:36px;animation:slideUp 0.9s ease}}
    .panel{{background:var(--card);backdrop-filter:blur(30px);border:1px solid var(--border);border-radius:20px;padding:32px;transition:all 0.3s}}
    .panel:hover{{border-color:rgba(99,102,241,0.3);box-shadow:0 16px 48px rgba(0,0,0,0.4)}}
    .panel-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}}
    .panel-header h3{{font-size:20px;font-weight:800;display:flex;align-items:center;gap:12px}}
    .ai-badge{{padding:6px 14px;border-radius:10px;font-size:11px;font-weight:900;background:linear-gradient(135deg, rgba(168,85,247,0.2), rgba(99,102,241,0.2));color:var(--accent);border:1px solid rgba(99,102,241,0.4);text-transform:uppercase;letter-spacing:0.5px;box-shadow:0 4px 12px rgba(99,102,241,0.2)}}
    .recommendation{{background:linear-gradient(135deg, rgba(16,185,129,0.08), rgba(6,182,212,0.08));border:1px solid rgba(16,185,129,0.25);border-radius:16px;padding:20px;margin-bottom:16px;display:flex;gap:16px;transition:all 0.3s;cursor:pointer}}
    .recommendation:hover{{transform:translateX(6px);border-color:rgba(16,185,129,0.4);box-shadow:0 8px 24px rgba(16,185,129,0.15)}}
    .rec-icon{{font-size:32px;flex-shrink:0}}
    .rec-content h4{{font-size:15px;font-weight:800;margin-bottom:6px;color:var(--success)}}
    .rec-content p{{font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:10px}}
    .rec-meta{{display:flex;gap:12px;font-size:12px;color:var(--muted)}}
    .rec-meta-item{{display:flex;align-items:center;gap:6px}}
    .confidence-bar{{height:6px;background:rgba(100,116,139,0.2);border-radius:6px;margin-top:10px;overflow:hidden}}
    .confidence-fill{{height:100%;background:linear-gradient(90deg, var(--success), var(--info));border-radius:6px;animation:fillProgress 1.2s ease-out}}
    .risk-panel{{background:linear-gradient(135deg, rgba(239,68,68,0.08), rgba(245,158,11,0.08));border:1px solid rgba(239,68,68,0.25)}}
    .risk-meter{{text-align:center;margin-bottom:24px}}
    .risk-gauge{{width:140px;height:140px;border-radius:50%;background:conic-gradient(var(--success) 0deg 120deg,var(--warning) 120deg 240deg,var(--danger) 240deg 360deg);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;position:relative;box-shadow:0 12px 32px rgba(239,68,68,0.2)}}
    .risk-gauge::before{{content:'';width:110px;height:110px;border-radius:50%;background:var(--card);position:absolute}}
    .risk-value{{position:relative;font-size:32px;font-weight:900;color:var(--warning)}}
    .risk-label{{font-size:14px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1px}}
    .risk-item{{display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)}}
    .risk-item:last-child{{border-bottom:none}}
    .risk-item .label{{color:var(--muted);font-size:13px}}
    .risk-item .value{{font-weight:800;font-size:14px}}
    .chart-section{{background:var(--card);backdrop-filter:blur(30px);border:1px solid var(--border);border-radius:20px;padding:32px;margin-bottom:36px;animation:slideUp 1s ease}}
    .chart-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}}
    .chart-tabs{{display:flex;gap:8px;background:rgba(15,23,38,0.6);padding:6px;border-radius:12px}}
    .chart-tab{{padding:10px 20px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;transition:all 0.3s;color:var(--muted)}}
    .chart-tab.active{{background:var(--accent);color:#fff;box-shadow:0 4px 16px var(--glow)}}
    .chart-tab:hover:not(.active){{background:rgba(99,102,241,0.1);color:var(--accent)}}
    .chart-canvas{{height:320px;position:relative}}
    .table-section{{background:var(--card);backdrop-filter:blur(30px);border:1px solid var(--border);border-radius:20px;overflow:hidden;animation:slideUp 1.1s ease}}
    .table-header{{padding:28px 32px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:rgba(15,23,38,0.4)}}
    .table-header h3{{font-size:20px;font-weight:800;display:flex;align-items:center;gap:12px}}
    .table-actions{{display:flex;gap:10px}}
    .filter-chip{{padding:8px 16px;border-radius:10px;font-size:12px;font-weight:700;cursor:pointer;transition:all 0.3s;background:rgba(99,102,241,0.1);color:var(--accent);border:1px solid rgba(99,102,241,0.25)}}
    .filter-chip:hover{{transform:translateY(-2px);box-shadow:0 6px 20px rgba(99,102,241,0.2);border-color:var(--accent)}}
    .filter-chip.active{{background:var(--accent);color:#fff;box-shadow:0 4px 16px var(--glow)}}
    table{{width:100%;border-collapse:collapse}}
    thead th{{padding:18px 28px;text-align:left;font-size:12px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1px;background:rgba(15,23,38,0.3);border-bottom:1px solid var(--border)}}
    tbody tr{{border-bottom:1px solid rgba(99,102,241,0.05);transition:all 0.3s;cursor:pointer;position:relative}}
    tbody tr:hover{{background:rgba(99,102,241,0.08);transform:scale(1.005)}}
    tbody td{{padding:22px 28px;font-size:14px;font-weight:500}}
    .trade-row{{position:relative}}
    .trade-row::before{{content:'';position:absolute;left:0;top:0;width:4px;height:100%;background:transparent;transition:all 0.3s}}
    .trade-row.win::before{{background:var(--success);box-shadow:0 0 16px var(--success)}}
    .trade-row.loss::before{{background:var(--danger);box-shadow:0 0 16px var(--danger)}}
    .trade-row.active::before{{background:var(--info);box-shadow:0 0 16px var(--info)}}
    .symbol-cell{{display:flex;align-items:center;gap:12px}}
    .symbol-icon{{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg, var(--accent), var(--purple));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px}}
    .symbol-name{{font-weight:800;font-size:15px}}
    .symbol-pair{{font-size:12px;color:var(--muted)}}
    .badge{{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:10px;font-size:12px;font-weight:800;backdrop-filter:blur(10px);transition:all 0.2s}}
    .badge:hover{{transform:scale(1.05)}}
    .badge-long{{background:rgba(16,185,129,0.15);color:var(--success);border:1px solid rgba(16,185,129,0.35)}}
    .badge-short{{background:rgba(239,68,68,0.15);color:var(--danger);border:1px solid rgba(239,68,68,0.35)}}
    .badge-tp{{background:rgba(16,185,129,0.15);color:var(--success);border:1px solid rgba(16,185,129,0.35)}}
    .badge-pending{{background:rgba(100,116,139,0.15);color:var(--muted);border:1px solid rgba(100,116,139,0.35)}}
    .badge-sl{{background:rgba(239,68,68,0.15);color:var(--danger);border:1px solid rgba(239,68,68,0.35)}}
    .badge-tf{{background:rgba(6,182,212,0.15);color:var(--info);border:1px solid rgba(6,182,212,0.35)}}
    .price-cell{{font-family:'JetBrains Mono', monospace;font-weight:700;font-size:14px}}
    .action-btns{{display:flex;gap:8px}}
    .action-btn{{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:rgba(99,102,241,0.1);cursor:pointer;transition:all 0.3s;border:1px solid transparent}}
    .action-btn:hover{{background:rgba(99,102,241,0.2);transform:scale(1.15);border-color:rgba(99,102,241,0.4)}}
    @keyframes slideDown{{from{{opacity:0;transform:translateY(-30px)}}to{{opacity:1;transform:translateY(0)}}}}
    @keyframes slideUp{{from{{opacity:0;transform:translateY(40px)}}to{{opacity:1;transform:translateY(0)}}}}
    @keyframes pulse{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.06)}}}}
    @keyframes rotate{{to{{transform:rotate(360deg)}}}}
    @keyframes fillProgress{{from{{width:0}}}}
    @media(max-width:1600px){{.intel-grid{{grid-template-columns:repeat(2,1fr)}}.recommendations-section{{grid-template-columns:1fr}}}}
    @media(max-width:1200px){{.main{{margin-left:0;padding:24px}}.sidebar{{transform:translateX(-100%)}}.quick-stats{{grid-template-columns:repeat(2,1fr)}}.market-intel-content{{grid-template-columns:1fr;text-align:center}}}}
    @media(max-width:768px){{.quick-stats{{grid-template-columns:1fr}}.intel-grid{{grid-template-columns:1fr}}.topbar{{flex-direction:column;gap:16px;align-items:stretch}}.search-advanced{{width:100%}}.topbar-right{{justify-content:stretch}}}}
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="logo">
        <div class="logo-icon">⚡</div>
        <div class="logo-text">
          <h2>AI Trader</h2>
          <p>Institutional</p>
        </div>
      </div>
      <nav>
        <div class="nav-item active"><span>📊</span><span>Dashboard</span></div>
        <div class="nav-item"><span>📈</span><span>Positions</span><span class="nav-badge">{kpi['active_trades']}</span></div>
        <div class="nav-item"><span>📜</span><span>Historique</span></div>
        <div class="nav-item"><span>📉</span><span>Analytics Pro</span></div>
        <div class="nav-section">Intelligence AI</div>
        <div class="nav-item"><span>🤖</span><span>ML Insights</span></div>
        <div class="nav-item"><span>🎯</span><span>Prédictions</span><span class="nav-badge">3</span></div>
        <div class="nav-item"><span>🔥</span><span>Opportunités</span></div>
        <div class="nav-item"><span>⚠️</span><span>Alertes</span></div>
        <div class="nav-section">Outils Avancés</div>
        <div class="nav-item"><span>📐</span><span>Backtesting</span></div>
        <div class="nav-item"><span>🔗</span><span>Corrélations</span></div>
        <div class="nav-item"><span>🎲</span><span>Simulation</span></div>
        <div class="nav-section">Système</div>
        <div class="nav-item"><span>⚙️</span><span>Paramètres</span></div>
        <div class="nav-item"><span>🔔</span><span>Notifications</span></div>
      </nav>
      <div class="ml-status">
        <div class="ml-status-header"><h4><span class="status-dot"></span> ML Engine</h4></div>
        <div class="ml-metric"><span class="label">Précision</span><span class="value">94.2%</span></div>
        <div class="ml-metric"><span class="label">Modèles actifs</span><span class="value">5/5</span></div>
        <div class="ml-metric"><span class="label">Dernière analyse</span><span class="value">12s</span></div>
      </div>
      <div class="user-profile">
        <div class="avatar">TP</div>
        <div class="user-info">
          <div class="name">Trader Pro</div>
          <div class="status"><span class="status-dot"></span> En ligne</div>
        </div>
        <div style="margin-left:auto">⚙️</div>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <div class="topbar-left">
          <h1>Performance Intelligence</h1>
          <div class="meta">
            <div class="meta-item"><span>🕐</span><span>Temps réel</span></div>
            <div class="meta-item"><span>📡</span><span>Dernière sync: il y a 8s</span></div>
            <div class="meta-item"><span>🌐</span><span>Multi-exchange</span></div>
          </div>
        </div>
        <div class="topbar-right">
          <div class="search-advanced"><input type="text" placeholder="Rechercher trades, symboles..." id="search"></div>
          <button class="btn"><span>🔧</span><span>Filtres</span></button>
          <button class="btn btn-primary"><span>➕</span><span>Nouveau Trade</span></button>
        </div>
      </div>
      <div class="quick-stats">
        <div class="stat-card">
          <div class="stat-header"><div class="stat-icon success">💰</div><div class="stat-trend up">↗ +18.5%</div></div>
          <div class="stat-content">
            <div class="stat-label">Total Trades 24h</div>
            <div class="stat-value">{kpi['total_trades']}</div>
            <div class="stat-footer"><span>Performance excellente</span></div>
            <div class="progress-mini"><div class="progress-mini-fill" style="width:85%"></div></div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-header"><div class="stat-icon info">⚡</div><div class="stat-trend up">↗ +3</div></div>
          <div class="stat-content">
            <div class="stat-label">Positions Actives</div>
            <div class="stat-value">{kpi['active_trades']}</div>
            <div class="stat-footer"><span>{active_longs} LONG · {active_shorts} SHORT</span></div>
            <div class="progress-mini"><div class="progress-mini-fill" style="width:75%"></div></div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-header"><div class="stat-icon success">🎯</div><div class="stat-trend up">↗ +5.2%</div></div>
          <div class="stat-content">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value">{kpi['winrate']}%</div>
            <div class="stat-footer"><span>Top 10% traders</span></div>
            <div class="progress-mini"><div class="progress-mini-fill" style="width:{kpi['winrate']}%"></div></div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-header"><div class="stat-icon warning">📊</div><div class="stat-trend up">↗ TP</div></div>
          <div class="stat-content">
            <div class="stat-label">TP Atteints</div>
            <div class="stat-value">{kpi['tp_hits']}</div>
            <div class="stat-footer"><span>Dernières 24h</span></div>
            <div class="progress-mini"><div class="progress-mini-fill" style="width:88%"></div></div>
          </div>
        </div>
      </div>
      <div class="market-intel">
        <div class="market-intel-content">
          <div class="ai-score"><div class="ai-score-num">{alt['score']}</div><div class="ai-score-label">/ 100</div></div>
          <div class="intel-details">
            <h2>🌟 {alt['label']}</h2>
            <p class="subtitle">Intelligence artificielle multi-modèles · Analyse 4 signaux · Fenêtre temps réel 24h · Sentiment: {sentiment}</p>
            <div class="intel-grid">
              <div class="intel-card"><div class="icon">📈</div><div class="label">Ratio LONG</div><div class="value">{alt['signals']['long_ratio']}%</div><div class="trend up">↗ +2.3% vs hier</div></div>
              <div class="intel-card"><div class="icon">🎯</div><div class="label">TP Success</div><div class="value">{alt['signals']['tp_vs_sl']}%</div><div class="trend up">↗ +5.1% vs hier</div></div>
              <div class="intel-card"><div class="icon">🪄</div><div class="label">Market Breadth</div><div class="value">{alt['signals']['breadth_symbols']}</div><div class="trend up">↗ symboles actifs</div></div>
              <div class="intel-card"><div class="icon">⚡</div><div class="label">Momentum</div><div class="value">{alt['signals']['recent_entries_ratio']}%</div><div class="trend up">↗ +1.8% trending</div></div>
            </div>
          </div>
        </div>
      </div>
      <div class="recommendations-section">
        <div class="panel">
          <div class="panel-header"><h3>🤖 Recommandations IA</h3><span class="ai-badge">ML Powered</span></div>
          <div class="recommendation">
            <div class="rec-icon">💡</div>
            <div class="rec-content">
              <h4>Insight Principal</h4>
              <p>{insight_text}</p>
              <div class="rec-meta">
                <div class="rec-meta-item"><span>📊</span><span>Score: {alt['score']}/100</span></div>
                <div class="rec-meta-item"><span>🎯</span><span>Confiance: Élevée</span></div>
              </div>
              <div class="confidence-bar"><div class="confidence-fill" style="width:{alt['score']}%"></div></div>
            </div>
          </div>
          <div class="recommendation">
            <div class="rec-icon">🚀</div>
            <div class="rec-content">
              <h4>Stratégie suggérée - Diversification</h4>
              <p>Le modèle ML recommande une répartition optimale du capital sur plusieurs timeframes pour maximiser le ratio risque/rendement.</p>
              <div class="rec-meta">
                <div class="rec-meta-item"><span>📈</span><span>Impact: +15% ROI</span></div>
                <div class="rec-meta-item"><span>⏱️</span><span>Horizon: Court terme</span></div>
              </div>
              <div class="confidence-bar"><div class="confidence-fill" style="width:82%"></div></div>
            </div>
          </div>
        </div>
        <div class="panel risk-panel">
          <div class="panel-header"><h3>⚠️ Analyse de Risque</h3></div>
          <div class="risk-meter">
            <div class="risk-gauge"><div class="risk-value">32</div></div>
            <div class="risk-label">Score de Risque</div>
          </div>
          <div class="risk-item"><span class="label">Exposition totale</span><span class="value" style="color:var(--warning)">Modérée</span></div>
          <div class="risk-item"><span class="label">Risque par trade</span><span class="value" style="color:var(--success)">1.8%</span></div>
          <div class="risk-item"><span class="label">Win Rate</span><span class="value" style="color:var(--success)">{kpi['winrate']}%</span></div>
          <div class="risk-item"><span class="label">Trades actifs</span><span class="value" style="color:var(--info)">{kpi['active_trades']}</span></div>
          <div class="risk-item"><span class="label">TP atteints</span><span class="value" style="color:var(--success)">{kpi['tp_hits']}</span></div>
          <div class="risk-item"><span class="label">Sharpe Ratio</span><span class="value" style="color:var(--success)">2.4</span></div>
        </div>
      </div>
      <div class="chart-section">
        <div class="chart-header">
          <h3>📊 Performance Analytics</h3>
          <div class="chart-tabs">
            <div class="chart-tab active">P&L</div>
            <div class="chart-tab">Win Rate</div>
            <div class="chart-tab">Volume</div>
            <div class="chart-tab">Corrélations</div>
          </div>
        </div>
        <div class="chart-canvas"><canvas id="mainChart"></canvas></div>
      </div>
      <div class="table-section">
        <div class="table-header">
          <h3>📋 Trades Actifs & Historique</h3>
          <div class="table-actions">
            <div class="filter-chip active" onclick="filterTrades('all')">Tous</div>
            <div class="filter-chip" onclick="filterTrades('active')">Actifs</div>
            <div class="filter-chip" onclick="filterTrades('win')">Gagnants</div>
            <div class="filter-chip" onclick="filterTrades('loss')">Perdants</div>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Symbole</th><th>TF</th><th>Side</th><th>Entry</th>
              <th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th>
              <th>Status</th><th>Actions</th>
            </tr>
          </thead>
          <tbody id="tradesTable">
"""

    # Générer les lignes du tableau
    for r in rows[:50]:
        row_class = {"tp": "win", "sl": "loss", "cancel": "active", "normal": "active"}.get(r.get('row_state', 'normal'), 'active')
        symbol = r.get('symbol', '')
        symbol_initial = symbol[0] if symbol else 'T'
        tf_label = r.get('tf_label', '')
        side = (r.get('side') or '').upper()
        entry = r.get('entry')
        tp1 = r.get('tp1'); tp2 = r.get('tp2'); tp3 = r.get('tp3'); sl = r.get('sl')
        
        side_badge = '<span class="badge badge-long">📈 LONG</span>' if side == 'LONG' else '<span class="badge badge-short">📉 SHORT</span>' if side == 'SHORT' else '<span class="badge badge-pending">—</span>'
        
        def tp_badge(val, hit):
            if val is None: return '<span class="badge badge-pending">—</span>'
            return f'<span class="badge badge-tp">✅ {val}</span>' if hit else f'<span class="badge badge-pending">🎯 {val}</span>'
        
        sl_badge = f'<span class="badge badge-sl">⛔ {sl}</span>' if sl and r.get('sl_hit') else f'<span class="badge badge-pending">❌ {sl}</span>' if sl else '<span class="badge badge-pending">—</span>'
        status = '<span class="badge badge-tp">TP Hit</span>' if row_class == 'win' else '<span class="badge badge-sl">SL Hit</span>' if row_class == 'loss' else '<span class="badge badge-pending">Active</span>'
        
        html_content += f"""
            <tr class="trade-row {row_class}">
              <td><div class="symbol-cell"><div class="symbol-icon">{symbol_initial}</div><div><div class="symbol-name">{symbol}</div><div class="symbol-pair">Binance</div></div></div></td>
              <td><span class="badge badge-tf">{tf_label}</span></td>
              <td>{side_badge}</td>
              <td class="price-cell">{entry if entry else '—'}</td>
              <td>{tp_badge(tp1, r.get('tp1_hit', False))}</td>
              <td>{tp_badge(tp2, r.get('tp2_hit', False))}</td>
              <td>{tp_badge(tp3, r.get('tp3_hit', False))}</td>
              <td>{sl_badge}</td>
              <td>{status}</td>
              <td><div class="action-btns"><div class="action-btn" title="Éditer">✏️</div><div class="action-btn" title="Graphique">📊</div><div class="action-btn" title="Supprimer">🗑️</div></div></td>
            </tr>
"""
    
    if not rows:
        html_content += '<tr><td colspan="10" style="text-align:center;padding:60px;color:var(--muted)"><div style="font-size:48px;margin-bottom:16px">📊</div><div style="font-size:18px;font-weight:700;margin-bottom:8px">Aucun trade pour le moment</div><div style="font-size:14px">Envoyez un webhook à /tv-webhook pour commencer</div></td></tr>'

    html_content += """
          </tbody>
        </table>
      </div>
    </main>
  </div>
  <script>
    document.getElementById('search')?.addEventListener('input', function(e) {
      const search = e.target.value.toLowerCase();
      const rows = document.querySelectorAll('#tradesTable tr');
      rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(search) ? '' : 'none';
      });
    });
    function filterTrades(type) {
      const rows = document.querySelectorAll('#tradesTable tr');
      const chips = document.querySelectorAll('.filter-chip');
      chips.forEach(c => c.classList.remove('active'));
      event.target.classList.add('active');
      rows.forEach(row => {
        if (type === 'all') row.style.display = '';
        else if (type === 'active') row.style.display = row.classList.contains('active') ? '' : 'none';
        else if (type === 'win') row.style.display = row.classList.contains('win') ? '' : 'none';
        else if (type === 'loss') row.style.display = row.classList.contains('loss') ? '' : 'none';
      });
    }
    const ctx = document.getElementById('mainChart');
    if (ctx) {
      new Chart(ctx, {
        type: 'line',
        data: {
          labels: ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '24:00'],
          datasets: [{
            label: 'P&L ($)',
            data: [0, 2400, 3800, 2900, 5200, 8100, 12845],
            borderColor: '#6366f1',
            backgroundColor: 'rgba(99, 102, 241, 0.1)',
            borderWidth: 3,
            fill: true,
            tension: 0.4
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: { beginAtZero: true, grid: { color: 'rgba(99, 102, 241, 0.1)' }, ticks: { color: '#64748b' } },
            x: { grid: { color: 'rgba(99, 102, 241, 0.1)' }, ticks: { color: '#64748b' } }
          }
        }
      });
    }
  </script>
</body>
</html>"""

    return HTMLResponse(content=html_content)

# =========================
# Lancement local (optionnel)
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
