# main.py
import os
import json
import time
from typing import Optional, Union, Dict, Any, List, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field

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
app = FastAPI(title="AI Trader PRO - Webhook + Dashboard", version="2.2.0")

# =========================
# MODELS
# =========================
Number = Optional[Union[float, int, str]]
EventType = Literal["ENTRY", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "TRADE_TERMINATED"]

class TVPayload(BaseModel):
    """
    JSON attendu depuis TradingView (aligné à ton indicateur):
    {
      "type": "ENTRY" | "TP1_HIT" | "TP2_HIT" | "TP3_HIT" | "SL_HIT" | "TRADE_TERMINATED",
      "symbol": "BTCUSDT",
      "tf": "15",
      "time": 1717777777,
      "side": "LONG" | "SHORT",         # seulement pour ENTRY
      "entry": 67000.12,                # prix d'insert (ENTRY) ou dernier prix (events)
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
    type: EventType
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
        extra = "allow"  # tolère des champs supplémentaires


# =========================
# IN-MEMORY STORE (volatile)
# =========================
# Render a un système de fichiers éphémère : on garde en mémoire pendant le runtime.
# Structure :
# TRADES: { trade_id: { ...infos d'entrée..., status, timestamps hits, events[] } }
TRADES: Dict[str, Dict[str, Any]] = {}
EVENTS: List[Dict[str, Any]] = []  # historique brut des évènements pour debug/stats

def _now_ms() -> int:
    return int(time.time() * 1000)

def _ensure_trade_id(p: TVPayload) -> str:
    if p.trade_id:
        return p.trade_id
    # fallback si l’indicateur n’envoie pas de trade_id
    # unique-ish: SYMBOL-TF-UNIX
    return f"{p.symbol}-{p.tf}-{p.time}"

def _status_after_event(ev_type: EventType, current: str) -> str:
    # Règle simple : SL_HIT ou TRADE_TERMINATED -> CLOSED
    # TP1/TP2 -> PARTIAL si pas déjà CLOSED
    # TP3 -> CLOSED
    if ev_type in ("SL_HIT", "TRADE_TERMINATED", "TP3_HIT"):
        return "CLOSED"
    if ev_type in ("TP1_HIT", "TP2_HIT"):
        return "PARTIAL" if current != "CLOSED" else "CLOSED"
    return current

def _record_entry(p: TVPayload):
    tid = _ensure_trade_id(p)
    TRADES[tid] = {
        "trade_id": tid,
        "symbol": p.symbol,
        "tf": p.tf,
        "time": p.time,
        "side": (p.side or "").upper(),
        "entry": p.entry,
        "sl": p.sl,
        "tp1": p.tp1,
        "tp2": p.tp2,
        "tp3": p.tp3,
        "r1": p.r1,
        "s1": p.s1,
        "status": "OPEN",
        # events/hits
        "tp1_hit_time": None,
        "tp2_hit_time": None,
        "tp3_hit_time": None,
        "sl_hit_time": None,
        "terminated_time": None,
        "last_event_time": p.time,
        "events": [{"type": "ENTRY", "t": p.time, "price": p.entry}],
    }

def _record_event(p: TVPayload):
    tid = _ensure_trade_id(p)
    if tid not in TRADES and p.type == "ENTRY":
        _record_entry(p)
        return

    if tid not in TRADES and p.type != "ENTRY":
        # On crée une coquille minimale pour ne pas 422 côté TV si évènement tardif
        TRADES[tid] = {
            "trade_id": tid,
            "symbol": p.symbol,
            "tf": p.tf,
            "time": p.time,
            "side": (p.side or "").upper(),
            "entry": p.entry,
            "sl": p.sl,
            "tp1": p.tp1,
            "tp2": p.tp2,
            "tp3": p.tp3,
            "r1": p.r1,
            "s1": p.s1,
            "status": "OPEN",
            "tp1_hit_time": None,
            "tp2_hit_time": None,
            "tp3_hit_time": None,
            "sl_hit_time": None,
            "terminated_time": None,
            "last_event_time": p.time,
            "events": [],
        }

    tr = TRADES[tid]
    tr["last_event_time"] = p.time
    tr["events"].append({"type": p.type, "t": p.time, "price": p.entry})

    if p.type == "TP1_HIT":
        tr["tp1_hit_time"] = tr.get("tp1_hit_time") or p.time
    elif p.type == "TP2_HIT":
        tr["tp2_hit_time"] = tr.get("tp2_hit_time") or p.time
    elif p.type == "TP3_HIT":
        tr["tp3_hit_time"] = tr.get("tp3_hit_time") or p.time
    elif p.type == "SL_HIT":
        tr["sl_hit_time"] = tr.get("sl_hit_time") or p.time
    elif p.type == "TRADE_TERMINATED":
        tr["terminated_time"] = tr.get("terminated_time") or p.time

    tr["status"] = _status_after_event(p.type, tr.get("status", "OPEN"))
    TRADES[tid] = tr

    # Historique global
    EVENTS.append({
        "trade_id": tid,
        "type": p.type,
        "symbol": p.symbol,
        "tf": p.tf,
        "t": p.time,
        "price": p.entry,
    })

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
    """Envoie un message Telegram si BOT + CHAT_ID configurés (non bloquant)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            # on ne casse pas le webhook si Telegram échoue
            pass

