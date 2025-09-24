# main.py
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

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

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

# DB path default = data/data.db; fallback auto to /tmp si read-only
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

# Telegram rate limit helper
TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0

# -------------------------
# OpenAI client (optional)
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
# SQLite (persistent)
# -------------------------
def resolve_db_path() -> None:
    """Try to create directory for DB_PATH; if permission denied, fallback to /tmp/ai_trader/data.db."""
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
        resolve_db_path()

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

resolve_db_path()
db_init()

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def save_event(payload: Dict[str, Any]) -> None:
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
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s", row["type"], row["symbol"], row["tf"], row["trade_id"])

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )

def fmt_num(v) -> str:
    try:
        if v is None:
            return ""
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "")

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
# -------------------------
# Build trades & stats
# -------------------------
class TradeOutcome:
    NONE = "NONE"
    TP1 = "TP1_HIT"
    TP2 = "TP2_HIT"
    TP3 = "TP3_HIT"
    SL = "SL_HIT"
    CLOSE = "CLOSE"

def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        dtobj = dt.datetime(y, m, d, 0, 0, 0)
        return int(dtobj.timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        dtobj = dt.datetime(y, m, d, 23, 59, 59)
        return int(dtobj.timestamp())
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
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)

    by_tid: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        tid = r["trade_id"] or f"noid:{r['symbol']}:{r['received_at']}"
        by_tid[tid].append(r)

    trades: List[Dict[str, Any]] = []
    total = wins = losses = 0
    hit_tp1 = hit_tp2 = hit_tp3 = 0
    times_to_outcome: List[int] = []
    win_streak = loss_streak = 0
    best_win_streak = 0
    worst_loss_streak = 0

    for tid, items in by_tid.items():
        entry = None
        outcome_type = TradeOutcome.NONE
        outcome_time = None
        side = None
        vsymbol = None
        vtf = None
        e_entry = e_sl = e_tp1 = e_tp2 = e_tp3 = None
        entry_time = None

        for ev in items:
            etype = ev["type"]
            if etype == "ENTRY" and entry is None:
                entry = ev
                vsymbol = ev["symbol"]; vtf = ev["tf"]; side = ev["side"]
                e_entry = ev["entry"]; e_sl = ev["sl"]; e_tp1 = ev["tp1"]; e_tp2 = ev["tp2"]; e_tp3 = ev["tp3"]
                entry_time = ev["received_at"]
            elif entry is not None:
                if etype in ("TP3_HIT","TP2_HIT","TP1_HIT","SL_HIT","CLOSE") and outcome_type == TradeOutcome.NONE:
                    outcome_type = etype; outcome_time = ev["received_at"]

        if entry is not None:
            total += 1
            if outcome_time and entry_time:
                times_to_outcome.append(int(outcome_time - entry_time))
            is_win = outcome_type in (TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3)
            if is_win:
                wins += 1; win_streak += 1; best_win_streak = max(best_win_streak, win_streak); loss_streak = 0
                if outcome_type == TradeOutcome.TP1: hit_tp1 += 1
                elif outcome_type == TradeOutcome.TP2: hit_tp2 += 1
                elif outcome_type == TradeOutcome.TP3: hit_tp3 += 1
            elif outcome_type == TradeOutcome.SL:
                losses += 1; loss_streak += 1; worst_loss_streak = max(worst_loss_streak, loss_streak); win_streak = 0

            trades.append({
                "trade_id": tid,
                "symbol": vsymbol,
                "tf": vtf,
                "side": side,
                "entry": e_entry,
                "sl": e_sl,
                "tp1": e_tp1,
                "tp2": e_tp2,
                "tp3": e_tp3,
                "entry_time": entry_time,
                "outcome": outcome_type,
                "outcome_time": outcome_time,
                "duration_sec": (outcome_time - entry_time) if (outcome_time and entry_time) else None,
            })

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

# -------------------------
# Telegram
# -------------------------
def send_telegram(text: str) -> bool:
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

        # send
        send_url = f"{api_base}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(send_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            payload = _json.loads(raw)
            if not payload.get("ok"):
                result["error"] = f"sendMessage failed: {raw[:200]}"
                log.warning("Telegram sendMessage error: %s", result["error"])
                return result
            msg = payload.get("result") or {}
            mid = msg.get("message_id")
            result["ok"] = True
            result["message_id"] = mid

        # pin
        if pin and result["message_id"] is not None:
            pin_url = f"{api_base}/pinChatMessage"
            pin_data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": result["message_id"],
            }).encode()
            preq = urllib.request.Request(pin_url, data=pin_data)
            try:
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

