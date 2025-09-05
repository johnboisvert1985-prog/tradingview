# main.py
import os
import json
from typing import Optional, Union, Dict, Any, List
from collections import deque

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

# =========================
# ENV
# =========================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # ex: nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "8000"))

# =========================
# APP
# =========================
app = FastAPI(title="AI Trader PRO - Webhook", version="2.1.0")

# =========================
# MODELS
# =========================
Number = Optional[Union[float, int, str]]

class TVPayload(BaseModel):
    """
    Aligné avec ton indicateur (version simple) :
    {
      "type": "ENTRY" | "TP1_HIT" | "TP2_HIT" | "TP3_HIT" | "SL_HIT" | "TRADE_TERMINATED",
      "symbol": "BTCUSDT",
      "tf": "15",
      "time": 1717777777,
      "side": "LONG" | "SHORT",           # pour ENTRY
      "entry": 67000.12,                  # prix (ENTRY ou event)
      "sl": 64000.0,
      "tp1": 68000.0,
      "tp2": 69000.0,
      "tp3": 70000.0,
      "r1": 70500.0,
      "s1": 66500.0,
      "secret": "....",
      "trade_id": "abc-123"
    }
    """
    type: str
    symbol: str
    tf: str
    time: int
    side: Optional[str] = None
    entry: Number = None
    sl: Number = None
    tp1: Number = None
    tp2: Number = None
    tp3: Number = None
    r1: Number = None
    s1: Number = None
    secret: Optional[str] = None
    trade_id: Optional[str] = None

    class Config:
        extra = "allow"  # tolère des champs en plus

# =========================
# STORAGE (in-memory)
# =========================
MAX_EVENTS = 5000
EVENTS: deque = deque(maxlen=MAX_EVENTS)      # journal brut des évènements
TRADES: Dict[str, Dict[str, Any]] = {}        # trade_id -> état du trade

def _safe_float(x: Number) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

def _get_tid(p: TVPayload) -> str:
    # Toujours avoir un ID exploitable
    return p.trade_id or f"{p.symbol}-{p.tf}-{p.time}"

def _risk_multiple(side: Optional[str], entry: Optional[float], sl: Optional[float], exit_price: Optional[float]) -> Optional[float]:
    if side is None or entry is None or sl is None or exit_price is None:
        return None
    side = side.upper()
    if side == "LONG":
        risk = entry - sl
        if risk <= 0:
            return None
        return (exit_price - entry) / risk
    elif side == "SHORT":
        risk = sl - entry
        if risk <= 0:
            return None
        return (entry - exit_price) / risk
    return None

def _update_trade_state(p: TVPayload):
    """
    Met à jour TRADES selon l'évènement reçu.
    """
    tid = _get_tid(p)
    t = p.type.upper()
    side = (p.side or "").upper() if p.side else None
    entry = _safe_float(p.entry)
    sl = _safe_float(p.sl)
    tp1 = _safe_float(p.tp1)
    tp2 = _safe_float(p.tp2)
    tp3 = _safe_float(p.tp3)

    if t == "ENTRY":
        TRADES[tid] = {
            "trade_id": tid,
            "symbol": p.symbol,
            "tf": p.tf,
            "open_time": p.time,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "r1": _safe_float(p.r1), "s1": _safe_float(p.s1),
            "status": "open",           # open | closed
            "outcome": None,            # win/loss/terminated
            "exit_time": None,
            "exit_price": None,
            "best_tp": 0,               # 0,1,2,3 (meilleur TP atteint)
            "best_r": None,             # R multiple au meilleur TP
            "events": []
        }

    # Si pas d'ENTRY préalable, on initialise un shell minimal
    if tid not in TRADES:
        TRADES[tid] = {
            "trade_id": tid,
            "symbol": p.symbol,
            "tf": p.tf,
            "open_time": p.time,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "r1": _safe_float(p.r1), "s1": _safe_float(p.s1),
            "status": "open",
            "outcome": None,
            "exit_time": None,
            "exit_price": None,
            "best_tp": 0,
            "best_r": None,
            "events": []
        }

    trade = TRADES[tid]
    trade["events"].append({"type": t, "time": p.time, "price": entry})

    # Propagation éventuelle des niveaux (si re­envoyés)
    if sl is not None:  trade["sl"]  = sl
    if tp1 is not None: trade["tp1"] = tp1
    if tp2 is not None: trade["tp2"] = tp2
    if tp3 is not None: trade["tp3"] = tp3
    if side:            trade["side"] = side
    if entry is not None and trade.get("entry") is None:
        trade["entry"] = entry

    # Gestion des événements
    if t in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        k = 1 if t == "TP1_HIT" else (2 if t == "TP2_HIT" else 3)
        if k > trade["best_tp"]:
            trade["best_tp"] = k
            tp_price = trade.get(f"tp{k}")
            # Calcule le R multiple au TPk (si possible)
            trade["best_r"] = _risk_multiple(trade["side"], trade["entry"], trade["sl"], tp_price if tp_price else entry)

    elif t == "SL_HIT":
        trade["status"] = "closed"
        trade["outcome"] = "loss"
        trade["exit_time"] = p.time
        trade["exit_price"] = entry
        trade["best_r"] = _risk_multiple(trade["side"], trade["entry"], trade["sl"], entry)

    elif t == "TRADE_TERMINATED":
        trade["status"] = "closed"
        trade["exit_time"] = p.time
        # Si TP touché au préalable -> win, sinon "terminated"
        if trade["best_tp"] >= 1:
            trade["outcome"] = "win"
            # Place la sortie sur le meilleur TP connu si on l’a
            best_tp_price = trade.get(f"tp{trade['best_tp']}")
            trade["exit_price"] = best_tp_price if best_tp_price is not None else entry
        else:
            trade["outcome"] = "terminated"
            trade["exit_price"] = entry

