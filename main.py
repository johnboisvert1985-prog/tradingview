# =========================
# main.py — Bloc 1/5
# Imports, Config, Logging, Telegram, DB
# =========================

import os
import re
import json
import math
import time
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("aitrader")

# ---------- Config ----------
DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
DB_PATH = os.path.join(DB_DIR, "data.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ALT_INTERVAL = int(os.getenv("ALT_INTERVAL", "60"))          # secondes entre snapshots
ALT_LOOKBACK_MIN = int(os.getenv("ALT_LOOKBACK_MIN", "360"))  # fenêtre d'analyse (minutes)
MAX_ROWS_DASH = int(os.getenv("MAX_ROWS_DASH", "200"))

# Cooldown Telegram (éviter flood / 429)
TELEGRAM_COOLDOWN_SEC = int(os.getenv("TELEGRAM_COOLDOWN_SEC", "2"))
_last_telegram_sent_ts = 0.0

# ---------- FastAPI ----------
app = FastAPI(title="AI Trader")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utils ----------
def now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds")

def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None: return default
        return float(x)
    except Exception:
        return default

def col(v: Optional[float], nd=6) -> str:
    return "" if v is None else f"{v:.{nd}f}"

def rate_limited_send() -> bool:
    global _last_telegram_sent_ts
    t = time.time()
    if t - _last_telegram_sent_ts < TELEGRAM_COOLDOWN_SEC:
        return False
    _last_telegram_sent_ts = t
    return True

# ---------- Telegram ----------
async def send_telegram(text: str, disable_notification: bool = False) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré; envoi ignoré")
        return
    if not rate_limited_send():
        log.warning("Telegram send skipped due to cooldown")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": disable_notification,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(url, json=payload)
            r.raise_for_status()
        log.info("Telegram message sent")
    except httpx.HTTPError as e:
        log.warning(f"Telegram send_telegram_ex exception: {e}")

def tg_square(color: str) -> str:
    """
    Petits carrés colorés pour Telegram :
      - 'green' = Vector UP
      - 'purple' = Vector DOWN
      - 'blue'   = Info
      - 'red'    = SL
    """
    colors = {
        "green": "🟩",
        "purple": "🟪",
        "blue": "🟦",
        "red": "🟥",
        "yellow": "🟨",
        "orange": "🟧",
        "white": "⬜",
        "black": "⬛",
        "brown": "🟫",
    }
    return colors.get(color, "⬜")

def tg_bold(s: str) -> str:
    return f"<b>{s}</b>"

def tg_mono(s: str) -> str:
    return f"<code>{s}</code>"

# ---------- DB ----------
def ensure_db_dir():
    if not os.path.isdir(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)
    log.info(f"DB dir OK: {DB_DIR} (using {DB_PATH})")

def init_db():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Table des évènements (webhooks TradingView)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        type TEXT NOT NULL,
        symbol TEXT,
        tf TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        r1 REAL,
        s1 REAL,
        leverage TEXT,
        note TEXT,
        trade_id TEXT,
        price REAL,
        direction TEXT,
        status TEXT
    )""")

    # Index utiles
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")

    conn.commit()
    conn.close()
    log.info(f"DB initialized at {DB_PATH}")
# =========================
# main.py — Bloc 2/5
# DB helpers, save_event, Telegram format, base API
# =========================

# ---------- DB Helpers ----------
def save_event(event: Dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO events (
        received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, r1, s1,
        leverage, note, trade_id, price, direction, status
    ) VALUES (
        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
    )""", (
        now_iso(),
        event.get("type"),
        event.get("symbol"),
        event.get("tf"),
        event.get("side"),
        safe_float(event.get("entry")),
        safe_float(event.get("sl")),
        safe_float(event.get("tp1")),
        safe_float(event.get("tp2")),
        safe_float(event.get("tp3")),
        safe_float(event.get("r1")),
        safe_float(event.get("s1")),
        event.get("leverage"),
        event.get("note"),
        event.get("trade_id"),
        safe_float(event.get("price")),
        event.get("direction"),
        event.get("status"),
    ))
    conn.commit()
    conn.close()
    log.info(f"Saved event: type={event.get('type')} symbol={event.get('symbol')} tf={event.get('tf')} trade_id={event.get('trade_id')}")

def load_events(limit: int = 50) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