def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    t = str(payload.get("type") or "EVENT").upper()
    if t in {"AOE_PREMIUM", "AOE_DISCOUNT"}:
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
        lines.append("🤖 Astuce: après TP1, placez SL au BE.")
        return "\n".join(lines)

    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        lines = []
        lines.append(f"✅ {label} — {sym} {tf_lbl}")
        if tp is not None:
            lines.append(f"Mark price : {num(tp)}")
        return "\n".join(lines)

    if t == "SL_HIT":
        lines = [f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None:
            lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        return "\n".join(lines)

    return f"[TV] {t} | {sym} | TF {tf_lbl}"

# -------------------------
# HTML templates (ASCII only)
# -------------------------
BASE_CSS = """
:root{--bg:#0f172a;--card:#111827;--text:#e5e7eb;--muted:#94a3b8;--green:#10b981;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--border:#1f2937;--chip-bg:#0b1220}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700;letter-spacing:.2px}
.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:1200px){.grid{grid-template-columns:1fr 1fr 1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 16px 8px 16px;box-shadow:0 4px 14px rgba(0,0,0,.25)}
.title{font-size:16px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--border)}
th{text-align:left;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}
.btn{display:inline-block;padding:8px 12px;border:1px solid var(--border);background:#0b1220;color:var(--text);text-decoration:none;font-weight:600;border-radius:8px}
.btn:hover{background:#0f1525}
.chip{display:inline-block;padding:2px 8px;border:1px solid var(--border);border-radius:999px;margin-right:8px;background:var(--chip-bg)}
.muted{color:var(--muted)}
.badge-win{border-color:#065f46;background:#052e2b}
.badge-loss{border-color:#7f1d1d;background:#2a0f13}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,select{background:#0b1220;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px}
label{font-size:12px;color:var(--muted);display:block;margin-bottom:4px}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--border)}
.kv:last-child{border-bottom:none}
.dot{display:inline-block;width:10px;height:10px;border-radius:10px;margin-left:8px}
.ok{background:#10b981}.warn{background:#fb923c}
"""

INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>$css</style></head><body>
<h1>AI Trader PRO - Status</h1>
<div class="grid">
  <div class="card"><div class="title">Environment</div>
    <table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>$rows_html</tbody></table>
    <div class="controls">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
      <a class="btn" href="/openai-health">/openai-health</a>
      <a class="btn" href="/trades">/trades</a>
      <a class="btn" href="/trades-admin">/trades-admin</a>
    </div></div>

  <div class="card"><div class="title">Webhook</div>
    <div>POST <code>/tv-webhook</code> with JSON (TradingView).</div>
    <div class="muted">Secret can be passed as ?secret=... or in JSON body "secret".</div>
    <div style="margin-top:8px">
      <span class="chip">ENTRY</span><span class="chip">TP1_HIT</span><span class="chip">TP2_HIT</span>
      <span class="chip">TP3_HIT</span><span class="chip">SL_HIT</span><span class="chip">CLOSE</span><span class="chip">AOE_PREMIUM</span><span class="chip">AOE_DISCOUNT</span>
    </div></div>

  <div class="card"><div class="title">Altseason — État rapide</div>
    <div id="alt-asof" class="muted">Loading…</div>
    <div class="kv"><div>BTC Dominance</div><div><span id="alt-btc">—</span> <span class="muted">&lt; $btc_thr%</span><span id="dot-btc" class="dot"></span></div></div>
    <div class="kv"><div>ETH/BTC</div><div><span id="alt-eth">—</span> <span class="muted">&gt; $eth_thr</span><span id="dot-eth" class="dot"></span></div></div>
    <div class="kv"><div>Altseason Index</div><div><span id="alt-asi">N/A</span> <span class="muted">&ge; $asi_thr</span><span id="dot-asi" class="dot"></span></div></div>
    <div class="kv"><div>TOTAL2 (ex-BTC)</div><div><span id="alt-t2">—</span> <span class="muted">&gt; $t2_thr T$$</span><span id="dot-t2" class="dot"></span></div></div>
    <div class="muted" style="margin-top:8px">Passe au vert quand ≥ 3 conditions sont validées.</div>
  </div>
</div>

<script>
(function(){
  const url = "/altseason/check";
  function setText(id, txt){ const el = document.getElementById(id); if (el) el.textContent = txt; }
  function setDot(id, ok){ const el = document.getElementById(id); if (el) el.className = "dot " + (ok ? "ok" : "warn"); }
  function num(v){ return typeof v === "number" ? v : Number(v); }

  fetch(url)
  .then(async (r) => {
    const txt = await r.text();
    if (!r.ok) throw new Error(txt.slice(0, 300));
    let s;
    try { s = JSON.parse(txt); } catch(e){ throw new Error("Invalid JSON: " + txt.slice(0, 200)); }
    if (typeof s !== "object" || s === null) throw new Error("Empty payload");
    const need = ["btc_dominance","eth_btc","total2_usd","triggers"];
    for (const k of need){ if (!(k in s)) throw new Error("Missing key: " + k); }

    setText("alt-asof", "As of " + (s.asof || "now") + (s.stale ? " (cache)" : ""));
    const btc = num(s.btc_dominance);
    const eth = num(s.eth_btc);
    const t2 = num(s.total2_usd);
    const asi = s.altseason_index;

    setText("alt-btc", Number.isFinite(btc) ? btc.toFixed(2) + " %" : "—");
    setDot ("dot-btc", !!(s.triggers && s.triggers.btc_dominance_ok));
    setText("alt-eth", Number.isFinite(eth) ? eth.toFixed(5) : "—");
    setDot ("dot-eth", !!(s.triggers && s.triggers.eth_btc_ok));
    setText("alt-asi", (asi == null) ? "N/A" : String(asi));
    setDot ("dot-asi", !!(s.triggers && s.triggers.altseason_index_ok));
    setText("alt-t2", Number.isFinite(t2) ? (t2/1e12).toFixed(2) + " T$" : "—");
    setDot ("dot-t2", !!(s.triggers && s.triggers.total2_ok));
  })
  .catch((e) => {
    setText("alt-asof", "Erreur: " + (e && e.message ? e.message : e));
    setText("alt-btc", "—");
    setText("alt-eth", "—");
    setText("alt-asi", "N/A");
    setText("alt-t2", "—");
    setDot("dot-btc", false);
    setDot("dot-eth", false);
    setDot("dot-asi", false);
    setDot("dot-t2", false);
  });
})();
</script>
</body></html>
""")

# ----- PUBLIC trades (avec carte Altseason) -----
TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Trades</title>
<style>$css</style></head><body>
<h1>Trades (Public)</h1>

<div class="card">
  <div class="title">Filtres</div>
  <form method="get" action="/trades" class="controls">
    <div><label>Symbol</label><input type="text" name="symbol" value="$symbol"></div>
    <div><label>TF</label><input type="text" name="tf" value="$tf" placeholder="15 / 60 / 240 / 1D"></div>
    <div><label>Start (YYYY-MM-DD)</label><input type="text" name="start" value="$start"></div>
    <div><label>End (YYYY-MM-DD)</label><input type="text" name="end" value="$end"></div>
    <div><label>Limit</label><input type="number" min="1" max="2000" name="limit" value="$limit"></div>
    <div style="align-self:end"><button class="btn" type="submit">Apply</button></div>
  </form>
</div>

<div class="grid">
  <div class="card">
    <div class="title">Résumé</div>
    <div class="kv"><div>Total</div><div>$total_trades</div></div>
    <div class="kv"><div>Winrate</div><div>$winrate_pct %</div></div>
    <div class="kv"><div>Wins</div><div>$wins</div></div>
    <div class="kv"><div>Losses</div><div>$losses</div></div>
    <div class="kv"><div>TP1/TP2/TP3</div><div>$tp1_hits / $tp2_hits / $tp3_hits</div></div>
    <div class="kv"><div>Avg time to outcome</div><div>$avg_time_to_outcome_sec s</div></div>
    <div class="kv"><div>Best / Worst streak</div><div>$best_win_streak / $worst_loss_streak</div></div>
  </div>

  <div class="card" style="grid-column: span 2">
    <div class="title">Derniers trades</div>
    <table>
      <thead><tr>
        <th>Trade ID</th><th>Symbol</th><th>TF</th><th>Side</th>
        <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
        <th>Outcome</th><th>Duration(s)</th>
      </tr></thead>
      <tbody>$rows_html</tbody>
    </table>
  </div>
</div>

<div class="card">
  <div class="title">Altseason — Rappels seuils</div>
  <div>BTC Dominance &lt; $btc_thr% | ETH/BTC &gt; $eth_thr | ASI ≥ $asi_thr | TOTAL2 &gt; $t2_thr T$</div>
</div>
</body></html>
""")