def _compute_stats() -> Dict[str, Any]:
    total = len(TRADES)
    closed = sum(1 for t in TRADES.values() if t["status"] == "closed")
    wins = sum(1 for t in TRADES.values() if t.get("outcome") == "win" or t.get("best_tp", 0) >= 1 and t["status"] == "closed")
    losses = sum(1 for t in TRADES.values() if t.get("outcome") == "loss")
    termi = sum(1 for t in TRADES.values() if t.get("outcome") == "terminated")

    # R multiples fermés
    r_values: List[float] = []
    for t in TRADES.values():
        if t["status"] == "closed" and t.get("best_r") is not None:
            try:
                r_values.append(float(t["best_r"]))
            except Exception:
                pass

    winrate = (wins / max(1, (wins + losses))) * 100.0
    avg_r = (sum(r_values) / len(r_values)) if r_values else 0.0
    best_r = max(r_values) if r_values else 0.0
    worst_r = min(r_values) if r_values else 0.0

    tp1_hits = sum(1 for t in TRADES.values() if t.get("best_tp", 0) >= 1)
    tp2_hits = sum(1 for t in TRADES.values() if t.get("best_tp", 0) >= 2)
    tp3_hits = sum(1 for t in TRADES.values() if t.get("best_tp", 0) >= 3)

    return {
        "total_trades": total,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "terminated": termi,
        "winrate_percent": round(winrate, 2),
        "avg_r_closed": round(avg_r, 3),
        "best_r": round(best_r, 3),
        "worst_r": round(worst_r, 3),
        "tp1_or_better": tp1_hits,
        "tp2_or_better": tp2_hits,
        "tp3_or_better": tp3_hits,
    }

# =========================
# HELPERS
# =========================
def _mask(val: Optional[str]) -> str:
    if not val:
        return "missing"
    return (val[:6] + "..." + val[-4:]) if len(val) > 12 else "***"

def _fmt_num(x: Number) -> str:
    if x is None:
        return "-"
    try:
        xf = float(x)
        return f"{xf:.8f}" if xf < 1 else f"{xf:.4f}"
    except Exception:
        return str(x)

async def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            pass

# =========================
# ROUTES — STATUS
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home():
    env_rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("PORT", str(PORT)),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'><code>{v}</code></td></tr>"
        for k, v in env_rows
    )
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>AI Trader PRO — Status</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.5;margin:20px;color:#111}}
    .card{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:14px 0}}
    .btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111;margin-right:8px}}
    table{{border-collapse:collapse;width:100%;font-size:14px}}
    code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
  </style>
</head>
<body>
  <h1>AI Trader PRO — Status</h1>
  <div class="card">
    <b>Environnement</b>
    <table>{rows_html}</table>
    <div style="margin-top:10px">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
      <a class="btn" href="/trades">/trades (dashboard)</a>
    </div>
  </div>
  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
  </div>
