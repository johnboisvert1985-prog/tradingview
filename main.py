# main.py
import os
import json
import time
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple, Iterable
from string import Template
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

# -------------------------
# Config / ENV
# -------------------------
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
LLM_ENABLED        = os.getenv("LLM_ENABLED", "0") in ("1", "true", "True")
LLM_MODEL          = os.getenv("LLM_MODEL", "gpt-4o-mini")
FORCE_LLM          = os.getenv("FORCE_LLM", "0") in ("1", "true", "True")
CONFIDENCE_MIN     = float(os.getenv("CONFIDENCE_MIN", "0.0") or 0.0)
PORT               = int(os.getenv("PORT", "8000"))
RISK_ACCOUNT_BAL   = float(os.getenv("RISK_ACCOUNT_BAL", "0") or 0)
RISK_PCT           = float(os.getenv("RISK_PCT", "1.0") or 1.0)

# Telegram cooldown
TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.2") or 1.2)
_last_tg = 0.0

# DB path default = data/data.db; fallback to /tmp if read-only
DB_PATH            = os.getenv("DB_PATH", "data/data.db")
DEBUG_MODE         = os.getenv("DEBUG", "0") in ("1", "true", "True")

# -------------------------
# Optional: Templates override via ENV (ASCII only)
# -------------------------
def _env_or(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if (v is not None and v.strip() != "") else default

TPL_ENTRY = _env_or("TELEGRAM_TEMPLATE_ENTRY",
"""📩 {symbol} {tfm} | {tier}
📈 {side} Entry: {entry}
🎯 Confiance: {confidence_pct}
💡 Leverage: {leverage}

🎯 Targets
• TP1: {tp1}
• TP2: {tp2}
• TP3: {tp3}
❌ Stop-Loss: {sl}

Note: apres TP1, passer le SL a BE.
""").strip()

TPL_TP_HIT = _env_or("TELEGRAM_TEMPLATE_TP",
"""🎯 {event} | {symbol} {tfm}
Side: {side}
Prix: {hit_price}
Profit: {profit_pct}
Trade ID: {trade_id}
""").strip()

TPL_SL_HIT = _env_or("TELEGRAM_TEMPLATE_SL",
"""🟥 SL_HIT | {symbol} {tfm}
Side: {side}
SL: {sl}
Perte: {profit_pct}
Trade ID: {trade_id}
""").strip()

TPL_CLOSE = _env_or("TELEGRAM_TEMPLATE_CLOSE",
"""🔔 CLOSE | {symbol} {tfm}
Raison: {reason}
Trade ID: {trade_id}
""").strip()

TPL_AOE = _env_or("TELEGRAM_TEMPLATE_AOE",
"""{label} | {symbol} {tfm}
Zone: {zone_info}
""").strip()

# -------------------------
# OpenAI (optional)
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
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as f:
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
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             row["type"], row["symbol"], row["tf"], row["trade_id"])

# -------------------------
# Helpers
# -------------------------
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

def fetch_events_filtered(symbol: Optional[str], tf: Optional[str],
                          start_ep: Optional[int], end_ep: Optional[int],
                          limit: int = 10000) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    if tf:
        sql += " AND tf = ?"
        args.append(tf)
    if start_ep is not None:
        sql += " AND received_at >= ?"
        args.append(start_ep)
    if end_ep is not None:
        sql += " AND received_at <= ?"
        args.append(end_ep)
    sql += " ORDER BY received_at ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return cur.fetchall()

# -------------------------
# Build trades & stats
# -------------------------
class TradeOutcome:
    NONE  = "NONE"
    TP1   = "TP1_HIT"
    TP2   = "TP2_HIT"
    TP3   = "TP3_HIT"
    SL    = "SL_HIT"
    CLOSE = "CLOSE"

