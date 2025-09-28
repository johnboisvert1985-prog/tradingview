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
# ============ main.py — BLOC 2/5 (Telegram utils + LLM confiance robuste + helpers Vector) ============

# ---------- Telegram (anti-spam simple + bouton dashboard) ----------
def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (fallback simple)."""
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
    Gestion douce des 429 (cooldown + message 'rate-limited').
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

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except Exception as e:
            # Si 429, on renvoie une réussite molle pour éviter le spam d'erreurs.
            err = str(e)
            if "429" in err:
                result["ok"] = False
                result["error"] = "HTTP 429: Too Many Requests"
                log.warning("Telegram 429 throttled")
                return result
            raise

        p = _json.loads(raw) if raw else {}
        if not p.get("ok"):
            result["error"] = f"sendMessage failed: {str(raw)[:200]}"
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
                    pp = _json.loads(praw) if praw else {}
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


# ---------- LLM: score de confiance pour ENTRY (robuste JSON) ----------
def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Retourne (pourcentage_confiance, rationale) ou None si indisponible.
    - Tolère les réponses non-JSON / vides / en code-block.
    - Fallback heuristique si parsing impossible.
    """
    # Désactivé ?
    if not (LLM_ENABLED and _openai_client and OPENAI_API_KEY):
        return None

    def _heuristic_fallback(p: Dict[str, Any]) -> Tuple[float, str]:
        """Petit score local si l'API ne renvoie pas un JSON propre."""
        side = (p.get("side") or "").upper()
        entry = _to_float(p.get("entry"))
        sl    = _to_float(p.get("sl"))
        tp1   = _to_float(p.get("tp1"))
        tp2   = _to_float(p.get("tp2"))
        tp3   = _to_float(p.get("tp3"))
        score = 50.0
        if entry and sl:
            try:
                risk = abs((entry - sl) / entry) * 100
                if 0.2 <= risk <= 1.5: score += 10
                elif risk < 0.1 or risk > 3.0: score -= 8
            except Exception:
                pass
        for tp in (tp1, tp2, tp3):
            if tp: score += 3
        if side in ("LONG", "SHORT"):
            score += 4
        score = max(0.0, min(100.0, score))
        return score, "fallback heuristique (SL/TP & structure)"

    try:
        sym    = str(payload.get("symbol") or "?")
        tf_lbl = tf_label_of(payload)
        side   = str(payload.get("side") or "N/A")
        entry  = payload.get("entry")
        sl     = payload.get("sl")
        tp1    = payload.get("tp1")
        tp2    = payload.get("tp2")
        tp3    = payload.get("tp3")

        sys_prompt = (
            "Tu es un assistant de trading. "
            "Note la probabilité (0-100) que l'ENTRY soit un bon setup à court terme, "
            "en te basant uniquement sur les champs fournis. Réponds au format JSON strict:\n"
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

        raw = (resp.choices[0].message.content or "").strip()

        # 1) tentative JSON direct
        try:
            data = json.loads(raw)
        except Exception:
            # 2) extraction d'un bloc { ... } via regex (inclut les ```json ... ``` éventuels)
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
            else:
                data = None

        if not isinstance(data, dict):
            # 3) fallback heuristique propre si parsing impossible
            return _heuristic_fallback(payload)

        conf = data.get("confidence", 0)
        try:
            conf = float(conf)
        except Exception:
            conf = 0.0
        conf = max(0.0, min(100.0, conf))

        rationale = str(data.get("rationale", "")).strip() or "estimation"
        return conf, rationale

    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        # Fallback final
        try:
            return _heuristic_fallback(payload)
        except Exception:
            return None



# ---------- Helpers: direction Vector (UP/DOWN) ----------
def infer_vector_direction(payload: Dict[str, Any]) -> str:
    """
    Devine UP/DOWN pour VECTOR_CANDLE à partir de: side|direction|note|reason|message.
    Renvoie 'UP', 'DOWN' ou 'N/A'.
    """
    fields = [
        str(payload.get("side") or ""),
        str(payload.get("direction") or ""),
        str(payload.get("note") or ""),
        str(payload.get("reason") or ""),
        str(payload.get("message") or ""),
    ]
    txt = " ".join(fields).lower()

    # mots-clés courants
    if re.search(r"\b(long|buy|bull|haussier|up|breakup|break\s?up|bullish)\b", txt):
        return "UP"
    if re.search(r"\b(short|sell|bear|baissier|down|breakdown|break\s?down|bearish)\b", txt):
        return "DOWN"

    # côté explicite
    side = (payload.get("side") or "").upper()
    if side == "LONG":
        return "UP"
    if side == "SHORT":
        return "DOWN"

    # rien trouvé
    return "N/A"


# Flag env pour notifier (permet de couper les vector si besoin)
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1", "true", "True")
# ============ main.py — BLOC 3/5 (Telegram message builder + Webhook amélioré VECTOR) ============

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

    # CLOSE
    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    # VECTOR_CANDLE
    if t == "VECTOR_CANDLE":
        direction = infer_vector_direction(payload)
        lines = [f"🟪 Vector Candle — {sym} {tf_lbl}"]
        lines.append(f"Direction: {direction}")
        lvl = payload.get("level") or payload.get("price")
        if lvl:
            lines.append(f"Niveau repéré: {num(_to_float(lvl))}")
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"


# ---------- Webhook TradingView ----------
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1","true","True")

# --- SAFETY GUARD: ensure save_event exists before tv-webhook uses it ---
if 'save_event' not in globals():
    def save_event(payload: dict) -> None:
        """Insert a TradingView event into SQLite (guarded fallback)."""
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
        try:
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
        except Exception as e:
            log.exception("save_event fallback failed: %s", e)


@app.api_route("/tv-webhook", methods=["POST", "GET"])
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Accepte les payloads TradingView/Autres.
    """
    # Secret check
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
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST JSON to this endpoint"})

    # Normalisation
    payload = dict(body or {})
    payload["type"] = str(payload.get("type") or "EVENT").upper()
    if "tf" in payload and isinstance(payload["tf"], str) and payload["tf"].isdigit():
        payload["tf"] = payload["tf"]

    # Sauvegarde brute
    save_event(payload)

    # Message Telegram
    msg = telegram_rich_message(payload)
    sent = None
    if msg:
        pin = False
        if payload["type"] == "VECTOR_CANDLE" and not TELEGRAM_NOTIFY_VECTOR:
            sent = False
        else:
            sent = send_telegram_ex(msg, pin=pin).get("ok")

    return JSONResponse({"ok": True, "telegram_sent": bool(sent), "type": payload["type"]})
# ============ main.py — BLOC 4/5 (Templates HTML + Dashboard Trades/Altseason) ============

TRADES_PUBLIC_HTML_TPL = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Trades — Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0e1117; color: #f0f0f0; margin: 20px; }
        h1, h2 { color: #00c3ff; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
        th, td { padding: 8px 12px; border-bottom: 1px solid #333; text-align: center; }
        th { background: #1a1d23; }
        tr:hover { background: #1f2937; }
        .chip { padding: 3px 8px; border-radius: 10px; font-size: 0.85em; }
        .chip.win { background: #16a34a; color: #fff; }
        .chip.loss { background: #dc2626; color: #fff; }
        .chip.close { background: #6b7280; color: #fff; }
        .altseason-box { padding: 15px; margin-top: 20px; border-radius: 8px; background: #1a1d23; }
        .altseason-title { font-size: 1.2em; color: #facc15; margin-bottom: 10px; }
        .altseason-row { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #333; }
        .altseason-row:last-child { border-bottom: none; }
    </style>
</head>
<body>
    <h1>Trades — Dashboard</h1>
    <table>
        <thead>
            <tr>
                <th>Trade ID</th>
                <th>Symbole</th>
                <th>TF</th>
                <th>Side</th>
                <th>Entry</th>
                <th>SL</th>
                <th>TP1</th>
                <th>TP2</th>
                <th>TP3</th>
                <th>Outcome</th>
                <th>Heure Entrée</th>
            </tr>
        </thead>
        <tbody>
            $rows
        </tbody>
    </table>

    <div class="altseason-box">
        <div class="altseason-title">📊 Indicateurs Altseason</div>
        <div class="altseason-row"><span>BTC.D (%)</span><span>$btc_d / ≤ $btc_thr</span></div>
        <div class="altseason-row"><span>ETH/BTC</span><span>$eth_btc / ≥ $eth_thr</span></div>
        <div class="altseason-row"><span>TOTAL2 (USD Tn)</span><span>$total2 / ≥ $total2_thr</span></div>
        <div class="altseason-row"><span>Altseason Index</span><span>$asi / ≥ $asi_thr</span></div>
        <div class="altseason-row"><strong>Signal global</strong><strong>$signal</strong></div>
    </div>
</body>
</html>
"""

def chip_class(outcome: str) -> str:
    if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT"):
        return "chip win"
    if outcome == "SL_HIT":
        return "chip loss"
    if outcome == "CLOSE":
        return "chip close"
    return "chip"

def render_trades_page() -> str:
    """Construit le HTML pour /trades avec tableau et altseason"""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT 100")
        rows = []
        for ev in cur.fetchall():
            ev = dict(ev)
            outcome = ev.get("type") or "NONE"
            trade_id = ev.get("trade_id") or ""
            entry_time = datetime.fromtimestamp(ev["received_at"]).strftime("%Y-%m-%d %H:%M:%S")
            rows.append(f"""
                <tr>
                    <td>{trade_id}</td>
                    <td>{ev.get('symbol')}</td>
                    <td>{ev.get('tf')}</td>
                    <td>{ev.get('side') or ''}</td>
                    <td>{fmt_num(ev.get('entry'))}</td>
                    <td>{fmt_num(ev.get('sl'))}</td>
                    <td>{fmt_num(ev.get('tp1'))}</td>
                    <td>{fmt_num(ev.get('tp2'))}</td>
                    <td>{fmt_num(ev.get('tp3'))}</td>
                    <td><span class="{chip_class(outcome)}">{outcome}</span></td>
                    <td>{entry_time}</td>
                </tr>
            """)
        trade_html = "\n".join(rows)

    # Snapshot altseason
    snap = load_altseason_snapshot()
    return Template(TRADES_PUBLIC_HTML_TPL).safe_substitute(
        rows=trade_html or "<tr><td colspan='11'>Aucun trade</td></tr>",
        btc_d=fmt_num((snap or {}).get("btc_d")),
        eth_btc=fmt_num((snap or {}).get("eth_btc")),
        total2=fmt_num((snap or {}).get("total2")),
        asi=fmt_num((snap or {}).get("asi")),
        btc_thr=BTC_DOM_THR,
        eth_thr=ETH_BTC_THR,
        total2_thr=TOTAL2_THR,
        asi_thr=ASI_THR,
        signal=(snap or {}).get("signal") or "—"
    )

@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    try:
        return render_trades_page()
    except Exception as e:
        log.exception("Error rendering trades page")
        return HTMLResponse(f"<h1>Trades — Dashboard</h1><p>An error occurred while rendering the page.</p><pre>{e}</pre>", status_code=500)
# ============ main.py — BLOC 5/5 (Helpers Altseason pour /trades, Home, Run) ============

# Seuils réutilisés par le rendu /trades (alias lisibles)
BTC_DOM_THR = ALT_BTC_DOM_THR
ETH_BTC_THR = ALT_ETH_BTC_THR
ASI_THR     = ALT_ASI_THR
TOTAL2_THR  = ALT_TOTAL2_THR_T  # en trillions USD

def load_altseason_snapshot() -> Dict[str, Any]:
    """
    Construit un petit snapshot formaté pour l'encart Altseason du dashboard /trades.
    - btc_d: dominance BTC (%)
    - eth_btc: prix ETH en BTC
    - total2: capitalisation hors BTC (en trillions USD)
    - asi: Altseason Index (0..100)
    - signal: texte 'ON' / 'OFF' selon les seuils et la règle GREENS_REQUIRED
    """
    try:
        summary = _altseason_summary(_altseason_snapshot(force=False))
        btc_d   = summary.get("btc_dominance")
        ethb    = summary.get("eth_btc")
        total2  = summary.get("total2_usd")
        asi     = summary.get("altseason_index")
        greens  = int(summary.get("greens") or 0)
        on      = bool(summary.get("ALTSEASON_ON"))

        return {
            "btc_d": (None if btc_d is None else float(btc_d)),
            "eth_btc": (None if ethb is None else float(ethb)),
            "total2": (None if total2 is None else float(total2) / 1e12),  # trillions
            "asi": (None if asi is None else int(asi)),
            "signal": "ALTSEASON ON ✅" if on else f"En veille ({greens}/{ALT_GREENS_REQUIRED})",
        }
    except Exception as e:
        log.warning("load_altseason_snapshot failed: %s", e)
        return {"btc_d": None, "eth_btc": None, "total2": None, "asi": None, "signal": "—"}

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

# -------------------------
# Run local (for debug)
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
