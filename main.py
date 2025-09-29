# ============ main.py — BLOC 1/5 (Imports, Config, App, DB, Helpers) ============

import os
import re
import json
import time
import math
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from datetime import datetime, timedelta, timezone

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
app = FastAPI(title="AI Trader PRO", docs_url=None, redoc_url=None)

# -------------------------
# Config / ENV
# -------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_ENABLED = os.getenv("LLM_ENABLED", "0") in ("1", "true", "True")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0") or 0)

PORT = int(os.getenv("PORT", "8000"))
DEBUG_MODE = os.getenv("DEBUG", "0") in ("1", "true", "True")

# Telegram anti-spam
TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0

# DB path (fallback /tmp si non writable)
DB_PATH = os.getenv("DB_PATH", "data/data.db")

# Altseason thresholds (utilisés par /altseason & /trades header)
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1", "true", "True")
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

# -------------------------
# Helpers généraux
# -------------------------
def tf_label_of(payload: Dict[str, Any]) -> str:
    """Formate la TF : '15' -> '15m', '60' -> '1h', '1440' -> '1D'"""
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

def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except Exception:
        return None

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;").replace("'", "&#39;")
    )

def fmt_num(v) -> str:
    try:
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return "—"
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v if v is not None else "—")

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

def safe_int(x, default=None) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return default

def parse_date_to_epoch(d: Optional[str]) -> Optional[int]:
    """'YYYY-MM-DD' -> epoch (00:00:00 UTC). None si invalide/None."""
    if not d:
        return None
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)  # ms pour cohérence TV
    except Exception:
        return None

