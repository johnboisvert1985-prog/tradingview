# main.py
import os
import json
import time
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# =========================
# Config / ENV
# =========================
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

# =========================
# OpenAI client (optionnel)
# =========================
_openai_client = None
_llm_reason_down = None
if LLM_ENABLED and OPENAI_API_KEY:
    try:
        # openai>=1.x style
        from openai import OpenAI  # type: ignore
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        _llm_reason_down = f"OpenAI client init failed: {e}"
else:
    _llm_reason_down = "LLM disabled or OPENAI_API_KEY missing"

# =========================
# SQLite setup (persistant)
# =========================
DB_PATH = os.getenv("DB_PATH", "data.db")

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        conn.commit()

db_init()

def save_event(payload: Dict[str, Any]) -> None:
    # Normalize fields
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

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

# =========================
# Business logic: trades & stats
# =========================
class TradeOutcome:
    NONE = "NONE"
    TP1 = "TP1_HIT"
    TP2 = "TP2_HIT"
    TP3 = "TP3_HIT"
    SL  = "SL_HIT"

def load_events(limit: int = 500) -> List[sqlite3.Row]:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM events ORDER BY received_at DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

def load_all_events_for_analysis(max_rows: int = 10000) -> List[sqlite3.Row]:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM events ORDER BY received_at ASC LIMIT ?",
            (max_rows,),
        )
        return cur.fetchall()

def build_trades() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Regroupe par trade_id. Le premier ENTRY démarre le trade.
    Resultat = premier évènement parmi {TP3, TP2, TP1, SL} observé après l'ENTRY.
    """
    rows = load_all_events_for_analysis()
    by_tid: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        tid = r["trade_id"]
        if not tid:
            # Pas de trade_id => ignore (ou groupe par symbol+time si tu veux)
            # On peut aussi créer un trade_id synthétique:
            tid = f"noid:{r['symbol']}:{r['received_at']}"
        by_tid[tid].append(r)

    trades: List[Dict[str, Any]] = []
    # Counters
    total = 0
    wins = 0
    losses = 0
    hit_tp1 = hit_tp2 = hit_tp3 = 0
    times_to_outcome: List[int] = []

    for tid, items in by_tid.items():
        # Ordre chronologique déjà asc
        entry = None
        outcome_type = TradeOutcome.NONE
        outcome_time = None
        side = None
        symbol = None
        tf = None
        e_entry = e_sl = e_tp1 = e_tp2 = e_tp3 = None
        entry_time = None

        for ev in items:
            etype = ev["type"]
            if etype == "ENTRY" and entry is None:
                entry = ev
                symbol = ev["symbol"]
                tf = ev["tf"]
                side = ev["side"]
                e_entry = ev["entry"]
                e_sl = ev["sl"]
                e_tp1 = ev["tp1"]
                e_tp2 = ev["tp2"]
                e_tp3 = ev["tp3"]
                entry_time = ev["received_at"]
                # continue scanning for outcome
            elif entry is not None:
                # Determine outcome order: TP3 > TP2 > TP1 > SL, but we pick the FIRST event time we encounter
                if etype in ("TP3_HIT", "TP2_HIT", "TP1_HIT", "SL_HIT") and outcome_type == TradeOutcome.NONE:
                    outcome_type = etype
                    outcome_time = ev["received_at"]

        # register only trades that had an ENTRY
        if entry is not None:
            total += 1
            if outcome_time and entry_time:
                times_to_outcome.append(int(outcome_time - entry_time))
            # win/lose
            if outcome_type in ("TP3_HIT", "TP2_HIT", "TP1_HIT"):
                wins += 1
                if outcome_type == "TP1_HIT":
                    hit_tp1 += 1
                elif outcome_type == "TP2_HIT":
                    hit_tp2 += 1
                elif outcome_type == "TP3_HIT":
                    hit_tp3 += 1
            elif outcome_type == "SL_HIT":
                losses += 1

            trades.append(
                {
                    "trade_id": tid,
                    "symbol": symbol,
                    "tf": tf,
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
    }
    return trades, summary

# =========================
# Telegram (optionnel)
# =========================
def send_telegram(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        import urllib.request
        import urllib.parse
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        ).encode()
        req = urllib.request.Request(api_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except Exception:
        return False

# =========================
# HTML templates (Template)
# =========================
INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #111827;
    --text: #e5e7eb;
    --muted: #94a3b8;
    --green: #10b981;
    --red: #ef4444;
    --blue: #3b82f6;
    --yellow: #f59e0b;
    --border: #1f2937;
    --chip-bg: #0b1220;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; padding:24px; background:var(--bg); color:var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
  }
  h1 { margin: 0 0 16px 0; font-size: 28px; font-weight: 700; letter-spacing: .2px; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 960px) { .grid { grid-template-columns: 1fr 1fr; } }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 16px 8px 16px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.25);
  }
  .title { font-size: 16px; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  table {
    width: 100%; border-collapse: collapse; font-size: 14px;
  }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { text-align: left; color: var(--muted); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  .btn {
    display: inline-block; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border);
    background: #0b1220; color: var(--text); text-decoration: none; font-weight: 600; margin-right: 8px;
  }
  .btn:hover { background: #0f1525; }
  .chip { display:inline-block; padding:2px 8px; border:1px solid var(--border); border-radius:999px; margin-right:8px; background:var(--chip-bg); }
  .muted { color: var(--muted); }
  .row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .cta-row { margin-top: 10px; }
</style>
</head>
<body>
<h1>AI Trader PRO - Status</h1>

<div class="grid">
  <div class="card">
    <div class="title">Environment</div>
    <table>
      <thead><tr><th>Key</th><th>Value</th></tr></thead>
      <tbody>
        $rows_html
      </tbody>
    </table>
    <div class="cta-row">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
      <a class="btn" href="/openai-health">/openai-health</a>
      <a class="btn" href="/trades">/trades</a>
    </div>
  </div>

  <div class="card">
    <div class="title">Webhook</div>
    <div>POST <code>/tv-webhook</code> with JSON (TradingView).</div>
    <div class="muted">Secret can be passed as ?secret=... or in JSON body "secret".</div>
    <div style="margin-top:8px" class="row">
      <span class="chip">ENTRY</span>
      <span class="chip">TP1_HIT</span>
      <span class="chip">TP2_HIT</span>
      <span class="chip">TP3_HIT</span>
      <span class="chip">SL_HIT</span>
      <span class="chip">AOE_PREMIUM</span>
      <span class="chip">AOE_DISCOUNT</span>
    </div>
  </div>
</div>

</body>
</html>
""")