</body>
</html>
"""

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "PORT": PORT,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: ça fonctionne.")
    return {"ok": True, "info": "Message Telegram envoyé (si BOT + CHAT_ID configurés)."}

# =========================
# ROUTE — WEBHOOK
# =========================
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 1) Sécurité
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) Log
    print("[tv-webhook] payload:", payload.dict())

    # 3) Mémorise l'évènement pour le dashboard
    EVENTS.append({"type": payload.type, "trade_id": _get_tid(payload), "time": payload.time, "symbol": payload.symbol, "tf": payload.tf, "price": _safe_float(payload.entry)})
    _update_trade_state(payload)

    # 4) Telegram
    t = payload.type.upper()
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    if t == "ENTRY":
        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{(payload.side or '—').upper()}</b> | Entry: <b>{_fmt_num(payload.entry)}</b>\n"
            f"🎯 SL: <b>{_fmt_num(payload.sl)}</b> | "
            f"TP1: <b>{_fmt_num(payload.tp1)}</b> | "
            f"TP2: <b>{_fmt_num(payload.tp2)}</b> | "
            f"TP3: <b>{_fmt_num(payload.tp3)}</b>\n"
            f"R1: <b>{_fmt_num(payload.r1)}</b>  •  S1: <b>{_fmt_num(payload.s1)}</b>"
        )
        await send_telegram(msg)

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)
        msg = f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\nPrix: <b>{_fmt_num(payload.entry)}</b>"
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
        msg = f"⏹ <b>TRADE TERMINÉ — VEUILLEZ FERMER</b>\nInstrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{trade_id_txt}"
        await send_telegram(msg)

    # 5) Réponse API
    return JSONResponse(
        {
            "ok": True,
            "received": payload.dict(),
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )

# =========================
# ROUTES — DASHBOARD TRADES
# =========================
@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    # Simple dashboard HTML (vanilla JS) lisant /trades-stats et /trades-data
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Trades & Stats</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;margin:18px;color:#111}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
    .card{{border:1px solid #e5e7eb;border-radius:12px;padding:12px}}
    .muted{{color:#6b7280}}
    table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:12px}}
    th,td{{border-bottom:1px solid #eee;padding:8px;text-align:left;white-space:nowrap}}
    code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
    .pill{{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #ddd;font-size:12px}}
    .ok{{background:#e8faf0}}
    .bad{{background:#fde8e8}}
    .warn{{background:#fff7e6}}
  </style>
</head>
<body>
  <h2>📈 Trades & Statistiques</h2>
  <div id="stats" class="grid" style="margin:12px 0"></div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <b>Historique des trades</b>
      <button id="refresh">Rafraîchir</button>
    </div>
    <div class="muted">Agrégation en mémoire depuis les évènements reçus par <code>/tv-webhook</code>.</div>
    <div style="overflow:auto">
      <table id="tbl">
        <thead>
          <tr>
            <th>Ouverture</th>
            <th>ID</th>
            <th>Symbole</th>
            <th>TF</th>
            <th>Side</th>
            <th>Entry</th>
            <th>SL</th>
            <th>TP1</th><th>TP2</th><th>TP3</th>
            <th>Best TP</th>
            <th>Status</th>
            <th>Outcome</th>
            <th>Best R</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

<script>
async function loadStats(){
  const res = await fetch('/trades-stats');
  const s = await res.json();
  const box = (label,val,cls="") => `
    <div class="card ${cls}">
      <div class="muted">${label}</div>
      <div style="font-size:18px;font-weight:600">${val}</div>
    </div>`;
  document.getElementById('stats').innerHTML =
    box("Total trades", s.total_trades) +
    box("Fermés", s.closed_trades) +
    box("Wins", s.wins, "ok") +
    box("Losses", s.losses, "bad") +
    box("Terminés", s.terminated, "warn") +
    box("Winrate", s.winrate_percent + "%") +
    box("Avg R (fermés)", s.avg_r_closed) +
    box("Best R", s.best_r) +
    box("Worst R", s.worst_r) +
    box("≥ TP1", s.tp1_or_better) +
    box("≥ TP2", s.tp2_or_better) +
    box("≥ TP3", s.tp3_or_better);
}
function fmtTs(ts){
  try{{ return new Date(ts*1000).toLocaleString(); }}catch(e){{ return ts; }}
}
function pill(txt, kind){
  const cls = kind==="open"?"warn":(kind==="win"?"ok":(kind==="loss"?"bad":""));
  return `<span class="pill ${cls}">${txt}</span>`;
}
async function loadTrades(){
  const res = await fetch('/trades-data');
  const data = await res.json();
  const tbody = document.querySelector('#tbl tbody');
  tbody.innerHTML = (data.trades || []).map(t => `
    <tr>
      <td>${fmtTs(t.open_time)}</td>
      <td><code>${t.trade_id}</code></td>
      <td>${t.symbol}</td>
      <td>${t.tf}</td>
      <td>${t.side || '-'}</td>
      <td>${t.entry ?? '-'}</td>
      <td>${t.sl ?? '-'}</td>
      <td>${t.tp1 ?? '-'}</td>
      <td>${t.tp2 ?? '-'}</td>
      <td>${t.tp3 ?? '-'}</td>
      <td>${t.best_tp}</td>
      <td>${pill(t.status, t.status)}</td>
      <td>${t.outcome ? pill(t.outcome, t.outcome) : '-'}</td>
      <td>${t.best_r ?? '-'}</td>
    </tr>
  `).join('');
}

document.getElementById('refresh').addEventListener('click', () => {{ loadStats(); loadTrades(); }});
loadStats(); loadTrades();
</script>
</body>
</html>
""")

@app.get("/trades-data")
def trades_data(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    # renvoie les trades triés (du plus récent)
    trades = sorted(TRADES.values(), key=lambda x: x.get("open_time", 0), reverse=True)
    return {"trades": trades, "events_count": len(EVENTS)}

@app.get("/trades-stats")
def trades_stats(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return _compute_stats()
