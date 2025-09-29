# =========================
# main.py — Bloc 1/5
# Fondations: imports, constantes, DB, FastAPI, Altseason snapshot
# =========================

from __future__ import annotations

import os
import json
import time
import math
import html
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Iterable

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# -------------------------
# Constantes / Config
# -------------------------

APP_TITLE = "AI Trader"
DB_DIR = os.environ.get("AI_TRADER_DB_DIR", "/tmp/ai_trader")
DB_PATH = os.path.join(DB_DIR, "data.db")

# Secret de webhook TradingView (facultatif mais recommandé)
WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg")

# Telegram
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_COOLDOWN_SECONDS = int(os.environ.get("TELEGRAM_COOLDOWN", "5"))

# Altseason
ALT_INTERVAL = int(os.environ.get("ALTSEASON_REFRESH_SECONDS", "60"))          # rafraîchissement snapshot
ALT_WINDOW_HOURS = int(os.environ.get("ALTSEASON_WINDOW_HOURS", "24"))         # fenêtre de calcul
ALT_VELOCITY_WINDOW_MIN = int(os.environ.get("ALTSEASON_VELOCITY_MIN", "60"))  # pour la vélocité TP/h

# Routes
TRADES_PAGE_SIZE = int(os.environ.get("TRADES_PAGE_SIZE", "120"))

# -------------------------
# Helpers temps
# -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")

def ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

# -------------------------
# DB layer (SQLite)
# -------------------------

def db_connect() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

DB = db_connect()

def db_execute(query: str, params: Iterable[Any] = ()) -> None:
    with DB:
        DB.execute(query, params)