# ----- Admin trades (protégé) -----
TRADES_ADMIN_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Admin</title>
<style>$css</style></head><body>
<h1>Trades (Admin)</h1>

<div class="card">
  <div class="title">Accès</div>
  <div class="muted">Cette page requiert le paramètre <code>?secret=... </code></div>
  <div class="controls">
    <a class="btn" href="/trades.json?secret=$secret">JSON</a>
    <a class="btn" href="/trades.csv?secret=$secret">CSV</a>
    <a class="btn" href="/events?secret=$secret">Events</a>
  </div>
</div>

<div class="card">
  <div class="title">Filtres</div>
  <form method="get" action="/trades-admin" class="controls">
    <input type="hidden" name="secret" value="$secret">
    <div><label>Symbol</label><input type="text" name="symbol" value="$symbol"></div>
    <div><label>TF</label><input type="text" name="tf" value="$tf" placeholder="15/60/240/1D"></div>
    <div><label>Start</label><input type="text" name="start" value="$start"></div>
    <div><label>End</label><input type="text" name="end" value="$end"></div>
    <div><label>Limit</label><input type="number" min="1" max="5000" name="limit" value="$limit"></div>
    <div style="align-self:end"><button class="btn" type="submit">Apply</button></div>
  </form>
</div>

<div class="grid">
  <div class="card">
    <div class="title">Résumé</div>
    <div class="kv"><div>Total</div><div>$total_trades</div></div>
    <div class="kv"><div>Winrate</div><div>$winrate_pct %</div></div>
    <div class="kv"><div>Wins</div><div>$wins</div></div>
    <div class="kv"><div>Losses</div><div>$losses</div></div>
    <div class="kv"><div>TP1/TP2/TP3</div><div>$tp1_hits / $tp2_hits / $tp3_hits</div></div>
    <div class="kv"><div>Avg time to outcome</div><div>$avg_time_to_outcome_sec s</div></div>
    <div class="kv"><div>Best / Worst streak</div><div>$best_win_streak / $worst_loss_streak</div></div>
  </div>

  <div class="card" style="grid-column: span 2">
    <div class="title">Derniers trades</div>
    <table>
      <thead><tr>
        <th>Trade ID</th><th>Symbol</th><th>TF</th><th>Side</th>
        <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
        <th>Outcome</th><th>Duration(s)</th>
      </tr></thead>
      <tbody>$rows_html</tbody>
    </table>
  </div>
