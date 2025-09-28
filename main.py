# ============ main.py — BLOC 1/5 (Imports, Config, App, Helpers, DB boot) ============
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

# ALTSEASON thresholds
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))            # seconds
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1", "true", "True")
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
TELEGRAM_RATE_BACKOFF_SECONDS = float(os.getenv("TELEGRAM_RATE_BACKOFF_SECONDS", "8.0") or 8.0)
_last_tg = 0.0
_rate_backoff_until = 0.0  # si 429, on “mute” jusqu’à cette époque

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
        y, m, d = map(int, date_str.split("-"))
        return int(datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        y, m, d = map(int, date_str.split("-"))
        return int(datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    except Exception:
        return None

def detect_vector_direction(payload: Dict[str, Any]) -> str:
    """
    Détecte 'UP' ou 'DOWN' pour les alertes VECTOR_CANDLE à partir de plusieurs champs possibles.
    Champs supportés: 'side', 'direction', 'vector', 'vec', 'dir', 'trend', 'arrow'.
    Valeurs prises: up/down/long/short/bull/bear/↑/↓ etc.
    """
    keys = ("side", "direction", "vector", "vec", "dir", "trend", "arrow")
    val = ""
    for k in keys:
        v = str(payload.get(k) or "").strip().lower()
        if v:
            val = v
            break
    # normalisation
    if any(t in val for t in ("up", "long", "bull", "buy", "↑", "uptrend", "hauss")):
        return "UP"
    if any(t in val for t in ("down", "short", "bear", "sell", "↓", "downtrend", "baiss")):
        return "DOWN"
    return ""  # inconnu

# =========================
# SQLite — init robuste
# =========================
DB_PATH = DB_PATH  # déjà défini plus haut

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
# ============ main.py — BLOC 2/5 (Telegram utils, LLM, save_event) ============

# ---------- Telegram utils avec anti-429 ----------
def send_telegram_ex(text: str, pin: bool = False) -> Dict[str, Any]:
    """
    Envoi enrichi (inline bouton vers /trades) + gestion anti-429 (backoff).
    """
    global _last_tg, _rate_backoff_until
    now = time.time()
    result = {"ok": False, "message_id": None, "pinned": False, "error": None}

    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        result["error"] = "Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
        return result

    if now < _rate_backoff_until:
        result["error"] = f"backoff active until {int(_rate_backoff_until - now)}s"
        return result

    if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
        result["error"] = "rate-limited (cooldown)"
        return result

    try:
        import urllib.request, urllib.parse, json as _json

        api_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
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
                if "Too Many Requests" in raw or '"error_code":429' in raw:
                    _rate_backoff_until = now + TELEGRAM_RATE_BACKOFF_SECONDS
                    log.warning("Telegram 429 -> backoff %ss", TELEGRAM_RATE_BACKOFF_SECONDS)
                return result
            msg = p.get("result") or {}
            result["ok"] = True
            result["message_id"] = msg.get("message_id")

        # pin si demandé
        if pin and result["message_id"]:
            try:
                pin_url = f"{api_base}/pinChatMessage"
                pdata = urllib.parse.urlencode({
                    "chat_id": TELEGRAM_CHAT_ID,
                    "message_id": result["message_id"],
                }).encode()
                preq = urllib.request.Request(pin_url, data=pdata)
                with urllib.request.urlopen(preq, timeout=10) as presp:
                    praw = presp.read().decode("utf-8", "ignore")
                    pp = _json.loads(praw)
                    if pp.get("ok"):
                        result["pinned"] = True
            except Exception as e:
                result["error"] = f"pin exception: {e}"
        _last_tg = now
    except Exception as e:
        result["error"] = f"send_telegram_ex exception: {e}"
        log.warning("Telegram send_telegram_ex exception: %s", e)
    return result

# ---------- LLM ----------
def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    if not (LLM_ENABLED and _openai_client and OPENAI_API_KEY):
        return None
    try:
        sys_prompt = (
            "Tu es un assistant de trading. "
            "Note la probabilité (0-100) que l'ENTRY soit un bon setup à court terme. "
            "Réponds en JSON: {\"confidence\": <0..100>, \"rationale\": \"...\"}"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
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
        return (max(0.0, min(100.0, conf)), rationale)
    except Exception as e:
        log.warning("llm_confidence_for_entry error: %s", e)
        return None

# ---------- save_event ----------
def save_event(payload: dict) -> None:
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
        log.exception("save_event failed: %s", e)
# ============ main.py — BLOC 3/5 (Message builder + Webhook TV) ============

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
    tp = _to_float(payload.get("tp"))   # niveau exécuté pour TP/SL hits
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

        # Confiance LLM (si dispo)
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
        spot_pct = pct(tp, entry) if (side and tp is not None and entry is not None) else None
        lev_pct = (spot_pct * lev_x) if (spot_pct is not None and lev_x) else None
        lines = [f"✅ {label} — {sym} {tf_lbl}"]
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

    # VECTOR_CANDLE (avec direction explicite)
    if t in {"VECTOR_CANDLE", "VECTOR", "VEC_CANDLE"}:
        # direction par priorité: side, direction, dir
        dir_raw = (payload.get("side") or payload.get("direction") or payload.get("dir") or "").upper()
        if dir_raw in {"UP","LONG","BUY","BULL"}:
            dir_label = "⬆️ Vector UP"
        elif dir_raw in {"DOWN","SHORT","SELL","BEAR"}:
            dir_label = "⬇️ Vector DOWN"
        else:
            dir_label = "Vector"

        lines = [f"🟪 {dir_label} — {sym} {tf_lbl}"]
        lvl = payload.get("level") or payload.get("price")
        if lvl:
            lines.append(f"Niveau repéré: {num(_to_float(lvl))}")
        note = payload.get("note") or payload.get("reason")
        if note:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    # Fallback
    return f"[TV] {t} | {sym} | TF {tf_lbl}"


# ---------- Webhook TradingView ----------
TELEGRAM_NOTIFY_VECTOR = os.getenv("TELEGRAM_NOTIFY_VECTOR", "1") in ("1","true","True")

@app.api_route("/tv-webhook", methods=["POST", "GET"])
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Accepte JSON ou form-encoded.
    Champs reconnus:
      type: ENTRY|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CLOSE|VECTOR_CANDLE|AOE_*
      symbol, tf/tf_label, side/direction, entry, sl, tp1..tp3, trade_id, leverage,
      (optionnel VECTOR) level/price, note/reason, dir.
    """
    # GET = ping simple
    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST JSON or form to this endpoint"})

    # Récup body (JSON puis form en fallback)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        try:
            body = dict(await request.form())
        except Exception:
            body = {}

    # Secret
    body_secret = (body or {}).get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    # Normalisation de base
    payload = dict(body or {})
    t = str(payload.get("type") or "EVENT").upper()
    if t in {"VECTOR", "VEC_CANDLE"}:
        t = "VECTOR_CANDLE"
    payload["type"] = t

    # side/direction harmonisée pour vector
    if t == "VECTOR_CANDLE":
        d = (payload.get("side") or payload.get("direction") or payload.get("dir") or "").upper()
        if d in {"UP","LONG","BUY","BULL"}:
            payload["side"] = "UP"
        elif d in {"DOWN","SHORT","SELL","BEAR"}:
            payload["side"] = "DOWN"

    # tf
    if payload.get("tf") is None and payload.get("tf_label") is not None:
        payload["tf"] = str(payload.get("tf_label"))

    # trade_id auto si manquant
    if not payload.get("trade_id"):
        sym = str(payload.get("symbol") or "UNK")
        tfv = str(payload.get("tf") or "?")
        ts = int(time.time())
        payload["trade_id"] = f"{sym}_{tfv}_{ts}"

    # Sauvegarde
    save_event(payload)

    # Compose message & décide notification
    msg = telegram_rich_message(payload)
    sent = False
    if msg:
        if t == "VECTOR_CANDLE" and not TELEGRAM_NOTIFY_VECTOR:
            sent = False
        else:
            # aucun pin auto pour éviter d’épingler chaque alerte
            sent = bool(send_telegram_ex(msg, pin=False).get("ok"))

    return JSONResponse({"ok": True, "saved": True, "telegram_sent": sent, "type": t, "trade_id": payload.get("trade_id")})
# ============ main.py — BLOC 4/5 (Altseason + Template HTML dashboard) ============

# ---------- Template HTML public pour /trades ----------
TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Trades — Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root{
    --bg:#0f172a; --card:#0b1220; --muted:#94a3b8; --fg:#e5e7eb;
    --border:#1f2937; --win:#16a34a; --loss:#ef4444; --close:#eab308; --open:#38bdf8;
  }
  html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font-family:system-ui,Segoe UI,Roboto,Inter,Arial}
  a{color:#93c5fd;text-decoration:none}
  .wrap{max-width:1200px;margin:24px auto;padding:0 12px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
  h1{margin:0 0 8px 0;font-size:22px}
  .muted{color:var(--muted)}
  .grid{display:grid;grid-template-columns:1fr;gap:12px}
  @media(min-width:900px){.grid{grid-template-columns:2fr 1fr}}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .pill{display:inline-block;background:#111827;border:1px solid var(--border);border-radius:999px;padding:6px 10px;margin:4px 6px 0 0;font-size:13px}
  .table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px;margin-top:12px}
  table{width:100%;border-collapse:collapse;min-width:980px}
  th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;font-size:14px;white-space:nowrap}
  th{position:sticky;top:0;background:#0f172a;font-weight:600}
  .chip{display:inline-block;padding:3px 8px;border-radius:10px;border:1px solid var(--border);font-size:12px}
  .chip.win{background:rgba(22,163,74,.12);border-color:rgba(22,163,74,.4);color:#86efac}
  .chip.loss{background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.4);color:#fca5a5}
  .chip.close{background:rgba(234,179,8,.12);border-color:rgba(234,179,8,.4);color:#fde68a}
  .chip.open{background:rgba(56,189,248,.12);border-color:rgba(56,189,248,.4);color:#bae6fd}
  .chip.muted{background:#0b1220;color:var(--muted)}
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px}
  .filters input{background:#0b1220;border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--fg)}
  .filters button{background:#1d4ed8;border:0;border-radius:8px;padding:8px 12px;color:white;cursor:pointer}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .hint{font-size:13px}
  /* Altseason block */
  .alt-card{margin-top:18px;padding:14px;border-radius:14px;border:1px solid var(--border);background:#111827}
  .alt-card h2{margin:0 0 10px;font-size:20px}
  .alt-table{width:100%;border-collapse:collapse;margin-top:8px}
  .alt-table th,.alt-table td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;font-size:14px}
  .ok{color:#16a34a;font-weight:600}
  .nok{color:#ef4444;font-weight:600}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Trades — Dashboard</h1>
    <div class="hint muted">Filtrez par symbole / timeframe / date, puis validez.</div>
    <form method="get" action="/trades" class="filters">
      <input type="text" name="symbol" placeholder="symbol (ex: BTCUSDT.P)" value="${symbol}" />
      <input type="text" name="tf" placeholder="tf (ex: 15, 60, 1D)" value="${tf}" />
      <input type="date" name="start" value="${start}" />
      <input type="date" name="end" value="${end}" />
      <input type="number" min="1" max="10000" name="limit" value="${limit}" />
      <button type="submit">Appliquer</button>
      <a href="/" class="pill">← Home</a>
    </form>

    <!-- Résumé trades -->
    <div class="grid">
      <div class="card" style="padding:12px">
        <div class="row">
          <span class="pill">Total: <strong>${total_trades}</strong></span>
          <span class="pill">Winrate: <strong>${winrate_pct}%</strong></span>
          <span class="pill">Wins: <strong>${wins}</strong></span>
          <span class="pill">Losses: <strong>${losses}</strong></span>
          <span class="pill">TP1: <strong>${tp1_hits}</strong></span>
          <span class="pill">TP2: <strong>${tp2_hits}</strong></span>
          <span class="pill">TP3: <strong>${tp3_hits}</strong></span>
          <span class="pill">Avg. time: <strong>${avg_time_to_outcome_sec}s</strong></span>
          <span class="pill">Best streak: <strong>${best_win_streak}</strong></span>
          <span class="pill">Worst loss streak: <strong>${worst_loss_streak}</strong></span>
        </div>
      </div>
    </div>

    <!-- Tableau trades -->
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

    <!-- Section Altseason -->
    <div class="alt-card">
      <h2>📊 Altseason Monitor</h2>
      <table class="alt-table">
        <tr><th>BTC Dominance</th><td>${btc_dom}</td><td>${btc_dom_ok}</td></tr>
        <tr><th>ETH/BTC</th><td>${eth_btc}</td><td>${eth_btc_ok}</td></tr>
        <tr><th>Total2 (Trillions)</th><td>${total2}</td><td>${total2_ok}</td></tr>
        <tr><th>Altseason Index</th><td>${alt_idx}</td><td>${alt_idx_ok}</td></tr>
        <tr><th>Greens</th><td colspan="2">${greens}/4 (need ${greens_required})</td></tr>
        <tr><th>Status</th><td colspan="2"><b>${alt_on}</b></td></tr>
      </table>
    </div>
  </div>
</div>
</body>
</html>
""")
# ============ main.py — BLOC 5/5 (Home, /trades renderer, run) ============

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
          <a class="pill" href="/altseason/check">🟢 Altseason — Check (JSON)</a>
          <a class="pill" href="/altseason/streaks">📈 Altseason — Streaks (JSON)</a>
          <a class="pill" href="/altseason/daemon-status">⚙️ Altseason — Daemon status (JSON)</a>
        </div>
      </div>
    </div>
    </body></html>
    """)

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

        # Build data (trades + summary)
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

        # ---------- Altseason snapshot for the template ----------
        snap = _altseason_snapshot(force=False)
        s = _altseason_summary(snap)

        def yn(ok: bool) -> str:
            return f"<span class='ok'>OK</span>" if ok else f"<span class='nok'>NO</span>"

        btc_dom_val = s.get("btc_dominance")
        eth_btc_val = s.get("eth_btc")
        total2_val  = s.get("total2_usd")
        asi_val     = s.get("altseason_index")

        btc_dom_txt = "—" if btc_dom_val is None else f"{btc_dom_val:.2f}% (≤ {ALT_BTC_DOM_THR:.2f}%)"
        eth_btc_txt = "—" if eth_btc_val is None else f"{eth_btc_val:.5f} (≥ {ALT_ETH_BTC_THR:.5f})"
        total2_txt  = "—" if total2_val is None else f"{total2_val/1e12:.2f}T (≥ {ALT_TOTAL2_THR_T:.2f}T)"
        asi_txt     = "—" if asi_val is None else f"{asi_val} (≥ {ALT_ASI_THR:.0f})"

        btc_ok = bool(s.get("triggers", {}).get("btc_dominance_ok"))
        eth_ok = bool(s.get("triggers", {}).get("eth_btc_ok"))
        t2_ok  = bool(s.get("triggers", {}).get("total2_ok"))
        asi_ok = bool(s.get("triggers", {}).get("altseason_index_ok"))
        greens = int(s.get("greens") or 0)
        alt_on = "✅ ALTSEASON ON" if s.get("ALTSEASON_ON") else "⏳ Conditions insuffisantes"

        # Safe string helper
        def sstr(v):
            try:
                return str(v if v is not None else "")
            except Exception:
                return ""

        # Inject into template
        html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
            symbol=escape_html(symbol or ""),
            tf=escape_html(tf or ""),
            start=escape_html(start or ""),
            end=escape_html(end or ""),
            limit=str(limit),
            total_trades=sstr(summary.get("total_trades")),
            winrate_pct=sstr(summary.get("winrate_pct")),
            wins=sstr(summary.get("wins")),
            losses=sstr(summary.get("losses")),
            tp1_hits=sstr(summary.get("tp1_hits")),
            tp2_hits=sstr(summary.get("tp2_hits")),
            tp3_hits=sstr(summary.get("tp3_hits")),
            avg_time_to_outcome_sec=sstr(summary.get("avg_time_to_outcome_sec")),
            best_win_streak=sstr(summary.get("best_win_streak")),
            worst_loss_streak=sstr(summary.get("worst_loss_streak")),
            rows_html=rows_html or '<tr><td colspan="12" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
            # Altseason placeholders
            btc_dom=btc_dom_txt,
            eth_btc=eth_btc_txt,
            total2=total2_txt,
            alt_idx=asi_txt,
            btc_dom_ok=yn(btc_ok),
            eth_btc_ok=yn(eth_ok),
            total2_ok=yn(t2_ok),
            alt_idx_ok=yn(asi_ok),
            greens=str(greens),
            greens_required=str(ALT_GREENS_REQUIRED),
            alt_on=alt_on,
        )
        return HTMLResponse(html)

    except Exception as e:
        log.exception("Error in /trades: %s", e)
        # graceful fallback (no 500)
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