def db_query(query: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    cur = DB.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return rows

def db_init() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    # Table des événements (webhook TV et dérivés)
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,              -- ENTRY | TP1_HIT | TP2_HIT | TP3_HIT | SL_HIT | CLOSE | VECTOR_CANDLE | AOE_PREMIUM | AOE_DISCOUNT
            symbol TEXT NOT NULL,
            tf TEXT,                         -- ex: "15"
            trade_id TEXT,                   -- identifiant de trade si fourni
            time_ms INTEGER,                 -- timestamp TradingView en ms (si fourni)
            side TEXT,                       -- LONG | SHORT | ou NULL
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
            direction TEXT,                  -- pour VECTOR_CANDLE: UP|DOWN
            price REAL,                      -- prix associé si pertinent
            note TEXT,
            payload_json TEXT,               -- payload brut
            created_at TEXT NOT NULL         -- iso UTC
        )
        """
    )
    # Index utiles
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_time_ms ON events(time_ms)")

db_init()

# -------------------------
# FastAPI
# -------------------------

app = FastAPI(title=APP_TITLE)

# -------------------------
# Altseason snapshot
# (NE PREND PAS EN COMPTE les VECTOR_* comme demandé)
# -------------------------

class AltseasonSnapshot(Dict[str, Any]):
    """
    Structure:
    {
      "score": int 0..100,
      "updated_at": iso,
      "window_hours": int,
      "factors": {
         "tp_sl_diff": {"value": float, "desc": "..."},
         "breadth": {"value": float, "desc": "..."},
         "premium_spread": {"value": float, "desc": "..."},
         "tp_velocity": {"value": float, "desc": "..."},
      },
      "explain": "Texte court"
    }
    """

_altseason_snapshot: AltseasonSnapshot = AltseasonSnapshot(
    score=0,
    updated_at=now_iso(),
    window_hours=ALT_WINDOW_HOURS,
    factors={
        "tp_sl_diff": {"value": 0.0, "desc": "Différence TPs - SLs rapportée aux entrées"},
        "breadth": {"value": 0.0, "desc": "Largeur (alters distinctes qui performent)"},
        "premium_spread": {"value": 0.0, "desc": "Δ AOE_PREMIUM vs AOE_DISCOUNT"},
        "tp_velocity": {"value": 0.0, "desc": "Vitesse de TP récents vs moyenne"},
    },
    explain="Initialisation…"
)

_altseason_lock = asyncio.Lock()

def _clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x

def _safe_ratio(a: float, b: float) -> float:
    return a / b if b else 0.0

def compute_altseason_snapshot() -> AltseasonSnapshot:
    """
    Calcule un score 0..100 basé sur 4 facteurs, SANS utiliser les VECTOR_CANDLE.
    Fenêtre: ALT_WINDOW_HOURS (par défaut 24h).
    """
    cutoff = (now_utc() - timedelta(hours=ALT_WINDOW_HOURS)).isoformat(timespec="seconds")

    # 1) Diff TPs - SLs rapportée aux entrées (seulement altcoins: on exclut BTC* et ETH*)
    rows = db_query(
        """
        SELECT type, symbol FROM events
        WHERE created_at >= ?
          AND type IN ('ENTRY','TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT')
        """,
        (cutoff,)
    )

    def is_alt(sym: str) -> bool:
        s = sym.upper()
        return not (s.startswith("BTC") or s.startswith("ETH"))

    entries = sum(1 for r in rows if r["type"] == "ENTRY" and is_alt(r["symbol"]))
    tp_hits = sum(1 for r in rows if r["type"] in ("TP1_HIT","TP2_HIT","TP3_HIT") and is_alt(r["symbol"]))
    sl_hits = sum(1 for r in rows if r["type"] == "SL_HIT" and is_alt(r["symbol"]))

    tp_sl_diff = _clamp01((tp_hits - sl_hits) / max(1, entries))  # [-∞,1] → clamp 0..1

    # 2) Breadth: nb d'altcoins distincts avec ≥1 TP_HIT / nb distinct avec un EVENT (ENTRY/SL/TP)
    syms_any = set(r["symbol"] for r in rows if is_alt(r["symbol"]))
    syms_tp = set(r["symbol"] for r in rows if r["type"] in ("TP1_HIT","TP2_HIT","TP3_HIT") and is_alt(r["symbol"]))
    breadth = _clamp01(_safe_ratio(len(syms_tp), len(syms_any)))

    # 3) Premium spread: AOE_PREMIUM vs AOE_DISCOUNT
    rows_aoe = db_query(
        """
        SELECT type FROM events
        WHERE created_at >= ?
          AND type IN ('AOE_PREMIUM','AOE_DISCOUNT')
        """,
        (cutoff,)
    )
    cnt_prem = sum(1 for r in rows_aoe if r["type"] == "AOE_PREMIUM")
    cnt_disc = sum(1 for r in rows_aoe if r["type"] == "AOE_DISCOUNT")
    # Normalisation simple: tanh-like par division par (prem+disc)
    premium_spread = _clamp01(_safe_ratio(cnt_prem - cnt_disc, max(1, cnt_prem + cnt_disc)))

    # 4) TP velocity: TPs dans la dernière heure vs leur moyenne /h sur la fenêtre
    cutoff_1h = (now_utc() - timedelta(minutes=ALT_VELOCITY_WINDOW_MIN)).isoformat(timespec="seconds")
    tp_recent = db_query(
        """
        SELECT COUNT(*) AS c FROM events
         WHERE created_at >= ?
           AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT')
        """,
        (cutoff_1h,)
    )[0]["c"]
    # moyenne/h sur la fenêtre
    tp_window = db_query(
        """
        SELECT COUNT(*) AS c FROM events
         WHERE created_at >= ?
           AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT')
        """,
        (cutoff,)
    )[0]["c"]
    hours = max(1.0, ALT_WINDOW_HOURS)
    avg_per_hour = tp_window / hours
    tp_velocity = _clamp01(_safe_ratio(tp_recent, max(1e-9, avg_per_hour)))

    # Pondérations (somme=1)
    w1, w2, w3, w4 = 0.35, 0.25, 0.20, 0.20
    score01 = w1*tp_sl_diff + w2*breadth + w3*premium_spread + w4*tp_velocity
    score = int(round(100 * _clamp01(score01)))

    explain = "Score basé sur 4 facteurs: Diff TP-SL, Breadth, Premium spread, Vélocité TP. Vectors exclus."
    snap = AltseasonSnapshot(
        score=score,
        updated_at=now_iso(),
        window_hours=ALT_WINDOW_HOURS,
        factors={
            "tp_sl_diff": {"value": float(round(tp_sl_diff, 3)), "desc": "Différence TPs - SLs rapportée aux entrées"},
            "breadth": {"value": float(round(breadth, 3)), "desc": "Largeur (alters distinctes qui performent)"},
            "premium_spread": {"value": float(round(premium_spread, 3)), "desc": "Δ AOE_PREMIUM vs AOE_DISCOUNT"},
            "tp_velocity": {"value": float(round(tp_velocity, 3)), "desc": "Vitesse de TP récents vs moyenne"},
        },
        explain=explain
    )
    return snap

async def altseason_daemon():
    """
    Tâche de fond: recalcul périodique du snapshot altseason.
    """
    global _altseason_snapshot
    while True:
        try:
            snap = compute_altseason_snapshot()
            async with _altseason_lock:
                _altseason_snapshot = snap
        except Exception as e:
            # On journalise mais on continue
            print(f"[altseason_daemon] error: {e}")
        await asyncio.sleep(ALT_INTERVAL)

@app.on_event("startup")
async def on_startup():
    # Démarre le daemon altseason
    asyncio.create_task(altseason_daemon())
# =========================
# main.py — Bloc 2/5
# Webhook TradingView, DB save, Telegram (Vector UP = 🟩 / DOWN = 🟥)
# =========================

# -------------------------
# Telegram
# -------------------------

_last_telegram_sent_at: float = 0.0

async def send_telegram(msg: str, disable_web_page_preview: bool = True) -> None:
    """
    Envoie un message Telegram avec anti-spam (cooldown).
    Vector UP/DOWN: carré VERT/ROUGE.
    """
    global _last_telegram_sent_at
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    now = time.time()
    if now - _last_telegram_sent_at < TELEGRAM_COOLDOWN_SECONDS:
        # Anti-flash flood: on loggue juste
        print("WARNING:aitrader:Telegram send skipped due to cooldown")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            _last_telegram_sent_at = now
            print("INFO:httpx:HTTP Request: POST https://api.telegram.org/bot****/sendMessage \"HTTP/1.1", r.status_code, "OK\"")
    except Exception as e:
        print(f"WARNING:aitrader:Telegram send failed: {e}")

def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "-"
    # Gestion de petits prix (meme coins)
    if p == 0:
        return "0"
    mag = abs(p)
    if mag >= 1000:
        return f"{p:,.2f}"
    if mag >= 1:
        return f"{p:.4f}"
    if mag >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"

def _is_valid_secret(payload: Dict[str, Any]) -> bool:
    secret = payload.get("secret")
    return (WEBHOOK_SECRET == "" or secret == WEBHOOK_SECRET)

def _extract_str(d: Dict[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    v = d.get(key, default)
    if v is None:
        return None
    return str(v)

def _extract_float(d: Dict[str, Any], key: str) -> Optional[float]:
    v = d.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _extract_int(d: Dict[str, Any], key: str) -> Optional[int]:
    v = d.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def save_event(payload: Dict[str, Any]) -> None:
    """
    Sauvegarde un événement TradingView dans la DB.
    """
    ev_type = _extract_str(payload, "type", "").upper()
    symbol = _extract_str(payload, "symbol", "")
    tf = _extract_str(payload, "tf")
    trade_id = _extract_str(payload, "trade_id")
    time_ms = _extract_int(payload, "time")
    side = _extract_str(payload, "side")
    entry = _extract_float(payload, "entry")
    sl = _extract_float(payload, "sl")
    tp1 = _extract_float(payload, "tp1")
    tp2 = _extract_float(payload, "tp2")
    tp3 = _extract_float(payload, "tp3")
    r1 = _extract_float(payload, "r1")
    s1 = _extract_float(payload, "s1")
    lev_reco = _extract_float(payload, "lev_reco")
    qty_reco = _extract_float(payload, "qty_reco")
    notional = _extract_float(payload, "notional")
    confidence = _extract_int(payload, "confidence")
    horizon = _extract_str(payload, "horizon")
    leverage = _extract_str(payload, "leverage")
    direction = _extract_str(payload, "direction")
    price = _extract_float(payload, "price")
    note = _extract_str(payload, "note")

    db_execute(
        """
        INSERT INTO events (
            type, symbol, tf, trade_id, time_ms, side, entry, sl, tp1, tp2, tp3,
            r1, s1, lev_reco, qty_reco, notional, confidence, horizon, leverage,
            direction, price, note, payload_json, created_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ev_type, symbol, tf, trade_id, time_ms, side, entry, sl, tp1, tp2, tp3,
            r1, s1, lev_reco, qty_reco, notional, confidence, horizon, leverage,
            direction, price, note, json.dumps(payload, ensure_ascii=False), now_iso(),
        )
    )

# -------------------------
# Format des messages Telegram
# -------------------------