</div>
</body></html>
""")

EVENTS_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Events</title>
<style>$css</style></head><body>
<h1>Events (Admin)</h1>
<div class="card">
  <div class="title">Accès</div>
  <div class="muted">Requiert <code>?secret=...</code></div>
  <div class="controls">
    <a class="btn" href="/events.json?secret=$secret">JSON</a>
    <a class="btn" href="/trades-admin?secret=$secret">Back to Admin</a>
  </div>
</div>

<div class="card">
  <div class="title">Derniers événements</div>
  <table>
    <thead><tr>
      <th>Time</th><th>Type</th><th>Symbol</th><th>TF</th><th>Side</th><th>Trade ID</th><th>Payload</th>
    </tr></thead>
    <tbody>$rows_html</tbody>
  </table>
</div>
</body></html>
""")
# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="AI Trader PRO")

# -------- Health / Ping --------
@app.get("/ping")
def ping():
    return {"ok": True}

# -------- Index (PUBLIC) --------
@app.get("/", response_class=HTMLResponse)
def index():
    rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("TELEGRAM_PIN_ALTSEASON", str(bool(TELEGRAM_PIN_ALTSEASON))),
        ("LLM_ENABLED", str(bool(LLM_ENABLED))),
        ("LLM_CLIENT_READY", str(bool(_openai_client is not None))),
        ("LLM_DOWN_REASON", _llm_reason_down or ""),
        ("LLM_MODEL", LLM_MODEL if (LLM_ENABLED and _openai_client) else ""),
        ("FORCE_LLM", str(bool(FORCE_LLM))),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
        ("PORT", str(PORT)),
        ("RISK_ACCOUNT_BAL", str(RISK_ACCOUNT_BAL)),
        ("RISK_PCT", str(RISK_PCT)),
        ("DB_PATH", DB_PATH),
        ("DEBUG", str(bool(DEBUG_MODE))),
        ("ALT_BTC_DOM_THR", str(ALT_BTC_DOM_THR)),
        ("ALT_ETH_BTC_THR", str(ALT_ETH_BTC_THR)),
        ("ALT_ASI_THR", str(ALT_ASI_THR)),
        ("ALT_TOTAL2_THR_T", str(ALT_TOTAL2_THR_T)),
        ("ALT_CACHE_TTL", str(ALT_CACHE_TTL)),
        ("ALT_GREENS_REQUIRED", str(ALT_GREENS_REQUIRED)),
        ("ALTSEASON_AUTONOTIFY", str(bool(ALTSEASON_AUTONOTIFY))),
        ("ALTSEASON_POLL_SECONDS", str(ALTSEASON_POLL_SECONDS)),
        ("ALTSEASON_NOTIFY_MIN_GAP_MIN", str(ALTSEASON_NOTIFY_MIN_GAP_MIN)),
    ]
    trs = "".join([f"<tr><td>{k}</td><td>{escape_html(v)}</td></tr>" for (k, v) in rows])
    html = INDEX_HTML_TPL.safe_substitute(
        css=BASE_CSS,
        rows_html=trs,
        btc_thr=str(int(ALT_BTC_DOM_THR)),
        eth_thr=f"{ALT_ETH_BTC_THR:.3f}",
        asi_thr=str(int(ALT_ASI_THR)),
        t2_thr=f"{ALT_TOTAL2_THR_T:.2f}"
    )
    return HTMLResponse(html)

