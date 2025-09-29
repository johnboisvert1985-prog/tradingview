# ============ main.py — BLOC 1/5 (Imports, Config, Helpers, DB, save_event) ============
import os
import re
import json
import time
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

# -------------------------
# FastAPI app
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

DB_PATH = os.getenv("DB_PATH", "data/data.db")
DEBUG_MODE = os.getenv("DEBUG", "0") in ("1", "true", "True")

# Altseason thresholds / daemon interval
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR     = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL   = int(os.getenv("ALT_CACHE_TTL", "120"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1","true","True")
ALTSEASON_AUTONOTIFY   = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1","true","True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

# IMPORTANT: valeur par défaut utilisée par le daemon (corrige ALT_INTERVAL non défini)
ALT_INTERVAL = ALTSEASON_POLL_SECONDS

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
def now_iso() -> str:
    """ISO8601 UTC (corrige NameError: now_iso)."""
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

def tf_label_of(payload: Dict[str, Any]) -> str:
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

def parse_date_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch seconds (00:00 UTC)."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch seconds end-of-day (23:59:59 UTC)."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp()) + 24*3600 - 1
    except Exception:
        return None

def fmt_ts(ts: Optional[int]) -> str:
    try:
        if ts is None:
            return ""
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ""

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

# -------------------------
# Persist: save_event (corrige NameError: now_iso)
# -------------------------
def save_event(payload: Dict[str, Any]) -> None:
    """Insert un event TV dans SQLite en étant tolérant aux champs manquants."""
    row = {
        "received_at": int(time.time()),
        "type": (payload.get("type") or "EVENT").upper(),
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
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO events
                (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id, raw_json)
                VALUES (:received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :trade_id, :raw_json)
                """,
                row,
            )
            conn.commit()
        log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
                 row["type"], row["symbol"], row["tf"], row["trade_id"])
    except Exception as e:
        log.error("save_event failed: %s", e, exc_info=True)
# ============ main.py — BLOC 2/5 (Telegram utils, LLM, Message builder) ============

# ---------- Telegram (anti-spam simple) ----------
def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (sans pin, sans inline keyboard)."""
    global _last_tg
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        now = time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            log.warning("Telegram send skipped due to cooldown")
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
    NOTE: par défaut, on NE PIN PAS les messages d'ENTRY (pin=False).
    Gère les 429 de Telegram en douceur.
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
            result["ok"] = False
            result["error"] = "cooldown"
            log.warning("Telegram send skipped due to cooldown")
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
        try:
            req = urllib.request.Request(send_url, data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                p = _json.loads(raw)
        except Exception as e:
            result["error"] = f"sendMessage exception: {e}"
            log.warning("Telegram sendMessage exception: %s", e)
            return result

        if not p.get("ok"):
            result["error"] = f"sendMessage failed: {str(p)[:200]}"
            log.warning("Telegram sendMessage error: %s", result["error"])
            return result

        msg = p.get("result") or {}
        result["ok"] = True
        result["message_id"] = msg.get("message_id")

        # Pin optionnel
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
    Tolérant aux erreurs d'API ou de parsing (corrige 'Expecting value').
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

        # Robust parsing
        try:
            data = json.loads(content)
        except Exception:
            # tenter d'extraire un JSON minimal entre { }
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    return None
            else:
                return None

        conf = float(data.get("confidence", 0))
        rationale = str(data.get("rationale", "")).strip()
        conf = max(0.0, min(100.0, conf))
        return (conf, rationale)
    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        return None


# ============ main.py — BLOC 3/5 (Message builder + Webhook) sera après ============
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

    direction = (payload.get("direction") or "").upper().strip()  # pour VECTOR_CANDLE
    # Petits carrés : UP=🟩, DOWN=🟥, sinon 🟪
    dir_square = "🟩" if direction == "UP" else ("🟥" if direction == "DOWN" else "🟪")

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
                    if conf_pct >= CONFIDENCE_MIN and rationale:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}% — {rationale}")
                    elif conf_pct >= 0:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}%")
        except Exception as e:
            log.warning("LLM confidence render failed: %s", e)

        lines.append("🤖 Astuce: après TP1, placez SL au BE.")
        return "\n".join(lines)

    # TP HITS
    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        # On ne peut pas calculer % que si on connait entry & tp & side (LONG/SHORT)
        spot_pct = None
        if side and tp is not None and entry is not None:
            if side.upper() == "LONG":
                spot_pct = pct(tp, entry)
            else:
                spot_pct = pct(entry, tp)
        lines = []
        lines.append(f"✅ {label} — {sym} {tf_lbl}")
        if tp is not None:
            lines.append(f"Mark price : {num(tp)}")
        if spot_pct is not None:
            lines.append(f"Profit (spot) : {spot_pct:.2f}%")
        return "\n".join(lines)

    # SL
    if t == "SL_HIT":
        lines = [f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None:
            lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    # CLOSE
    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    # VECTOR_CANDLE
    if t == "VECTOR_CANDLE":
        # Carré vert si UP, rouge si DOWN, violet sinon
        note = payload.get("note")
        price = _to_float(payload.get("price") or payload.get("level"))
        lines = [f"{dir_square} Vector Candle — {sym} {tf_lbl}"]
        if direction in ("UP", "DOWN"):
            lines.append(f"Direction: {direction}")
        if price is not None:
            lines.append(f"Niveau repéré: {num(price)}")
        if note:
            lines.append(str(note))
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"


# ---------- Flag pour envoi Telegram des Vector Candles ----------
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1","true","True")
# ============ main.py — BLOC 3/5 (Helpers trades + save_event + Webhook) ============

# ---------- Helpers généraux supplémentaires ----------
def now_iso() -> str:
    """Timestamp lisible UTC (pour logs/HTML)."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def parse_date_to_epoch(d: Optional[str]) -> Optional[int]:
    """
    YYYY-MM-DD -> epoch seconds (début de journée UTC). None si vide/mauvais.
    """
    if not d:
        return None
    try:
        dt = datetime.strptime(d.strip(), "%Y-%m-%d")
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(d: Optional[str]) -> Optional[int]:
    """
    YYYY-MM-DD -> epoch seconds (fin de journée 23:59:59 UTC). None si vide/mauvais.
    """
    if not d:
        return None
    try:
        dt = datetime.strptime(d.strip(), "%Y-%m-%d")
        dt = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def fmt_ts(epoch_s: Optional[int]) -> str:
    if not epoch_s:
        return ""
    try:
        return datetime.utcfromtimestamp(int(epoch_s)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def outcome_label(o: str) -> str:
    o = (o or "").upper()
    return {
        "TP1": "TP1",
        "TP2": "TP2",
        "TP3": "TP3",
        "SL": "SL",
        "CLOSE": "Close",
        "OPEN": "OPEN",
    }.get(o, o or "—")

def chip_class(o: str) -> str:
    o = (o or "").upper()
    if o in ("TP1", "TP2", "TP3"):
        return "chip win"
    if o == "SL":
        return "chip loss"
    if o == "CLOSE":
        return "chip close"
    if o == "OPEN":
        return "chip open"
    return "chip muted"


# ---------- Aggregation / résumé d'un flux d'événements ----------
def _collect_events_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_epoch: Optional[int],
    end_epoch: Optional[int],
    max_rows: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Récupère des évènements de la table SQLite (filtrés), ordonnés par received_at croissant.
    """
    q = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol)
    if tf:
        q += " AND tf = ?"
        args.append(tf)
    if start_epoch:
        q += " AND received_at >= ?"
        args.append(int(start_epoch))
    if end_epoch:
        q += " AND received_at <= ?"
        args.append(int(end_epoch))
    q += " ORDER BY received_at ASC LIMIT ?"
    args.append(int(max_rows))

    with db_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # raw_json peut contenir des champs utiles (direction, price, etc.)
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except Exception:
            raw = {}
        d["raw"] = raw
        out.append(d)
    return out


def build_trades_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_epoch: Optional[int],
    end_epoch: Optional[int],
    max_rows: int = 5000,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Agrège les événements par trade_id et produit une liste de trades avec outcome, durations, etc.
    Retourne (trades_list, summary_stats).
    """
    evs = _collect_events_filtered(symbol, tf, start_epoch, end_epoch, max_rows=max_rows)

    # Grouper par trade_id (ou pseudo-id si None)
    grouped: Dict[str, Dict[str, Any]] = {}
    fallback_idx = 0

    for e in evs:
        tid = e.get("trade_id")
        if not tid:
            # pseudo trade_id basé sur symbol/tf/received_at pour ne pas perdre l'info
            fallback_idx += 1
            tid = f"{e.get('symbol')}_{e.get('tf')}_{e.get('received_at')}_{fallback_idx}"

        g = grouped.setdefault(tid, {
            "trade_id": tid,
            "symbol": e.get("symbol"),
            "tf": e.get("tf"),
            "side": None,
            "entry": None, "sl": None, "tp1": None, "tp2": None, "tp3": None,
            "entry_time": None,
            "outcome": "OPEN",      # par défaut
            "outcome_time": None,
            "duration_sec": None,
        })

        typ = (e.get("type") or "").upper()
        raw = e.get("raw") or {}

        # synchroniser params typiques d'entry
        if typ == "ENTRY":
            g["side"] = raw.get("side") or g["side"]
            g["entry"] = _to_float(raw.get("entry")) or g["entry"]
            g["sl"] = _to_float(raw.get("sl")) or g["sl"]
            g["tp1"] = _to_float(raw.get("tp1")) or g["tp1"]
            g["tp2"] = _to_float(raw.get("tp2")) or g["tp2"]
            g["tp3"] = _to_float(raw.get("tp3")) or g["tp3"]
            if g["entry_time"] is None:
                g["entry_time"] = e.get("received_at")

        # outcome selon l'ordre de priorité (TP3 > TP2 > TP1 > SL > CLOSE)
        if typ in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"):
            if typ == "TP3_HIT":
                g["outcome"] = "TP3"
                g["outcome_time"] = e.get("received_at")
            elif typ == "TP2_HIT":
                # n'écrase pas TP3
                if g["outcome"] not in ("TP3",):
                    g["outcome"] = "TP2"
                    g["outcome_time"] = e.get("received_at")
            elif typ == "TP1_HIT":
                if g["outcome"] not in ("TP3", "TP2"):
                    g["outcome"] = "TP1"
                    g["outcome_time"] = e.get("received_at")
            elif typ == "SL_HIT":
                # n'écrase pas un TP (si TP déjà atteint on ne met pas SL)
                if g["outcome"] not in ("TP1", "TP2", "TP3"):
                    g["outcome"] = "SL"
                    g["outcome_time"] = e.get("received_at")
            elif typ == "CLOSE":
                # n'écrase pas TP/SL
                if g["outcome"] not in ("TP1", "TP2", "TP3", "SL"):
                    g["outcome"] = "CLOSE"
                    g["outcome_time"] = e.get("received_at")

    trades: List[Dict[str, Any]] = []
    for tid, g in grouped.items():
        if g["entry_time"] and g["outcome_time"]:
            try:
                g["duration_sec"] = int(g["outcome_time"]) - int(g["entry_time"])
            except Exception:
                g["duration_sec"] = None
        trades.append(g)

    # Stats
    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] in ("TP1", "TP2", "TP3"))
    losses = sum(1 for t in trades if t["outcome"] == "SL")
    tp1_hits = sum(1 for t in trades if t["outcome"] == "TP1")
    tp2_hits = sum(1 for t in trades if t["outcome"] == "TP2")
    tp3_hits = sum(1 for t in trades if t["outcome"] == "TP3")

    # winrate simple
    denom = wins + losses
    winrate_pct = round((wins / denom) * 100.0, 2) if denom > 0 else 0.0

    # streaks (simple)
    best_win_streak = 0
    worst_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for t in sorted(trades, key=lambda x: (x["outcome_time"] or 0)):
        if t["outcome"] in ("TP1", "TP2", "TP3"):
            cur_win += 1
            best_win_streak = max(best_win_streak, cur_win)
            cur_loss = 0
        elif t["outcome"] == "SL":
            cur_loss += 1
            worst_loss_streak = max(worst_loss_streak, cur_loss)
            cur_win = 0
        else:
            cur_win = 0
            cur_loss = 0

    # temps moyen (entry -> outcome)
    durations = [t["duration_sec"] for t in trades if t.get("duration_sec")]
    avg_time_to_outcome_sec = int(sum(durations) / len(durations)) if durations else 0

    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "tp1_hits": tp1_hits,
        "tp2_hits": tp2_hits,
        "tp3_hits": tp3_hits,
        "winrate_pct": winrate_pct,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
        "avg_time_to_outcome_sec": avg_time_to_outcome_sec,
    }

    return trades, summary


# ---------- Persistance d'un événement TradingView ----------
def _safe_trade_id(payload: Dict[str, Any]) -> Optional[str]:
    """
    Construit un trade_id s'il est absent, basé sur (symbol, tf, time).
    """
    tid = payload.get("trade_id")
    if tid:
        return str(tid)
    sym = str(payload.get("symbol") or "UNK")
    tf = str(payload.get("tf") or payload.get("tf_label") or "?")
    # time (ms) émis par TV si dispo, sinon now ms
    ts_ms = None
    for k in ("time", "ts", "timestamp", "entry_time", "t_ms"):
        if payload.get(k) is not None:
            try:
                ts_ms = int(payload[k])
                break
            except Exception:
                pass
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return f"{sym}_{tf}_{ts_ms}"

def save_event(payload: Dict[str, Any]) -> None:
    """
    Insert un événement dans la table 'events'.
    """
    try:
        t = str(payload.get("type") or "EVENT").upper()
        sym = str(payload.get("symbol") or "?")
        tf = str(payload.get("tf") or payload.get("tf_label") or "")
        row = {
            "received_at": int(time.time()),
            "type": t,
            "symbol": sym,
            "tf": tf,
            "side": payload.get("side"),
            "entry": _to_float(payload.get("entry")),
            "sl": _to_float(payload.get("sl")),
            "tp1": _to_float(payload.get("tp1")),
            "tp2": _to_float(payload.get("tp2")),
            "tp3": _to_float(payload.get("tp3")),
            "trade_id": _safe_trade_id(payload),
            "raw_json": json.dumps(payload, ensure_ascii=False),
        }
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO events (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id, raw_json)
                VALUES (:received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :trade_id, :raw_json)
                """,
                row,
            )
            conn.commit()
        log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
                 row["type"], row["symbol"], row["tf"], row["trade_id"])
    except Exception as e:
        log.error("save_event failed: %s", e)