TRADES_HTML_TPL = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Trades</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #111827;
    --text: #e5e7eb;
    --muted: #94a3b8;
    --green: #10b981;
    --red: #ef4444;
    --blue: #3b82f6;
    --yellow: #f59e0b;
    --border: #1f2937;
    --chip-bg: #0b1220;
  }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text); font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; }
  h1 { margin: 0 0 16px 0; font-size: 28px; font-weight: 700; }
  .grid { display:grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 1100px) { .grid { grid-template-columns: 360px 1fr; } }
  .card { background: var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; box-shadow: 0 4px 14px rgba(0,0,0,0.25); }
  .title { font-size: 16px; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  .kpi {
    display:grid; grid-template-columns: repeat(2,1fr); gap:8px; margin-top:6px;
  }
  .kpi .item { background:#0b1220; border:1px solid var(--border); border-radius:10px; padding:10px; }
  .kpi .label { color: var(--muted); font-size:12px; }
  .kpi .value { font-size:22px; font-weight:700; }
  .kpi .green { color: var(--green); } .kpi .red { color: var(--red); } .kpi .blue { color: var(--blue); } .kpi .yellow { color: var(--yellow); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { text-align:left; color: var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:none; }
  .chip { display:inline-block; padding:2px 8px; border:1px solid var(--border); border-radius:999px; background:var(--chip-bg); }
  .badge-win { color:#10b981; border-color:#0f5132; }
  .badge-loss { color:#ef4444; border-color:#5c1e1e; }
  .muted { color: var(--muted); }
</style>
</head>
<body>
<h1>AI Trader PRO - Trades</h1>
<div class="grid">
  <div class="card">
    <div class="title">Summary</div>
    <div class="kpi">
      <div class="item">
        <div class="label">Total</div>
        <div class="value">$total_trades</div>
      </div>
      <div class="item">
        <div class="label">Winrate</div>
        <div class="value green">$winrate_pct%</div>
      </div>
      <div class="item">
        <div class="label">Wins</div>
        <div class="value green">$wins</div>
      </div>
      <div class="item">
        <div class="label">Losses</div>
        <div class="value red">$losses</div>
      </div>
      <div class="item">
        <div class="label">TP1 hits</div>
        <div class="value blue">$tp1_hits</div>
      </div>
      <div class="item">
        <div class="label">TP2 hits</div>
        <div class="value blue">$tp2_hits</div>
      </div>
      <div class="item">
        <div class="label">TP3 hits</div>
        <div class="value yellow">$tp3_hits</div>
      </div>
      <div class="item">
        <div class="label">Avg time to outcome</div>
        <div class="value">$avg_time_to_outcome_sec s</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="title">Recent trades</div>
    <table>
      <thead>
        <tr>
          <th>Trade ID</th>
          <th>Symbol</th>
          <th>TF</th>
          <th>Side</th>
          <th>Entry</th>
          <th>SL</th>
          <th>TP1</th>
          <th>TP2</th>
          <th>TP3</th>
          <th>Outcome</th>
          <th>Duration (s)</th>
        </tr>
      </thead>
      <tbody>
        $rows_html
      </tbody>
    </table>
    <div class="muted">Showing up to $limit trades (grouped by trade_id).</div>
  </div>
</div>
</body>
</html>
""")

# =========================
# FastAPI app
# =========================
app = FastAPI(title="AI Trader PRO")

# ------------- Index -------------
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
    ]
    trs = "".join([f"<tr><td>{k}</td><td>{v}</td></tr>" for (k, v) in rows])
    html = INDEX_HTML_TPL.substitute(rows_html=trs)
    return HTMLResponse(html)

# ------------- Env sanity -------------
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
    }

# ------------- Telegram health -------------
@app.get("/tg-health")
def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    ok = send_telegram("Test Telegram: OK")
    return {"ok": ok}

# ------------- OpenAI health -------------
@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        # minimal ping
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=2,
        )
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ------------- Webhook TradingView -------------
@app.post("/tv-webhook")
async def tv_webhook(request: Request, secret: Optional[str] = Query(None)):
    """
    Reçoit les alertes JSON TradingView.
    Secret accepté en query (?secret=...) ou dans le JSON ("secret": "...").
    Stocke l'évènement en base.
    """
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON must be an object")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    body_secret = payload.get("secret")
    if WEBHOOK_SECRET:
        if secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # enregistrer
    save_event(payload)

    # (optionnel) Telegram résumé succinct
    try:
        t = payload.get("type", "EVENT")
        sym = payload.get("symbol", "?")
        tf = payload.get("tf", "?")
        msg = f"[TV] {t} | {sym} | TF {tf}"
        send_telegram(msg)
    except Exception:
        pass

    return {"ok": True}

# ------------- Trades JSON brut -------------
@app.get("/trades.json")
def trades_json(secret: Optional[str] = Query(None), limit: int = 100):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    trades, summary = build_trades()
    # renvoyer jusqu'au "limit" derniers selon l'ordre inverse d'insertion
    resp = {
        "summary": summary,
        "trades": trades[-limit:] if limit else trades
    }
    return JSONResponse(resp)

# ------------- Trades (HTML) -------------
@app.get("/trades", response_class=HTMLResponse)
def trades(secret: Optional[str] = Query(None), limit: int = 100):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    trades, summary = build_trades()
    # Construire lignes
    rows_html = ""
    # afficher les plus récents en bas du tableau? on garde l'ordre d'assemblage
    for tr in trades[-limit:]:
        badge_class = "badge-win" if tr["outcome"] in ("TP1_HIT", "TP2_HIT", "TP3_HIT") else ("badge-loss" if tr["outcome"] == "SL_HIT" else "")
        outcome_html = f'<span class="chip {badge_class}">{tr["outcome"] or "NONE"}</span>'
        rows_html += (
            "<tr>"
            f"<td>{escape_html(tr['trade_id'])}</td>"
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
        total_trades=summary["total_trades"],
        winrate_pct=summary["winrate_pct"],
        wins=summary["wins"],
        losses=summary["losses"],
        tp1_hits=summary["tp1_hits"],
        tp2_hits=summary["tp2_hits"],
        tp3_hits=summary["tp3_hits"],
        avg_time_to_outcome_sec=summary["avg_time_to_outcome_sec"],
        rows_html=rows_html or '<tr><td colspan="11" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
        limit=limit
    )
    return HTMLResponse(html)

# ------------- Alias: /trades/secret=... -> redirect -------------
@app.get("/trades/secret={secret}")
def trades_alias(secret: str):
    return RedirectResponse(url=f"/trades?secret={secret}", status_code=307)

# ============ Helpers ============
def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )

def fmt_num(v) -> str:
    try:
        if v is None:
            return ""
        # compact formatting
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "")

# ============ Run (local) ============
# For Render, use: gunicorn -k uvicorn.workers.UvicornWorker -w 1 main:app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
