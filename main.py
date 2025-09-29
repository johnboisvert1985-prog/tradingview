# ============ main.py — BLOC 1/5 (Imports, Config, App, Helpers, DB boot) ============

import os
import re
import json
import time
import math
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0") or 0)

PORT = int(os.getenv("PORT", "8000"))

# DB path default = data/data.db; fallback auto vers /tmp si read-only
DB_PATH = os.getenv("DB_PATH", "data/data.db")

# Altseason thresholds
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg_ts = 0.0

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
        return float(v) if v is not None and str(v) != "" else None
    except Exception:
        return None

def escape_html(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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

def fmt_ts(epoch_ms: Optional[int]) -> str:
    try:
        if not epoch_ms:
            return ""
        dt = datetime.fromtimestamp(int(epoch_ms)/1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

def parse_date_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch_ms début de journée UTC."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def parse_date_end_to_epoch(s: Optional[str]) -> Optional[int]:
    """YYYY-MM-DD -> epoch_ms fin de journée UTC (exclu)."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def chip_class(outcome: str) -> str:
    m = (outcome or "").upper()
    if m in ("TP1","TP2","TP3"): return "chip win"
    if m in ("SL","STOP","SL_HIT"): return "chip loss"
    if m in ("CLOSE","CLOSED"): return "chip close"
    if m in ("OPEN","ENTRY"): return "chip open"
    return "chip muted"

def outcome_label(outcome: str) -> str:
    m = (outcome or "").upper()
    if m in ("TP1","TP2","TP3","SL","CLOSE","OPEN"): return m
    if m == "SL_HIT": return "SL"
    return m or "—"

# -------------------------
# SQLite — init robuste (schéma complet)
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
    """Crée/upgrade la table events (inclut flags TP hit, outcome, entry_time...)."""
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
                tp REAL,                -- niveau exécuté pour TP*_HIT / SL_HIT
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp3_hit INTEGER DEFAULT 0,
                outcome TEXT,           -- OPEN / TP1 / TP2 / TP3 / SL / CLOSE
                entry_time INTEGER,     -- epoch ms du signal ENTRY
                exit_time INTEGER,      -- epoch ms de la sortie (tp/sl/close)
                duration_sec INTEGER,
                trade_id TEXT,
                reason TEXT,
                r1 REAL,
                s1 REAL,
                leverage TEXT,
                raw_json TEXT
            )
            """
        )
        # Index utiles
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf ON events(tf)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

# Boot DB
resolve_db_path()
db_init()
# ============ main.py — BLOC 2/5 (Telegram utils, LLM, Persist/Update, Webhook core) ============

# ---------- Telegram (anti-spam simple + bouton Trades) ----------
def _tg_rate_limited() -> bool:
    global _last_tg_ts
    now = time.time()
    if now - _last_tg_ts < TELEGRAM_COOLDOWN_SECONDS:
        log.warning("Telegram send skipped due to cooldown")
        return True
    _last_tg_ts = now
    return False

def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (fallback, sans bouton)."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        if _tg_rate_limited():
            return False
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
    """
    result = {"ok": False, "message_id": None, "pinned": False, "error": None}
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        result["error"] = "Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
        return result
    if _tg_rate_limited():
        result["ok"] = False
        result["error"] = "rate-limited (cooldown)"
        return result

    try:
        import urllib.request, urllib.parse, json as _json
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
    Ultra-robuste: ignore toute erreur réseau/JSON.
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
        # Parfois l'API retourne du texte non-JSON -> try/except
        try:
            data = json.loads(content)
        except Exception:
            # extraction simple "confidence: 68" etc.
            m = re.search(r"(\d{1,3})", content)
            conf = float(m.group(1)) if m else 50.0
            return (max(0.0, min(100.0, conf)), "estimation heuristique")

        conf = float(data.get("confidence", 0))
        rationale = str(data.get("rationale", "")).strip()
        conf = max(0.0, min(100.0, conf))
        return (conf, rationale or "estimation heuristique")
    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        return None


# ---------- Persistence & agrégation Trade ----------
def save_event(payload: dict) -> None:
    """
    Insert brut + mise à jour cohérente des flags/outcome/durations selon type.
    """
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
        "tp": _to_float(payload.get("tp")),
        "trade_id": payload.get("trade_id"),
        "reason": payload.get("reason"),
        "r1": _to_float(payload.get("r1")),
        "s1": _to_float(payload.get("s1")),
        "leverage": str(payload.get("leverage") or payload.get("lev") or payload.get("lev_reco") or ""),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Insert brut
            cur.execute(
                """
                INSERT INTO events (
                    received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, tp,
                    trade_id, reason, r1, s1, leverage, raw_json
                ) VALUES (
                    :received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :tp,
                    :trade_id, :reason, :r1, :s1, :leverage, :raw_json
                )
                """, row
            )

            # Mises à jour logiques par type
            t = (row["type"] or "").upper()
            tid = row["trade_id"]

            # ENTRY -> initialise état "OPEN"
            if t == "ENTRY" and tid:
                cur.execute(
                    """
                    UPDATE events SET outcome='OPEN', entry_time=COALESCE(entry_time, :ts)
                    WHERE trade_id=:tid AND (outcome IS NULL OR outcome='')
                    """,
                    {"tid": tid, "ts": int(time.time()*1000)}
                )

            # TP hits
            if t in ("TP1_HIT","TP2_HIT","TP3_HIT") and tid:
                col = {"TP1_HIT":"tp1_hit","TP2_HIT":"tp2_hit","TP3_HIT":"tp3_hit"}[t]
                cur.execute(
                    f"""
                    UPDATE events SET {col}=1, tp=:tp, outcome=:out, exit_time=COALESCE(exit_time, :now)
                    WHERE trade_id=:tid
                    """,
                    {
                        "tp": row["tp"], "out": t.replace("_HIT",""), "now": int(time.time()*1000), "tid": tid
                    }
                )
                # calculer duration si on a entry_time
                cur.execute("SELECT entry_time, exit_time FROM events WHERE trade_id=? ORDER BY id DESC LIMIT 1", (tid,))
                r = cur.fetchone()
                if r and r["entry_time"] and r["exit_time"]:
                    dur = max(0, int((int(r["exit_time"]) - int(r["entry_time"])) / 1000))
                    cur.execute("UPDATE events SET duration_sec=? WHERE trade_id=?", (dur, tid))

            # SL
            if t in ("SL_HIT", "STOP") and tid:
                cur.execute(
                    """
                    UPDATE events SET outcome='SL', exit_time=COALESCE(exit_time, :now), tp=:tp
                    WHERE trade_id=:tid
                    """,
                    {"now": int(time.time()*1000), "tid": tid, "tp": row["tp"]}
                )
                cur.execute("SELECT entry_time, exit_time FROM events WHERE trade_id=? ORDER BY id DESC LIMIT 1", (tid,))
                r = cur.fetchone()
                if r and r["entry_time"] and r["exit_time"]:
                    dur = max(0, int((int(r["exit_time"]) - int(r["entry_time"])) / 1000))
                    cur.execute("UPDATE events SET duration_sec=? WHERE trade_id=?", (dur, tid))

            # CLOSE (raison facultative)
            if t == "CLOSE" and tid:
                cur.execute(
                    """
                    UPDATE events SET outcome='CLOSE', exit_time=COALESCE(exit_time, :now), reason=COALESCE(:reason, reason)
                    WHERE trade_id=:tid
                    """,
                    {"now": int(time.time()*1000), "tid": tid, "reason": row["reason"]}
                )
                cur.execute("SELECT entry_time, exit_time FROM events WHERE trade_id=? ORDER BY id DESC LIMIT 1", (tid,))
                r = cur.fetchone()
                if r and r["entry_time"] and r["exit_time"]:
                    dur = max(0, int((int(r["exit_time"]) - int(r["entry_time"])) / 1000))
                    cur.execute("UPDATE events SET duration_sec=? WHERE trade_id=?", (dur, tid))

            conn.commit()
        log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
                 row["type"], row["symbol"], row["tf"], row["trade_id"])
    except Exception as e:
        log.exception("save_event failed: %s", e)

# ---------- Message Telegram (inclut Vector UP/DOWN vert/rouge) ----------
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
            if LLM_ENABLED and _openai_client:
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
            lines.append(f"Prix exec.: {num(tp)}")
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

    # CLOSE
    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    # VECTOR_CANDLE (carré VERT si UP, ROUGE si DOWN)
    if t == "VECTOR_CANDLE":
        direction = str(payload.get("direction") or "").upper()
        mark = "🟩" if direction == "UP" else ("🟥" if direction == "DOWN" else "🟪")
        price = payload.get("price") or payload.get("level")
        lines = [f"{mark} Vector Candle — {sym} {tf_lbl}"]
        if direction:
            lines.append(f"Direction: {direction}")
        if price is not None:
            lines.append(f"Niveau repéré: {num(_to_float(price))}")
        note = payload.get("note")
        if note:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"


# ---------- Webhook TradingView ----------
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1","true","True")

@app.api_route("/tv-webhook", methods=["POST", "GET"])
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Accepte payloads:
    {
      "type": "ENTRY|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CLOSE|VECTOR_CANDLE|AOE_*",
      "symbol": "...", "tf": "15", "side": "LONG|SHORT",
      "entry": 1.234, "sl": 1.111, "tp1": ..., "tp2": ..., "tp3": ...,
      "trade_id": "...", "leverage": "10x",
      "direction": "UP|DOWN", "price": 1.23, "note": "..."
    }
    """
    # GET -> hint
    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST JSON to this endpoint"})

    # Body JSON/form
    try:
        body = await request.json()
    except Exception:
        try:
            body = dict(await request.form())
        except Exception:
            body = {}

    # Secret check
    body_secret = (body or {}).get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    # Normalisation
    payload = dict(body or {})
    payload["type"] = str(payload.get("type") or "EVENT").upper()
    if "tf" in payload and isinstance(payload["tf"], str) and payload["tf"].isdigit():
        payload["tf"] = payload["tf"]

    log.info("Webhook payload: %s", payload)

    # Sauvegarde brute + cohérences
    save_event(payload)

    # Message Telegram
    msg = telegram_rich_message(payload)
    sent = None
    if msg:
        pin = (payload["type"] in {"TP3_HIT"} )  # ex: on épingle TP3
        if payload["type"] == "VECTOR_CANDLE" and not TELEGRAM_NOTIFY_VECTOR:
            sent = False
        else:
            sent = send_telegram_ex(msg, pin=pin).get("ok")

    return JSONResponse({"ok": True, "telegram_sent": bool(sent), "type": payload["type"]})
# ============ main.py — BLOC 3/5 (Altseason, Dashboards, HTML templates) ============

# ---------- Altseason Dashboard ----------
_altseason_snapshot: Dict[str, Any] = {}

def altseason_summary() -> str:
    """
    Retourne résumé altseason (>=3/4 seuils verts).
    """
    global _altseason_snapshot
    try:
        snap = _altseason_snapshot or {}
        if not snap:
            return "Pas de snapshot altseason disponible."

        greens = snap.get("greens", 0)
        total = snap.get("total", 4)
        lines = []
        lines.append("📊 **Indicateurs Altseason**")
        lines.append(f"✅ {greens}/{total} conditions franchies.")
        for k, v in snap.items():
            if k not in ("greens","total"):
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        log.warning("Altseason summary error: %s", e)
        return "Erreur altseason."


# ---------- Dashboard trades HTML ----------
TRADES_PUBLIC_HTML_TPL = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Trades — Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; background:#111; color:#eee; padding:20px; }
    h1 { color:#0f0; }
    table { border-collapse: collapse; width: 100%; margin-top:20px; }
    th, td { border:1px solid #444; padding:6px 8px; text-align:center; }
    th { background:#222; }
    .chip { display:inline-block; padding:2px 6px; border-radius:4px; font-size:0.85em; }
    .win { background:#060; color:#fff; }
    .loss { background:#600; color:#fff; }
    .close { background:#555; color:#fff; }
    .tp-hit { background:#0a0; color:#fff; }
    .sl-hit { background:#a00; color:#fff; }
    .vector { background:#550a88; color:#fff; }
    .vector-up { background:#080; color:#fff; }
    .vector-down { background:#a00; color:#fff; }
  </style>
</head>
<body>
  <h1>Trades — Dashboard</h1>
  <div id="altseason">
    <pre>{{altseason}}</pre>
  </div>
  <table>
    <thead>
      <tr>
        <th>Heure entrée</th>
        <th>Symbol</th>
        <th>TF</th>
        <th>Side</th>
        <th>Entry</th>
        <th>SL</th>
        <th>TP1</th>
        <th>TP2</th>
        <th>TP3</th>
        <th>Outcome</th>
        <th>Durée (s)</th>
        <th>Raison</th>
      </tr>
    </thead>
    <tbody>
      {{rows}}
    </tbody>
  </table>
</body>
</html>
"""

def chip_class(outcome: str) -> str:
    if outcome in ("TP1","TP2","TP3"):
        return "chip tp-hit"
    if outcome == "SL":
        return "chip sl-hit"
    if outcome == "CLOSE":
        return "chip close"
    if outcome == "VECTOR_CANDLE":
        return "chip vector"
    return "chip"

def render_trades_html(rows: List[Dict[str, Any]]) -> str:
    """
    Rend le tableau trades en HTML.
    Colore TP1/TP2/TP3 en vert si hit, etc.
    """
    def row_html(r: Dict[str, Any]) -> str:
        out = r.get("outcome") or ""
        chip = chip_class(out)
        tp1c = f"<td class='tp-hit'>{fmt_num(r['tp1'])}</td>" if r.get("tp1_hit") else f"<td>{fmt_num(r.get('tp1'))}</td>"
        tp2c = f"<td class='tp-hit'>{fmt_num(r['tp2'])}</td>" if r.get("tp2_hit") else f"<td>{fmt_num(r.get('tp2'))}</td>"
        tp3c = f"<td class='tp-hit'>{fmt_num(r['tp3'])}</td>" if r.get("tp3_hit") else f"<td>{fmt_num(r.get('tp3'))}</td>"
        return f"""
        <tr>
          <td>{r.get('entry_time')}</td>
          <td>{r.get('symbol')}</td>
          <td>{r.get('tf')}</td>
          <td>{r.get('side')}</td>
          <td>{fmt_num(r.get('entry'))}</td>
          <td>{fmt_num(r.get('sl'))}</td>
          {tp1c}
          {tp2c}
          {tp3c}
          <td><span class='{chip}'>{out}</span></td>
          <td>{r.get('duration_sec') or ''}</td>
          <td>{r.get('reason') or ''}</td>
        </tr>
        """

    body = "".join([row_html(r) for r in rows])
    html = TRADES_PUBLIC_HTML_TPL.replace("{{rows}}", body)
    html = html.replace("{{altseason}}", altseason_summary())
    return html
# ============ main.py — BLOC 4/5 (Endpoints trades + webhook) ============

@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/trades")


@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT 200")
        rows_db = c.fetchall()
        conn.close()
        cols = [d[0] for d in c.description] if c.description else []
        rows = []
        for r in rows_db:
            row = dict(zip(cols, r))
            row["entry_time"] = format_dt(row.get("received_at"))
            rows.append(row)
        html = render_trades_html(rows)
        return HTMLResponse(content=html)
    except Exception as e:
        log.error("Trades page error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
        log.info("Webhook payload: %s", payload)

        if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

        ev_type = payload.get("type")
        save_event(payload)

        # --- VECTOR_CANDLE special handling ---
        if ev_type == "VECTOR_CANDLE":
            symbol = payload.get("symbol")
            tf = payload.get("tf")
            direction = payload.get("direction", "").upper()
            price = payload.get("price")
            note = payload.get("note", "")

            color = "🟩" if direction == "UP" else "🟥"
            msg = f"{color} Vector Candle {direction} — {symbol} {tf}m\nNiveau repéré: {price}"
            send_telegram(msg)
            return {"status": "ok"}

        # --- Normal ENTRY/EXIT/TP/SL handling ---
        if ev_type == "ENTRY":
            msg = f"🔔 ENTRY — {payload.get('symbol')} {payload.get('tf_label')}\n" \
                  f"{json.dumps(payload, indent=2)}"
            send_telegram(msg)

        elif ev_type in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
            msg = f"🎯 {ev_type} — {payload.get('symbol')} {payload.get('tf_label')}"
            send_telegram(msg)

        elif ev_type == "SL_HIT":
            msg = f"❌ SL HIT — {payload.get('symbol')} {payload.get('tf_label')}"
            send_telegram(msg)

        elif ev_type == "CLOSE":
            msg = f"🔒 CLOSE — {payload.get('symbol')} {payload.get('tf_label')}"
            send_telegram(msg)

        elif ev_type.startswith("AOE_"):
            msg = f"✨ {ev_type} — {payload.get('symbol')} {payload.get('tf_label')}"
            send_telegram(msg)

        return {"status": "ok"}
    except Exception as e:
        log.error("tv_webhook error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
# ============ main.py — BLOC 5/5 (Altseason + run app) ============

@app.get("/altseason", response_class=JSONResponse)
def altseason_status():
    """Retourne un snapshot JSON de l'altseason actuel"""
    try:
        snap = _altseason_snapshot if "_altseason_snapshot" in globals() else {}
        return JSONResponse(content=snap)
    except Exception as e:
        log.warning("Altseason summary error: %s", e, exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)


def run_altseason_daemon(interval: int = ALT_INTERVAL):
    """Boucle background qui refresh le snapshot altseason"""
    import threading
    import time
    import requests

    def loop():
        global _altseason_snapshot
        while True:
            try:
                # Simuler un appel externe aux marchés
                _altseason_snapshot = {
                    "BTC.d": 48.2,
                    "ETH/BTC": 0.055,
                    "TOTAL2": 890_000_000_000,
                    "ASI": 11,
                    "greens": 3,
                    "thresholds": {
                        "btc_dom_thr": BTC_DOM_THR,
                        "eth_thr": ETH_THR,
                        "total2_thr": TOTAL2_THR,
                        "asi_thr": ASI_THR,
                    },
                    "timestamp": int(time.time()),
                }
                log.info("Altseason snapshot refreshed: %s", _altseason_snapshot)
            except Exception as e:
                log.warning("Altseason daemon error: %s", e, exc_info=True)
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn

    # Démarrer le daemon altseason
    run_altseason_daemon()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