# ---------- Webhook TradingView ----------
@app.api_route("/tv-webhook", methods=["POST", "GET"])
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Accepte payloads de TradingView:
    {
      "type": "ENTRY|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CLOSE|VECTOR_CANDLE|AOE_*",
      "symbol": "...", "tf": "15", "side": "LONG|SHORT",
      "entry": 1.234, "sl": 1.111, "tp1": ..., "tp2": ..., "tp3": ...,
      "trade_id": "...", "leverage": "10x",
      "direction": "UP|DOWN", "price": 123.45, "note": "...", // VECTOR_CANDLE optionnels
      "time": 1712345678901 // ms optionnel
    }
    """
    # lecture body
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            try:
                body = dict(await request.form())
            except Exception:
                body = {}
    body_secret = (body or {}).get("secret")

    # Secret check
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST JSON to this endpoint"})

    # normalisation
    payload = dict(body or {})
    payload["type"] = str(payload.get("type") or "EVENT").upper()
    # tf: laisser tel quel (string '15' ou '60' ou '1D'), déjà géré dans tf_label_of

    # log debug (non verbeux)
    log.info("Webhook payload: %s", {k: payload.get(k) for k in ["type","symbol","tf","direction","price","trade_id"]})

    # persist
    save_event(payload)

    # Telegram
    msg = telegram_rich_message(payload)
    sent = None
    if msg:
        if payload["type"] == "VECTOR_CANDLE" and not TELEGRAM_NOTIFY_VECTOR:
            sent = False
        else:
            # on ne pin pas par défaut
            sent = send_telegram_ex(msg, pin=False).get("ok")

    return JSONResponse({"ok": True, "telegram_sent": bool(sent), "type": payload["type"], "ts": now_iso()})
# ============ main.py — BLOC 4/5 (Altseason + Templates HTML) ============

# ---------- Template HTML public pour /trades (avec Altseason en haut) ----------
TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>Trades — Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root{
    --bg:#0f172a; --card:#0b1220; --muted:#94a3b8; --fg:#e5e7eb;
    --border:#1f2937; --win:#16a34a; --loss:#ef4444; --close:#eab308; --open:#38bdf8;
    --accent:#22c55e; --accent2:#06b6d4; --bad:#ef4444; --ok:#22c55e; --warn:#eab308;
  }
  html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font-family:system-ui,Segoe UI,Roboto,Inter,Arial}
  a{color:#93c5fd;text-decoration:none}
  .wrap{max-width:1200px;margin:24px auto;padding:0 12px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
  .muted{color:var(--muted)}
  .grid{display:grid;grid-template-columns:1fr;gap:12px}
  @media(min-width:1000px){.grid{grid-template-columns:2fr 1fr}}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .pill{display:inline-flex;align-items:center;gap:6px;background:#111827;border:1px solid var(--border);border-radius:999px;padding:6px 10px;margin:4px 6px 0 0;font-size:13px}
  .ok{color:#86efac;border-color:rgba(34,197,94,.4)}
  .bad{color:#fca5a5;border-color:rgba(239,68,68,.4)}
  .warn{color:#fde68a;border-color:rgba(234,179,8,.4)}

  .table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;min-width:980px}
  th,td{padding:9px 10px;border-bottom:1px solid var(--border);text-align:left;font-size:14px;white-space:nowrap}
  th{position:sticky;top:0;background:#0f172a;font-weight:600}
  .chip{display:inline-block;padding:3px 8px;border-radius:10px;border:1px solid var(--border);font-size:12px}
  .chip.win{background:rgba(22,163,74,.12);border-color:rgba(22,163,74,.4);color:#86efac}
  .chip.loss{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.4);color:#fca5a5}
  .chip.close{background:rgba(234,179,8,.12);border-color:rgba(234,179,8,.4);color:#fde68a}
  .chip.open{background:rgba(56,189,248,.12);border-color:rgba(56,189,248,.4);color:#bae6fd}
  .chip.muted{background:#0b1220;color:var(--muted)}

  /* Inputs / boutons */
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px}
  .filters input{background:#0b1220;border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--fg)}
  .filters button{background:#1d4ed8;border:0;border-radius:8px;padding:8px 12px;color:white;cursor:pointer}

  /* Coloration des colonnes TP1/TP2/TP3 si hit */
  td.tp-hit{background:rgba(22,163,74,.12)}
  td.sl-hit{background:rgba(239,68,68,.12)}
  td.none{background:transparent}

  /* Altseason board */
  .alts-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  @media(max-width:820px){.alts-grid{grid-template-columns:repeat(2,1fr)}}
  .alts-kpi{background:#0b1220;border:1px solid var(--border);border-radius:12px;padding:12px}
  .alts-kpi h3{margin:0 0 8px 0;font-size:13px;color:var(--muted);font-weight:600}
  .alts-kpi .v{font-weight:700;font-size:18px}
  .ok .v{color:#86efac}
  .bad .v{color:#fca5a5}
  .alts-footer{margin-top:6px;font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1 style="margin:0 0 8px 0">Trades — Dashboard</h1>
    <div class="muted" style="margin-bottom:10px">Filtrez par symbole / timeframe / date, puis validez.</div>

    <!-- Altseason mini-dashboard -->
    ${altseason_html}

    <form method="get" action="/trades" class="filters">
      <input type="text" name="symbol" placeholder="symbol (ex: BTCUSDT.P)" value="${symbol}" />
      <input type="text" name="tf" placeholder="tf (ex: 15, 60, 1D)" value="${tf}" />
      <input type="date" name="start" value="${start}" />
      <input type="date" name="end" value="${end}" />
      <input type="number" min="1" max="10000" name="limit" value="${limit}" />
      <button type="submit">Appliquer</button>
      <a href="/" class="pill">← Home</a>
    </form>

    <div class="card" style="padding:12px;margin-top:6px">
      <div class="row">
        <span class="pill">Total: <strong>${total_trades}</strong></span>
        <span class="pill">Winrate: <strong>${winrate_pct}%</strong></span>
        <span class="pill ok">Wins: <strong>${wins}</strong></span>
        <span class="pill bad">Losses: <strong>${losses}</strong></span>
        <span class="pill ok">TP1: <strong>${tp1_hits}</strong></span>
        <span class="pill ok">TP2: <strong>${tp2_hits}</strong></span>
        <span class="pill ok">TP3: <strong>${tp3_hits}</strong></span>
        <span class="pill">Avg. time: <strong>${avg_time_to_outcome_sec}s</strong></span>
        <span class="pill ok">Best streak: <strong>${best_win_streak}</strong></span>
        <span class="pill bad">Worst loss streak: <strong>${worst_loss_streak}</strong></span>
      </div>
    </div>

    <div class="table-wrap" style="margin-top:12px">
      <table>
        <thead>
          <tr>
            <th>Trade ID</th><th>Symbol</th><th>TF</th><th>Side</th>
            <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
            <th>Opened</th><th>Outcome</th><th>Duration(s)</th>
          </tr>
        </thead>
        <tbody>
          ${rows_html}
        </tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
""")

