# ============ main.py — BLOC 1/5 (Imports, Config, App, DB boot, Helpers) ============
import os
import re
import json
import time
import sqlite3
import logging
import threading
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

# -------------------------
# FastAPI app (IMPORTANT: doit être défini AVANT tout @app.get)
# -------------------------
app = FastAPI(title="AI Trader PRO")

# -------------------------
# Config / ENV
# -------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_ENABLED = os.getenv("LLM_ENABLED", "0") in ("1", "true", "True")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
FORCE_LLM = os.getenv("FORCE_LLM", "0") in ("1", "true", "True")
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0") or 0.0)

PORT = int(os.getenv("PORT", "8000"))

RISK_ACCOUNT_BAL = float(os.getenv("RISK_ACCOUNT_BAL", "0") or 0)
RISK_PCT = float(os.getenv("RISK_PCT", "1.0") or 1.0)

# DB path default = data/data.db; fallback auto vers /tmp si read-only
DB_PATH = os.getenv("DB_PATH", "data/data.db")
DEBUG_MODE = os.getenv("DEBUG", "0") in ("1", "true", "True")

# -------------------------
# ALTSEASON thresholds
# -------------------------
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))  # seconds
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1", "true", "True")
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0

# -------------------------
# OpenAI client (optionnel)
# -------------------------
_openai_client = None
_llm_reason_down = None
if LLM_ENABLED and OPENAI_API_KEY:
    try:
        from openai import OpenAI  # type: ignore
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        _llm_reason_down = f"OpenAI client init failed: {e}"
else:
    _llm_reason_down = "LLM disabled or OPENAI_API_KEY missing"

# -------------------------
# Helpers généraux
# -------------------------
def tf_label_of(payload: Dict[str, Any]) -> str:
    """Joli libellé TF (ex: '15m', '1h', '1D')."""
    label = str(payload.get("tf_label") or payload.get("tf") or "?")
    try:
        if label.isdigit():
            n = int(label)
            if n < 60:
                return f"{n}m"
            if n % 60 == 0 and n < 1440:
                return f"{n//60}h"
            if n == 1440:
                return "1D"
    except Exception:
        pass
    return label

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )

def fmt_num(v) -> str:
    try:
        if v is None:
            return "—"
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "—")

def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        if a is None or b is None or b == 0:
            return None
        return (a - b) / b * 100.0
    except Exception:
        return None

def parse_leverage_x(leverage: Optional[str]) -> Optional[float]:
    if not leverage:
        return None
    try:
        s = leverage.lower().replace("x", " ").split()
        for token in s:
            if token.replace(".", "", 1).isdigit():
                return float(token)
    except Exception:
        return None
    return None

# =========================
# SQLite — init robuste
# =========================
def resolve_db_path() -> None:
    """Assure un chemin DB writable; fallback /tmp/ai_trader/data.db si besoin."""
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.info("DB dir OK: %s (using %s)", d, DB_PATH)
    except Exception as e:
        fallback_dir = "/tmp/ai_trader"
        os.makedirs(fallback_dir, exist_ok=True)
        DB_PATH = os.path.join(fallback_dir, "data.db")
        log.warning("DB dir '%s' not writable (%s). Falling back to %s", d, e, DB_PATH)

def db_conn() -> sqlite3.Connection:
    """Connexion SQLite avec options sensées."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def db_init() -> None:
    """Crée la table events si absente."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                type TEXT,
                symbol TEXT,
                tf TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                trade_id TEXT,
                raw_json TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf ON events(tf)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

# Boot DB
resolve_db_path()
db_init()
# ============ main.py — BLOC 2/5 (Telegram utils, LLM confiance, Webhook + Vector Candle) ============