# =========================
# ROUTES — STATUS / HOME
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/favicon.ico")
def favicon():
    # supprime le bruit 404 favicon
    return HTMLResponse(status_code=204)

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
      <a class="btn" href="/docs">/docs</a>
      <a class="btn" href="/trades">/trades (dashboard)</a>
    </div>
  </div>
  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
    <small>Rappel: <code>GET /tv-webhook</code> renverra 405 — c'est normal, seule la méthode POST est autorisée.</small>
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
# ROUTE — TV WEBHOOK
# =========================
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 1) Sécurité: secret
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) Log simple (console)
    print("[tv-webhook] payload:", payload.dict())

    # 3) Enregistre en mémoire
    if payload.type == "ENTRY":
        _record_entry(payload)
    else:
        _record_event(payload)

    # 4) Compose message Telegram
    t = payload.type
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{_ensure_trade_id(payload)}</code>"

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
        msg = (
            f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Prix: <b>{_fmt_num(payload.entry)}</b>"
        )
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
        # Message demandé : “TRADE TERMINÉ — VEUILLEZ FERMER …”
        msg = (
            f"⏹ <b>TRADE TERMINÉ — VEUILLEZ FERMER</b>\n"
            f"Instrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{trade_id_txt}"
        )
        await send_telegram(msg)

    # 5) Réponse API
    tid = _ensure_trade_id(payload)
    return JSONResponse(
        {
            "ok": True,
            "received": payload.dict(),
            "trade_id": tid,
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )

# =========================
# ROUTES — DASHBOARD
# =========================
@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    # dashboard minimal HTML + JS (fetch /trades-data & /trades-stats)
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>AI Trader PRO — Trades</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.4;margin:20px;color:#111;background:#fafafa}
    .row{display:flex;gap:14px;flex-wrap:wrap}
    .card{border:1px solid #e5e7eb;border-radius:12px;padding:14px;background:#fff}
    .stat{min-width:180px}
    table{border-collapse:collapse;width:100%;font-size:14px}
    th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid #e5e7eb}
    .pill.open{background:#ecfdf5;border-color:#10b981}
    .pill.partial{background:#fff7ed;border-color:#f59e0b}
    .pill.closed{background:#fef2f2;border-color:#ef4444}
    .sideL{color:#10b981;font-weight:600}
    .sideS{color:#ef4444;font-weight:600}
    .muted{color:#6b7280}
    .small{font-size:12px}
    .nowrap{white-space:nowrap}
  </style>
</head>
<body>
  <h1>AI Trader PRO — Trades</h1>
  <div class="row">
    <div class="card stat">
      <div id="stat_total" class="big">—</div>
      <div class="muted small">Total trades</div>
    </div>
    <div class="card stat">
      <div id="stat_open" class="big">—</div>
      <div class="muted small">Ouverts</div>
    </div>
    <div class="card stat">
      <div id="stat_closed" class="big">—</div>
      <div class="muted small">Fermés</div>
    </div>
    <div class="card stat">
      <div id="stat_win" class="big">—</div>
      <div class="muted small">TP3 (victoires)</div>
    </div>
    <div class="card stat">
      <div id="stat_loss" class="big">—</div>
      <div class="muted small">SL (pertes)</div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <table id="tbl"><thead>
      <tr>
        <th>Heure</th>
        <th>Symbole</th>
        <th>TF</th>
        <th>Side</th>
        <th>Entry</th>
        <th>SL</th>
        <th>TP1</th>
        <th>TP2</th>
        <th>TP3</th>
        <th>Status</th>
        <th class="nowrap">Dernier évènement</th>
        <th>ID</th>
      </tr>
    </thead><tbody id="rows"></tbody></table>
  </div>

  <script>
  async function fetchJSON(url){
    const r = await fetch(url);
    if(!r.ok) throw new Error(url+" => "+r.status);
    return r.json();
  }
  function fmtN(x){
    if(x===null || x===undefined) return "-";
    const v = Number(x);
    if(!isFinite(v)) return String(x);
    return (v < 1 ? v.toFixed(8) : v.toFixed(4));
  }
  function tsToTime(t){
    try{
      const d = new Date((String(t).length>10? t : t*1000));
      return d.toLocaleString();
    }catch(e){ return String(t); }
  }
  function pill(s){
    const st = (s||"").toUpperCase();
    if(st==="OPEN") return '<span class="pill open">OPEN</span>';
    if(st==="PARTIAL") return '<span class="pill partial">PARTIAL</span>';
    if(st==="CLOSED") return '<span class="pill closed">CLOSED</span>';
    return '<span class="pill">'+(s||"-")+'</span>';
  }
  function side(s){
    s = (s||"").toUpperCase();
    if(s==="LONG") return '<span class="sideL">LONG</span>';
    if(s==="SHORT") return '<span class="sideS">SHORT</span>';
    return '—';
  }

  async function refresh(){
    try{
      const stats = await fetchJSON("/trades-stats");
      document.getElementById("stat_total").textContent = stats.total;
      document.getElementById("stat_open").textContent = stats.open;
      document.getElementById("stat_closed").textContent = stats.closed;
      document.getElementById("stat_win").textContent = stats.win_tp3;
      document.getElementById("stat_loss").textContent = stats.loss_sl;

      const data = await fetchJSON("/trades-data");
      const tbody = document.getElementById("rows");
      tbody.innerHTML = "";
      data.trades.sort((a,b)=> (b.time||0)-(a.time||0)).forEach(tr=>{
        const trEl = document.createElement("tr");
        trEl.innerHTML = `
          <td class="nowrap">${tsToTime(tr.time)}</td>
          <td>${tr.symbol||"-"}</td>
          <td>${tr.tf||"-"}</td>
          <td>${side(tr.side)}</td>
          <td>${fmtN(tr.entry)}</td>
          <td>${fmtN(tr.sl)}</td>
          <td>${fmtN(tr.tp1)}</td>
          <td>${fmtN(tr.tp2)}</td>
          <td>${fmtN(tr.tp3)}</td>
          <td>${pill(tr.status)}</td>
          <td class="nowrap">${tsToTime(tr.last_event_time||tr.time)}</td>
          <td class="small muted">${tr.trade_id||"-"}</td>
        `;
        tbody.appendChild(trEl);
      });
    }catch(e){
      console.error(e);
    }
  }
  refresh();
  setInterval(refresh, 10000);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=200)

@app.get("/trades-data")
def trades_data(secret: Optional[str] = Query(None)):
    # (Optionnel) protéger /trades-data derrière le secret :
    # if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
    #     raise HTTPException(status_code=401, detail="Invalid secret")
    return {"trades": list(TRADES.values()), "events_count": len(EVENTS)}

@app.get("/trades-stats")
def trades_stats(secret: Optional[str] = Query(None)):
    # if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
    #     raise HTTPException(status_code=401, detail="Invalid secret")
    total = len(TRADES)
    open_cnt = sum(1 for t in TRADES.values() if t.get("status") == "OPEN")
    closed_cnt = sum(1 for t in TRADES.values() if t.get("status") == "CLOSED")
    win_tp3 = sum(1 for t in TRADES.values() if t.get("tp3_hit_time"))
    loss_sl = sum(1 for t in TRADES.values() if t.get("sl_hit_time"))
    return {
        "total": total,
        "open": open_cnt,
        "closed": closed_cnt,
        "win_tp3": win_tp3,
        "loss_sl": loss_sl,
    }