# ---------- Générateur du bloc HTML Altseason (inséré en haut de /trades) ----------
def _kfmt(n: Optional[float]) -> str:
    try:
        if n is None: return "—"
        n = float(n)
        if abs(n) >= 1e12: return f"{n/1e12:.2f} T"
        if abs(n) >= 1e9:  return f"{n/1e9:.2f} B"
        if abs(n) >= 1e6:  return f"{n/1e6:.2f} M"
        if abs(n) >= 1e3:  return f"{n/1e3:.2f} K"
        return f"{n:.2f}"
    except Exception:
        return "—"

def altseason_html_block(summary: Dict[str, Any]) -> str:
    """
    Retourne un board HTML compact avec 4 KPIs principaux + statut.
    """
    asof = summary.get("asof") or ""
    btc = summary.get("btc_dominance")
    eth = summary.get("eth_btc")
    t2  = summary.get("total2_usd")
    asi = summary.get("altseason_index")

    trg = summary.get("triggers") or {}
    c_btc = "ok" if trg.get("btc_dominance_ok") else "bad"
    c_eth = "ok" if trg.get("eth_btc_ok") else "bad"
    c_t2  = "ok" if trg.get("total2_ok") else "bad"
    c_asi = "ok" if trg.get("altseason_index_ok") else "bad"

    greens = int(summary.get("greens") or 0)
    on = bool(summary.get("ALTSEASON_ON"))
    badge = "<span class='pill ok'>ALTSEASON: ON</span>" if on else "<span class='pill warn'>ALTSEASON: WATCH</span>"

    return f"""
    <div class="card" style="margin-bottom:10px">
      <div class="row" style="justify-content:space-between">
        <div class="row">
          <strong style="font-size:16px">Indicateurs Altseason</strong>
          {badge}
          <span class="pill">Greens: <strong>{greens}</strong> / {int((summary.get('thresholds') or {}).get('greens_required') or 4)}</span>
        </div>
        <div class="muted">as of {escape_html(str(asof))}</div>
      </div>
      <div class="alts-grid" style="margin-top:10px">
        <div class="alts-kpi {c_btc}">
          <h3>BTC Dominance &lt; {fmt_num((summary.get('thresholds') or {}).get('btc_dominance_max'))}%</h3>
          <div class="v">{fmt_num(btc)}%</div>
          <div class="alts-footer">{'OK' if c_btc=='ok' else 'Pas atteint'}</div>
        </div>
        <div class="alts-kpi {c_eth}">
          <h3>ETH/BTC &gt; {fmt_num((summary.get('thresholds') or {}).get('eth_btc_min'))}</h3>
          <div class="v">{fmt_num(eth)}</div>
          <div class="alts-footer">{'OK' if c_eth=='ok' else 'Pas atteint'}</div>
        </div>
        <div class="alts-kpi {c_t2}">
          <h3>Total2 &gt; {fmt_num((summary.get('thresholds') or {}).get('total2_min_trillions'))} T$</h3>
          <div class="v">{_kfmt(t2)}</div>
          <div class="alts-footer">{'OK' if c_t2=='ok' else 'Pas atteint'}</div>
        </div>
        <div class="alts-kpi {c_asi}">
          <h3>Altseason Index &gt; {fmt_num((summary.get('thresholds') or {}).get('altseason_index_min'))}</h3>
          <div class="v">{fmt_num(asi)}</div>
          <div class="alts-footer">{'OK' if c_asi=='ok' else 'Pas atteint'}</div>
        </div>
      </div>
    </div>
    """