# ---------- Telegram (anti-spam simple) ----------
def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (sans pin, sans inline keyboard)."""
    global _last_tg
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        now = time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            return False
        _last_tg = now

        import urllib.request, urllib.parse
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(api_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False

def send_telegram_ex(text: str, pin: bool = False) -> Dict[str, Any]:
    """
    Envoi enrichi (inline button vers /trades) + option pin.
    NOTE: par défaut, on NE PIN PAS les messages d'ENTRY. (pin=False)
    """
    result = {"ok": False, "message_id": None, "pinned": False, "error": None}
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        result["error"] = "Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
        return result

    try:
        import urllib.request, urllib.parse, json as _json, time as _time
        global _last_tg
        now = _time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            # Soft rate-limit: on ne renvoie pas d’erreur dure
            result["ok"] = True
            result["error"] = "rate-limited (cooldown)"
            return result
        _last_tg = now

        api_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

        # sendMessage avec bouton "Voir les trades"
        send_url = f"{api_base}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "reply_markup": _json.dumps({
                "inline_keyboard": [[
                    {"text": "📊 Voir les trades", "url": "https://tradingview-gd03.onrender.com/trades"}
                ]]
            })
        }
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(send_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            p = _json.loads(raw)
            if not p.get("ok"):
                result["error"] = f"sendMessage failed: {raw[:200]}"
                log.warning("Telegram sendMessage error: %s", result["error"])
                return result
            msg = p.get("result") or {}
            result["ok"] = True
            result["message_id"] = msg.get("message_id")

        # Pin optionnel (désactivé par défaut pour ENTRY)
        if pin and result["message_id"] is not None:
            pin_url = f"{api_base}/pinChatMessage"
            pin_data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": result["message_id"],
            }).encode()
            try:
                preq = urllib.request.Request(pin_url, data=pin_data)
                with urllib.request.urlopen(preq, timeout=10) as presp:
                    praw = presp.read().decode("utf-8", "ignore")
                    pp = _json.loads(praw)
                    if pp.get("ok"):
                        result["pinned"] = True
                    else:
                        result["error"] = f"pinChatMessage failed: {praw[:200]}"
                        log.warning("Telegram pinChatMessage error: %s", result["error"])
            except Exception as e:
                result["error"] = f"pinChatMessage exception: {e}"
                log.warning("Telegram pin exception: %s", e)

        return result

    except Exception as e:
        result["error"] = f"send_telegram_ex exception: {e}"
        log.warning("Telegram send_telegram_ex exception: %s", e)
        return result

# ---------- LLM: score de confiance pour ENTRY ----------
def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Retourne (pourcentage_confiance, rationale) ou None si indisponible.
    Ne bloque pas le flux en cas d’erreur.
    """
    if not (LLM_ENABLED and _openai_client and OPENAI_API_KEY):
        return None
    try:
        # Prompt compact et déterministe
        sym = str(payload.get("symbol") or "?")
        tf_lbl = tf_label_of(payload)
        side = str(payload.get("side") or "N/A")
        entry = payload.get("entry")
        sl = payload.get("sl")
        tp1 = payload.get("tp1")
        tp2 = payload.get("tp2")
        tp3 = payload.get("tp3")

        sys_prompt = (
            "Tu es un assistant de trading. "
            "Note la probabilité (0-100) que l'ENTRY soit un bon setup à court terme, "
            "en te basant uniquement sur les champs fournis. Réponds au format JSON strict: "
            '{"confidence": <0..100>, "rationale": "<très bref>"}'
        )
        user_prompt = json.dumps({
            "symbol": sym, "tf": tf_lbl, "side": side,
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3
        }, ensure_ascii=False)

        # API responses.format=JSON (compat V1 SDK)
        resp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        conf = float(data.get("confidence", 0))
        rationale = str(data.get("rationale", "")).strip()
        conf = max(0.0, min(100.0, conf))
        return (conf, rationale)
    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        return None