def build_telegram_message(payload: Dict[str, Any]) -> Optional[str]:
    """
    Compose un message Telegram propre selon le type d'événement.
    VECTOR_CANDLE:
      - UP => carré vert 🟩
      - DOWN => carré rouge 🟥
    """
    t = _extract_str(payload, "type", "").upper()
    sym = _extract_str(payload, "symbol", "")
    tf_label = _extract_str(payload, "tf_label") or _extract_str(payload, "tf") or ""
    side = _extract_str(payload, "side") or ""
    entry = _extract_float(payload, "entry")
    sl = _extract_float(payload, "sl")
    tp1 = _extract_float(payload, "tp1")
    tp2 = _extract_float(payload, "tp2")
    tp3 = _extract_float(payload, "tp3")
    price = _extract_float(payload, "price")
    direction = (_extract_str(payload, "direction") or "").upper()
    note = _extract_str(payload, "note") or ""

    if t == "ENTRY":
        parts = [
            f"📥 <b>ENTRY {html.escape(side or '')}</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})",
        ]
        if entry is not None: parts.append(f"• Entry: <b>{_fmt_price(entry)}</b>")
        if sl is not None:    parts.append(f"• SL: {_fmt_price(sl)}")
        tps = []
        if tp1 is not None: tps.append(f"TP1 {_fmt_price(tp1)}")
        if tp2 is not None: tps.append(f"TP2 {_fmt_price(tp2)}")
        if tp3 is not None: tps.append(f"TP3 {_fmt_price(tp3)}")
        if tps:
            parts.append("• " + "  |  ".join(tps))
        if note:
            parts.append(f"— {html.escape(note)}")
        return "\n".join(parts)

    if t in ("TP1_HIT","TP2_HIT","TP3_HIT"):
        tp_label = t.replace("_HIT", "")
        parts = [
            f"✅ <b>{tp_label} HIT</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})",
        ]
        if price is not None:
            parts.append(f"• {tp_label} @ <b>{_fmt_price(price)}</b>")
        if side:
            parts.append(f"• Side: {html.escape(side)}")
        return "\n".join(parts)

    if t == "SL_HIT":
        parts = [
            f"⛔ <b>SL HIT</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})",
        ]
        if price is not None:
            parts.append(f"• SL @ <b>{_fmt_price(price)}</b>")
        if side:
            parts.append(f"• Side: {html.escape(side)}")
        return "\n".join(parts)

    if t == "CLOSE":
        parts = [
            f"🔚 <b>CLOSE</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})",
        ]
        reason = _extract_str(payload, "reason")
        if reason:
            parts.append(f"• Raison: {html.escape(reason)}")
        if side:
            parts.append(f"• Side: {html.escape(side)}")
        return "\n".join(parts)

    if t == "AOE_PREMIUM":
        return f"🟡 <b>AOE PREMIUM</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})"

    if t == "AOE_DISCOUNT":
        return f"🟣 <b>AOE DISCOUNT</b> — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})"

    if t == "VECTOR_CANDLE":
        if direction == "UP":
            # carré VERT demandé
            sq = "🟩"
            head = f"{sq} <b>Vector Candle UP</b>"
        elif direction == "DOWN":
            sq = "🟥"
            head = f"{sq} <b>Vector Candle DOWN</b>"
        else:
            head = "🟦 <b>Vector Candle</b>"

        parts = [f"{head} — <code>{html.escape(sym)}</code> ({html.escape(tf_label)})"]
        if price is not None:
            parts.append(f"• Prix: <b>{_fmt_price(price)}</b>")
        if note:
            parts.append(f"— {html.escape(note)}")
        return "\n".join(parts)

    # Par défaut: pas de message
    return None

# -------------------------
# Webhook route
# -------------------------

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    # Secret check
    if not _is_valid_secret(payload):
        raise HTTPException(status_code=403, detail="Forbidden: bad secret")

    # Log
    print(f"INFO:aitrader:Webhook payload: {payload}")

    # Enregistrement DB
    try:
        save_event(payload)
        sym = payload.get("symbol")
        t = payload.get("type")
        tf = payload.get("tf")
        if isinstance(tf, str):
            tf_sfx = tf
        else:
            tf_sfx = str(tf) if tf is not None else ""
        # Fabrique un trade_id si manquant pour cohérence d'affichage
        if not payload.get("trade_id"):
            # suffixe avec epoch ms pour unicité
            suffix = int(time.time() * 1000)
            gen_trade_id = f"{sym}_{tf_sfx}_{suffix}"
            print(f"INFO:aitrader:Saved event: type={t} symbol={sym} tf={tf_sfx} trade_id={gen_trade_id}")
        else:
            print(f"INFO:aitrader:Saved event: type={t} symbol={sym} tf={tf_sfx} trade_id={payload.get('trade_id')}")
    except Exception as e:
        print(f"ERROR:aitrader:save_event failed: {e}")
        # On ne bloque pas le 200 TradingView
        # mais on remonte l'erreur au client si nécessaire
        # Ici on continue pour compat

    # Telegram (optionnel, respect cooldown)
    try:
        msg = build_telegram_message(payload)
        if msg:
            await send_telegram(msg)
    except Exception as e:
        print(f"WARNING:aitrader:Telegram build/send error: {e}")

    return JSONResponse({"status": "ok"})
# =========================
# main.py — Bloc 3/5
# Route /trades : affiche un tableau complet des trades + header "Indicateurs Altseason"
# - TP1/TP2/TP3 deviennent verts quand HIT
# - SL devient rouge quand HIT
# - Les VECTOR_CANDLE NE comptent PAS dans la chaleur Altseason
# =========================

from fastapi.responses import HTMLResponse
from collections import defaultdict, deque
from datetime import timedelta

# ------------- Helpers Altseason -------------

MAJORS = {
    "BTCUSDT.P","ETHUSDT.P","BTCUSD.P","ETHUSD.P",
    "BTCUSDC.P","ETHUSDC.P","BTCUSD","ETHUSD","BTCUSDT","ETHUSDT",
    "BTC","ETH"
}

def is_alt(symbol: Optional[str]) -> bool:
    if not symbol:
        return False
    sym = symbol.upper()
    return sym not in MAJORS and sym.endswith((".P",".USDT.P",".USDC.P",".USD.P",".USDT",".USDC",".USD"))

def _now_ms() -> int:
    return int(time.time() * 1000)