# -------- Env sanity (PROTÉGÉ) --------
@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "TELEGRAM_PIN_ALTSEASON": bool(TELEGRAM_PIN_ALTSEASON),
        "LLM_ENABLED": bool(LLM_ENABLED),
        "LLM_CLIENT_READY": bool(_openai_client is not None),
        "LLM_DOWN_REASON": _llm_reason_down,
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "FORCE_LLM": bool(FORCE_LLM),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
        "RISK_ACCOUNT_BAL": RISK_ACCOUNT_BAL,
        "RISK_PCT": RISK_PCT,
        "DB_PATH": DB_PATH,
        "DEBUG": DEBUG_MODE,
        "ALTSEASON": {
            "ALT_BTC_DOM_THR": ALT_BTC_DOM_THR,
            "ALT_ETH_BTC_THR": ALT_ETH_BTC_THR,
            "ALT_ASI_THR": ALT_ASI_THR,
            "ALT_TOTAL2_THR_T": ALT_TOTAL2_THR_T,
            "ALT_CACHE_TTL": ALT_CACHE_TTL,
            "ALT_GREENS_REQUIRED": ALT_GREENS_REQUIRED,
            "AUTONOTIFY": ALTSEASON_AUTONOTIFY,
            "POLL_SECONDS": ALTSEASON_POLL_SECONDS,
            "NOTIFY_MIN_GAP_MIN": ALTSEASON_NOTIFY_MIN_GAP_MIN,
        }
    }

# -------- Telegram health (PROTÉGÉ) --------
@app.get("/tg-health")
def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    ok = send_telegram("Test Telegram: OK")
    return {"ok": ok}