# ---------- Mise en forme des messages Telegram ----------
def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    """
    Construit un message Telegram lisible pour les événements TradingView.
    Retourne None pour ignorer certains types (ex: AOE_*).
    """
    t = str(payload.get("type") or "EVENT").upper()
    if t.startswith("AOE_"):
        return None

    sym = str(payload.get("symbol") or "?")
    tf_lbl = tf_label_of(payload)
    side = str(payload.get("side") or "")
    entry = _to_float(payload.get("entry"))
    sl = _to_float(payload.get("sl"))
    tp = _to_float(payload.get("tp"))   # pour TP/SL hits: niveau exécuté
    tp1 = _to_float(payload.get("tp1"))
    tp2 = _to_float(payload.get("tp2"))
    tp3 = _to_float(payload.get("tp3"))
    leverage = payload.get("leverage") or payload.get("lev") or payload.get("lev_reco")
    lev_x = parse_leverage_x(str(leverage) if leverage is not None else None)

    def num(v): return fmt_num(v) if v is not None else "—"

    # ENTRY
    if t == "ENTRY":
        lines = []
        lines.append(f"📩 {sym} {tf_lbl}")
        if side:
            lines.append(("📈 Long Entry:" if side.upper()=="LONG" else "📉 Short Entry:") + f" {num(entry)}")
        if leverage:
            lines.append(f"💡Leverage: {leverage}")
        if tp1: lines.append(f"🎯 TP1: {num(tp1)}")
        if tp2: lines.append(f"🎯 TP2: {num(tp2)}")
        if tp3: lines.append(f"🎯 TP3: {num(tp3)}")
        if sl:  lines.append(f"❌ SL: {num(sl)}")

        # Confiance LLM (affichée si dispo)
        try:
            if LLM_ENABLED and _openai_client and (FORCE_LLM or True):
                res = llm_confidence_for_entry(payload)
                if res:
                    conf_pct, rationale = res
                    if conf_pct >= CONFIDENCE_MIN:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}% — {rationale or 'estimation heuristique'}")
                    else:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}%")
        except Exception as e:
            log.warning("LLM confidence render failed: %s", e)

        lines.append("🤖 Astuce: après TP1, placez SL au BE.")
        return "\n".join(lines)

    # TP HITS
    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        spot_pct = pct(tp, entry) if (side and tp is not None and entry is not None) else None
        lev_pct = (spot_pct * lev_x) if (spot_pct is not None and lev_x) else None
        lines = []
        lines.append(f"✅ {label} — {sym} {tf_lbl}")
        if tp is not None:
            lines.append(f"Mark price : {num(tp)}")
        if spot_pct is not None:
            base = f"Profit (spot) : {spot_pct:.2f}%"
            if lev_pct is not None:
                base += f" | avec {int(lev_x)}x : {lev_pct:.2f}%"
            lines.append(base)
        return "\n".join(lines)

    # SL
    if t == "SL_HIT":
        lines = [f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None:
            lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    # CLOSE (fermeture neutre, ex flip de signal)
    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    # VECTOR_CANDLE (nouveau)
    if t == "VECTOR_CANDLE":
        # Message sobre pour chandelle vectorielle
        lines = [f"🟪 Vector Candle — {sym} {tf_lbl}"]
        if side:
            lines.append(f"Contexte: {side}")
        lvl = payload.get("level") or payload.get("price")
        if lvl:
            lines.append(f"Niveau repéré: {num(_to_float(lvl))}")
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"

# ---------- Webhook TradingView ----------
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1","true","True")

@app.api_route("/tv-webhook", methods=["POST", "GET"])
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Accepte les payloads TradingView/Autres:
    {
      "type": "ENTRY|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CLOSE|VECTOR_CANDLE|AOE_*",
      "symbol": "...", "tf": "15", "side": "LONG|SHORT",
      "entry": 1.234, "sl": 1.111, "tp1": ..., "tp2": ..., "tp3": ...,
      "trade_id": "...", "leverage": "10x",
      // pour VECTOR_CANDLE optionnel: "level": prix, "price": prix
    }
    """
    # Secret check (query ou body)
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            try:
                # Certains envoient du form-encoded
                body = dict(await request.form())
            except Exception:
                body = {}
    body_secret = (body or {}).get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    # GET simple test
    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST JSON to this endpoint"})

    # Normalisation
    payload = dict(body or {})
    payload["type"] = str(payload.get("type") or "EVENT").upper()
    if "tf" in payload and isinstance(payload["tf"], str) and payload["tf"].isdigit():
        payload["tf"] = payload["tf"]  # déjà OK (string numérique)
    # Sauvegarde brute
    save_event(payload)

    # Message Telegram (ignore AOE_*)
    msg = telegram_rich_message(payload)
    sent = None
    if msg:
        # IMPORTANT: on ne PIN PAS les ENTRY. (pin=False)
        # On peut pin les alertes Altseason ailleurs.
        pin = False
        if payload["type"] == "VECTOR_CANDLE" and not TELEGRAM_NOTIFY_VECTOR:
            sent = False  # désactivé via env
        else:
            sent = send_telegram_ex(msg, pin=pin).get("ok")

    return JSONResponse({"ok": True, "telegram_sent": bool(sent), "type": payload["type"]})
# ============ main.py — BLOC 3/5 (SQLite, modèles trades, helpers d’affichage) ============

# ---------- SQLite : chemin résilient + connexion + init ----------
def resolve_db_path() -> None:
    """Assure un chemin DB writable; fallback /tmp/ai_trader/data.db si besoin."""
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.info("DB dir OK: %s (using %s)", d, DB_PATH)
    except Exception as e:
        fallback_dir = "/tmp/ai_trader"
        os.makedirs(fallback_dir, exist_ok=True)
        DB_PATH = os.path.join(fallback_dir, "data.db")
        log.warning("DB dir '%s' not writable (%s). Falling back to %s", d, e, DB_PATH)

def db_conn() -> sqlite3.Connection:
    """Connexion SQLite avec options sensées."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def db_init() -> None:
    """Crée la table events si absente."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                type TEXT,
                symbol TEXT,
                tf TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                trade_id TEXT,
                raw_json TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade  ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time   ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf     ON events(tf)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

# Boot DB à l’import
resolve_db_path()
db_init()

# ---------- Persistance d’un event (webhook) ----------
def save_event(payload: dict) -> None:
    """Insère un event TradingView tel quel dans la table `events`."""
    row = {
        "received_at": int(time.time()),
        "type": payload.get("type"),
        "symbol": payload.get("symbol"),
        "tf": str(payload.get("tf")) if payload.get("tf") is not None else None,
        "side": payload.get("side"),
        "entry": _to_float(payload.get("entry")),
        "sl": _to_float(payload.get("sl")),
        "tp1": _to_float(payload.get("tp1")),
        "tp2": _to_float(payload.get("tp2")),
        "tp3": _to_float(payload.get("tp3")),
        "trade_id": payload.get("trade_id"),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO events (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id, raw_json)
            VALUES (:received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :trade_id, :raw_json)
            """,
            row,
        )
        conn.commit()
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             row["type"], row["symbol"], row["tf"], row["trade_id"])

# ---------- Reconstruction des trades & stats ----------
class TradeOutcome:
    NONE  = "NONE"
    TP1   = "TP1_HIT"
    TP2   = "TP2_HIT"
    TP3   = "TP3_HIT"
    SL    = "SL_HIT"
    CLOSE = "CLOSE"

FINAL_EVENTS = {TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3,
                TradeOutcome.SL, TradeOutcome.CLOSE}

def row_get(row: sqlite3.Row, key: str, default=None):
    """Accès sûr aux colonnes d'un sqlite3.Row (pas de .get())."""
    try:
        return row[key]
    except Exception:
        return default

def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 0, 0, 0).timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 23, 59, 59).timestamp())
    except Exception:
        return None

def fetch_events_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    limit: int = 10000
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        sql += " AND symbol = ?"; args.append(symbol)
    if tf:
        sql += " AND tf = ?"; args.append(tf)
    if start_ep is not None:
        sql += " AND received_at >= ?"; args.append(start_ep)
    if end_ep is not None:
        sql += " AND received_at <= ?"; args.append(end_ep)
    sql += " ORDER BY received_at ASC"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return cur.fetchall()