# ---------- Altseason: fetch + cache + résumés + endpoints ----------
_alt_cache: Dict[str, Any] = {"ts": 0, "snap": None}
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120") or 120)
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3") or 3)
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0") or 55.0)
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045") or 0.045)
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0") or 75.0)
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78") or 1.78)  # trillions
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1","true","True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300") or 300)
TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1","true","True")

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
        "User-Agent": "altseason-bot/1.7",
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

    # Total2
    if mcap_usd is not None and btc_dom is not None:
        total2_usd = mcap_usd * (1.0 - btc_dom/100.0)
    else:
        total2_usd = None

    # ETH/BTC
    eth_btc = None
    try:
        j = get_json("https://api.binance.com/api/v3/ticker/price?symbol=ETHBTC")
        eth_btc = float(j["price"])
    except Exception as e:
        out["errors"].append(f"binance: {e!r}")

    # Altseason Index (scrape léger si possible)
    asi = None
    try:
        import requests
        from bs4 import BeautifulSoup  # type: ignore
        html = requests.get(
            "https://www.blockchaincenter.net/altcoin-season-index/",
            timeout=12, headers=headers
        ).text
        soup = BeautifulSoup(html, "html.parser")
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"Altcoin Season Index[^0-9]*([0-9]{2,3})", txt)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100:
                asi = v
    except Exception as e:
        out["errors"].append(f"altseason_index_scrape: {e!r}")

    out["total_mcap_usd"] = (None if mcap_usd is None else float(mcap_usd))
    out["btc_dominance"] = (None if btc_dom is None else float(btc_dom))
    out["total2_usd"] = (None if total2_usd is None else float(total2_usd))
    out["eth_btc"] = (None if eth_btc is None else float(eth_btc))
    out["altseason_index"] = (None if asi is None else int(asi))
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
    try:
        snap = _altseason_snapshot(force=False)
        return _altseason_summary(snap)
    except Exception as e:
        log.warning("Altseason summary error: %s", e)
        raise

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
    # Auth
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
# ============ main.py — BLOC 5/5 (Routes + Helpers finaux + Run) ============