def build_trades_filtered(symbol: Optional[str], tf: Optional[str],
                          start_ep: Optional[int], end_ep: Optional[int],
                          max_rows: int = 20000) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
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
                vsymbol = ev["symbol"]
                vtf = ev["tf"]
                side = ev["side"]
                e_entry = ev["entry"]
                e_sl = ev["sl"]
                e_tp1 = ev["tp1"]
                e_tp2 = ev["tp2"]
                e_tp3 = ev["tp3"]
                entry_time = ev["received_at"]
            elif entry is not None:
                if etype in ("TP3_HIT", "TP2_HIT", "TP1_HIT", "SL_HIT", "CLOSE") and outcome_type == TradeOutcome.NONE:
                    outcome_type = etype
                    outcome_time = ev["received_at"]

        if entry is not None:
            total += 1
            if outcome_time and entry_time:
                times_to_outcome.append(int(outcome_time - entry_time))
            is_win = outcome_type in (TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3)
            if is_win:
                wins += 1
                win_streak += 1
                best_win_streak = max(best_win_streak, win_streak)
                loss_streak = 0
                if outcome_type == TradeOutcome.TP1: hit_tp1 += 1
                elif outcome_type == TradeOutcome.TP2: hit_tp2 += 1
                elif outcome_type == TradeOutcome.TP3: hit_tp3 += 1
            elif outcome_type == TradeOutcome.SL:
                losses += 1
                loss_streak += 1
                worst_loss_streak = max(worst_loss_streak, loss_streak)
                win_streak = 0

            trades.append(
                {
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
                }
            )

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

        import urllib.request
        import urllib.parse
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(api_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False

# -------------------------
# HTML templates (ASCII only)
# -------------------------
INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>
:root{--bg:#0f172a;--card:#111827;--text:#e5e7eb;--muted:#94a3b8;--green:#10b981;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--border:#1f2937;--chip-bg:#0b1220}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700;letter-spacing:.2px}.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:960px){.grid{grid-template-columns:1fr 1fr}}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 16px 8px 16px;box-shadow:0 4px 14px rgba(0,0,0,.25)}
.title{font-size:16px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--border)}th{text-align:left;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:none}
.btn{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:#0b1220;color:var(--text);text-decoration:none;font-weight:600;margin-right:8px}
.btn:hover{background:#0f1525}.chip{display:inline-block;padding:2px 8px;border:1px solid var(--border);border-radius:999px;margin-right:8px;background:var(--chip-bg)}.muted{color:var(--muted)}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.cta-row{margin-top:10px}
</style></head><body>
<h1>AI Trader PRO - Status</h1>
<div class="grid">
<div class="card"><div class="title">Environment</div>
<table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>$rows_html</tbody></table>
<div class="cta-row">
  <a class="btn" href="/env-sanity">/env-sanity</a>
  <a class="btn" href="/tg-health">/tg-health</a>
  <a class="btn" href="/openai-health">/openai-health</a>
  <a class="btn" href="/trades">/trades</a>
</div></div>
<div class="card"><div class="title">Webhook</div>
<div>POST <code>/tv-webhook</code> with JSON (TradingView).</div>
<div class="muted">Secret can be passed as ?secret=... or in JSON body "secret".</div>
<div style="margin-top:8px" class="row">
  <span class="chip">ENTRY</span><span class="chip">TP1_HIT</span><span class="chip">TP2_HIT</span>
  <span class="chip">TP3_HIT</span><span class="chip">SL_HIT</span><span class="chip">CLOSE</span><span class="chip">AOE_PREMIUM</span><span class="chip">AOE_DISCOUNT</span>
</div></div></div></body></html>
""")

TRADES_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Trades</title>
<style>
:root{--bg:#0f172a;--card:#111827;--text:#e5e7eb;--muted:#94a3b8;--green:#10b981;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--border:#1f2937;--chip-bg:#0b1220}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700}.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:1100px){.grid{grid-template-columns:360px 1fr}}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:0 4px 14px rgba(0,0,0,.25)}
.title{font-size:16px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}.kpi{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:6px}
.kpi .item{background:#0b1220;border:1px solid var(--border);border-radius:10px;padding:10px}.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:22px;font-weight:700}
.kpi .green{color:var(--green)}.kpi .red{color:var(--red)}.kpi .blue{color:var(--blue)}.kpi .yellow{color:var(--yellow)}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--border)}th{text-align:left;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:none}
.chip{display:inline-block;padding:2px 8px;border:1px solid var(--border);border-radius:999px;background:var(--chip-bg)}.badge-win{color:#10b981;border-color:#0f5132}
.badge-loss{color:#ef4444;border-color:#5c1e1e}.muted{color:var(--muted)}.row{display:flex;gap:8px;flex-wrap:wrap}
.filter{display:grid;gap:8px}.filter input{width:100%;padding:8px;border-radius:8px;border:1px solid var(--border);background:#0b1220;color:var(--text)}
.btn{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:#0b1220;color:var(--text);text-decoration:none;font-weight:600;margin-right:8px}
.btn:hover{background:#0f1525}.spark{width:100%;height:60px}
</style></head><body>
<h1>AI Trader PRO - Trades</h1>
<div class="grid">
  <div class="card">
    <div class="title">Filters</div>
    <form method="get" class="filter">
      <input type="hidden" name="secret" value="$secret">
      <label>Symbol <input type="text" name="symbol" value="$symbol" placeholder="ex: BTCUSDT"></label>
      <label>TF <input type="text" name="tf" value="$tf" placeholder="ex: 15, 60, 1D"></label>
      <label>Start (YYYY-MM-DD) <input type="text" name="start" value="$start" placeholder="YYYY-MM-DD"></label>
      <label>End (YYYY-MM-DD) <input type="text" name="end" value="$end" placeholder="YYYY-MM-DD"></label>
      <label>Limit rows <input type="number" min="1" max="50000" step="1" name="limit" value="$limit"></label>
      <div class="row">
        <button class="btn" type="submit">Apply</button>
        <a class="btn" href="/trades.csv?secret=$secret&symbol=$symbol&tf=$tf&start=$start&end=$end&limit=$limit">Export CSV</a>
        <a class="btn" href="/events?secret=$secret">Raw events</a>
        <a class="btn" href="/selftest?secret=$secret">Self test</a>
        <form method="post" action="/reset/trades?secret=$secret" onsubmit="return confirm('Delete ALL trades?');" style="display:inline-block">
          <input type="hidden" name="confirm" value="YES"/>
          <button class="btn" type="submit" style="border-color:#5c1e1e;color:#ef4444">Delete ALL</button>
        </form>
      </div>
    </form>
  </div>

  <div class="card">
    <div class="title">Summary</div>
    <div class="kpi">
      <div class="item"><div class="label">Total</div><div class="value">$total_trades</div></div>
      <div class="item"><div class="label">Winrate</div><div class="value green">$winrate_pct%</div></div>
      <div class="item"><div class="label">Wins</div><div class="value green">$wins</div></div>
      <div class="item"><div class="label">Losses</div><div class="value red">$losses</div></div>
      <div class="item"><div class="label">TP1 hits</div><div class="value blue">$tp1_hits</div></div>
      <div class="item"><div class="label">TP2 hits</div><div class="value blue">$tp2_hits</div></div>
      <div class="item"><div class="label">TP3 hits</div><div class="value yellow">$tp3_hits</div></div>
      <div class="item"><div class="label">Avg time to outcome</div><div class="value">$avg_time_to_outcome_sec s</div></div>
      <div class="item"><div class="label">Best win streak</div><div class="value green">$best_win_streak</div></div>
      <div class="item"><div class="label">Worst loss streak</div><div class="value red">$worst_loss_streak</div></div>
    </div>
    <canvas class="spark" id="spark"></canvas>
  </div>

  <div class="card" style="grid-column:1/-1">
    <div class="title">Recent trades</div>
    <table>
      <thead><tr>
        <th>Trade ID</th><th>Symbol</th><th>TF</th><th>Side</th>
        <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
        <th>Outcome</th><th>Duration (s)</th>
      </tr></thead>
      <tbody>$rows_html</tbody>
    </table>
    <div class="muted">Showing up to $limit trades (grouped by trade_id).</div>
  </div>
</div>

<script>
const data = $spark_data;
const canvas = document.getElementById('spark');
if (canvas && data && data.length > 0) {
  const ctx = canvas.getContext('2d');
  const W = canvas.clientWidth, H = canvas.clientHeight; canvas.width=W; canvas.height=H;
  const n = data.length, pad=6; function x(i){return pad + i*(W-2*pad)/Math.max(1,(n-1));}
  function y(v){ return H - pad - (v-0)*(H-2*pad)/(1-0); }
  ctx.lineWidth=2; ctx.strokeStyle='#3b82f6'; ctx.beginPath();
  for (let i=0;i<n;i++){ const xp=x(i), yp=y(data[i]); if(i===0)ctx.moveTo(xp,yp); else ctx.lineTo(xp,yp); } ctx.stroke();
  ctx.strokeStyle='#1f2937'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad, y(0.5)); ctx.lineTo(W-pad, y(0.5)); ctx.stroke();
}
</script>
</body></html>
""")

EVENTS_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Events</title>
<style>
:root{--bg:#0f172a;--card:#111827;--text:#e5e7eb;--muted:#94a3b8;--border:#1f2937}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.muted{color:var(--muted)}.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.btn{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:#0b1220;color:var(--text);text-decoration:none;font-weight:600}
input{padding:8px;border-radius:8px;border:1px solid var(--border);background:#0b1220;color:var(--text)}
</style></head><body>
<h1>AI Trader PRO - Raw Events</h1>
<div class="card">
<form method="get" class="row">
  <input type="hidden" name="secret" value="$secret" />
  <label>Limit <input type="number" name="limit" value="$limit" min="1" max="50000" /></label>
  <button class="btn" type="submit">Apply</button>
  <a class="btn" href="/trades?secret=$secret">Back to Trades</a>
</form>
<table>
  <thead><tr><th>Time (server)</th><th>Type</th><th>Symbol</th><th>TF</th><th>Side</th><th>Trade ID</th><th>Payload</th></tr></thead>
  <tbody>$rows_html</tbody>
</table></div></body></html>
""")

# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="AI Trader PRO")

def escape_html(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
             .replace('"',"&quot;").replace("'","&#39;"))

def fmt_num(v) -> str:
    try:
        if v is None: return ""
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "")

def _parse_raw_json(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        return json.loads(row["raw_json"] or "{}")
    except Exception:
        return {}

def _get_entry_by_trade(trade_id: str) -> Optional[sqlite3.Row]:
    if not trade_id:
        return None
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM events WHERE trade_id=? AND type='ENTRY' ORDER BY received_at ASC LIMIT 1",
            (trade_id,)
        )
        r = cur.fetchone()
        return r

def _tf_to_str(tf: Optional[str]) -> str:
    if not tf:
        return ""
    tf = str(tf)
    if tf in ("1", "3", "5", "15", "30"): return tf + "m"
    if tf in ("45"): return tf + "m"
    if tf in ("60"): return "1h"
    if tf in ("120"): return "2h"
    if tf in ("180"): return "3h"
    if tf in ("240"): return "4h"
    return tf  # e.g. "1D", "1W", custom

def _profit_pct(hit: Optional[float], entry: Optional[float], side: Optional[str]) -> Optional[float]:
    if hit is None or entry is None or entry == 0 or not side:
        return None
    try:
        if str(side).upper().startswith("LONG"):
            return (hit - entry) / entry * 100.0
        else:
            return (entry - hit) / entry * 100.0
    except Exception:
        return None

def _first_nonempty(*vals: Iterable[Optional[str]]) -> str:
    for v in vals:
        if v is None: 
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return str(v)
    return ""

def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{v:.2f}%"

def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None

def build_rich_message(payload: Dict[str, Any]) -> str:
    # 1) Pine-provided message wins if present
    tele_msg = (payload.get("tele_msg") or "").strip()
    if tele_msg:
        return tele_msg

    # 2) Otherwise, build from templates
    t = (payload.get("type") or "").upper()
    symbol = payload.get("symbol") or "?"
    tf = _tf_to_str(str(payload.get("tf") or ""))
    side = (payload.get("side") or "").upper()
    trade_id = payload.get("trade_id") or ""
    reason = payload.get("reason") or ""

    entry = _safe_float(payload.get("entry"))
    sl    = _safe_float(payload.get("sl"))
    tp1   = _safe_float(payload.get("tp1"))
    tp2   = _safe_float(payload.get("tp2"))
    tp3   = _safe_float(payload.get("tp3"))

    # Best-effort: get info from ENTRY row if missing
    entry_row = _get_entry_by_trade(trade_id) if trade_id else None
    if entry_row is not None:
        if entry is None: entry = entry_row["entry"]
        if not side: side = (entry_row["side"] or "").upper()
        if sl is None: sl = entry_row["sl"]
        if tp1 is None: tp1 = entry_row["tp1"]
        if tp2 is None: tp2 = entry_row["tp2"]
        if tp3 is None: tp3 = entry_row["tp3"]

    # Optional: read extra fields from raw_json
    hit_price = _safe_float(payload.get("tp")) or _safe_float(payload.get("price")) or _safe_float(payload.get("hit")) or _safe_float(payload.get("mark"))
    # confidence, leverage
    confidence_pct = ""
    conf = payload.get("confidence")
    if conf is not None:
        try:
            confidence_pct = f"{float(conf):.2f}%"
        except Exception:
            confidence_pct = str(conf)

    # Leverage from ENTRY lev_reco if present
    leverage = ""
    if entry_row is not None:
        try:
            rj = json.loads(entry_row["raw_json"] or "{}")
            lev = rj.get("lev_reco")
            if lev is not None:
                # display as Nx cross rounded
                try:
                    leverage = f"{float(lev):.0f}x cross"
                except Exception:
                    leverage = f"{lev}x cross"
        except Exception:
            pass

    # Friendly tier text (you peux adapter selon tf)
    tier = "MidTerm" if tf in ("60", "1h", "120", "180", "240", "2h", "3h", "4h") else "Intraday"

    # Format numbers
    mapping = {
        "symbol": symbol,
        "tfm": tf,
        "tier": tier,
        "side": "LONG" if side.startswith("LONG") else ("SHORT" if side.startswith("SHORT") else side),
        "entry": fmt_num(entry),
        "sl": fmt_num(sl),
        "tp1": fmt_num(tp1),
        "tp2": fmt_num(tp2),
        "tp3": fmt_num(tp3),
        "confidence_pct": confidence_pct,
        "leverage": leverage,
        "trade_id": trade_id,
        "reason": reason or "flip / manual",
        "event": t,
        "hit_price": fmt_num(hit_price),
        "profit_pct": "",  # may fill below
        "zone_info": "",
        "label": "",
    }

    # compute profit pct for TP / SL when possible
    if t in ("TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT") and entry is not None:
        p = _profit_pct(hit_price if hit_price is not None else entry, entry, mapping["side"])
        mapping["profit_pct"] = _fmt_pct(p)

    # AOE extras
    if t in ("AOE_PREMIUM","AOE_DISCOUNT"):
        upper = payload.get("upper")
        lower = payload.get("lower")
        hiwin = payload.get("hiWin")
        lowin = payload.get("loWin")
        if t == "AOE_PREMIUM":
            mapping["label"] = "🟥 AOE_PREMIUM"
            mapping["zone_info"] = f"hi={fmt_num(hiwin)} upper={fmt_num(upper)}"
        else:
            mapping["label"] = "🟩 AOE_DISCOUNT"
            mapping["zone_info"] = f"lo={fmt_num(lowin)} lower={fmt_num(lower)}"

    # Choose template
    if t == "ENTRY":
        tpl = TPL_ENTRY
    elif t in ("TP1_HIT","TP2_HIT","TP3_HIT"):
        tpl = TPL_TP_HIT
    elif t == "SL_HIT":
        tpl = TPL_SL_HIT
    elif t == "CLOSE":
        tpl = TPL_CLOSE
    elif t in ("AOE_PREMIUM","AOE_DISCOUNT"):
        tpl = TPL_AOE
    else:
        # fallback simple
        return f"[TV] {t} | {symbol} | TF {tf}"

    # Render
    try:
        msg = tpl.format(**mapping)
        return msg.strip()
    except Exception as e:
        log.warning("Template render failed: %s", e)
        return f"[TV] {t} | {symbol} | TF {tf}"

# -------------------------
# Routes
# -------------------------
@app.get("/ping")
def ping():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
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
    ]
    trs = "".join([f"<tr><td>{k}</td><td>{escape_html(v)}</td></tr>" for (k, v) in rows])
    html = INDEX_HTML_TPL.substitute(rows_html=trs)
    return HTMLResponse(html)

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
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
    }

@app.get("/tg-health")
def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    ok = send_telegram("Test Telegram: OK")
    return {"ok": ok}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=2,
        )
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
        log.warning("Invalid secret on tv-webhook")
        raise HTTPException(status_code=401, detail="Invalid secret")

    log.info("Webhook payload: %s", json.dumps(payload)[:400])
    save_event(payload)

    # Rich Telegram message
    try:
        msg = build_rich_message(payload)
        if msg:
            send_telegram(msg)
    except Exception as e:
        log.warning("Telegram compose/send failed: %s", e)

    return {"ok": True}

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
    end_ep   = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(1000, limit*10))
    return JSONResponse({"summary": summary, "trades": trades[-limit:] if limit else trades})

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
    end_ep   = parse_date_end_to_epoch(end)
    trades, _ = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit*10))
    data = trades[-limit:] if limit else trades
    headers = ["trade_id","symbol","tf","side","entry","sl","tp1","tp2","tp3","entry_time","outcome","outcome_time","duration_sec"]
    lines = [",".join(headers)]
    for tr in data:
        row = [str(tr.get(h,"")) for h in headers]
        row = [("\"%s\"" % x) if ("," in x) else x for x in row]
        lines.append(",".join(row))
    return Response(content="\n".join(lines), media_type="text/csv")

@app.get("/trades", response_class=HTMLResponse)
def trades(
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
    end_ep   = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit*10))

    rows_html = ""
    spark_values = []
    data = trades[-limit:] if limit else trades
    for tr in data:
        outcome = tr["outcome"] or "NONE"
        badge_class = "badge-win" if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT") else ("badge-loss" if outcome == "SL_HIT" else "")
        spark_values.append(1.0 if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT") else (0.0 if outcome == "SL_HIT" else 0.5))
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

    html = TRADES_HTML_TPL.substitute(
        secret=escape_html(secret or ""),
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
        rows_html=rows_html or '<tr><td colspan="11" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
        spark_data=json.dumps(spark_values)
    )
    return HTMLResponse(html)

@app.get("/events", response_class=HTMLResponse)
def events(secret: Optional[str] = Query(None), limit: int = Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()

    def fmt_time(ts: int) -> str:
        try:
            import datetime as dt
            return dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(ts)

    rows_html = ""
    for r in rows:
        rows_html += (
            "<tr>"
            f"<td>{escape_html(fmt_time(r['received_at']))}</td>"
            f"<td>{escape_html(r['type'] or '')}</td>"
            f"<td>{escape_html(r['symbol'] or '')}</td>"
            f"<td>{escape_html(r['tf'] or '')}</td>"
            f"<td>{escape_html(r['side'] or '')}</td>"
            f"<td>{escape_html(r['trade_id'] or '')}</td>"
            f"<td><pre style='white-space:pre-wrap;margin:0'>{escape_html(r['raw_json'] or '')}</pre></td>"
            "</tr>"
        )
    html = EVENTS_HTML_TPL.substitute(
        secret=escape_html(secret or ""), limit=str(limit),
        rows_html=rows_html or '<tr><td colspan="7" class="muted">No events.</td></tr>'
    )
    return HTMLResponse(html)

@app.get("/events.json")
def events_json(secret: Optional[str] = Query(None), limit: int = Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    return JSONResponse({"events": rows})

@app.get("/trades/secret={secret}")
def trades_alias(secret: str):
    return RedirectResponse(url=f"/trades?secret={secret}", status_code=307)

# -------- Reset (POST) --------
@app.post("/reset/trades")
def reset_trades(secret: Optional[str] = Query(None), confirm: str = Form(...)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if confirm != "YES":
        raise HTTPException(status_code=400, detail="Confirm must be YES")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM events")
        conn.commit()
    send_telegram("Reset: all trades cleared.")
    return RedirectResponse(url=f"/trades?secret={secret or ''}", status_code=303)

# -------- Self test --------
@app.get("/selftest")
def selftest(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    tid = f"SELFTEST_{int(time.time())}"
    save_event({"type":"ENTRY","symbol":"TESTUSD","tf":"15","side":"LONG","entry":100.0,"sl":95.0,"tp1":101.0,"tp2":102.0,"tp3":105.0,"trade_id":tid, "lev_reco": 10})
    time.sleep(1)
    save_event({"type":"TP1_HIT","symbol":"TESTUSD","tf":"15","trade_id":tid, "tp": 101.0})
    return {"ok": True, "trade_id": tid}

# ============ Run local ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