def build_trades_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    max_rows: int = 20000
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Reconstitue les trades à partir de la séquence d'events.
    - ENTRY crée un trade ouvert
    - TPx_HIT / SL_HIT / CLOSE ferment le trade le plus récent correspondant
      (par trade_id s'il est fourni, sinon par paire (symbol, tf) en LIFO).
    """
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)

    trades: List[Dict[str, Any]] = []
    open_by_tid: Dict[str, Dict[str, Any]] = {}                       # trades ouverts indexés par trade_id
    open_stack_by_key: Dict[Tuple[str, str], List[int]] = defaultdict(list)  # pile d'index par (symbol, tf)

    # Statistiques
    total = wins = losses = 0
    hit_tp1 = hit_tp2 = hit_tp3 = 0
    times_to_outcome: List[int] = []
    win_streak = loss_streak = 0
    best_win_streak = 0
    worst_loss_streak = 0

    def synth_tid(ev: sqlite3.Row) -> str:
        tid = row_get(ev, "trade_id")
        if tid:
            return tid
        return f"{row_get(ev,'symbol')}_{row_get(ev,'tf')}_{row_get(ev,'received_at')}"

    for ev in rows:
        etype = row_get(ev, "type")
        sym = row_get(ev, "symbol")
        tfv = row_get(ev, "tf")
        key = (sym, tfv)

        # OUVERTURE
        if etype == "ENTRY":
            tid = synth_tid(ev)
            t = {
                "trade_id": tid,
                "symbol": sym,
                "tf": tfv,
                "side": row_get(ev, "side"),
                "entry": row_get(ev, "entry"),
                "sl": row_get(ev, "sl"),
                "tp1": row_get(ev, "tp1"),
                "tp2": row_get(ev, "tp2"),
                "tp3": row_get(ev, "tp3"),
                "entry_time": row_get(ev, "received_at"),
                "outcome": TradeOutcome.NONE,
                "outcome_time": None,
                "duration_sec": None,
            }
            trades.append(t)
            open_by_tid[tid] = t
            open_stack_by_key[key].append(len(trades) - 1)
            continue

        # CLÔTURES
        if etype in FINAL_EVENTS:
            targ: Optional[Dict[str, Any]] = None
            tid = row_get(ev, "trade_id")

            # Essaie par trade_id
            if tid and tid in open_by_tid:
                targ = open_by_tid[tid]
            else:
                # Sinon, dernier trade ouvert pour (symbol, tf)
                stack = open_stack_by_key.get(key) or []
                while stack:
                    idx = stack[-1]
                    cand = trades[idx]
                    if cand["outcome"] == TradeOutcome.NONE:
                        targ = cand
                        break
                    else:
                        stack.pop()  # nettoie les fermés

            if targ is not None and targ["outcome"] == TradeOutcome.NONE:
                targ["outcome"] = etype
                targ["outcome_time"] = row_get(ev, "received_at")
                if targ["entry_time"] and targ["outcome_time"]:
                    targ["duration_sec"] = int(targ["outcome_time"] - targ["entry_time"])
                # fermer l'état ouvert
                open_by_tid.pop(targ["trade_id"], None)
                if key in open_stack_by_key and open_stack_by_key[key]:
                    if open_stack_by_key[key][-1] == trades.index(targ):
                        open_stack_by_key[key].pop()

    # Agrégation stats (CLOSE = neutre, ne compte pas en win/loss mais ferme le trade)
    for t in trades:
        if t["entry_time"] is not None:
            total += 1
        if t["outcome_time"] and t["entry_time"]:
            times_to_outcome.append(int(t["outcome_time"] - t["entry_time"]))

        if t["outcome"] in (TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3):
            wins += 1
            win_streak += 1
            loss_streak = 0
            best_win_streak = max(best_win_streak, win_streak)
            if t["outcome"] == TradeOutcome.TP1: hit_tp1 += 1
            elif t["outcome"] == TradeOutcome.TP2: hit_tp2 += 1
            elif t["outcome"] == TradeOutcome.TP3: hit_tp3 += 1
        elif t["outcome"] == TradeOutcome.SL:
            losses += 1
            loss_streak += 1
            win_streak = 0
            worst_loss_streak = max(worst_loss_streak, loss_streak)
        else:
            # NONE (toujours ouvert) ou CLOSE (neutre)
            pass

    winrate = (wins / total * 100.0) if total else 0.0
    avg_sec = int(sum(times_to_outcome) / len(times_to_outcome)) if times_to_outcome else 0

    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "tp1_hits": hit_tp1,
        "tp2_hits": hit_tp2,
        "tp3_hits": hit_tp3,
        "avg_time_to_outcome_sec": avg_sec,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
    }
    return trades, summary

# ---------- Helpers d'affichage pour le dashboard ----------
def chip_class(outcome: str) -> str:
    """Classe CSS pour le badge Outcome."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        return "chip win"
    if outcome == "SL_HIT":
        return "chip loss"
    if outcome == "CLOSE":
        return "chip close"
    return "chip open"  # NONE => trade encore ouvert

def outcome_label(outcome: str) -> str:
    """Texte lisible pour Outcome (NONE -> OPEN)."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"):
        return outcome.replace("_HIT", "").title()  # TP1/TP2/TP3/Sl/Close
    return "OPEN"

def fmt_ts(ts: int | None, tz: timezone | None = None) -> str:
    """Formatte epoch en 'YYYY-MM-DD HH:MM:SS' (UTC par défaut)."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ts), tz or timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"
# ============ main.py — BLOC 4/5 (Altseason, Webhook TV, LLM confidence) ============

# ---------- Heuristique LLM locale pour la confiance ENTRY ----------
def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Renvoie (confiance_en_% , justification_courte) pour un ENTRY.
    Implémentation simple/locale pour éviter les NameError si OpenAI est off.
    """
    try:
        t = str(payload.get("type") or "").upper()
        if t != "ENTRY":
            return None

        side = (payload.get("side") or "").upper()
        entry = _to_float(payload.get("entry"))
        sl    = _to_float(payload.get("sl"))
        tp1   = _to_float(payload.get("tp1"))
        tp2   = _to_float(payload.get("tp2"))
        tp3   = _to_float(payload.get("tp3"))

        # Petits signaux heuristiques
        score = 50.0
        if entry and sl:
            # SL proche sans être trop serré
            risk = abs((entry - sl) / entry) * 100
            if 0.2 <= risk <= 1.5:
                score += 10
            elif risk < 0.1 or risk > 3.0:
                score -= 8

        # Plus de TP = un poil plus de structure
        for tp in (tp1, tp2, tp3):
            if tp:
                score += 3

        if side in ("LONG", "SHORT"):
            score += 4

        score = max(0.0, min(100.0, score))
        rationale = "score heuristique (SL/TP & structure)"
        return score, rationale
    except Exception:
        return None

# ========== Altseason: fetch + cache + résumés + endpoints publics ==========

_alt_cache: Dict[str, Any] = {"ts": 0, "snap": None}
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

def _alt_cache_file_path() -> str:
    return os.getenv("ALT_CACHE_FILE", "/tmp/altseason_last.json")

def _load_last_snapshot() -> Optional[Dict[str, Any]]:
    try:
        p = _alt_cache_file_path()
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            snap = json.load(f)
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None

def _save_last_snapshot(snap: Dict[str, Any]) -> None:
    try:
        p = _alt_cache_file_path()
        d = os.path.dirname(p) or "/tmp"
        os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    except Exception:
        pass

def _altseason_fetch() -> Dict[str, Any]:
    out = {"asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "errors": []}
    try:
        import requests
    except Exception:
        out["errors"].append("Missing dependency: requests")
        return out

    headers = {
        "User-Agent": "altseason-bot/1.6",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    def get_json(url: str, timeout: int = 12) -> Dict[str, Any]:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        body_preview = (r.text or "")[:220].replace("\n", " ").replace("\r", " ")
        if r.status_code != 200:
            raise RuntimeError(f"{url} -> HTTP {r.status_code}: {body_preview}")
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"{url} -> Non-JSON response: {body_preview}")

    # Global & BTC dominance
    mcap_usd = btc_dom = None
    try:
        alt = get_json("https://api.alternative.me/v2/global/")
        d0 = (alt.get("data") or [{}])[0]
        qusd = (d0.get("quotes") or {}).get("USD") or {}
        mcap = qusd.get("total_market_cap")
        dom = d0.get("bitcoin_percentage_of_market_cap")
        if mcap is not None and dom is not None:
            mcap_usd = float(mcap); btc_dom = float(dom)
    except Exception as e:
        out["errors"].append(f"alternative.me: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            g = get_json("https://api.coingecko.com/api/v3/global")
            data = g.get("data") or {}
            mcap_usd = float(data["total_market_cap"]["usd"])
            btc_dom = float(data["market_cap_percentage"]["btc"])
        except Exception as e:
            out["errors"].append(f"coingecko: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            pg = get_json("https://api.coinpaprika.com/v1/global")
            mcap_usd = float(pg["market_cap_usd"])
            btc_dom = float(pg["bitcoin_dominance_percentage"])
        except Exception as e:
            out["errors"].append(f"coinpaprika: {e!r}")

    # Fallback CoinCap/Coinlore
    if mcap_usd is None or btc_dom is None:
        try:
            cc = get_json("https://api.coincap.io/v2/assets?limit=2000")
            assets = cc.get("data") or []
            total = 0.0; btc_mcap = 0.0
            for a in assets:
                mc = a.get("marketCapUsd")
                if mc is not None:
                    try: total += float(mc)
                    except: pass
            for a in assets:
                if a.get("id") == "bitcoin":
                    try: btc_mcap = float(a.get("marketCapUsd") or 0.0)
                    except: btc_mcap = 0.0
                    break
            if total > 0:
                mcap_usd = total; btc_dom = (btc_mcap / total) * 100.0
        except Exception as e:
            out["errors"].append(f"coincap: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            cl = get_json("https://api.coinlore.net/api/global/")
            g = cl[0] if isinstance(cl, list) and cl else cl
            mcap = g.get("total_mcap_usd") or g.get("total_mcap") or g.get("mcap_total_usd")
            dom = g.get("btc_d") or g.get("bitcoin_dominance_percentage") or g.get("btc_dominance")
            if mcap is not None and dom is not None:
                mcap_usd = float(mcap); btc_dom = float(dom)
        except Exception as e:
            out["errors"].append(f"coinlore: {e!r}")

    out["total_mcap_usd"] = (None if mcap_usd is None else float(mcap_usd))
    out["btc_dominance"] = (None if btc_dom is None else float(btc_dom))
    out["total2_usd"] = (None if (mcap_usd is None or btc_dom is None) else float(mcap_usd * (1.0 - btc_dom/100.0)))

    # ETH/BTC
    eth_btc = None
    try:
        j = get_json("https://api.binance.com/api/v3/ticker/price?symbol=ETHBTC")
        eth_btc = float(j["price"])
    except Exception as e:
        out["errors"].append(f"binance: {e!r}")
    if eth_btc is None:
        try:
            sp = get_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=btc,usd")
            eth_btc = float(sp["ethereum"]["btc"])
        except Exception as e:
            out["errors"].append(f"coingecko_simple: {e!r}")

    out["eth_btc"] = (None if eth_btc is None else float(eth_btc))

    # Altseason Index (scrape léger)
    out["altseason_index"] = None
    try:
        import requests
        from bs4 import BeautifulSoup
        html = requests.get("https://www.blockchaincenter.net/altcoin-season-index/",
                            timeout=12, headers=headers).text
        soup = BeautifulSoup(html, "html.parser")
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"Altcoin Season Index[^0-9]*([0-9]{2,3})", txt)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100:
                out["altseason_index"] = v
    except Exception as e:
        out["errors"].append(f"altseason_index_scrape: {e!r}")

    return out

def _ok_cmp(val: Optional[float], thr: float, direction: str) -> bool:
    if val is None:
        return False
    return (val < thr) if direction == "below" else (val > thr)

def _altseason_summary(snap: Dict[str, Any]) -> Dict[str, Any]:
    btc = snap.get("btc_dominance")
    eth = snap.get("eth_btc")
    t2  = snap.get("total2_usd")
    asi = snap.get("altseason_index")

    btc_ok = _ok_cmp(btc, ALT_BTC_DOM_THR, "below")
    eth_ok = _ok_cmp(eth, ALT_ETH_BTC_THR, "above")
    t2_ok  = _ok_cmp(t2,  ALT_TOTAL2_THR_T * 1e12, "above")
    asi_ok = (asi is not None) and _ok_cmp(float(asi), ALT_ASI_THR, "above")

    greens = sum([btc_ok, eth_ok, t2_ok, asi_ok])
    on = greens >= ALT_GREENS_REQUIRED

    return {
        "asof": snap.get("asof"),
        "stale": bool(snap.get("stale", False)),
        "errors": snap.get("errors", []),
        "btc_dominance": (None if btc is None else float(btc)),
        "eth_btc": (None if eth is None else float(eth)),
        "total2_usd": (None if t2 is None else float(t2)),
        "altseason_index": (None if asi is None else int(asi)),
        "thresholds": {
            "btc_dominance_max": ALT_BTC_DOM_THR,
            "eth_btc_min": ALT_ETH_BTC_THR,
            "altseason_index_min": ALT_ASI_THR,
            "total2_min_trillions": ALT_TOTAL2_THR_T,
            "greens_required": ALT_GREENS_REQUIRED
        },
        "triggers": {
            "btc_dominance_ok": btc_ok,
            "eth_btc_ok": eth_ok,
            "total2_ok": t2_ok,
            "altseason_index_ok": asi_ok
        },
        "greens": greens,
        "ALTSEASON_ON": on
    }

def _altseason_snapshot(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if (not force) and _alt_cache["snap"] and (now - _alt_cache["ts"] < ALT_CACHE_TTL):
        snap = dict(_alt_cache["snap"])
        snap.setdefault("stale", False)
        return snap
    try:
        snap = _altseason_fetch()
        snap["stale"] = False
        _alt_cache["snap"] = snap
        _alt_cache["ts"] = now
        _save_last_snapshot(snap)
        return snap
    except Exception as e:
        if _alt_cache["snap"]:
            s = dict(_alt_cache["snap"])
            s["stale"] = True
            s.setdefault("errors", []).append(f"live_fetch_exception: {e!r}")
            return s
        disk = _load_last_snapshot()
        if isinstance(disk, dict):
            disk = dict(disk)
            disk["stale"] = True
            disk.setdefault("errors", []).append(f"live_fetch_exception: {e!r}")
            return disk
        return {
            "asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "btc_dominance": None, "eth_btc": None, "total2_usd": None, "altseason_index": None,
            "errors": [f"live_fetch_exception: {e!r}"], "stale": True,
        }

# --- Endpoints Altseason ---
@app.get("/altseason/check")
def altseason_check_public():
    snap = _altseason_snapshot(force=False)
    return _altseason_summary(snap)

def _load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(ALTSEASON_STATE_FILE):
            with open(ALTSEASON_STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {
        "last_on": False, "last_sent_ts": 0, "last_tick_ts": 0,
        "consec_3of4_days": 0, "consec_4of4_days": 0,
        "last_streak_date": None
    }

def _save_state(state: Dict[str, Any]) -> None:
    try:
        d = os.path.dirname(ALTSEASON_STATE_FILE) or "/tmp"
        os.makedirs(d, exist_ok=True)
        with open(ALTSEASON_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def _today_utc_str() -> str:
    import datetime as dt
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def _update_daily_streaks(state: Dict[str, Any], summary: Dict[str, Any]) -> None:
    import datetime as dt
    today = _today_utc_str()
    last_date = state.get("last_streak_date")
    if last_date == today:
        return
    greens = int(summary.get("greens") or 0)
    is3 = greens >= 3
    is4 = greens >= 4
    if last_date is None:
        state["consec_3of4_days"] = 1 if is3 else 0
        state["consec_4of4_days"] = 1 if is4 else 0
    else:
        try:
            d_last = dt.datetime.strptime(last_date, "%Y-%m-%d")
            d_today = dt.datetime.strptime(today, "%Y-%m-%d")
            consecutive = (d_today - d_last).days == 1
        except Exception:
            consecutive = False
        if consecutive:
            state["consec_3of4_days"] = (state.get("consec_3of4_days", 0) + 1) if is3 else 0
            state["consec_4of4_days"] = (state.get("consec_4of4_days", 0) + 1) if is4 else 0
        else:
            state["consec_3of4_days"] = 1 if is3 else 0
            state["consec_4of4_days"] = 1 if is4 else 0
    state["last_streak_date"] = today

@app.get("/altseason/streaks")
def altseason_streaks():
    st = _load_state()
    s = _altseason_summary(_altseason_snapshot(force=False))
    _update_daily_streaks(st, s)
    _save_state(st)
    return {
        "asof": s.get("asof"),
        "greens": s.get("greens"),
        "ALT3_ON": bool(int(s.get("greens") or 0) >= 3),
        "ALT4_ON": bool(int(s.get("greens") or 0) >= 4),
        "consec_3of4_days": int(st.get("consec_3of4_days") or 0),
        "consec_4of4_days": int(st.get("consec_4of4_days") or 0),
    }

@app.get("/altseason/daemon-status")
def altseason_daemon_status():
    st = _load_state()
    return {
        "autonotify_enabled": ALTSEASON_AUTONOTIFY,
        "poll_seconds": ALTSEASON_POLL_SECONDS,
        "notify_min_gap_min": ALTSEASON_NOTIFY_MIN_GAP_MIN,
        "greens_required": ALT_GREENS_REQUIRED,
        "state": st
    }

@app.api_route("/altseason/notify", methods=["GET", "POST"])
async def altseason_notify(
    request: Request,
    secret: Optional[str] = Query(None),
    force: Optional[bool] = Query(False),
    message: Optional[str] = Query(None),
    pin: Optional[bool] = Query(False)
):
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    body_secret = body.get("secret") if isinstance(body, dict) else None
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")
    if request.method == "POST":
        force = bool(body.get("force", force))
        message = body.get("message", message)
        pin = bool(body.get("pin", pin))
    pin = bool(pin or TELEGRAM_PIN_ALTSEASON)

    s = _altseason_summary(_altseason_snapshot(force=bool(force)))
    sent = None
    pin_res = None
    if s["ALTSEASON_ON"] or force:
        if message:
            msg = message
        else:
            if s["ALTSEASON_ON"]:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — ALTSEASON DÉBUTÉ !"
            else:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — EN VEILLE (conditions insuffisantes)"
        pin_result = send_telegram_ex(msg, pin=bool(pin))
        sent = pin_result.get("ok")
        pin_res = {"pinned": pin_result.get("pinned"),
                   "message_id": pin_result.get("message_id"),
                   "error": pin_result.get("error")}
        log.info("Altseason notify: sent=%s pinned=%s err=%s",
                 sent, pin_res.get("pinned"), pin_res.get("error"))

    return {"summary": s, "telegram_sent": sent, "pin_result": pin_res}

# ========== Webhook TradingView (/tv-webhook) avec “VECTOR_CANDLE” ==========
@app.post("/tv-webhook")
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Webhook générique. Attend un JSON contenant au minimum `type` et `symbol`.
    Accepte les types: ENTRY, TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, CLOSE,
                       AOE_PREMIUM / AOE_DISCOUNT (enregistrés mais non notifiés),
                       VECTOR_CANDLE (alerte “chandelle vector”).
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Secret: query ?secret=... OU champ JSON "secret"
    body_secret = payload.get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    # Normalise
    t = str(payload.get("type") or "").upper()
    payload["type"] = t

    # Crée un trade_id s’il manque
    if not payload.get("trade_id"):
        sym = str(payload.get("symbol") or "UNK")
        tfv = str(payload.get("tf") or payload.get("tf_label") or "?")
        # on accepte aussi un timestamp TradingView si fourni (payload['ts'])
        ts = payload.get("ts") or int(time.time())
        try:
            ts = int(ts)
        except Exception:
            ts = int(time.time())
        payload["trade_id"] = f"{sym}_{tfv}_{ts}"

    # Sauvegarde en base (on garde tout)
    save_event(payload)

    # Décide si on notifie Telegram
    notify = True
    if t.startswith("AOE_"):   # ces signaux sont enregistrés mais non notifiés
        notify = False

    # Compose message
    text = None
    if t == "ENTRY" or t in {"TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT","CLOSE"}:
        text = telegram_rich_message(payload)
    elif t in {"VECTOR_CANDLE", "VEC_CANDLE", "VECTOR"}:
        sym = str(payload.get("symbol") or "?")
        tf_lbl = tf_label_of(payload)
        direction = (payload.get("side") or payload.get("direction") or "").upper()
        extra = payload.get("note") or payload.get("reason") or ""
        text = f"⚡️ Chandelle Vector — {sym} {tf_lbl}\n" \
               f"Direction: {direction or 'N/A'}\n" \
               f"{extra}".strip() or f"⚡️ Chandelle Vector — {sym} {tf_lbl}"

    # Ajoute la confiance LLM pour ENTRY (si dispo)
    if t == "ENTRY" and text:
        try:
            res = llm_confidence_for_entry(payload) if (FORCE_LLM or True) else None
            if res:
                conf_pct, rationale = res
                if conf_pct >= CONFIDENCE_MIN:
                    text += f"\n🧠 Confiance LLM: {conf_pct:.0f}% — {rationale or 'estimation heuristique'}"
                else:
                    text += f"\n🧠 Confiance LLM: {conf_pct:.0f}%"
        except Exception as e:
            log.warning("LLM confidence render failed: %s", e)

    sent = False
    if notify and text:
        # IMPORTANT: pas d’épinglage automatique pour les signaux unitaires (ENTRY/TP/etc.)
        # On force pin=False ici pour ne pas épingler chaque entrée.
        res = send_telegram_ex(text, pin=False)
        sent = bool(res.get("ok"))
        if not sent and res.get("error"):
            log.warning("Telegram notify error: %s", res.get("error"))

    return {"ok": True, "saved": True, "telegram_sent": sent, "type": t, "trade_id": payload.get("trade_id")}
# ============ main.py — BLOC 5/5 (Trades Dashboard + Fin) ============

def chip_class(outcome: str) -> str:
    if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT"):
        return "chip win"
    if outcome == "SL_HIT":
        return "chip loss"
    if outcome == "CLOSE":
        return "chip close"
    if outcome == "NONE":
        return "chip muted"
    return "chip"

@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    try:
        # Normalize/guard inputs
        limit = max(1, min(int(limit or 100), 10000))
        start_ep = parse_date_to_epoch(start)
        end_ep = parse_date_end_to_epoch(end)

        # Build data
        trades, summary = build_trades_filtered(
            symbol, tf, start_ep, end_ep, max_rows=max(5000, limit * 10)
        )

        # Render rows
        rows_html = ""
        data = trades[-limit:] if limit else trades
        for tr in data:
            outcome = (tr.get("outcome") or "NONE")
            badge = chip_class(outcome)          # chip win/loss/close/open
            label = outcome_label(outcome)       # TP1/TP2/TP3/SL/Close/OPEN
            rows_html += (
                "<tr>"
                f"<td>{escape_html(str(tr.get('trade_id') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
                f"<td>{fmt_num(tr.get('entry'))}</td>"
                f"<td>{fmt_num(tr.get('sl'))}</td>"
                f"<td>{fmt_num(tr.get('tp1'))}</td>"
                f"<td>{fmt_num(tr.get('tp2'))}</td>"
                f"<td>{fmt_num(tr.get('tp3'))}</td>"
                f"<td>{escape_html(fmt_ts(tr.get('entry_time')))}</td>"
                f"<td><span class='{badge}'>{escape_html(label)}</span></td>"
                f"<td>{'' if tr.get('duration_sec') is None else str(tr.get('duration_sec'))}</td>"
                "</tr>"
            )

        # Safe numbers for template
        def s(v): 
            try:
                return str(v if v is not None else "")
            except Exception:
                return ""

        html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
            symbol=escape_html(symbol or ""),
            tf=escape_html(tf or ""),
            start=escape_html(start or ""),
            end=escape_html(end or ""),
            limit=str(limit),
            total_trades=s(summary.get("total_trades")),
            winrate_pct=s(summary.get("winrate_pct")),
            wins=s(summary.get("wins")),
            losses=s(summary.get("losses")),
            tp1_hits=s(summary.get("tp1_hits")),
            tp2_hits=s(summary.get("tp2_hits")),
            tp3_hits=s(summary.get("tp3_hits")),
            avg_time_to_outcome_sec=s(summary.get("avg_time_to_outcome_sec")),
            best_win_streak=s(summary.get("best_win_streak")),
            worst_loss_streak=s(summary.get("worst_loss_streak")),
            rows_html=rows_html or '<tr><td colspan="12" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
            pill_values="[]"
        )
        return HTMLResponse(html)

    except Exception as e:
        # Log the real cause for you
        log.exception("Error in /trades: %s", e)
        # Return a graceful page so you don't get a 500
        safe_msg = escape_html(f"{type(e).__name__}: {e}")
        fallback = f"""
        <!doctype html><html><head><meta charset="utf-8"><title>Trades — Error</title>
        <style>body{{background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto}}
        .card{{max-width:840px;margin:32px auto;background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px}}
        .muted{{color:#94a3b8}}</style></head><body>
        <div class="card">
          <h1>Trades — Dashboard</h1>
          <p class="muted">An error occurred while rendering the page.</p>
          <pre style="white-space:pre-wrap">{safe_msg}</pre>
          <p class="muted">Check server logs for the full traceback.</p>
          <p><a href="/" style="color:#93c5fd">← Back to Home</a></p>
        </div>
        </body></html>"""
        return HTMLResponse(fallback, status_code=200)


# -------------------------
# Run local (for debug)
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

