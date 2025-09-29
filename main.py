# ============ main.py — BLOC 1/5 (Imports, Config, App, DB boot, Helpers) ============
import os
import re
import json
import time
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

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

def fmt_ts(ts: int | None, tz: timezone | None = None) -> str:
    """Formatte epoch en 'YYYY-MM-DD HH:MM:SS' (UTC par défaut)."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ts), tz or timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade  ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time   ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf     ON events(tf)")
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
# ============ main.py — BLOC 3/5 (SQLite, save_event, reconstruction trades, helpers) ============

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


# ---------- Helpers dates & affichage ----------
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

def fmt_ts(ts: int | None, tz: timezone | None = None) -> str:
    """Formatte epoch en 'YYYY-MM-DD HH:MM:SS' (UTC par défaut)."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ts), tz or timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"

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


# ---------- Requêtes DB ----------
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
# ============ main.py — BLOC 5/5 (Webhook TradingView, Vector Candle, Telegram) ============

from fastapi import Request
import httpx

# ------------------------- Telegram Helpers -------------------------

_last_tg = 0
TELEGRAM_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", 2))

def send_telegram_ex(msg: str, pin: bool = False):
    global _last_tg
    import time
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        now = time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            log.warning("Telegram send skipped due to cooldown")
            return
        _last_tg = now
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
            httpx.post(url, json=payload, timeout=10)
            if pin:
                pin_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
                httpx.post(pin_url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": None}, timeout=10)
        except Exception as e:
            log.warning(f"Telegram send_telegram_ex exception: {e}")

# ------------------------- LLM Confidence -------------------------

async def llm_confidence_for_entry(symbol: str, entry: float, side: str) -> Optional[int]:
    if not LLM_ENABLED or not OPENAI_API_KEY:
        return None
    try:
        prompt = f"Analyse ce trade crypto: {symbol}, side={side}, entry={entry}. Donne une confiance 0-100."
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 20,
                },
            )
        js = r.json()
        txt = js["choices"][0]["message"]["content"]
        val = int("".join([c for c in txt if c.isdigit()]) or "0")
        return max(0, min(100, val))
    except Exception as e:
        log.warning(f"llm_confidence_for_entry error: {e}")
        return None

# ------------------------- Webhook Endpoint -------------------------

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    log.info(f"Webhook payload: {payload}")

    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    save_event(payload)

    etype = payload.get("type")
    symbol = payload.get("symbol", "?")
    tf = payload.get("tf_label", payload.get("tf", "?"))
    side = payload.get("side", "")
    entry = payload.get("entry")

    msg = None

    # === ENTRY ===
    if etype == "ENTRY":
        conf = await llm_confidence_for_entry(symbol, entry, side)
        conf_str = f"\nConfiance LLM: {conf}%" if conf is not None else ""
        msg = (
            f"🔔 ENTRY — {symbol} {tf}\n"
            f"Side: {side}\nEntry: {entry}{conf_str}"
        )

    # === TP / SL / CLOSE ===
    elif etype in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"):
        msg = f"🔔 {etype} — {symbol} {tf}"

    # === Vector Candles ===
    elif etype == "VECTOR_CANDLE":
        vdir = payload.get("vector_dir", "").upper()
        if vdir == "UP":
            msg = f"🟩 Vector Candle UP — {symbol} {tf}\nNiveau repéré: {entry}"
        elif vdir == "DOWN":
            msg = f"🟥 Vector Candle DOWN — {symbol} {tf}\nNiveau repéré: {entry}"
        else:
            msg = f"🟪 Vector Candle — {symbol} {tf}\nNiveau repéré: {entry}"

    # === AOE Premium / autres ===
    elif etype and etype.startswith("AOE_"):
        msg = f"🔔 {etype} — {symbol} {tf}"

    if msg:
        send_telegram_ex(msg, pin=(etype == "ENTRY"))

    return {"ok": True}