# --- Helpers manquants / sûreté ---
def now_iso(ts: Optional[float] = None) -> str:
    try:
        t = ts if ts is not None else time.time()
        return datetime.utcfromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_date_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch seconds (00:00:00)."""
    if not s: return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch seconds fin de journée (23:59:59)."""
    if not s: return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        dt = dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def fmt_ts(v: Optional[int]) -> str:
    try:
        if not v: return ""
        return datetime.utcfromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def outcome_label(o: str) -> str:
    o = (o or "").upper()
    return {
        "TP1":"TP1", "TP2":"TP2", "TP3":"TP3",
        "SL":"SL", "CLOSE":"Close", "OPEN":"OPEN"
    }.get(o, o or "—")

def chip_class(o: str) -> str:
    o = (o or "").upper()
    if o in ("TP1","TP2","TP3"): return "chip win"
    if o == "SL": return "chip loss"
    if o == "CLOSE": return "chip close"
    if o == "OPEN": return "chip open"
    return "chip muted"

def _row_class_for_cols(outcome: str) -> Dict[str, str]:
    """Retourne classes pour TD TP1/TP2/TP3/SL selon l'issue."""
    o = (outcome or "").upper()
    cls = {"tp1":"","tp2":"","tp3":"","sl":""}
    if o == "TP1": cls["tp1"] = "tp-hit"
    elif o == "TP2": cls["tp2"] = "tp-hit"
    elif o == "TP3": cls["tp3"] = "tp-hit"
    elif o == "SL": cls["sl"] = "sl-hit"
    return cls