def parse_date_end_to_epoch(d: Optional[str]) -> Optional[int]:
    """'YYYY-MM-DD' -> epoch fin de journée (23:59:59 UTC)."""
    if not d:
        return None
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1, seconds=-1)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def fmt_ts(ms: Optional[int]) -> str:
    """Epoch ms -> ISO courte UTC."""
    try:
        if ms is None:
            return ""
        return datetime.utcfromtimestamp(int(ms)/1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def chip_class(outcome: str) -> str:
    oc = (outcome or "").upper()
    if oc in ("TP3","TP3_HIT"): return "chip win"
    if oc in ("TP2","TP2_HIT"): return "chip win"
    if oc in ("TP1","TP1_HIT"): return "chip win"
    if oc in ("SL","SL_HIT"):   return "chip loss"
    if oc in ("CLOSE",):        return "chip close"
    if oc in ("OPEN","ENTRY"):  return "chip open"
    return "chip muted"

def outcome_label(outcome: str) -> str:
    oc = (outcome or "").upper()
    mapping = {
        "TP1_HIT":"TP1", "TP2_HIT":"TP2", "TP3_HIT":"TP3",
        "SL_HIT":"SL", "CLOSE":"Close", "ENTRY":"OPEN"
    }
    return mapping.get(oc, oc or "—")

# -------------------------
# SQLite — init robuste
# -------------------------
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
    """Crée la table events si absente (utilisée par /tv-webhook et /trades)."""
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
                tp REAL,
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
# Sauvegarde d'un event (utilisé par /tv-webhook)
# -------------------------
def save_event(payload: Dict[str, Any]) -> None:
    """Insert un événement TradingView dans SQLite (robuste)."""
    row = {
        "received_at": int(time.time()),
        "type": str(payload.get("type") or "EVENT").upper(),
        "symbol": payload.get("symbol"),
        "tf": str(payload.get("tf")) if payload.get("tf") is not None else None,
        "side": payload.get("side"),
        "entry": _to_float(payload.get("entry")),
        "sl": _to_float(payload.get("sl")),
        "tp1": _to_float(payload.get("tp1")),
        "tp2": _to_float(payload.get("tp2")),
        "tp3": _to_float(payload.get("tp3")),
        "tp": _to_float(payload.get("tp")),  # niveau exécuté sur TP*_HIT/SL_HIT
        "trade_id": payload.get("trade_id"),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO events
                (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, tp, trade_id, raw_json)
                VALUES
                (:received_at,:type,:symbol,:tf,:side,:entry,:sl,:tp1,:tp2,:tp3,:tp,:trade_id,:raw_json)
                """,
                row,
            )
            conn.commit()
        log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
                 row["type"], row["symbol"], row["tf"], row["trade_id"])
    except Exception as e:
        log.exception("save_event failed: %s", e)
# ============ main.py — BLOC 2/5 (Telegram, LLM, Message builder) ============

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
# Telegram minimal + enrichi (anti-spam)
# -------------------------
def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (sans bouton). Respecte un cooldown simple."""
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
    Envoi enrichi: inline bouton vers /trades + option pin.
    Respecte le cooldown et gère HTTP 429 proprement.
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
            log.warning("Telegram send skipped due to cooldown")
            result["ok"] = False
            result["error"] = "cooldown"
            return result
        _last_tg = now

        api_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
        # sendMessage
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
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except Exception as e:
            # Gestion explicite des 429
            result["error"] = f"sendMessage exception: {e}"
            log.warning("Telegram sendMessage exception: %s", e)
            return result

        try:
            p = _json.loads(raw)
        except Exception:
            p = {"ok": False, "error": raw[:200]}

        if not p.get("ok"):
            result["error"] = f"sendMessage failed: {str(p)[:200]}"
            log.warning("Telegram sendMessage error: %s", result["error"])
            return result

        msg = p.get("result") or {}
        result["ok"] = True
        result["message_id"] = msg.get("message_id")

        # Pin si demandé
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
                    try:
                        pp = _json.loads(praw)
                    except Exception:
                        pp = {"ok": False, "error": praw[:200]}
                    if pp.get("ok"):
                        result["pinned"] = True
                    else:
                        result["error"] = f"pinChatMessage failed: {str(pp)[:200]}"
                        log.warning("Telegram pinChatMessage error: %s", result["error"])
            except Exception as e:
                result["error"] = f"pinChatMessage exception: {e}"
                log.warning("Telegram pin exception: %s", e)

        return result

    except Exception as e:
        result["error"] = f"send_telegram_ex exception: {e}"
        log.warning("Telegram send_telegram_ex exception: %s", e)
        return result

# -------------------------
# LLM: score de confiance ENTRY (parser robuste)
# -------------------------
def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Retourne (pourcentage_confiance, rationale) ou None si indisponible.
    Parsing robuste pour éviter l'erreur: 'Expecting value: line 1...'
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

        # 1) Essai JSON direct
        try:
            data = json.loads(content)
            conf = float(data.get("confidence", 0))
            rationale = str(data.get("rationale", "")).strip()
            conf = max(0.0, min(100.0, conf))
            return (conf, rationale)
        except Exception:
            pass

        # 2) Extraction regex d'un nombre 0..100
        m = re.search(r"confidence[^0-9]{0,10}(\d{1,3})", content, re.I)
        if m:
            conf = float(min(100, max(0, int(m.group(1)))))
            # Récup rationale courte si possible
            m2 = re.search(r"rationale[^:]*:\s*['\"]?(.+?)['\"]?\s*(?:[}\n]|$)", content, re.I)
            rationale = (m2.group(1).strip() if m2 else "").splitlines()[0][:120]
            return (conf, rationale)

        # 3) Par défaut si la réponse est non JSON mais fournie
        if content:
            # Cherche le premier pourcentage dans le texte
            m3 = re.search(r"(\d{1,3})\s*%", content)
            if m3:
                conf = float(min(100, max(0, int(m3.group(1)))))
                rationale = content[:120]
                return (conf, rationale)

        return None
    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        return None

# -------------------------
# Builder des messages Telegram (inclut Vector UP/DOWN)
# -------------------------
def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    """
    Construit un message Telegram lisible pour événements TradingView.
    Retourne None pour ignorer certains types (ex: AOE_*).
    """
    t = str(payload.get("type") or "EVENT").upper()
    # On ignore les "AOE_*" dans Telegram
    if t.startswith("AOE_"):
        return None

    sym = str(payload.get("symbol") or "?")
    tf_lbl = tf_label_of(payload)
    side = str(payload.get("side") or "")
    entry = _to_float(payload.get("entry"))
    sl = _to_float(payload.get("sl"))
    tp = _to_float(payload.get("tp"))   # niveau exécuté pour TP/SL hits
    tp1 = _to_float(payload.get("tp1"))
    tp2 = _to_float(payload.get("tp2"))
    tp3 = _to_float(payload.get("tp3"))
    lev = payload.get("leverage") or payload.get("lev") or payload.get("lev_reco")

    def num(v): return fmt_num(v) if v is not None else "—"

    # ENTRY
    if t == "ENTRY":
        lines = [f"📩 {sym} {tf_lbl}"]
        if side:
            lines.append(("📈 Long Entry:" if side.upper()=="LONG" else "📉 Short Entry:") + f" {num(entry)}")
        if lev: lines.append(f"💡Leverage: {lev}")
        if tp1: lines.append(f"🎯 TP1: {num(tp1)}")
        if tp2: lines.append(f"🎯 TP2: {num(tp2)}")
        if tp3: lines.append(f"🎯 TP3: {num(tp3)}")
        if sl:  lines.append(f"❌ SL: {num(sl)}")

        # Confiance LLM (affichage non bloquant)
        try:
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
        # On essaie de calculer le % par rapport à l'entry si elle est connue
        spot_pct = pct(tp, entry) if (tp is not None and entry is not None and side) else None
        lines = [f"✅ {label} — {sym} {tf_lbl}"]
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

    # VECTOR_CANDLE (avec direction)
    if t == "VECTOR_CANDLE":
        direction = str(payload.get("direction") or "").upper()
        price = _to_float(payload.get("level") or payload.get("price"))
        note = str(payload.get("note") or "")
        # Badge direction
        if direction == "UP":
            head = f"🟪 Vector Candle (UP) — {sym} {tf_lbl} ⬆️"
        elif direction == "DOWN":
            head = f"🟪 Vector Candle (DOWN) — {sym} {tf_lbl} ⬇️"
        else:
            head = f"🟪 Vector Candle — {sym} {tf_lbl}"
        lines = [head]
        if side:  # si jamais un champ 'side' est envoyé
            lines.append(f"Contexte: {side}")
        if price is not None:
            lines.append(f"Niveau repéré: {num(price)}")
        if note:
            lines.append(note)
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"
# ============ main.py — BLOC 3/5 (Webhook & Save Event) ============

# -------------------------
# Persistence des événements
# -------------------------
def save_event(payload: Dict[str, Any]) -> None:
    """Enregistre un événement brut dans SQLite."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO events (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3,
                                r1, s1, trade_id, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now_iso(),
            payload.get("type"),
            payload.get("symbol"),
            str(payload.get("tf") or ""),
            payload.get("side"),
            payload.get("entry"),
            payload.get("sl"),
            payload.get("tp1"),
            payload.get("tp2"),
            payload.get("tp3"),
            payload.get("r1"),
            payload.get("s1"),
            payload.get("trade_id"),
            json.dumps(payload, ensure_ascii=False)
        ))
        conn.commit()
        conn.close()
        log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
                 payload.get("type"), payload.get("symbol"), payload.get("tf"), payload.get("trade_id"))
    except Exception as e:
        log.error("save_event failed: %s", e)

# -------------------------
# Webhook TradingView
# -------------------------
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    """Réception des alertes TradingView."""
    try:
        payload = await request.json()
    except Exception:
        # Cas : TradingView envoie du non-JSON (erreurs Invalid JSON: Expecting ',' delimiter)
        body = await request.body()
        log.error("Invalid JSON payload: %s", body[:200])
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Vérif secret
    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    # Ajout trade_id si ENTRY
    if payload.get("type") == "ENTRY":
        if "trade_id" not in payload or not payload["trade_id"]:
            payload["trade_id"] = make_trade_id(payload)

    save_event(payload)

    # Message Telegram (si applicable)
    text = telegram_rich_message(payload)
    if text:
        send_telegram_ex(text, pin=False)

    return {"ok": True}
# ============ main.py — BLOC 4/5 (UI /trades : Altseason + Tableau Trades) ============

# ---------- Helpers locaux sûrs (si absents) ----------
if "outcome_label" not in globals():
    def outcome_label(outcome: str) -> str:
        return (outcome or "NONE").replace("_HIT", "").title() if outcome else "OPEN"

if "chip_class" not in globals():
    def chip_class(outcome: str) -> str:
        if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT"): return "chip win"
        if outcome == "SL_HIT": return "chip loss"
        if outcome == "CLOSE":  return "chip close"
        return "chip open"

if "fmt_ts" not in globals():
    def fmt_ts(ts: int | None) -> str:
        if not ts: return "—"
        try:
            return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "—"

if "parse_date_to_epoch" not in globals():
    def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
        if not date_str: return None
        try:
            y, m, d = map(int, date_str.split("-"))
            return int(datetime(y, m, d, 0, 0, 0).timestamp())
        except Exception:
            return None

if "parse_date_end_to_epoch" not in globals():
    def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
        if not date_str: return None
        try:
            y, m, d = map(int, date_str.split("-"))
            return int(datetime(y, m, d, 23, 59, 59).timestamp())
        except Exception:
            return None

# ---------- Construction des trades depuis la table `events` ----------
def fetch_events_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    limit_rows: int = 20000
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        sql += " AND symbol = ?"; args.append(symbol)
    if tf:
        sql += " AND tf = ?"; args.append(str(tf))
    if start_ep is not None:
        sql += " AND strftime('%s', received_at) >= ?" if isinstance(start_ep, str) else " AND CAST(strftime('%s', received_at) AS INT) >= ?"
        args.append(str(start_ep))
    if end_ep is not None:
        sql += " AND CAST(strftime('%s', received_at) AS INT) <= ?"
        args.append(str(end_ep))
    sql += " ORDER BY received_at ASC"
    if limit_rows:
        sql += " LIMIT ?"; args.append(int(limit_rows))

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, tuple(args))
    rows = cur.fetchall()
    conn.close()
    return rows

def build_trades_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    max_rows: int = 20000
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)

    trades: List[Dict[str, Any]] = []
    open_by_key: Dict[Tuple[str, str], List[int]] = {}

    def open_stack(key: Tuple[str,str]) -> List[int]:
        if key not in open_by_key: open_by_key[key] = []
        return open_by_key[key]

    # Reconstitution LIFO par (symbol, tf)
    for r in rows:
        et = (r["type"] or "").upper()
        sym = r["symbol"]
        tfv = str(r["tf"] or "")
        key = (sym, tfv)
        if et == "ENTRY":
            tr = {
                "trade_id": r["trade_id"] or f"{sym}_{tfv}_{int(time.time())}",
                "symbol": sym, "tf": tfv, "side": r["side"],
                "entry": r["entry"], "sl": r["sl"], "tp1": r["tp1"], "tp2": r["tp2"], "tp3": r["tp3"],
                "entry_time": int(time.time()),  # fallback si colonne textuelle
                "outcome": "NONE", "outcome_time": None, "duration_sec": None,
            }
            # si le champ created_at/received_at est texte (ISO), essaye de parser
            try:
                # supporte string ISO ou déjà epoch-like
                if isinstance(r["received_at"], (int, float)): tr["entry_time"] = int(r["received_at"])
                else:
                    tr["entry_time"] = int(datetime.fromisoformat(str(r["received_at"]).replace("Z","")).timestamp())
            except Exception:
                pass
            trades.append(tr)
            open_stack(key).append(len(trades)-1)
            continue

        if et in ("TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT","CLOSE"):
            stack = open_stack(key)
            # ferme le plus récent encore ouvert
            while stack:
                idx = stack[-1]
                if trades[idx]["outcome"] == "NONE":
                    trades[idx]["outcome"] = et
                    try:
                        ts = r["received_at"]
                        if isinstance(ts, (int, float)): ts_ep = int(ts)
                        else: ts_ep = int(datetime.fromisoformat(str(ts).replace("Z","")).timestamp())
                    except Exception:
                        ts_ep = int(time.time())
                    trades[idx]["outcome_time"] = ts_ep
                    if trades[idx]["entry_time"]:
                        trades[idx]["duration_sec"] = max(0, int(ts_ep - int(trades[idx]["entry_time"])))
                    stack.pop()
                    break
                else:
                    stack.pop()

    # Stats
    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] in ("TP1_HIT","TP2_HIT","TP3_HIT"))
    losses = sum(1 for t in trades if t["outcome"] == "SL_HIT")
    winrate = round((wins/total*100.0), 2) if total else 0.0
    tp1_hits = sum(1 for t in trades if t["outcome"] == "TP1_HIT")
    tp2_hits = sum(1 for t in trades if t["outcome"] == "TP2_HIT")
    tp3_hits = sum(1 for t in trades if t["outcome"] == "TP3_HIT")
    avg_time = 0
    times = [t["duration_sec"] for t in trades if t["duration_sec"]]
    if times:
        avg_time = int(sum(times)/len(times))

    summary = {
        "total_trades": total, "wins": wins, "losses": losses,
        "winrate_pct": winrate, "tp1_hits": tp1_hits, "tp2_hits": tp2_hits, "tp3_hits": tp3_hits,
        "avg_time_to_outcome_sec": avg_time, "best_win_streak": "", "worst_loss_streak": ""
    }
    return trades, summary

# ---------- Template /trades (inclut l’encadré Altseason) ----------
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
    --pill:#111827;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font-family:system-ui,Segoe UI,Roboto,Inter,Arial}
  a{color:#93c5fd;text-decoration:none}
  .wrap{max-width:1280px;margin:22px auto;padding:0 12px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
  h1{margin:0 0 12px 0;font-size:22px}
  h2{margin:0 0 8px 0;font-size:18px}
  .muted{color:var(--muted)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--pill);border:1px solid var(--border);border-radius:999px;padding:6px 10px;font-size:13px;margin:4px 6px 0 0}
  .pill.ok{border-color:rgba(22,163,74,.5);color:#86efac}
  .pill.ko{border-color:rgba(239,68,68,.5);color:#fca5a5}
  .pill.warn{border-color:rgba(234,179,8,.5);color:#fde68a}
  .grid{display:grid;grid-template-columns:1fr;gap:12px}
  @media(min-width:1050px){.grid{grid-template-columns:2fr 1fr}}
  .table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;min-width:980px}
  th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;font-size:14px;white-space:nowrap}
  th{position:sticky;top:0;background:#0f172a;font-weight:600;z-index:2}
  .chip{display:inline-block;padding:3px 8px;border-radius:10px;border:1px solid var(--border);font-size:12px}
  .chip.win{background:rgba(22,163,74,.12);border-color:rgba(22,163,74,.4);color:#86efac}
  .chip.loss{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.4);color:#fca5a5}
  .chip.close{background:rgba(234,179,8,.12);border-color:rgba(234,179,8,.4);color:#fde68a}
  .chip.open{background:rgba(56,189,248,.12);border-color:rgba(56,189,248,.4);color:#bae6fd}
  .chip.muted{background:#0b1220;color:var(--muted)}
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
  .filters input{background:#0b1220;border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--fg)}
  .filters button{background:#1d4ed8;border:0;border-radius:8px;padding:8px 12px;color:white;cursor:pointer}
  .tp-hit{background:rgba(22,163,74,.16);color:#bbf7d0;border-radius:6px;padding:0 6px}
  .tp-miss{opacity:.85}
  .alt-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
  @media(max-width:820px){.alt-grid{grid-template-columns:repeat(2,1fr)}}
  .alt-box{background:#0b1220;border:1px solid var(--border);border-radius:12px;padding:12px}
  .alt-title{font-size:13px;color:#cbd5e1;margin-bottom:6px}
  .alt-value{font-size:18px;font-weight:700}
  .badge-ok{color:#22c55e}
  .badge-ko{color:#ef4444}
  .badge-na{color:#94a3b8}
</style>
</head>
<body>
<div class="wrap">
  <!-- Section Altseason -->
  <div class="card" style="margin-bottom:12px">
    <h2>Indicateurs Altseason</h2>
    <div class="muted" style="margin-bottom:6px">Résumé rapide des 4 déclencheurs. Un vert = condition validée.</div>
    <div class="alt-grid">
      <div class="alt-box">
        <div class="alt-title">BTC Dominance (≤ ${btc_thr}%)</div>
        <div class="alt-value">${btc_dominance} <span class="${btc_ok_class}">${btc_ok_lbl}</span></div>
      </div>
      <div class="alt-box">
        <div class="alt-title">ETH/BTC (≥ ${eth_thr})</div>
        <div class="alt-value">${eth_btc} <span class="${eth_ok_class}">${eth_ok_lbl}</span></div>
      </div>
      <div class="alt-box">
        <div class="alt-title">TOTAL2 (≥ ${t2_thr} T$)</div>
        <div class="alt-value">${total2} <span class="${t2_ok_class}">${t2_ok_lbl}</span></div>
      </div>
      <div class="alt-box">
        <div class="alt-title">Altseason Index (≥ ${asi_thr})</div>
        <div class="alt-value">${asi} <span class="${asi_ok_class}">${asi_ok_lbl}</span></div>
      </div>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="pill ${alt_overall_class}">Greens: <strong>${greens}/4</strong></span>
      <span class="pill ${alt_overall_class}">ALTSEASON: <strong>${alt_on}</strong></span>
      <span class="pill">As of: ${asof}</span>
      <a class="pill" href="/altseason/check">Détails JSON</a>
    </div>
  </div>

  <!-- Filtres & Stats -->
  <div class="card">
    <h1>Trades — Dashboard</h1>
    <div class="muted">Filtrez par symbole / timeframe / date, puis validez.</div>
    <form method="get" action="/trades" class="filters">
      <input type="text" name="symbol" placeholder="symbol (ex: BTCUSDT.P)" value="${symbol}" />
      <input type="text" name="tf" placeholder="tf (ex: 15, 60, 1D)" value="${tf}" />
      <input type="date" name="start" value="${start}" />
      <input type="date" name="end" value="${end}" />
      <input type="number" min="1" max="10000" name="limit" value="${limit}" />
      <button type="submit">Appliquer</button>
      <a class="pill" href="/">← Home</a>
    </form>

    <div class="row" style="margin:6px 0 10px">
      <span class="pill">Total: <strong>${total_trades}</strong></span>
      <span class="pill">Winrate: <strong>${winrate_pct}%</strong></span>
      <span class="pill">Wins: <strong>${wins}</strong></span>
      <span class="pill">Losses: <strong>${losses}</strong></span>
      <span class="pill">TP1: <strong>${tp1_hits}</strong></span>
      <span class="pill">TP2: <strong>${tp2_hits}</strong></span>
      <span class="pill">TP3: <strong>${tp3_hits}</strong></span>
      <span class="pill">Avg. time: <strong>${avg_time_to_outcome_sec}s</strong></span>
    </div>

    <div class="table-wrap">
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

def _badge(ok: Optional[bool]) -> Tuple[str, str]:
    if ok is None: return ("badge-na", "N/A")
    return ("badge-ok","OK") if ok else ("badge-ko","KO")

# ---------- Route /trades ----------
@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    # Altseason snapshot/summary
    alt_sum = {}
    try:
        snap = _altseason_snapshot(force=False)
        alt_sum = _altseason_summary(snap)
    except Exception as e:
        log.warning("Altseason summary error: %s", e)
        alt_sum = {
            "asof":"—","greens":0,"ALTSEASON_ON":False,
            "btc_dominance":None,"eth_btc":None,"total2_usd":None,"altseason_index":None,
            "thresholds":{"btc_dominance_max":55.0,"eth_btc_min":0.045,"total2_min_trillions":1.78,"altseason_index_min":75}
        }

    # Filtres
    limit = max(1, min(int(limit or 100), 10000))
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)

    # Données trades
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit*10))

    # Rendu lignes avec surbrillance TP
    def outcome_rank(out: str) -> int:
        return {"TP1_HIT":1,"TP2_HIT":2,"TP3_HIT":3}.get(out or "", 0)
    rows_html = ""
    data = trades[-limit:] if limit else trades
    for tr in data:
        out = tr.get("outcome") or "NONE"
        rank = outcome_rank(out)
        badge = chip_class(out)
        label = outcome_label(out)

        def td_tp(val, hit_level):
            txt = escape_html(fmt_num(val)) if val is not None else "—"
            cls = "tp-hit" if rank >= hit_level else "tp-miss"
            return f"<td><span class='{cls}'>{txt}</span></td>"

        rows_html += (
            "<tr>"
            f"<td>{escape_html(str(tr.get('trade_id') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
            f"<td>{fmt_num(tr.get('entry'))}</td>"
            f"<td>{fmt_num(tr.get('sl'))}</td>"
            f"{td_tp(tr.get('tp1'), 1)}"
            f"{td_tp(tr.get('tp2'), 2)}"
            f"{td_tp(tr.get('tp3'), 3)}"
            f"<td>{escape_html(fmt_ts(tr.get('entry_time')))}</td>"
            f"<td><span class='{badge}'>{escape_html(label)}</span></td>"
            f"<td>{'' if tr.get('duration_sec') is None else str(tr.get('duration_sec'))}</td>"
            "</tr>"
        )

    # Valeurs Altseason formatées
    thr = alt_sum.get("thresholds", {})
    btc_ok = alt_sum.get("triggers",{}).get("btc_dominance_ok") if "triggers" in alt_sum else None
    eth_ok = alt_sum.get("triggers",{}).get("eth_btc_ok") if "triggers" in alt_sum else None
    t2_ok  = alt_sum.get("triggers",{}).get("total2_ok") if "triggers" in alt_sum else None
    asi_ok = alt_sum.get("triggers",{}).get("altseason_index_ok") if "triggers" in alt_sum else None
    btc_cls, btc_lbl = _badge(btc_ok)
    eth_cls, eth_lbl = _badge(eth_ok)
    t2_cls,  t2_lbl  = _badge(t2_ok)
    asi_cls, asi_lbl = _badge(asi_ok)

    alt_on = "ON" if alt_sum.get("ALTSEASON_ON") else "OFF"
    overall_cls = "ok" if alt_sum.get("ALTSEASON_ON") else "warn" if (alt_sum.get("greens",0)>=2) else "ko"

    def s(v):
        try:
            return str("" if v is None else v)
        except Exception:
            return ""

    html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
        # Filtres
        symbol=escape_html(symbol or ""), tf=escape_html(tf or ""),
        start=escape_html(start or ""), end=escape_html(end or ""),
        limit=str(limit),
        # Stats
        total_trades=s(summary.get("total_trades")), winrate_pct=s(summary.get("winrate_pct")),
        wins=s(summary.get("wins")), losses=s(summary.get("losses")),
        tp1_hits=s(summary.get("tp1_hits")), tp2_hits=s(summary.get("tp2_hits")), tp3_hits=s(summary.get("tp3_hits")),
        avg_time_to_outcome_sec=s(summary.get("avg_time_to_outcome_sec")),
        # Tableau
        rows_html=rows_html or '<tr><td colspan="12" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
        # Altseason
        btc_thr=s(thr.get("btc_dominance_max", 55.0)),
        eth_thr=s(thr.get("eth_btc_min", 0.045)),
        t2_thr=s(thr.get("total2_min_trillions", 1.78)),
        asi_thr=s(thr.get("altseason_index_min", 75)),
        btc_dominance=s(round(alt_sum.get("btc_dominance"),2) if alt_sum.get("btc_dominance") is not None else "—"),
        eth_btc=s(round(alt_sum.get("eth_btc"),4) if alt_sum.get("eth_btc") is not None else "—"),
        total2=s(round((alt_sum.get("total2_usd") or 0)/1e12,2) if alt_sum.get("total2_usd") else "—"),
        asi=s(int(alt_sum.get("altseason_index")) if alt_sum.get("altseason_index") is not None else "—"),
        btc_ok_class=btc_cls, btc_ok_lbl=btc_lbl,
        eth_ok_class=eth_cls, eth_ok_lbl=eth_lbl,
        t2_ok_class=t2_cls,  t2_ok_lbl=t2_lbl,
        asi_ok_class=asi_cls, asi_ok_lbl=asi_lbl,
        greens=s(alt_sum.get("greens",0)),
        alt_on=alt_on, alt_overall_class=overall_cls,
        asof=s(alt_sum.get("asof","—")),
    )
    return HTMLResponse(html)
# ============ main.py — BLOC 5/5 (Home, Health, Fins & Run) ============

# --- Garde-fou chemins/constantes partagées ---
# Plusieurs blocs utilisent DB_FILE : on l'aligne sur DB_PATH si absent.
if "DB_FILE" not in globals():
    DB_FILE = DB_PATH  # alias propre

# --- Page d'accueil minimaliste et utile ---
@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8">
    <title>AI Trader PRO — Home</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root{--bg:#0f172a;--card:#0b1220;--fg:#e5e7eb;--muted:#94a3b8;--border:#1f2937}
      html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font-family:system-ui,Segoe UI,Roboto}
      .wrap{max-width:860px;margin:48px auto;padding:0 12px}
      .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
      a{color:#93c5fd;text-decoration:none}
      .row{display:flex;flex-direction:column;gap:8px}
      .pill{display:inline-block;background:#111827;border:1px solid var(--border);border-radius:999px;padding:9px 14px}
      .muted{color:var(--muted)}
    </style></head><body>
    <div class="wrap">
      <div class="card">
        <h1>AI Trader PRO</h1>
        <p class="muted">Bienvenue. Accédez au tableau des trades et aux indicateurs Altseason.</p>
        <div class="row" style="margin-top:8px">
          <a class="pill" href="/trades">📊 Trades — Dashboard</a>
          <a class="pill" href="/altseason/check">🟢 Altseason — Check (JSON)</a>
          <a class="pill" href="/altseason/streaks">📈 Altseason — Streaks</a>
          <a class="pill" href="/altseason/daemon-status">⚙️ Altseason — Daemon Status</a>
          <a class="pill" href="/healthz">✅ Health</a>
        </div>
      </div>
    </div>
    </body></html>
    """)

# --- Endpoint de santé simple (utile pour Render/monitoring) ---
@app.get("/healthz")
def healthz():
    try:
        # Ping DB basique
        with sqlite3.connect(DB_FILE) as c:
            c.execute("SELECT 1")
        return {"ok": True, "db": "up", "app": "running"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# --- Fallback 404 plus propre (optionnel) ---
from fastapi.responses import PlainTextResponse
from starlette.requests import Request as _StarletteRequest
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: _StarletteRequest, exc: StarletteHTTPException):
    # Laisse passer les autres codes; pour 404, on affiche un petit “guide”.
    if exc.status_code == 404:
        return HTMLResponse("""
        <!doctype html><html><head><meta charset="utf-8"><title>404</title>
        <style>body{background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto}
        .card{max-width:720px;margin:40px auto;background:#0b1220;border:1px solid #1f2937;border-radius:12px;padding:18px}
        a{color:#93c5fd;text-decoration:none}.muted{color:#94a3b8}
        </style></head><body>
        <div class="card">
          <h2>404 — Not Found</h2>
          <p class="muted">Route introuvable. Liens utiles :</p>
          <p><a href="/">← Home</a> · <a href="/trades">/trades</a> · <a href="/altseason/check">/altseason/check</a></p>
        </div></body></html>
        """, status_code=404)
    # Autres erreurs HTTP par défaut
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

# --- Lancement local (dev) ---
if __name__ == "__main__":
    import uvicorn
    # PORT déjà lu plus haut; fallback à 8000
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", PORT or 8000)))