# ---------- Telegram Formatting ----------
def format_event_msg(ev: Dict[str, Any]) -> str:
    t = ev.get("type", "")
    s = ev.get("symbol", "")
    tf = ev.get("tf", "")
    price = ev.get("price")
    trade_id = ev.get("trade_id", "")
    dirn = ev.get("direction")
    side = ev.get("side")
    msg = ""

    if t == "ENTRY":
        msg = f"🔔 {tg_bold('ENTRY')} — {s} {tf}\n" \
              f"{tg_mono(str(ev))}"
    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        step = t.replace("_HIT", "")
        msg = f"{tg_square('green')} {tg_bold(step)} — {s} {tf}"
    elif t == "SL_HIT":
        msg = f"{tg_square('red')} {tg_bold('STOP LOSS')} — {s} {tf}"
    elif t == "CLOSE":
        msg = f"{tg_square('blue')} {tg_bold('CLOSE')} — {s} {tf}"
    elif t == "VECTOR_CANDLE":
        if dirn == "UP":
            msg = f"{tg_square('green')} {tg_bold('VECTOR UP')} — {s} {tf} {tg_mono(col(price,6))}"
        elif dirn == "DOWN":
            msg = f"{tg_square('purple')} {tg_bold('VECTOR DOWN')} — {s} {tf} {tg_mono(col(price,6))}"
        else:
            msg = f"{tg_square('blue')} {tg_bold('VECTOR')} — {s} {tf}"
    elif t.startswith("AOE_"):
        msg = f"⚡ {tg_bold(t)} — {s} {tf}"
    else:
        msg = f"ℹ️ {tg_bold(t)} — {s} {tf}"

    if trade_id:
        msg += f"\nID: {tg_mono(trade_id)}"
    return msg

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
def root():
    return "<h1>AI Trader Pro — Backend OK</h1>"

@app.get("/trades", response_class=JSONResponse)
def trades(limit: int = 50):
    return {"events": load_events(limit=limit)}
# =========================
# main.py — Bloc 3/5
# TV webhook: parse, persist, telegram notify
# =========================

# Types que l’on accepte depuis TradingView
ACCEPTED_TYPES = {
    "ENTRY", "CLOSE",
    "TP1_HIT", "TP2_HIT", "TP3_HIT",
    "SL_HIT",
    "VECTOR_CANDLE",
    "AOE_PREMIUM", "AOE_DISCOUNT"
}

def normalize_type(raw: Optional[str]) -> str:
    if not raw:
        return ""
    t = raw.strip().upper()
    # normalisations courantes
    t = t.replace("TP1", "TP1").replace("TP2", "TP2").replace("TP3", "TP3")
    if t not in ACCEPTED_TYPES:
        # cas "TP1", "TP2", "TP3" sans suffixe HIT
        if t in {"TP1", "TP2", "TP3"}:
            t = f"{t}_HIT"
    return t