# --- Re-définition du message Telegram pour Vector: 🟩 UP, 🟥 DOWN ---
def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    """
    Construit un message Telegram lisible (Vector Candle: carré vert pour UP, rouge pour DOWN).
    """
    t = str(payload.get("type") or "EVENT").upper()
    if t.startswith("AOE_"):
        return None

    sym = str(payload.get("symbol") or "?")
    tf_lbl = tf_label_of(payload)
    side = str(payload.get("side") or "")
    entry = _to_float(payload.get("entry"))
    sl = _to_float(payload.get("sl"))
    tp = _to_float(payload.get("tp"))
    tp1 = _to_float(payload.get("tp1"))
    tp2 = _to_float(payload.get("tp2"))
    tp3 = _to_float(payload.get("tp3"))
    leverage = payload.get("leverage") or payload.get("lev") or payload.get("lev_reco")

    def num(v): return fmt_num(v) if v is not None else "—"

    if t == "ENTRY":
        lines = [f"📩 {sym} {tf_lbl}"]
        if side:
            lines.append(("📈 Long Entry:" if side.upper()=="LONG" else "📉 Short Entry:") + f" {num(entry)}")
        if leverage: lines.append(f"💡Leverage: {leverage}")
        if tp1: lines.append(f"🎯 TP1: {num(tp1)}")
        if tp2: lines.append(f"🎯 TP2: {num(tp2)}")
        if tp3: lines.append(f"🎯 TP3: {num(tp3)}")
        if sl:  lines.append(f"❌ SL: {num(sl)}")
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

    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        lines = [f"✅ {label} — {sym} {tf_lbl}"]
        if tp is not None: lines.append(f"Mark price : {num(tp)}")
        return "\n".join(lines)

    if t == "SL_HIT":
        lines = [f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None: lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason: lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    if t == "VECTOR_CANDLE":
        direction = str(payload.get("direction") or "").upper()
        price = payload.get("price") or payload.get("level")
        icon = "🟩" if direction == "UP" else ("🟥" if direction == "DOWN" else "🟪")
        title = f"{icon} Vector Candle — {sym} {tf_lbl}"
        lines = [title]
        if direction: lines.append(f"Direction: {direction}")
        if price is not None: lines.append(f"Niveau repéré: {fmt_num(_to_float(price))}")
        note = payload.get("note")
        if note: lines.append(str(note))
        return "\n".join(lines)

    return f"[TV] {t} | {sym} | TF {tf_lbl}"

# --- Lecture & agrégation des trades ---
def _extract_event_ts(row: sqlite3.Row) -> int:
    """Prend received_at par défaut, sinon 'time' (ms) dans raw_json s'il existe."""
    try:
        j = json.loads(row["raw_json"] or "{}")
        ms = j.get("time")
        if isinstance(ms, (int, float)) and ms > 1e11:
            return int(float(ms) / 1000.0)
    except Exception:
        pass
    return int(row["received_at"])

def build_trades_filtered(symbol: Optional[str], tf: Optional[str],
                          start_ep: Optional[int], end_ep: Optional[int],
                          max_rows: int = 5000) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Reconstruit les trades par trade_id à partir de la table events et calcule un 'outcome' simple:
    - si TP3_HIT, outcome=TP3 ; sinon TP2_HIT -> TP2 ; TP1_HIT -> TP1 ; SL_HIT -> SL ;
      sinon CLOSE -> CLOSE ; sinon OPEN.
    """
    q = "SELECT * FROM events WHERE 1=1"
    params: List[Any] = []
    if symbol:
        q += " AND symbol = ?"; params.append(symbol)
    if tf:
        q += " AND tf = ?"; params.append(tf)
    if start_ep:
        q += " AND received_at >= ?"; params.append(int(start_ep))
    if end_ep:
        q += " AND received_at <= ?"; params.append(int(end_ep))
    q += " ORDER BY received_at ASC LIMIT ?"; params.append(int(max_rows))

    trades: Dict[str, Dict[str, Any]] = {}
    with db_conn() as conn:
        for row in conn.execute(q, params):
            r = dict(row)
            tid = r.get("trade_id") or ""
            if not tid:
                # on regroupe les events sans trade_id par (symbol|tf|received_at)
                tid = f"{r.get('symbol')}_{r.get('tf')}_{r.get('received_at')}"
            t = trades.setdefault(tid, {
                "trade_id": tid,
                "symbol": r.get("symbol"),
                "tf": r.get("tf"),
                "side": r.get("side"),
                "entry": r.get("entry"),
                "sl": r.get("sl"),
                "tp1": r.get("tp1"),
                "tp2": r.get("tp2"),
                "tp3": r.get("tp3"),
                "entry_time": None,
                "duration_sec": None,
                "outcome": "OPEN",
            })

            etype = (r.get("type") or "").upper()
            ets = _extract_event_ts(row)
            if etype == "ENTRY" and t["entry_time"] is None:
                t["entry_time"] = ets
                # complétion des champs
                for k in ("entry","sl","tp1","tp2","tp3","side"):
                    if r.get(k) is not None:
                        t[k] = r.get(k)

            # outcome progression
            if etype in ("TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT","CLOSE"):
                priority = {"TP1_HIT":1,"TP2_HIT":2,"TP3_HIT":3,"SL_HIT":0,"CLOSE":-1}
                current = t.get("_prio", -2)
                pr = priority.get(etype, -2)
                if pr > current:
                    t["_prio"] = pr
                    t["outcome"] = {"TP1_HIT":"TP1","TP2_HIT":"TP2","TP3_HIT":"TP3","SL_HIT":"SL","CLOSE":"CLOSE"}[etype]
                    if t.get("entry_time") is not None:
                        t["duration_sec"] = max(0, ets - int(t["entry_time"]))

    # stats
    arr = list(trades.values())
    wins = sum(1 for x in arr if x["outcome"] in ("TP1","TP2","TP3"))
    losses = sum(1 for x in arr if x["outcome"] == "SL")
    total = len(arr)
    winrate = (wins/total*100.0) if total else 0.0
    avg_dur = int(sum(x.get("duration_sec") or 0 for x in arr)/total) if total else 0
    # streaks simplifiés
    best_win_streak = worst_loss_streak = 0
    cur_win = cur_loss = 0
    for x in arr:
        if x["outcome"] in ("TP1","TP2","TP3"):
            cur_win += 1; best_win_streak = max(best_win_streak, cur_win); cur_loss = 0
        elif x["outcome"] == "SL":
            cur_loss += 1; worst_loss_streak = max(worst_loss_streak, cur_loss); cur_win = 0
        else:
            cur_win = 0; cur_loss = 0

    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "tp1_hits": sum(1 for x in arr if x["outcome"] == "TP1"),
        "tp2_hits": sum(1 for x in arr if x["outcome"] == "TP2"),
        "tp3_hits": sum(1 for x in arr if x["outcome"] == "TP3"),
        "avg_time_to_outcome_sec": avg_dur,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
    }
    return arr, summary

# --- Home ---
@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8">
    <title>AI Trader PRO</title>
    <style>
      body{background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto}
      .wrap{max-width:860px;margin:48px auto;padding:0 12px}
      .card{background:#0b1220;border:1px solid #1f2937;border-radius:14px;padding:16px}
      a{color:#93c5fd;text-decoration:none}
      .row{display:flex;flex-direction:column;gap:8px}
      .pill{display:inline-block;background:#111827;border:1px solid #1f2937;border-radius:999px;padding:8px 12px}
    </style></head><body>
    <div class="wrap">
      <div class="card">
        <h1>AI Trader PRO</h1>
        <p>Bienvenue. Utilisez le tableau des trades ou les outils Altseason.</p>
        <div class="row">
          <a class="pill" href="/trades">📊 Trades — Dashboard</a>
          <a class="pill" href="/altseason/check">🟢 Altseason — Check</a>
          <a class="pill" href="/altseason/streaks">📈 Altseason — Streaks</a>
          <a class="pill" href="/altseason/daemon-status">⚙️ Altseason — Daemon status</a>
        </div>
      </div>
    </div>
    </body></html>
    """)