# -------- OpenAI health (PROTÉGÉ) --------
@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=2,
        )
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return {"ok": False, "error": str(e)}
# -------------------------
# Webhook TradingView (PROTÉGÉ)
# -------------------------
@app.post("/tv-webhook")
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON must be an object")
    except Exception as e:
        log.error("Invalid JSON: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    body_secret = payload.get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    log.info("Webhook payload: %s", json.dumps(payload)[:300])
    save_event(payload)

    # 👉 Envoi Telegram
    try:
        msg = telegram_rich_message(payload)
        if msg:
            res = send_telegram_ex(msg, pin=False)
            log.info("TV webhook -> telegram sent=%s pinned=%s err=%s",
                     res.get("ok"), res.get("pinned"), res.get("error"))
    except Exception as e:
        log.warning("TV webhook telegram send error: %s", e)

    return {"ok": True}

# -------------------------
# Trades JSON (PROTÉGÉ)
# -------------------------
@app.get("/trades.json")
def trades_json(
    secret: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep,
                                            max_rows=max(1000, limit*10))
    return JSONResponse({"summary": summary,
                         "trades": trades[-limit:] if limit else trades})

# -------------------------
# Trades CSV (PROTÉGÉ)
# -------------------------
@app.get("/trades.csv")
def trades_csv(
    secret: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(1000)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, _ = build_trades_filtered(symbol, tf, start_ep, end_ep,
                                      max_rows=max(5000, limit*10))
    data = trades[-limit:] if limit else trades
    headers = ["trade_id","symbol","tf","side","entry","sl","tp1","tp2","tp3",
               "entry_time","outcome","outcome_time","duration_sec"]
    lines = [",".join(headers)]
    for tr in data:
        row = [str(tr.get(h,"")) for h in headers]
        row = [("\"%s\"" % x) if ("," in x) else x for x in row]
        lines.append(",".join(row))
    return Response(content="\n".join(lines), media_type="text/csv")

# -------------------------
# Trades PUBLIC (avec Altseason)
# -------------------------
@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep,
                                            max_rows=max(5000, limit*10))

    rows_html = ""
    spark_values = []
    data = trades[-limit:] if limit else trades
    for tr in data:
        outcome = tr["outcome"] or "NONE"
        badge_class = "badge-win" if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT") \
                      else ("badge-loss" if outcome == "SL_HIT" else "")
        spark_values.append(1.0 if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT")
                            else (0.0 if outcome == "SL_HIT" else 0.5))
        outcome_html = f'<span class="chip {badge_class}">{escape_html(outcome)}</span>'
        rows_html += (
            "<tr>"
            f"<td>{escape_html(str(tr['trade_id']))}</td>"
            f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
            f"<td>{fmt_num(tr.get('entry'))}</td>"
            f"<td>{fmt_num(tr.get('sl'))}</td>"
            f"<td>{fmt_num(tr.get('tp1'))}</td>"
            f"<td>{fmt_num(tr.get('tp2'))}</td>"
            f"<td>{fmt_num(tr.get('tp3'))}</td>"
            f"<td>{outcome_html}</td>"
            f"<td>{tr.get('duration_sec') if tr.get('duration_sec') is not None else ''}</td>"
            "</tr>"
        )

    html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
        symbol=escape_html(symbol or ""),
        tf=escape_html(tf or ""),
        start=escape_html(start or ""),
        end=escape_html(end or ""),
        limit=str(limit),
        total_trades=str(summary["total_trades"]),
        winrate_pct=str(summary["winrate_pct"]),
        wins=str(summary["wins"]),
        losses=str(summary["losses"]),
        tp1_hits=str(summary["tp1_hits"]),
        tp2_hits=str(summary["tp2_hits"]),
        tp3_hits=str(summary["tp3_hits"]),
        avg_time_to_outcome_sec=str(summary["avg_time_to_outcome_sec"]),
        best_win_streak=str(summary["best_win_streak"]),
        worst_loss_streak=str(summary["worst_loss_streak"]),
        rows_html=rows_html or '<tr><td colspan="11" class="muted">No trades yet.</td></tr>',
        spark_data=json.dumps(spark_values),
        btc_thr=str(int(ALT_BTC_DOM_THR)),
        eth_thr=f"{ALT_ETH_BTC_THR:.3f}",
        asi_thr=str(int(ALT_ASI_THR)),
        t2_thr=f"{ALT_TOTAL2_THR_T:.2f}"
    )
    return HTMLResponse(html)

# -------------------------
# Trades ADMIN (protégé)
# -------------------------
@app.get("/trades-admin", response_class=HTMLResponse)
def trades_admin(secret: Optional[str] = Query(None), ...):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    ...
    return HTMLResponse(html)

# -------------------------
# Events, Reset, Selftest, Daemon
# -------------------------
@app.get("/events", response_class=HTMLResponse)
def events(secret: Optional[str] = Query(None), limit: int = Query(200)):
    ...

@app.get("/events.json")
def events_json(secret: Optional[str] = Query(None), limit: int = Query(200)):
    ...

@app.get("/reset")
def reset_all(secret: Optional[str] = Query(None), confirm: Optional[str] = Query(None)):
    ...

@app.get("/selftest")
def selftest(secret: Optional[str] = Query(None)):
    ...

@app.get("/altseason/daemon-status")
def altseason_daemon_status():
    ...

@app.on_event("startup")
def _start_daemon():
    ...

@app.on_event("shutdown")
def _stop_daemon():
    ...

# ============ Run local ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