def normalize_dir(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in {"UP", "LONG", "BUY"}:
        return "UP"
    if s in {"DOWN", "SHORT", "SELL"}:
        return "DOWN"
    return None

def normalize_tf(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    # autoriser "15" -> "15", "15m" -> "15m"
    if s.endswith("m") or s.endswith("h") or s.endswith("d"):
        return s
    return s  # laisser tel quel (ex: "15")

def event_status(ev_type: str) -> str:
    # statut simple pour UI
    if ev_type == "ENTRY":
        return "open"
    if ev_type in {"TP1_HIT", "TP2_HIT", "TP3_HIT"}:
        return "tp"
    if ev_type == "SL_HIT":
        return "sl"
    if ev_type == "CLOSE":
        return "closed"
    if ev_type == "VECTOR_CANDLE":
        return "vector"
    if ev_type.startswith("AOE_"):
        return "aoe"
    return "info"

def extract_price(payload: Dict[str, Any]) -> Optional[float]:
    # plusieurs clés possibles suivant tes alertes
    for k in ("price", "close", "entry", "tp", "sl", "hiWin", "upper"):
        v = payload.get(k)
        if v is not None:
            return safe_float(v)
    return None

def parse_trade_id(payload: Dict[str, Any]) -> Optional[str]:
    # utiliser celui fourni si présent, sinon en fabriquer un (pour tracer)
    tid = payload.get("trade_id") or payload.get("tradeId")
    if tid:
        return str(tid)
    # fallback: symbol_tf_timestamplike
    sym = (payload.get("symbol") or "").strip()
    tf  = (payload.get("tf") or "").strip()
    # horodatage: time ou now
    ts = payload.get("time")
    if isinstance(ts, (int, float)) and ts > 0:
        stamp = int(ts)
    else:
        # micro fallback rapide
        stamp = int(dt.datetime.utcnow().timestamp() * 1000)
    return f"{sym}_{tf}_{stamp}"

def allowed_by_secret(payload: Dict[str, Any]) -> bool:
    secret = payload.get("secret") or payload.get("s")
    expected = os.getenv("TV_WEBHOOK_SECRET")
    if expected:
        return (secret == expected)
    # si pas de secret défini côté serveur, on autorise
    return True

def should_send_tg(ev_type: str) -> bool:
    # limiter le spam : on notifie les principaux
    return ev_type in {
        "ENTRY", "CLOSE",
        "TP1_HIT", "TP2_HIT", "TP3_HIT",
        "SL_HIT",
        "VECTOR_CANDLE",
        "AOE_PREMIUM", "AOE_DISCOUNT",
    }

def cooldown_ok(kind: str, key: str, seconds: int = 20) -> bool:
    # kind: p.ex. "tg", key: symbol+tf+type
    k = f"{kind}:{key}"
    now = time.time()
    last = _tg_cooldown.get(k)
    if last and (now - last) < seconds:
        return False
    _tg_cooldown[k] = now
    return True

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    """
    Webhook TradingView : accepte JSON de tes alertes.
    Normalise, sauvegarde l'event, et notifie Telegram avec le style demandé.
    """
    try:
        payload = await req.json()
    except Exception:
        # tenter parsing texte brut (TradingView peut envoyer du texte)
        body = await req.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            log.error("Webhook payload non JSON")
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    # filtrage par secret si défini
    if not allowed_by_secret(payload):
        log.warning("Webhook secret mismatch — ignored")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Normalisations
    ev_type = normalize_type(payload.get("type"))
    symbol  = (payload.get("symbol") or "").strip()
    tf      = normalize_tf(payload.get("tf"))
    dirn    = normalize_dir(payload.get("direction"))
    note    = payload.get("note")
    side    = payload.get("side")
    price   = extract_price(payload)
    trade_id = parse_trade_id(payload)

    # Construire l’event interne
    ev: Dict[str, Any] = {
        "type": ev_type,
        "symbol": symbol,
        "tf": tf,
        "side": side,
        "entry": payload.get("entry"),
        "sl": payload.get("sl"),
        "tp1": payload.get("tp1"),
        "tp2": payload.get("tp2"),
        "tp3": payload.get("tp3"),
        "r1": payload.get("r1"),
        "s1": payload.get("s1"),
        "leverage": payload.get("leverage") or payload.get("lev_reco"),
        "note": note,
        "trade_id": trade_id,
        "price": price,
        "direction": dirn,
        "status": event_status(ev_type),
    }

    # Sauvegarde DB (robuste même si champs manquent)
    try:
        save_event(ev)
    except Exception as e:
        log.exception(f"save_event failed: {e}")
        # on continue quand même pour renvoyer 200 à TradingView
        # (afin d’éviter les retries côté TV)

    # Envoi Telegram si permis + cooldown
    if TELEGRAM_ENABLED and should_send_tg(ev_type):
        # clé cooldown = type+symbol+tf (éviter spam)
        cd_key = f"{ev_type}:{symbol}:{tf}"
        if cooldown_ok("tg", cd_key, seconds=TELEGRAM_COOLDOWN_S):
            msg = format_event_msg(ev)
            ok = send_telegram(msg)
            if not ok:
                log.warning("Telegram send skipped due to cooldown or failure")
        else:
            log.warning("Telegram send skipped due to cooldown")

    # Réponse HTTP
    return JSONResponse({"ok": True, "received_at": now_iso(), "normalized_type": ev_type, "trade_id": trade_id})
# =========================
# main.py — Bloc 4/5
# /trades : Dashboard Altseason + tableau des trades
# =========================

from starlette.responses import HTMLResponse

def _fetch_recent_events(limit:int=1500) -> List[Dict[str,Any]]:
    rows = db_query("""
        SELECT 
            created_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, 
            r1, s1, leverage, note, trade_id, price, direction, status
        FROM events
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    out = []
    for r in rows:
        out.append({
            "created_at": r[0], "type": r[1], "symbol": r[2], "tf": r[3],
            "side": r[4], "entry": r[5], "sl": r[6], "tp1": r[7], "tp2": r[8], "tp3": r[9],
            "r1": r[10], "s1": r[11], "leverage": r[12], "note": r[13],
            "trade_id": r[14], "price": r[15], "direction": r[16], "status": r[17],
        })
    return out

def _aggregate_trades(events: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """
    Regroupe par trade_id et détermine l'état (TP1/TP2/TP3/SL/CLOSE).
    On conserve la dernière ENTRY (si présente) pour afficher la config.
    """
    by_tid: Dict[str, Dict[str,Any]] = {}
    for ev in events[::-1]:  # du plus ancien au plus récent pour initialiser puis marquer les hits
        tid = ev.get("trade_id") or f"{ev.get('symbol','')}_{ev.get('tf','')}"
        bucket = by_tid.get(tid)
        if not bucket:
            bucket = {
                "trade_id": tid,
                "symbol": ev.get("symbol"),
                "tf": ev.get("tf"),
                "side": ev.get("side"),
                "entry": ev.get("entry"),
                "sl": ev.get("sl"),
                "tp1": ev.get("tp1"),
                "tp2": ev.get("tp2"),
                "tp3": ev.get("tp3"),
                "r1": ev.get("r1"),
                "s1": ev.get("s1"),
                "leverage": ev.get("leverage"),
                "last_type": ev.get("type"),
                "last_price": ev.get("price"),
                "created_at": ev.get("created_at"),
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "sl_hit": False,
                "closed": False,
                "vector_last": None,   # "UP" / "DOWN"
                "confidence": ev.get("confidence"),
                "horizon": ev.get("horizon"),
            }
            by_tid[tid] = bucket

        t = (ev.get("type") or "").upper()
        if t == "ENTRY":
            # rafraîchir les niveaux si ré-ENTRY
            for k in ("entry","sl","tp1","tp2","tp3","r1","s1","leverage","side"):
                v = ev.get(k)
                if v is not None:
                    bucket[k] = v
        elif t == "TP1_HIT":
            bucket["tp1_hit"] = True
        elif t == "TP2_HIT":
            bucket["tp2_hit"] = True
        elif t == "TP3_HIT":
            bucket["tp3_hit"] = True
        elif t == "SL_HIT":
            bucket["sl_hit"] = True
        elif t == "CLOSE":
            bucket["closed"] = True
        elif t == "VECTOR_CANDLE":
            d = (ev.get("direction") or "").upper()
            if d in ("UP","DOWN"):
                bucket["vector_last"] = d

        bucket["last_type"]  = t or bucket["last_type"]
        bucket["last_price"] = ev.get("price") if ev.get("price") is not None else bucket["last_price"]
        bucket["created_at"] = ev.get("created_at") or bucket["created_at"]

    # ordonner: trades récents d'abord
    ordered = sorted(by_tid.values(), key=lambda x: x.get("created_at") or "", reverse=True)
    return ordered

def _altseason_snapshot_safe() -> Dict[str,Any]:
    """
    Renvoie un snapshot Altseason sans lever d’erreur si rien n’est encore calculé.
    Essaie d’utiliser la dernière ligne d’`altseason_snapshots`, sinon fallback neutre.
    """
    try:
        row = db_query("""
            SELECT created_at, btc_dom, btc_7d, alts_7d, alts_btc_ratio, heat, phase
            FROM altseason_snapshots
            ORDER BY created_at DESC
            LIMIT 1
        """)
        if row:
            r = row[0]
            return {
                "created_at": r[0],
                "btc_dom": r[1],
                "btc_7d": r[2],
                "alts_7d": r[3],
                "alts_btc_ratio": r[4],
                "heat": r[5],
                "phase": r[6],
            }
    except Exception as e:
        log.warning(f"Altseason snapshot query failed: {e}")
    # fallback par défaut
    return {
        "created_at": now_iso(),
        "btc_dom": None,
        "btc_7d": 0.0,
        "alts_7d": 0.0,
        "alts_btc_ratio": 1.0,
        "heat": 0.0,
        "phase": "Neutre",
    }

def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{html.escape(text)}</span>'

def _vector_chip(v: Optional[str]) -> str:
    if v == "UP":
        return '<span class="chip chip-up" title="Vector UP"></span>'
    if v == "DOWN":
        return '<span class="chip chip-down" title="Vector DOWN"></span>'
    return '<span class="chip chip-none" title="No vector"></span>'

def _fmt_price(x: Any) -> str:
    v = safe_float(x)
    if v is None:
        return "—"
    # format compact
    if v == 0:
        return "0"
    if abs(v) < 0.001:
        return f"{v:.8f}"
    if abs(v) < 1:
        return f"{v:.6f}"
    if abs(v) < 100:
        return f"{v:.4f}"
    return f"{v:.2f}"

def _row_for_trade(t: Dict[str,Any]) -> str:
    sym = html.escape(str(t.get("symbol") or "—"))
    tf  = html.escape(str(t.get("tf") or "—"))
    side = (t.get("side") or "").upper()
    side_badge = _badge("LONG","green") if side=="LONG" else (_badge("SHORT","red") if side=="SHORT" else _badge("N/A","muted"))

    entry = _fmt_price(t.get("entry"))
    sl    = _fmt_price(t.get("sl"))
    tp1   = _fmt_price(t.get("tp1"))
    tp2   = _fmt_price(t.get("tp2"))
    tp3   = _fmt_price(t.get("tp3"))

    tp1_cls = "hit" if t.get("tp1_hit") else "idle"
    tp2_cls = "hit" if t.get("tp2_hit") else "idle"
    tp3_cls = "hit" if t.get("tp3_hit") else "idle"
    sl_cls  = "hit" if t.get("sl_hit") else "idle"

    status = "Closed" if t.get("closed") else "Open"
    status_badge = _badge(status, "muted" if t.get("closed") else "blue")

    lev = html.escape(str(t.get("leverage") or ""))
    lev = lev or "—"

    vec = _vector_chip(t.get("vector_last"))

    return (
        "<tr>"
        f"<td class='sticky'>{sym}<div class='sub'>{tf}</div></td>"
        f"<td>{side_badge}</td>"
        f"<td>{entry}</td>"
        f"<td class='sl {sl_cls}'>{sl}</td>"
        f"<td class='tp {tp1_cls}'>{tp1}</td>"
        f"<td class='tp {tp2_cls}'>{tp2}</td>"
        f"<td class='tp {tp3_cls}'>{tp3}</td>"
        f"<td>{lev}</td>"
        f"<td class='center'>{vec}</td>"
        f"<td>{status_badge}</td>"
        "</tr>"
    )

@app.get("/trades")
def trades_page():
    # données
    events = _fetch_recent_events(limit=2000)
    trades = _aggregate_trades(events)
    alt = _altseason_snapshot_safe()

    # --- HTML header (pas d’f-string ici -> CSS intact) ---
    head = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Trader — Trades</title>
<style>
:root{
  --bg:#0b0f14; --panel:#111723; --muted:#778099; --card:#0f1520;
  --txt:#e6edf3; --green:#22c55e; --green-weak:#14301f;
  --red:#ef4444; --red-weak:#2a1416; --blue:#60a5fa; --blue-weak:#142234;
  --amber:#f59e0b; --amber-weak:#2b220f; --border:#1f2937;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.4 system-ui,Segoe UI,Roboto,Helvetica,Arial}
.container{max-width:1200px;margin:18px auto;padding:0 12px}

.panel{
  background:linear-gradient(180deg,var(--panel),var(--card));
  border:1px solid var(--border); border-radius:14px; padding:14px; margin-bottom:14px;
  box-shadow:0 10px 30px rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.03);
}
h2{margin:0 0 10px 0; font-size:18px}
.grid{
  display:grid; gap:10px;
  grid-template-columns:repeat(5,minmax(0,1fr));
}
.indik{
  background:#0b1220; border:1px solid var(--border); border-radius:12px; padding:12px;
}
.indik .label{color:var(--muted); font-size:12px}
.indik .val{font-weight:700; font-size:18px; margin-top:2px}
.indik .hint{color:var(--muted); font-size:12px; margin-top:4px}

.badge{
  display:inline-block; padding:3px 8px; border-radius:999px; font-size:12px; line-height:1; border:1px solid var(--border);
  background:#0e1625; color:#cbd5e1;
}
.badge.green{background:var(--green-weak); color:#bbf7d0; border-color:#19452b}
.badge.red{background:var(--red-weak); color:#fecaca; border-color:#3b161a}
.badge.blue{background:var(--blue-weak); color:#cfe7ff; border-color:#1e3760}
.badge.muted{background:#0f1520; color:#95a3b9; border-color:var(--border)}

.table{
  width:100%; border-collapse:separate; border-spacing:0; overflow:hidden;
  border:1px solid var(--border); border-radius:14px;
}
thead th{
  text-align:left; font-weight:600; color:#cbd5e1; font-size:12px; letter-spacing:.02em;
  background:#0f1520; position:sticky; top:0; z-index:2; padding:10px;
  border-bottom:1px solid var(--border);
}
tbody td{ padding:10px; border-bottom:1px solid var(--border); vertical-align:middle }
tbody tr:hover td{ background:#0b1220 }
td.sticky{ position:sticky; left:0; background:linear-gradient(90deg,#0f1520,#0f1520); z-index:1 }
td .sub{ color:var(--muted); font-size:12px }

.tp.idle{ background:rgba(34,197,94,.06) }
.tp.hit{ background:rgba(34,197,94,.18); color:#dcfce7; font-weight:600; outline:1px solid rgba(34,197,94,.35) }
.sl.idle{ background:rgba(239,68,68,.06) }
.sl.hit{ background:rgba(239,68,68,.18); color:#fee2e2; font-weight:600; outline:1px solid rgba(239,68,68,.35) }

.center{ text-align:center }

.chip{
  display:inline-block; width:10px; height:10px; border-radius:2px; border:1px solid var(--border);
  box-shadow:0 0 0 1px rgba(0,0,0,.2) inset;
}
.chip-up{ background:var(--green) }       /* VERT pour VECTOR UP */
.chip-down{ background:#8b5cf6 }          /* MAUVE pour VECTOR DOWN */
.chip-none{ background:#334155 }

.footer-note{ color:var(--muted); font-size:12px; margin-top:8px }

@media (max-width:900px){
  .grid{ grid-template-columns:repeat(2,minmax(0,1fr)) }
  thead .hide-sm, tbody .hide-sm{ display:none }
}
</style>
</head>
<body>
<div class="container">
"""

    # --- Dashboard Altseason ---
    alt_rows = []
    def _fmt(v, suffix=""):
        if v is None: return "—"
        try:
            return f"{float(v):.2f}{suffix}"
        except Exception:
            return f"{v}{suffix}"

    alt_html = f"""
<div class="panel">
  <h2>Indicateurs Altseason</h2>
  <div class="grid">
    <div class="indik">
      <div class="label">BTC Dominance</div>
      <div class="val">{_fmt(alt.get('btc_dom'), '%')}</div>
      <div class="hint">Poids de BTC sur le marché</div>
    </div>
    <div class="indik">
      <div class="label">BTC 7j</div>
      <div class="val">{_fmt(alt.get('btc_7d'), '%')}</div>
      <div class="hint">Perf 7 jours de BTC</div>
    </div>
    <div class="indik">
      <div class="label">Alts 7j</div>
      <div class="val">{_fmt(alt.get('alts_7d'), '%')}</div>
      <div class="hint">Perf moyenne des Altcoins (7j)</div>
    </div>
    <div class="indik">
      <div class="label">Rapport Alts/BTC</div>
      <div class="val">{_fmt(alt.get('alts_btc_ratio'))}</div>
      <div class="hint">>1 favorise les Alts</div>
    </div>
    <div class="indik">
      <div class="label">Phase</div>
      <div class="val">{html.escape(str(alt.get('phase') or '—'))}</div>
      <div class="hint">Synthèse du momentum</div>
    </div>
  </div>
  <div class="footer-note">Dernière mise à jour : {html.escape(str(alt.get('created_at') or '—'))}</div>
</div>
"""

    # --- Tableau des trades ---
    table_head = """
<div class="panel">
  <h2>Trades en cours & récents</h2>
  <table class="table">
    <thead>
      <tr>
        <th>Symbole</th>
        <th>Side</th>
        <th>Entry</th>
        <th>SL</th>
        <th>TP1</th>
        <th>TP2</th>
        <th>TP3</th>
        <th class="hide-sm">Lev.</th>
        <th class="center hide-sm">Vector</th>
        <th>Statut</th>
      </tr>
    </thead>
    <tbody>
"""

    rows_html = []
    for t in trades:
        rows_html.append(_row_for_trade(t))

    table_tail = """
    </tbody>
  </table>
  <div class="footer-note">TP en <b>vert</b> quand atteint · SL en <b>rouge</b> · Carré <b>vert</b> = Vector UP · Carré <b>mauve</b> = Vector DOWN.</div>
</div>
</div>
</body>
</html>
"""

    html_out = head + alt_html + table_head + "".join(rows_html) + table_tail
    return HTMLResponse(html_out)
# =========================
# main.py — Bloc 5/5
# API JSON + Altseason Daemon + CORS + Health/404
# =========================

from typing import List, Dict, Any, Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi import Request
from datetime import datetime, timezone
import threading
import time
import statistics

# --- Gardes utilitaires au cas où des blocs précédents ne les ont pas définis ---
if 'now_iso' not in globals():
    def now_iso() -> str:
        return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

if 'safe_float' not in globals():
    def safe_float(x, default=None):
        try:
            return float(x)
        except Exception:
            return default

if 'db_execute' not in globals() or 'db_query' not in globals():
    raise RuntimeError("db_execute/db_query doivent être définis par les blocs précédents.")

# --- CORS (front, TV, autres origines) ---
try:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    # app peut déjà avoir CORS; ignorer
    pass

# --- Tables nécessaires (si pas déjà créées) ---
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
);
""")

# --- Paramètre du daemon Altseason ---
ALT_INTERVAL = int(os.environ.get("ALT_INTERVAL", "120"))  # secondes

# --- Helpers déjà utilisés par /trades (bloc 4) ---
def _fetch_recent_events(limit:int=2000) -> List[Dict[str,Any]]:
    rows = db_query("""
        SELECT 
            created_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, 
            r1, s1, leverage, note, trade_id, price, direction, status
        FROM events
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    out = []
    for r in rows:
        out.append({
            "created_at": r[0], "type": r[1], "symbol": r[2], "tf": r[3],
            "side": r[4], "entry": r[5], "sl": r[6], "tp1": r[7], "tp2": r[8], "tp3": r[9],
            "r1": r[10], "s1": r[11], "leverage": r[12], "note": r[13],
            "trade_id": r[14], "price": r[15], "direction": r[16], "status": r[17],
        })
    return out

def _aggregate_trades(events: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    by_tid: Dict[str, Dict[str,Any]] = {}
    for ev in events[::-1]:
        tid = ev.get("trade_id") or f"{ev.get('symbol','')}_{ev.get('tf','')}"
        if tid not in by_tid:
            by_tid[tid] = {
                "trade_id": tid, "symbol": ev.get("symbol"), "tf": ev.get("tf"),
                "side": ev.get("side"), "entry": ev.get("entry"), "sl": ev.get("sl"),
                "tp1": ev.get("tp1"), "tp2": ev.get("tp2"), "tp3": ev.get("tp3"),
                "r1": ev.get("r1"), "s1": ev.get("s1"), "leverage": ev.get("leverage"),
                "last_type": ev.get("type"), "last_price": ev.get("price"),
                "created_at": ev.get("created_at"),
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "sl_hit": False, "closed": False, "vector_last": None,
                "confidence": ev.get("confidence"), "horizon": ev.get("horizon"),
            }
        bucket = by_tid[tid]
        t = (ev.get("type") or "").upper()
        if t == "ENTRY":
            for k in ("entry","sl","tp1","tp2","tp3","r1","s1","leverage","side"):
                v = ev.get(k)
                if v is not None:
                    bucket[k] = v
        elif t == "TP1_HIT":
            bucket["tp1_hit"] = True
        elif t == "TP2_HIT":
            bucket["tp2_hit"] = True
        elif t == "TP3_HIT":
            bucket["tp3_hit"] = True
        elif t == "SL_HIT":
            bucket["sl_hit"] = True
        elif t == "CLOSE":
            bucket["closed"] = True
        elif t == "VECTOR_CANDLE":
            d = (ev.get("direction") or "").upper()
            if d in ("UP","DOWN"): bucket["vector_last"] = d
        bucket["last_type"]  = t or bucket["last_type"]
        if ev.get("price") is not None:
            bucket["last_price"] = ev.get("price")
        if ev.get("created_at"):
            bucket["created_at"] = ev.get("created_at")
    return sorted(by_tid.values(), key=lambda x: x.get("created_at") or "", reverse=True)

# --- Calcul Altseason (proxy simple depuis les events) ---
def _compute_altseason_snapshot() -> Dict[str,Any]:
    """
    Proxy robuste basé sur les événements:
    - btc_dom: ratio (VECTOR/TP hits) BTC vs tout (approx)
    - btc_7d, alts_7d: scores d’élan 7j (count UP/DOWN)
    - alts_btc_ratio: alts_7d / max(btc_7d,1e-9)
    - heat: normalisé [0..100] basé sur proportion de signaux UP
    - phase: texte synthétique
    """
    # Derniers 7 jours (si created_at est ISO), sinon prendre 10k derniers events
    rows = db_query("""
        SELECT created_at, type, symbol, direction
        FROM events
        ORDER BY created_at DESC
        LIMIT 10000
    """)
    btc_tags = ("BTCUSDT","BTCUSD",".BTC")
    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    total_up = 0
    total = 0
    btc_hits = 0
    alts_hits = 0
    btc_up = 0
    alts_up = 0

    for r in rows:
        created_at, etype, symbol, direction = r[0], (r[1] or "").upper(), (r[2] or ""), (r[3] or "")
        # Filtre temporel lax si ISO: on garde tout; Render n’a pas TZ uniforme => on ne jette rien
        is_btc = any(tag in symbol for tag in btc_tags)

        # Compter signaux impactants
        if etype in ("VECTOR_CANDLE","TP1_HIT","TP2_HIT","TP3_HIT"):
            total += 1
            if is_btc: btc_hits += 1
            else: alts_hits += 1

            if etype == "VECTOR_CANDLE":
                if (direction or "").upper() == "UP":
                    total_up += 1
                    if is_btc: btc_up += 1
                    else: alts_up += 1
            else:
                # les TP sont pro-haussiers (signal “succès”)
                total_up += 1
                if is_btc: btc_up += 1
                else: alts_up += 1

    btc_dom = (btc_hits / max(total, 1)) * 100.0
    if total == 0:
        heat = 0.0
    else:
        heat = (total_up / total) * 100.0

    # Scores “7j” approximés par proportion UP par univers
    btc_7d = (btc_up / max(btc_hits, 1)) * 100.0 if btc_hits else 0.0
    alts_7d = (alts_up / max(alts_hits, 1)) * 100.0 if alts_hits else 0.0
    alts_btc_ratio = (alts_7d / max(btc_7d, 1e-6)) if btc_7d > 0 else (2.0 if alts_7d > 0 else 1.0)

    # Phase
    if heat > 66 and alts_btc_ratio > 1.2:
        phase = "Altseason (forte)"
    elif heat > 55 and alts_btc_ratio > 1.0:
        phase = "Altseason (modérée)"
    elif heat < 40 and btc_dom > 60:
        phase = "BTC season"
    else:
        phase = "Neutre"

    snap = {
        "created_at": now_iso(),
        "btc_dom": round(btc_dom, 2),
        "btc_7d": round(btc_7d, 2),
        "alts_7d": round(alts_7d, 2),
        "alts_btc_ratio": round(alts_btc_ratio, 2),
        "heat": round(heat, 2),
        "phase": phase,
    }
    return snap

def _save_altseason_snapshot(s: Dict[str,Any]) -> None:
    db_execute("""
        INSERT INTO altseason_snapshots (created_at, btc_dom, btc_7d, alts_7d, alts_btc_ratio, heat, phase)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        s.get("created_at"), s.get("btc_dom"), s.get("btc_7d"), s.get("alts_7d"),
        s.get("alts_btc_ratio"), s.get("heat"), s.get("phase"),
    ))

def _latest_altseason_snapshot() -> Dict[str,Any]:
    row = db_query("""
        SELECT created_at, btc_dom, btc_7d, alts_7d, alts_btc_ratio, heat, phase
        FROM altseason_snapshots
        ORDER BY created_at DESC
        LIMIT 1
    """)
    if row:
        r = row[0]
        return {
            "created_at": r[0], "btc_dom": r[1], "btc_7d": r[2], "alts_7d": r[3],
            "alts_btc_ratio": r[4], "heat": r[5], "phase": r[6]
        }
    # fallback
    return {
        "created_at": now_iso(), "btc_dom": None, "btc_7d": 0.0, "alts_7d": 0.0,
        "alts_btc_ratio": 1.0, "heat": 0.0, "phase": "Neutre"
    }

# --- Daemon Altseason ---
_altseason_thread: Optional[threading.Thread] = None
_altseason_running = False

def run_altseason_daemon(interval: int = ALT_INTERVAL):
    global _altseason_running
    if _altseason_running:
        return
    _altseason_running = True
    while True:
        try:
            snap = _compute_altseason_snapshot()
            _save_altseason_snapshot(snap)
        except Exception as e:
            log.warning(f"Altseason daemon error: {e}")
        time.sleep(max(10, int(interval)))

@app.on_event("startup")
def _start_altseason():
    global _altseason_thread
    try:
        if _altseason_thread is None or not _altseason_thread.is_alive():
            _altseason_thread = threading.Thread(target=run_altseason_daemon, args=(ALT_INTERVAL,), daemon=True)
            _altseason_thread.start()
    except Exception as e:
        log.warning(f"Unable to start altseason daemon: {e}")

# --- API JSON ---
@app.get("/api/altseason")
def api_altseason():
    try:
        snap = _latest_altseason_snapshot()
        return JSONResponse({"ok": True, "data": snap})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/trades")
def api_trades(limit: int = 500):
    try:
        ev = _fetch_recent_events(limit=3000)
        trades = _aggregate_trades(ev)
        return JSONResponse({"ok": True, "data": trades[:max(10, min(limit, 1000))]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# --- Health & accueil ---
@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "time": now_iso()})

@app.get("/")
def root():
    # rediriger vers le dashboard
    return RedirectResponse(url="/trades", status_code=302)

# --- 404 propre ---
@app.exception_handler(404)
async def not_found(request: Request, exc):
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>404</title>"
        "<style>body{font:14px system-ui;background:#0b0f14;color:#e6edf3;display:grid;place-items:center;height:100vh}"
        ".card{background:#111723;border:1px solid #1f2937;border-radius:12px;padding:20px;max-width:560px}"
        "a{color:#60a5fa;text-decoration:none}</style></head><body>"
        "<div class='card'><h2>Page introuvable</h2>"
        "<p>La ressource demandée n’existe pas. Retour au <a href='/trades'>dashboard</a>.</p>"
        "</div></body></html>",
        status_code=404
    )