# --- Trades public (avec Altseason board + coloration TP) ---
@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    try:
        limit = max(1, min(int(limit or 100), 10000))
        start_ep = parse_date_to_epoch(start)
        end_ep = parse_date_end_to_epoch(end)

        trades, summary = build_trades_filtered(
            symbol, tf, start_ep, end_ep, max_rows=max(5000, limit * 10)
        )

        # Altseason mini-board (robuste)
        alt_html = ""
        try:
            s = _altseason_summary(_altseason_snapshot(force=False))
            alt_html = altseason_html_block(s)
        except Exception as e:
            log.warning("Altseason summary error: %s", e)
            alt_html = "<div class='muted' style='margin-bottom:8px'>Altseason indisponible pour le moment.</div>"

        rows_html = ""
        data = trades[-limit:] if limit else trades
        for tr in data:
            outcome = (tr.get("outcome") or "NONE")
            badge = chip_class(outcome)
            label = outcome_label(outcome)
            cls = _row_class_for_cols(outcome)

            rows_html += (
                "<tr>"
                f"<td>{escape_html(str(tr.get('trade_id') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
                f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
                f"<td>{fmt_num(tr.get('entry'))}</td>"
                f"<td class='{cls['sl'] or 'none'}'>{fmt_num(tr.get('sl'))}</td>"
                f"<td class='{cls['tp1'] or 'none'}'>{fmt_num(tr.get('tp1'))}</td>"
                f"<td class='{cls['tp2'] or 'none'}'>{fmt_num(tr.get('tp2'))}</td>"
                f"<td class='{cls['tp3'] or 'none'}'>{fmt_num(tr.get('tp3'))}</td>"
                f"<td>{escape_html(fmt_ts(tr.get('entry_time')))}</td>"
                f"<td><span class='{badge}'>{escape_html(label)}</span></td>"
                f"<td>{'' if tr.get('duration_sec') is None else str(tr.get('duration_sec'))}</td>"
                "</tr>"
            )

        def s(v): return "" if v is None else str(v)

        html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
            altseason_html=alt_html,
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
        )
        return HTMLResponse(html)

    except Exception as e:
        log.exception("Error in /trades: %s", e)
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

# --- (Optionnel) Daemon altseason : éviter ALT_INTERVAL non défini ---
ALT_INTERVAL = ALTSEASON_POLL_SECONDS  # alias + cohérence
def run_altseason_daemon(interval: int = ALT_INTERVAL):
    """Boucle simple: fetch périodique + mémo sur disque (facultatif)."""
    while True:
        try:
            _ = _altseason_snapshot(force=True)
        except Exception as e:
            log.warning("altseason daemon tick error: %s", e)
        time.sleep(max(60, int(interval)))

# -------------------------
# Run local (for debug)
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