def calc_altseason_snapshot(now_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    Calcule la 'Chaleur Altseason (0-100)' à partir de 4 signaux.
    EXCLUSION: on ignore VECTOR_CANDLE (ne déclenche pas l'altseason).
    Fenêtres utilisées (approx):
      - 2h pour l'activité actuelle (entries, breadth)
      - 4h pour les résultats (TP/SL hits)
    """
    if now_ms is None:
        now_ms = _now_ms()
    two_h_ms = now_ms - 2 * 3600 * 1000
    four_h_ms = now_ms - 4 * 3600 * 1000

    # Récupération brute des événements récents
    evs_2h = db_query(
        """
        SELECT type, symbol, time_ms FROM events
        WHERE time_ms >= ? AND type != 'VECTOR_CANDLE'
        """,
        (two_h_ms,)
    )
    evs_4h = db_query(
        """
        SELECT type, symbol, time_ms FROM events
        WHERE time_ms >= ? AND type != 'VECTOR_CANDLE'
        """,
        (four_h_ms,)
    )

    # Signaux
    # S1: activité 'ENTRY' LONG sur ALTS (2h)
    S1 = 0
    # S2: breadth - nb d'alts uniques ayant eu ENTRY ou AOE_*(2h)
    breadth_syms = set()
    # S3: hit-rate = TP hits / (TP hits + SL hits) (4h) sur ALTS
    tp_hits = 0
    sl_hits = 0
    # S4: momentum = croissance des entries alts entre la dernière heure et l'heure précédente
    one_h_ms = now_ms - 3600 * 1000
    evs_1h = [e for e in evs_2h if e["time_ms"] >= one_h_ms]
    evs_prev1h = [e for e in evs_2h if e["time_ms"] < one_h_ms]

    for e in evs_2h:
        t = (e["type"] or "").upper()
        sym = e["symbol"]
        if t == "ENTRY" and is_alt(sym):
            S1 += 1
            breadth_syms.add(sym)
        elif t.startswith("AOE_") and is_alt(sym):
            breadth_syms.add(sym)

    for e in evs_4h:
        t = (e["type"] or "").upper()
        sym = e["symbol"]
        if not is_alt(sym):
            continue
        if t in ("TP1_HIT","TP2_HIT","TP3_HIT"):
            tp_hits += 1
        elif t == "SL_HIT":
            sl_hits += 1

    # Normalisations simples (clips) -> 0..100
    # N1: S1 vs. seuil 30 entries (ajustable)
    N1 = min(100, int(S1 * (100/30)))  # 30 entries -> 100
    # N2: breadth vs. seuil 25 alts actives
    N2 = min(100, int(len(breadth_syms) * (100/25)))
    # N3: hit-rate
    denom = tp_hits + sl_hits
    hit_rate = (tp_hits / denom)*100 if denom > 0 else 0
    N3 = int(hit_rate)
    # N4: momentum des entries alts (1h vs 1h précédente)
    s_curr = sum(1 for e in evs_1h if (e["type"] or "").upper()=="ENTRY" and is_alt(e["symbol"]))
    s_prev = sum(1 for e in evs_prev1h if (e["type"] or "").upper()=="ENTRY" and is_alt(e["symbol"]))
    diff = max(0, s_curr - s_prev)  # seulement momentum positif
    N4 = min(100, int(diff * (100/15)))  # +15 entries d'écart -> 100

    heat = int((N1 + N2 + N3 + N4) / 4)

    # Qualificatif
    if heat >= 85:
        label = "Altseason (forte)"
    elif heat >= 60:
        label = "Altseason (modérée)"
    elif heat >= 35:
        label = "Neutre / Mixte"
    else:
        label = "Faible / Dominance majors"

    return {
        "heat": heat,
        "label": label,
        "components": {
            "Activité entries (2h)": N1,
            "Breadth alts (2h)": N2,
            "Hit-rate TP vs SL (4h)": N3,
            "Momentum entries (1h)": N4,
        },
        "raw": {
            "S1_entries_alts_2h": S1,
            "S2_breadth": len(breadth_syms),
            "S3_tp_hits": tp_hits,
            "S3_sl_hits": sl_hits,
            "S4_entries_curr1h": s_curr,
            "S4_entries_prev1h": s_prev,
        }
    }

# ------------- Construction du tableau -------------

def _row_key(ev) -> tuple:
    # ordonne par time décroissant avec fallback
    tm = ev.get("time_ms") or 0
    return (-int(tm), ev.get("symbol") or "", ev.get("type") or "")

def build_trade_rows() -> List[Dict[str, Any]]:
    """
    Recompose les trades par trade_id en agrégeant ENTRY + hits/close.
    """
    # on prend un historique large
    events = db_query(
        """
        SELECT type, symbol, tf, tf_label, trade_id, time_ms, side,
               entry, sl, tp1, tp2, tp3, direction, price, note, payload_json
        FROM events
        WHERE type != 'VECTOR_CANDLE'
        ORDER BY time_ms DESC
        LIMIT 1500
        """
    )

    # map par trade_id (ou par (symbol,tf_label,time_ms) si manquant)
    groups: Dict[str, Dict[str, Any]] = {}

    def gid(ev) -> str:
        tid = ev.get("trade_id")
        if tid:
            return tid
        # fallback pseudo-id stable par symbole + fenêtre 6h
        sym = (ev.get("symbol") or "UNK").upper()
        tf = ev.get("tf_label") or ev.get("tf") or ""
        tms = int(ev.get("time_ms") or 0) // (6*3600*1000)
        return f"{sym}_{tf}_{tms}"

    for ev in events:
        t = (ev["type"] or "").upper()
        gid_ = gid(ev)
        g = groups.setdefault(gid_, {
            "trade_id": gid_,
            "symbol": ev.get("symbol"),
            "tf": ev.get("tf_label") or ev.get("tf"),
            "time_ms": ev.get("time_ms"),
            "side": ev.get("side"),
            "entry": ev.get("entry"),
            "sl": ev.get("sl"),
            "tp1": ev.get("tp1"),
            "tp2": ev.get("tp2"),
            "tp3": ev.get("tp3"),
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "sl_hit": False,
            "closed": False,
        })

        # rafraîchit quelques champs s’ils apparaissent sur un ENTRY plus récent
        if t == "ENTRY":
            # si plusieurs ENTRY, garde le plus récent
            if (ev.get("time_ms") or 0) >= (g["time_ms"] or 0):
                for k in ("time_ms","side","entry","sl","tp1","tp2","tp3"):
                    if ev.get(k) is not None:
                        g[k] = ev.get(k)

        if t == "TP1_HIT": g["tp1_hit"] = True
        if t == "TP2_HIT": g["tp2_hit"] = True
        if t == "TP3_HIT": g["tp3_hit"] = True
        if t == "SL_HIT":  g["sl_hit"]  = True
        if t == "CLOSE":   g["closed"]  = True

    # transforme en liste, tri par time desc
    rows = list(groups.values())
    rows.sort(key=lambda r: (-(r["time_ms"] or 0), r.get("symbol") or ""))
    return rows[:400]

def _dt_str(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    try:
        dt = datetime.utcfromtimestamp(ms/1000.0)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"

# ------------- Route /trades -------------

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    # Altseason snapshot (ne compte pas VECTOR_CANDLE)
    snap = calc_altseason_snapshot()
    rows = build_trade_rows()

    # HTML/CSS/JS
    html_out = f"""
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Trades — Dashboard</title>
<style>
  :root {{
    --bg: #0b0f14;
    --card: #121821;
    --muted: #9fb3c8;
    --txt: #e6edf3;
    --green: #22c55e;
    --red: #ef4444;
    --amber: #f59e0b;
    --cyan: #06b6d4;
    --border: #1f2937;
    --chip: #1f2a37;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: linear-gradient(180deg, #0b0f14 0%, #0a0e13 100%);
    color: var(--txt); font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu;
  }}
  .wrap {{ max-width: 1400px; margin: 0 auto; }}
  .header {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 22px;
  }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.04);
  }}
  .title {{ font-size: 18px; margin: 0 0 8px 0; }}
  .muted {{ color: var(--muted); }}
  .heat {{
    display:flex; align-items:center; gap:16px;
  }}
  .heatbar {{
    flex:1; height: 12px; background: #11161d; border-radius: 999px; overflow: hidden; border:1px solid var(--border);
  }}
  .heatbar > div {{
    height: 100%;
    background: linear-gradient(90deg, #1ec8a5, #22c55e, #f59e0b, #ef4444);
    width: {snap["heat"]}%;
    transition: width .6s ease;
  }}
  .heatbulb {{
    font-weight: 700; font-size: 22px;
  }}
  .kpis {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
  .chip {{
    background: var(--chip); border:1px solid var(--border);
    border-radius: 999px; padding: 6px 10px; font-size: 12px;
  }}

  table {{
    width: 100%; border-collapse: collapse; overflow: hidden;
    border-radius: 16px; border:1px solid var(--border);
    background: var(--card);
  }}
  thead th {{
    text-align: left; padding: 12px; font-size: 12px; color: var(--muted); background: #0f141c;
    position: sticky; top: 0; z-index: 1; border-bottom:1px solid var(--border);
  }}
  tbody td {{
    padding: 10px 12px; border-bottom: 1px solid var(--border);
  }}
  tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .tag {{
    display:inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; border:1px solid var(--border);
    background:#0e1520; color:#b9d3ea;
  }}
  .hit {{ background: rgba(34, 197, 94, 0.18); color: #b7f7cf; border-radius: 8px; padding: 4px 6px; display:inline-block; }}
  .miss {{ opacity: .7; }}
  .slhit {{ background: rgba(239,68,68,0.18); color:#ffc4c4; border-radius: 8px; padding: 4px 6px; display:inline-block; }}
  .statusdot {{
    width:10px; height:10px; border-radius:50%;
    display:inline-block; vertical-align:middle; margin-right:8px;
    background:#475569;
  }}
  .statusdot.hit {{ background: var(--green); }}
  .statusdot.slhit {{ background: var(--red); }}
</style>
</head>
<body>
<div class="wrap">

  <!-- Indicateurs Altseason -->
  <div class="header">
    <div class="card">
      <h3 class="title">🔥 Indicateurs Altseason</h3>
      <div class="heat">
        <div class="heatbar"><div></div></div>
        <div class="heatbulb">{snap["heat"]}/100</div>
      </div>
      <div class="muted" style="margin-top:8px;">{snap["label"]}</div>
      <div class="kpis">
        {"".join(f'<span class="chip">{k}: <b>{v}</b></span>' for k,v in snap["components"].items())}
      </div>
      <div class="muted" style="margin-top:8px;font-size:12px;">
        Méthodo: moyenne de 4 signaux (2h activité entries alts, 2h breadth alts uniques, 4h hit-rate TP vs SL, 1h momentum des entries). 
        <b>Vector Candle exclu</b> du calcul.
      </div>
    </div>

    <div class="card">
      <h3 class="title">Légende & Statuts</h3>
      <div class="muted">
        <div style="margin-bottom:6px;"><span class="statusdot hit"></span>TP1/TP2/TP3 HIT → cellule verte</div>
        <div style="margin-bottom:6px;"><span class="statusdot slhit"></span>SL HIT → badge rouge</div>
        <div class="chip">AOE_* et ENTRY = pris en compte pour l’altseason</div>
        <div class="chip">VECTOR_CANDLE = <b>non pris en compte</b></div>
      </div>
    </div>
  </div>

  <!-- Tableau Trades -->
  <div class="card" style="padding:0;">
    <table>
      <thead>
        <tr>
          <th>Date (UTC)</th>
          <th>Symbole</th>
          <th>TF</th>
          <th>Side</th>
          <th class="mono">Entry</th>
          <th class="mono">SL</th>
          <th class="mono">TP1</th>
          <th class="mono">TP2</th>
          <th class="mono">TP3</th>
          <th>Statut</th>
        </tr>
      </thead>
      <tbody>
        {"".join(render_row(r) for r in rows)}
      </tbody>
    </table>
  </div>

</div>
<script>
  // Tout est server-side. JS futur: filtres/sort si besoin.
</script>
</body>
</html>
    """

    return HTMLResponse(html_out)


def render_row(r: Dict[str, Any]) -> str:
    dt = _dt_str(r.get("time_ms"))
    sym = html.escape(r.get("symbol") or "-")
    tf = html.escape(r.get("tf") or "-")
    side = html.escape(r.get("side") or "-")

    def fmt(x):
        return html.escape(_fmt_price(x))

    tp1_cls = "hit" if r.get("tp1_hit") else "miss"
    tp2_cls = "hit" if r.get("tp2_hit") else "miss"
    tp3_cls = "hit" if r.get("tp3_hit") else "miss"

    status_bits = []
    if r.get("tp1_hit"): status_bits.append('<span class="statusdot hit"></span>TP1')
    if r.get("tp2_hit"): status_bits.append('<span class="statusdot hit"></span>TP2')
    if r.get("tp3_hit"): status_bits.append('<span class="statusdot hit"></span>TP3')
    if r.get("sl_hit"):  status_bits.append('<span class="statusdot slhit"></span>SL')

    if not status_bits:
        status_html = '<span class="muted">—</span>'
    else:
        status_html = " ".join(status_bits)

    sl_html = f'<span class="slhit">{fmt(r.get("sl"))}</span>' if r.get("sl_hit") else fmt(r.get("sl"))

    return f"""
<tr>
  <td class="mono">{dt}</td>
  <td><span class="tag">{sym}</span></td>
  <td>{tf}</td>
  <td>{side}</td>
  <td class="mono">{fmt(r.get("entry"))}</td>
  <td class="mono">{sl_html}</td>
  <td class="mono"><span class="{tp1_cls}">{fmt(r.get("tp1"))}</span></td>
  <td class="mono"><span class="{tp2_cls}">{fmt(r.get("tp2"))}</span></td>
  <td class="mono"><span class="{tp3_cls}">{fmt(r.get("tp3"))}</span></td>
  <td>{status_html}</td>
</tr>
"""# =========================
# main.py — Bloc 4/5
# DB utils (SQLite), schéma, helpers communs (_fmt_price, html, time utils)
# S'initialise proprement même si déjà défini par un autre bloc.
# =========================

import os, sqlite3, time, json, html
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------- Helpers temps & formats ----------

def now_ms() -> int:
    return int(time.time() * 1000)

def now_iso() -> str:
    # ISO UTC sans microsecondes
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except Exception:
        return str(x)
    # Affichage adaptatif
    if v == 0:
        return "0"
    abs_v = abs(v)
    if abs_v >= 1000:
        return f"{v:,.2f}".replace(",", " ").replace("\xa0", " ")
    if abs_v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if abs_v >= 0.01:
        return f"{v:.6f}".rstrip("0").rstrip(".")
    # très petit -> scientifique compact
    return f"{v:.2e}"

# ---------- Connexion DB & row factory ----------

DB_PATH = os.environ.get("DB_PATH", "/tmp/ai_trader/data.db")

def _dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

# Singleton connexion
if "_DB_CONN" not in globals():
    _DB_CONN = _get_conn()

# ---------- Exposition helpers DB (si non déjà définis) ----------

if "db_execute" not in globals():
    def db_execute(sql: str, params: Tuple = ()) -> None:
        cur = _DB_CONN.cursor()
        cur.execute(sql, params)
        _DB_CONN.commit()

if "db_query" not in globals():
    def db_query(sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        cur = _DB_CONN.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

# ---------- Schéma & index ----------

def ensure_schema():
    # Table des événements bruts (webhook)
    db_execute("""
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT    NOT NULL,
        time_ms      INTEGER NOT NULL,
        type         TEXT    NOT NULL,
        symbol       TEXT,
        tf           TEXT,
        tf_label     TEXT,
        trade_id     TEXT,
        side         TEXT,
        entry        REAL,
        sl           REAL,
        tp1          REAL,
        tp2          REAL,
        tp3          REAL,
        direction    TEXT,
        price        REAL,
        note         TEXT,
        payload_json TEXT
    );
    """)

    # Index utiles
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time_ms);")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol);")
    db_execute("CREATE INDEX IF NOT EXISTS idx_events_tradeid ON events(trade_id);")

ensure_schema()

# ---------- Enregistrement d'un évènement (utilisable par /tv-webhook) ----------

def save_event(
    etype: str,
    symbol: Optional[str] = None,
    tf: Optional[str] = None,
    tf_label: Optional[str] = None,
    trade_id: Optional[str] = None,
    side: Optional[str] = None,
    entry: Optional[float] = None,
    sl: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    direction: Optional[str] = None,
    price: Optional[float] = None,
    note: Optional[str] = None,
    time_override_ms: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Insert propre dans la table events.
    Remplit created_at et time_ms si manquants.
    """
    tms = time_override_ms if time_override_ms is not None else now_ms()
    created = now_iso()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)

    db_execute(
        """
        INSERT INTO events
        (created_at, time_ms, type, symbol, tf, tf_label, trade_id, side,
         entry, sl, tp1, tp2, tp3, direction, price, note, payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            created, tms, etype, symbol, tf, tf_label, trade_id, side,
            entry, sl, tp1, tp2, tp3, direction, price, note, payload_json
        )
    )
# =========================
# main.py — Bloc 5/5
# - Endpoint /tv-webhook (sécurisé par SECRET)
# - Envoi Telegram : badge 🟩 (vert) pour VECTOR_CANDLE UP, 🔻 rouge pour DOWN
# - /trades : tableau propre, TP1/TP2/TP3 deviennent VERT quand “HIT”
# - Les VECTOR_CANDLE sont listées à part (et n’influencent PAS l’Altseason)
# =========================

import os, time, json
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# ---------- Récup helpers définis dans les blocs 1-4 ----------
if "db_query" not in globals() or "db_execute" not in globals():
    raise RuntimeError("db_execute/db_query doivent être définis par les blocs précédents.")

if "_fmt_price" not in globals():
    def _fmt_price(x: Optional[float]) -> str:
        return "-" if x is None else str(x)

if "now_ms" not in globals():
    import time as _t
    def now_ms() -> int: return int(_t.time()*1000)

if "now_iso" not in globals():
    from datetime import datetime as _dt
    def now_iso() -> str: return _dt.utcnow().replace(microsecond=0).isoformat()+"Z"

if "save_event" not in globals():
    # Filet de sécurité (ne devrait pas arriver si Bloc 4 posé)
    def save_event(**kwargs):  # type: ignore
        payload = kwargs.copy()
        db_execute("INSERT INTO events(created_at,time_ms,type,payload_json) VALUES(?,?,?,?)",
                   (now_iso(), now_ms(), payload.get("etype","UNKNOWN"), json.dumps(payload, ensure_ascii=False)))

# ---------- App FastAPI ----------
if "app" not in globals():
    app = FastAPI(title="AI Trader")

# ---------- Sécurité Webhook ----------
TV_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "").strip()

def _check_secret(payload: Dict[str, Any]):
    if not TV_SECRET:
        return  # pas de secret configuré => passthrough
    # le champ “secret” peut être au niveau racine
    if payload.get("secret") != TV_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

# ---------- Telegram ----------
import httpx

TG_BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TG_COOLDOWN_SEC = int(os.environ.get("TELEGRAM_COOLDOWN_SEC", "6"))

_last_tg_sent_at = 0.0

async def send_telegram_ex(text: str, disable_web_page_preview: bool = True):
    global _last_tg_sent_at
    if not (TG_BOT and TG_CHAT):
        return
    now = time.time()
    if now - _last_tg_sent_at < TG_COOLDOWN_SEC:
        # Respecter cooldown, ne rien envoyer (logs côté serveur déjà présents)
        return
    _last_tg_sent_at = now
    url = f"https://api.telegram.org/bot{TG_BOT}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": disable_web_page_preview}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()

def _escape(s: Any) -> str:
    try:
        return (str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    except Exception:
        return str(s)

def _fmt_symbol(sym: Optional[str]) -> str:
    return _escape(sym or "-")

def _fmt_tf(tf_label: Optional[str], tf: Optional[str]) -> str:
    return _escape(tf_label or tf or "-")

def _fmt_pct(p: Optional[float]) -> str:
    if p is None: return "-"
    try:
        return f"{float(p)*100:.2f}%"
    except Exception:
        return str(p)

def _vector_badge(direction: Optional[str]) -> str:
    # Carré VERT pour UP (souhait utilisateur), flèche rouge pour DOWN.
    if (direction or "").upper() == "UP":
        return "🟩"
    if (direction or "").upper() == "DOWN":
        return "🔻"
    return "🟪"  # fallback neutre

def format_telegram_message(evt: Dict[str, Any]) -> str:
    etype = (evt.get("type") or "").upper()
    sym = _fmt_symbol(evt.get("symbol"))
    tf = _fmt_tf(evt.get("tf_label"), evt.get("tf"))
    # Entrée
    if etype == "ENTRY":
        side = _escape(evt.get("side") or "?")
        entry = _fmt_price(evt.get("entry"))
        sl = _fmt_price(evt.get("sl"))
        tp1 = _fmt_price(evt.get("tp1"))
        tp2 = _fmt_price(evt.get("tp2"))
        tp3 = _fmt_price(evt.get("tp3"))
        conf = evt.get("confidence")
        conf_s = f" | Confiance: {conf}%" if conf is not None else ""
        return (f"🚀 <b>ENTRY</b> — <b>{sym}</b> {tf}\n"
                f"Side: {side}{conf_s}\n"
                f"Entrée: {entry} | SL: {sl}\n"
                f"TP1: {tp1} | TP2: {tp2} | TP3: {tp3}")
    # TP / SL / CLOSE
    if etype in ("TP1_HIT","TP2_HIT","TP3_HIT"):
        tp_name = etype.replace("_HIT", "")
        return f"✅ <b>{tp_name}</b> — <b>{sym}</b> {tf}"
    if etype == "SL_HIT":
        return f"🛑 <b>Stop Loss</b> — <b>{sym}</b> {tf}"
    if etype == "CLOSE":
        reason = evt.get("reason")
        reason_s = f" ({_escape(reason)})" if reason else ""
        return f"🔚 <b>Close</b> — <b>{sym}</b> {tf}{reason_s}"
    # AOE (neutre)
    if etype in ("AOE_PREMIUM", "AOE_DISCOUNT"):
        label = "Premium" if etype.endswith("PREMIUM") else "Discount"
        return f"📊 <b>AOE {label}</b> — <b>{sym}</b> {tf}"
    # Vectors (badge vert pour UP)
    if etype == "VECTOR_CANDLE":
        badge = _vector_badge(evt.get("direction"))
        price = _fmt_price(evt.get("price"))
        note = evt.get("note")
        note_s = f" — {_escape(note)}" if note else ""
        return f"{badge} <b>Vector Candle</b> — <b>{sym}</b> {tf} @ {price}{note_s}"
    # fallback
    return f"ℹ️ <b>{etype}</b> — <b>{sym}</b> {tf}"

# ---------- Webhook TradingView ----------
@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    _check_secret(payload)

    etype = (payload.get("type") or "").upper()
    symbol = payload.get("symbol")
    tf = payload.get("tf")
    tf_label = payload.get("tf_label")
    trade_id = payload.get("trade_id")
    side = payload.get("side")
    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    tp3 = payload.get("tp3")
    direction = payload.get("direction")
    price = payload.get("price")
    note = payload.get("note")
    t = payload.get("time")
    t_ms = int(t) if isinstance(t, (int, float)) else None

    # enregistre
    save_event(
        etype=etype,
        symbol=symbol,
        tf=tf,
        tf_label=tf_label,
        trade_id=trade_id,
        side=side,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        direction=direction,
        price=price,
        note=note,
        time_override_ms=t_ms,
        payload=payload,
    )

    # Envoi Telegram (vector UP -> carré VERT)
    try:
        txt = format_telegram_message({"type": etype, "symbol": symbol, "tf": tf, "tf_label": tf_label,
                                       "side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                                       "direction": direction, "price": price, "note": note, "reason": payload.get("reason"),
                                       "confidence": payload.get("confidence")})
        await send_telegram_ex(txt)
    except Exception:
        # on n'échoue pas le webhook si Telegram rate
        pass

    return JSONResponse({"ok": True})

# ---------- Agrégation “state des trades” ----------

def _latest_by_trade_id() -> Dict[str, Dict[str, Any]]:
    """
    Retourne un dict par trade_id avec l'état courant (ENTRY/TPs/SL/CLOSE).
    """
    rows = db_query("""
        SELECT * FROM events
        WHERE trade_id IS NOT NULL
        ORDER BY time_ms ASC, id ASC
    """)
    state: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        tid = r.get("trade_id")
        if not tid:
            continue
        et = (r.get("type") or "").upper()
        st = state.get(tid) or {
            "trade_id": tid, "symbol": r.get("symbol"), "tf": r.get("tf"), "tf_label": r.get("tf_label"),
            "side": None, "entry": None, "sl": None, "tp1": None, "tp2": None, "tp3": None,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_hit": False, "closed": False,
            "first_ms": r.get("time_ms"), "last_ms": r.get("time_ms")
        }
        st["last_ms"] = r.get("time_ms")
        # Mise à jour
        if et == "ENTRY":
            st["side"] = r.get("side")
            st["entry"] = r.get("entry")
            st["sl"] = r.get("sl")
            st["tp1"] = r.get("tp1")
            st["tp2"] = r.get("tp2")
            st["tp3"] = r.get("tp3")
        elif et == "TP1_HIT":
            st["tp1_hit"] = True
        elif et == "TP2_HIT":
            st["tp2_hit"] = True
        elif et == "TP3_HIT":
            st["tp3_hit"] = True
        elif et == "SL_HIT":
            st["sl_hit"] = True
        elif et == "CLOSE":
            st["closed"] = True
        state[tid] = st
    return state

def _latest_vectors(limit: int = 30) -> List[Dict[str, Any]]:
    return db_query("""
        SELECT symbol, tf, tf_label, direction, price, time_ms
        FROM events
        WHERE type='VECTOR_CANDLE'
        ORDER BY time_ms DESC, id DESC
        LIMIT ?
    """, (limit,))

# ---------- Page /trades ----------
@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    state = _latest_by_trade_id()
    # tri : le plus récent d’abord
    trades = sorted(state.values(), key=lambda x: (x.get("last_ms") or 0), reverse=True)

    vectors = _latest_vectors(24)  # 24 derniers, affichés dans un bandeau séparé (n’impacte pas Altseason)

    # Styles minimaux (TP colorés)
    css = """
    <style>
    :root{
      --bg:#0b0e14; --card:#121623; --muted:#8ea0b5; --ok:#1db954; --warn:#f5a623; --err:#ff4d4f; --ink:#e5eef9;
      --up:#1db954; --down:#ff4d4f; --vec:#7c3aed;
    }
    body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif;}
    .wrap{padding:18px;max-width:1400px;margin:0 auto;}
    h1{font-size:22px;margin:0 0 12px 0}
    .grid{display:grid;gap:14px}
    .card{background:var(--card);border:1px solid #1c2030;border-radius:14px;padding:14px}
    table{width:100%;border-collapse:collapse;}
    th,td{padding:10px 8px;border-bottom:1px solid #1c2030;text-align:left;font-size:14px}
    th{color:var(--muted);font-weight:600}
    .mono{font-variant-numeric:tabular-nums}
    .badge{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;font-size:12px}
    .side-long{background:#0f2a19;color:#9ff3b8;border:1px solid #1e5633}
    .side-short{background:#2a1010;color:#ffb0b0;border:1px solid #5e1e1e}
    .status{font-weight:600}
    .hit{background:#0f2a19;color:#9ff3b8;border-radius:8px;padding:2px 6px;border:1px solid #1e5633}
    .pending{background:#1b2234;color:#c7d3ea;border-radius:8px;padding:2px 6px;border:1px dashed #2a3550}
    .miss{background:#2a1010;color:#ffb0b0;border-radius:8px;padding:2px 6px;border:1px solid #5e1e1e;text-decoration:line-through}
    .vec-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
    .vec{display:flex;align-items:center;gap:10px;background:#0e1220;border:1px solid #1c2030;border-radius:10px;padding:8px 10px}
    .dot{width:10px;height:10px;border-radius:3px;background:var(--vec);display:inline-block}
    .dot.up{background:var(--up)}
    .dot.down{background:var(--down)}
    .pill{font-size:12px;color:#b7c2d6}
    .sym{font-weight:700}
    .tf{color:#9fb0c8;font-size:12px}
    .sep{height:1px;background:#1c2030;margin:10px 0}
    </style>
    """

    # Table trades
    rows_html = []
    for t in trades:
        sym = _escape(t.get("symbol"))
        tf = _escape(t.get("tf_label") or t.get("tf") or "-")
        side = (t.get("side") or "").upper()
        side_badge = f'<span class="badge side-long">LONG</span>' if side=="LONG" else (f'<span class="badge side-short">SHORT</span>' if side=="SHORT" else "-")

        entry = _fmt_price(t.get("entry"))
        sl = _fmt_price(t.get("sl"))
        tp1 = _fmt_price(t.get("tp1"))
        tp2 = _fmt_price(t.get("tp2"))
        tp3 = _fmt_price(t.get("tp3"))

        tp1_cls = "hit" if t.get("tp1_hit") else ("miss" if t.get("sl_hit") else "pending")
        tp2_cls = "hit" if t.get("tp2_hit") else ("miss" if t.get("sl_hit") else "pending")
        tp3_cls = "hit" if t.get("tp3_hit") else ("miss" if t.get("sl_hit") else "pending")

        status = "CLOSÉ" if t.get("closed") else ("SL" if t.get("sl_hit") else ("EN COURS" if t.get("entry") else ""))
        status_html = f'<span class="status">{_escape(status)}</span>'

        rows_html.append(
            "<tr>"
            f"<td><span class='sym'>{sym}</span><div class='tf'>{tf}</div></td>"
            f"<td>{side_badge}</td>"
            f"<td class='mono'>{entry}</td>"
            f"<td class='mono'>{sl}</td>"
            f"<td><span class='{tp1_cls}'>{tp1}</span></td>"
            f"<td><span class='{tp2_cls}'>{tp2}</span></td>"
            f"<td><span class='{tp3_cls}'>{tp3}</span></td>"
            f"<td>{status_html}</td>"
            "</tr>"
        )

    if not rows_html:
        rows_html.append("<tr><td colspan='8' style='color:#9fb0c8;'>Aucun trade enregistré pour le moment.</td></tr>")

    table_html = f"""
    <div class="card">
      <h1>Trades</h1>
      <table>
        <thead>
          <tr>
            <th>Symbole</th>
            <th>Side</th>
            <th>Entrée</th>
            <th>SL</th>
            <th>TP1</th>
            <th>TP2</th>
            <th>TP3</th>
            <th>Statut</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </div>
    """

    # Bandeau vectors (n’influence PAS Altseason)
    vec_items = []
    for v in vectors:
        sym = _escape(v.get("symbol"))
        tf = _escape(v.get("tf_label") or v.get("tf") or "-")
        price = _fmt_price(v.get("price"))
        diru = (v.get("direction") or "").upper()
        dot_cls = "dot up" if diru == "UP" else ("dot down" if diru == "DOWN" else "dot")
        vec_items.append(f"""
          <div class="vec">
            <span class="{dot_cls}"></span>
            <div>
              <div><span class="sym">{sym}</span> <span class="pill">Vector</span></div>
              <div class="tf">{tf} @ <span class="mono">{price}</span></div>
            </div>
          </div>
        """)
    vec_html = f"""
    <div class="card">
      <h1>Vector Candles (dernieres 24)</h1>
      <div class="vec-list">
        {''.join(vec_items) if vec_items else "<div class='pill'>Aucun signal récent.</div>"}
      </div>
    </div>
    """

    html_out = f"""<!doctype html>
    <html lang="fr">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>Trades</title>
        {css}
      </head>
      <body>
        <div class="wrap grid">
          {table_html}
          {vec_html}
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html_out)

