# main.py
import os
import json
import time
import math
import queue
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from uvicorn import run as uvicorn_run

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

app = FastAPI(title="TradingView → Dashboard → Telegram")

# ========= In-memory ==========
class Trade(BaseModel):
    id: int
    created_at: str
    side: str
    symbol: str
    tf: Optional[str] = "-"
    entry: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl: Optional[float] = None
    direction: Optional[str] = None
    entry_time: Optional[str] = None
    alert_name: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None
    confidence: Optional[int] = None
    confidence_reason: Optional[List[str]] = None

class MarketState(BaseModel):
    fg: Optional[int] = 28
    btc_d: Optional[float] = 57.2
    mc_trillion: Optional[float] = 3.85
    btc_price: Optional[float] = 110_800.0
    last_update: Optional[str] = None

TRADES: List[Trade] = []
TRD_LOCK = threading.Lock()
COUNTER = 0
MARKET = MarketState(last_update=datetime.now(timezone.utc).isoformat())

# ========= Config ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
FG_API_URL = os.getenv("FG_API_URL", "")              # optionnel
GLOBAL_API_URL = os.getenv("GLOBAL_API_URL", "")      # optionnel (btc_d, mc_trillion, btc_price)
BTC_PRICE_API = os.getenv("BTC_PRICE_API", "")        # optionnel

# ========= Telegram worker (anti-429) ==========
TG_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()

def tg_worker():
    session = requests.Session()
    while True:
        item = TG_QUEUE.get()
        if item is None:
            break
        try:
            url = f"https://api.telegram.org/bot{item['token']}/sendMessage"
            payload = {
                "chat_id": item["chat_id"],
                "text": item["text"],
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            r = session.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                try:
                    ra = r.json().get("parameters", {}).get("retry_after", 5)
                except Exception:
                    ra = 5
                log.error(f"❌ Telegram: 429 - retry after {ra}s")
                time.sleep(int(ra))
                r = session.post(url, json=payload, timeout=10)
            if r.ok:
                log.info("✅ Telegram envoyé")
            else:
                log.error(f"❌ Telegram: {r.status_code} - {r.text}")
        except Exception as e:
            log.exception(f"❌ Telegram exception: {e}")
        finally:
            time.sleep(0.25)
            TG_QUEUE.task_done()

threading.Thread(target=tg_worker, daemon=True).start()

# ========= Helpers ==========
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def to_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).strip().replace(",", "")
        if s.upper() in {"", "-", "NA", "N/A"}:
            return None
        return float(s)
    except Exception:
        return None

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    if x >= 1:
        return f"${x:,.2f}"
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return "$" + s

# ========= Market refresh ==========
def refresh_market():
    updated = False
    session = requests.Session()
    try:
        if FG_API_URL:
            r = session.get(FG_API_URL, timeout=8)
            if r.ok:
                data = r.json()
                v = data.get("value") or data.get("fg") or data.get("index")
                if v is not None:
                    MARKET.fg = int(v)
                    updated = True
    except Exception:
        pass
    try:
        if GLOBAL_API_URL:
            r = session.get(GLOBAL_API_URL, timeout=8)
            if r.ok:
                data = r.json()
                if "btc_d" in data: MARKET.btc_d = float(data["btc_d"]); updated = True
                if "mc_trillion" in data: MARKET.mc_trillion = float(data["mc_trillion"]); updated = True
                if "btc_price" in data and data["btc_price"] is not None:
                    MARKET.btc_price = float(data["btc_price"]); updated = True
    except Exception:
        pass
    try:
        if BTC_PRICE_API and MARKET.btc_price is None:
            r = session.get(BTC_PRICE_API, timeout=8)
            if r.ok:
                MARKET.btc_price = float(r.json().get("price"))
                updated = True
    except Exception:
        pass
    if updated:
        MARKET.last_update = now_iso()

def market_refresher_loop():
    while True:
        try: refresh_market()
        except Exception: pass
        time.sleep(60)

threading.Thread(target=market_refresher_loop, daemon=True).start()

# ========= Confidence ==========
def compute_confidence(side: str, fg: Optional[int], btc_d: Optional[float]) -> (int, List[str]):
    notes = []
    score = 50
    s = (side or "").upper()

    if fg is not None:
        if fg <= 25:
            score += 10 if s == "BUY" else -3
            notes.append("✅ Fear extrême = zone d'achat idéale" if s == "BUY"
                         else "⚠️ Sentiment frileux : shorts moins évidents")
        elif fg <= 45:
            score += 3 if s == "BUY" else -2
            notes.append("✅ Sentiment frileux : léger avantage aux longs" if s == "BUY"
                         else "⚠️ Sentiment frileux : shorts moins évidents")
        elif fg >= 75:
            score += 8 if s == "SELL" else -5
            notes.append("⚠️ Euphorie: prudence sur les nouveaux longs" if s == "BUY"
                         else "✅ Euphorie: opportunités de short")
        else:
            notes.append("ℹ️ Sentiment neutre")

    if btc_d is not None:
        if btc_d >= 57.0:
            score += 2 if s == "SELL" else -5
            notes.append("⚠️ BTC trop dominant pour altcoins")
        elif btc_d <= 45.0 and s == "BUY":
            score += 3
            notes.append("✅ Dominance BTC modérée : altcoins plus libres")

    return max(0, min(100, score)), notes

def badge_text(v: int) -> str:
    if v >= 70: return f"{v}% (ÉLEVÉ)"
    if v >= 55: return f"{v}% (MOYEN)"
    return f"{v}% (FAIBLE)"

# ========= TP/SL auto ==========
def compute_levels(entry: Optional[float], side: str,
                   tp1: Optional[float], tp2: Optional[float], tp3: Optional[float],
                   sl: Optional[float]):
    if entry is None:
        return tp1, tp2, tp3, sl
    s = (side or "").upper()
    if tp1 is None or tp2 is None or tp3 is None:
        if s == "BUY":
            tp1 = tp1 if tp1 is not None else entry * 1.015
            tp2 = tp2 if tp2 is not None else entry * 1.025
            tp3 = tp3 if tp3 is not None else entry * 1.040
        else:
            tp1 = tp1 if tp1 is not None else entry * 0.985
            tp2 = tp2 if tp2 is not None else entry * 0.975
            tp3 = tp3 if tp3 is not None else entry * 0.960
    if sl is None:
        sl = entry * (0.980 if s == "BUY" else 1.020)
    return tp1, tp2, tp3, sl

# ========= Webhook parsing ==========
ALIASES = {
    "timeframe": "tf", "interval": "tf",
    "direction": "direction", "action": "side", "type": "type",
    "symbol": "symbol", "ticker": "symbol", "s": "symbol",
    "entry": "entry", "price": "entry", "p": "entry",
    "tp": "tp1", "tp1": "tp1", "tp2": "tp2", "tp3": "tp3",
    "sl": "sl", "stop": "sl",
    "entry_time": "entry_time", "time": "entry_time", "timenow": "entry_time",
    "alert_name": "alert_name", "name": "alert_name",
}
def norm_key(k: str) -> str:
    return ALIASES.get(k.strip().lower(), k.strip().lower())

def parse_text_payload(raw_text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    # essai JSON dans text/plain
    try:
        j = json.loads(raw_text)
        if isinstance(j, dict):
            for k, v in j.items():
                data[norm_key(str(k))] = v
            return data
    except Exception:
        pass
    # kv lignes
    for line in raw_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
        elif "=" in line:
            k, v = line.split("=", 1)
        else:
            continue
        data[norm_key(k)] = v.strip()
    return data

def deduce_side(side: Optional[str], direction: Optional[str]) -> Optional[str]:
    s = (side or "").strip().upper()
    if s in {"BUY", "SELL"}: return s
    d = (direction or "").strip().upper()
    if d in {"LONG", "BUY"}: return "BUY"
    if d in {"SHORT", "SELL"}: return "SELL"
    return None

def guess_symbol(d: Dict[str, Any]) -> Optional[str]:
    for k in ("symbol", "ticker", "s"):
        if d.get(k): return str(d.get(k)).strip().upper()
    return None

# ========= Telegram ==========
def send_telegram_entry(t: Trade):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    fg = MARKET.fg if MARKET.fg is not None else "-"
    btc_d = f"{MARKET.btc_d:.1f}%" if MARKET.btc_d is not None else "—"
    entry = fmt_money(t.entry)
    tp1, tp2, tp3, sl = fmt_money(t.tp1), fmt_money(t.tp2), fmt_money(t.tp3), fmt_money(t.sl)
    tf = t.tf or "-"
    direction = (t.direction or ("LONG" if (t.side or "").upper() == "BUY" else "SHORT")).upper()
    conf = t.confidence if t.confidence is not None else 50
    conf_txt = badge_text(conf)
    reasons = t.confidence_reason or []

    lines = []
    lines.append(f"🎯 NOUVEAU TRADE — {t.symbol}")
    lines.append("")
    lines.append(f"📊 {t.side.upper()}")
    lines.append(f"📈 Direction: {direction} | {tf}")
    if t.entry_time:
        lines.append(f"🕒 Heure: {t.entry_time}")
    lines.append("")
    lines.append(f"💰 Entry: {entry}")
    lines.append("")
    lines.append("🎯 Take Profits:")
    lines.append(f"  TP1: {tp1}")
    lines.append(f"  TP2: {tp2}")
    lines.append(f"  TP3: {tp3}")
    lines.append("")
    lines.append(f"🛑 Stop Loss: {sl}")
    lines.append("")
    lines.append(f"📊 CONFIANCE: {conf_txt}")
    if reasons:
        lines.append("")
        lines.append("Pourquoi ce score ?")
        for r in reasons[:3]:
            lines.append(f"  • {r}")
    lines.append("")
    lines.append(f"💡 Marché: F&G {fg} | BTC.D {btc_d}")

    TG_QUEUE.put({"token": TELEGRAM_BOT_TOKEN, "chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines).strip()})

# ========= Routes ==========
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    ct = request.headers.get("content-type", "").lower()
    log.info(f"📥 Webhook content-type: {ct}")

    payload: Dict[str, Any] = {}
    try:
        if "application/json" in ct:
            j = await request.json()
            if isinstance(j, dict):
                for k, v in j.items():
                    payload[norm_key(str(k))] = v
            else:
                payload = parse_text_payload(json.dumps(j))
        else:
            text = (await request.body()).decode("utf-8", errors="ignore")
            payload = parse_text_payload(text)
            log.info(f"📥 Webhook payload (keys via text): {list(payload.keys())}")
    except Exception as e:
        log.warning(f"⚠️ Webhook parse error: {e}")
        return PlainTextResponse("Bad payload", status_code=400)

    side = deduce_side(payload.get("side"), payload.get("direction"))
    if not side:
        log.warning("⚠️ Side manquant")
        return PlainTextResponse("Missing side", status_code=400)

    symbol = guess_symbol(payload)
    if not symbol:
        log.warning("⚠️ Webhook: Symbol manquant")
        return PlainTextResponse("Missing symbol", status_code=400)

    tf = payload.get("tf")
    entry = to_float(payload.get("entry"))
    tp1 = to_float(payload.get("tp1")); tp2 = to_float(payload.get("tp2")); tp3 = to_float(payload.get("tp3"))
    sl  = to_float(payload.get("sl"))

    entry_time = payload.get("entry_time")
    if entry_time: entry_time = str(entry_time)
    else: entry_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    tp1, tp2, tp3, sl = compute_levels(entry, side, tp1, tp2, tp3, sl)
    conf, reasons = compute_confidence(side, MARKET.fg, MARKET.btc_d)
    alert_name = payload.get("alert_name")

    global COUNTER
    with TRD_LOCK:
        COUNTER += 1
        t = Trade(
            id=COUNTER, created_at=now_iso(), side=side, symbol=symbol,
            tf=str(tf) if tf else "-", entry=entry, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
            direction=("LONG" if side=="BUY" else "SHORT"), entry_time=entry_time,
            alert_name=str(alert_name) if alert_name else None, raw=payload,
            confidence=conf, confidence_reason=reasons[:3] if reasons else None
        )
        TRADES.append(t)

    log.info(f"✅ Trade #{t.id}: {t.symbol} {t.side} @ {t.entry if t.entry else '-'}")
    send_telegram_entry(t)
    return JSONResponse({"ok": True, "id": t.id})

# ======= APIs « comme avant » =======
@app.get("/api/trades")
def api_trades():
    with TRD_LOCK:
        return [t.dict() for t in TRADES]

@app.get("/api/fear-greed")
def api_fg():
    return {"fg": MARKET.fg, "last_update": MARKET.last_update}

@app.get("/api/bullrun-phase")
def api_bull():
    return {"marketcap_trillion": MARKET.mc_trillion, "btc_d": MARKET.btc_d, "btc_price": MARKET.btc_price}

# (stubs pour pages existantes)
@app.get("/api/equity-curve")
def api_equity_curve():
    # retourne une courbe simple (timestamp, equity)
    return {"points":[{"t":i, "eq": 10000 + i*5 + (i%7)*20} for i in range(1,121)]}

@app.get("/api/journal")
def api_journal():
    with TRD_LOCK:
        rows = [{
            "id": t.id, "time": t.entry_time or t.created_at, "symbol": t.symbol,
            "side": t.side, "tf": t.tf, "entry": t.entry, "tp1": t.tp1, "tp2": t.tp2, "tp3": t.tp3, "sl": t.sl,
            "confidence": t.confidence
        } for t in TRADES]
    return {"rows": rows}

@app.get("/api/heatmap")
def api_heatmap():
    # mini heatmap synthétique
    return {"symbols":[
        {"s":"BTCUSDT", "chg": +0.8},
        {"s":"ETHUSDT", "chg": -0.4},
        {"s":"SOLUSDT", "chg": +1.6},
        {"s":"BNBUSDT", "chg": +0.2},
        {"s":"XRPUSDT", "chg": -1.1},
    ]}

@app.get("/api/news")
def api_news():
    # placeholder
    return {"items":[
        {"title":"Marché calme, BTC range", "link":"#", "src":"cryptoast"},
        {"title":"Altseason ? Signaux mitigés", "link":"#", "src":"cointelegraph"},
    ]}

@app.post("/api/reset")
def api_reset():
    global TRADES, COUNTER
    with TRD_LOCK:
        TRADES = []; COUNTER = 0
    log.info("♻️ TradingState reset")
    return {"ok": True}

# ======= PAGES (HTML, sans f-strings pour éviter {}/f-string bug) =======
BASE_CSS = """
<style>
:root { --bg:#0b0f1a; --card:#111827; --muted:#9ca3af; --acc:#22c55e; --warn:#f59e0b; --bad:#ef4444; }
*{box-sizing:border-box;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,Arial}
body{margin:0;background:var(--bg);color:#e5e7eb}
nav{display:flex;gap:14px;align-items:center;padding:14px 18px;background:#0f172a;border-bottom:1px solid #1f2937;position:sticky;top:0;z-index:10}
nav a{color:#cbd5e1;text-decoration:none;padding:8px 10px;border-radius:10px}
nav a:hover{background:#1f2937}
.container{max-width:1100px;margin:18px auto;padding:0 14px}
.card{background:var(--card);border:1px solid #1f2937;border-radius:14px;padding:16px;margin-bottom:16px}
h1,h2{margin:8px 0 12px 0}
.small{color:var(--muted);font-size:.92rem}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:600}
.badge.good{background:#064e3b;color:#a7f3d0}.badge.warn{background:#3f2f06;color:#fde68a}.badge.bad{background:#4c0519;color:#fecdd3}
table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #1f2937;padding:10px 8px;text-align:left;vertical-align:top}
tfoot td{border-top:1px solid #1f2937}.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace}
.kpi{display:flex;gap:14px;flex-wrap:wrap}.kpi .tile{flex:1 1 200px;background:#0f172a;border:1px solid #1f2937;border-radius:14px;padding:12px}
.tile h3{margin:0 0 6px 0;font-size:.95rem;color:#cbd5e1}.tile .v{font-size:1.25rem;font-weight:700}
footer{color:#6b7280;font-size:.85rem;padding:20px 0 30px;text-align:center}
</style>
"""

NAV_HTML = """
<nav>
  <a href="/">🏠 Dashboard</a>
  <a href="/trades">📈 Equity</a>
  <a href="/journal">📝 Journal</a>
  <a href="/heatmap">🔥 Heatmap</a>
  <a href="/strategie">⚙️ Stratégie</a>
  <a href="/backtest">⏮️ Backtest</a>
  <a href="/annonces">🗞️ Annonces</a>
  <a href="/patterns">📐 Patterns</a>
  <a href="/advanced-metrics">📊 Avancé</a>
</nav>
"""

DASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Dashboard</title>
""" + BASE_CSS + """
</head><body>
""" + NAV_HTML + """
<div class="container">
  <div class="card">
    <h1>Dashboard</h1>
    <div class="kpi">
      <div class="tile"><h3>Fear &amp; Greed</h3><div class="v" id="fg">—</div></div>
      <div class="tile"><h3>BTC Dominance</h3><div class="v" id="btcd">—</div></div>
      <div class="tile"><h3>MarketCap</h3><div class="v" id="mc">—</div></div>
      <div class="tile"><h3>BTC Price</h3><div class="v" id="btcpx">—</div></div>
      <div class="tile"><h3>Trades</h3><div class="v" id="trdcnt">—</div></div>
    </div>
    <div class="small">MAJ auto 5s — <span id="lu">—</span></div>
  </div>

  <div class="card">
    <h2>Derniers trades</h2>
    <table id="t">
      <thead>
        <tr><th>#</th><th>Heure</th><th>Symbole</th><th>Side</th><th>TF</th><th>Entry</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th><th>Conf.</th></tr>
      </thead>
      <tbody></tbody>
    </table>
    <div class="small">Actualisation auto (5s)</div>
  </div>

  <footer>© Dashboard</footer>
</div>
<script>
async function fetchJSON(u){try{const r=await fetch(u);if(!r.ok)return null;return await r.json();}catch(e){return null;}}
function money(x){
  if(x===null||x===undefined||x==='') return '-';
  const n=Number(x); if(!isFinite(n)) return '-';
  if(n>=1) return '$'+n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  let s=n.toFixed(10); s=s.replace(/0+$/,'').replace(/\.$/,''); return '$'+s;
}
function badge(conf){
  if(conf===null||conf===undefined) return '—';
  const v=Number(conf); let cls='warn'; if(v>=70) cls='good'; else if(v<55) cls='bad';
  return '<span class="badge '+cls+'">'+v+'%</span>';
}
async function refresh(){
  const gl=await fetchJSON('/api/bullrun-phase');
  if(gl){
    if(gl.btc_d!=null) document.getElementById('btcd').textContent = gl.btc_d.toFixed(1)+'%';
    if(gl.marketcap_trillion!=null) document.getElementById('mc').textContent = '$'+gl.marketcap_trillion.toFixed(2)+'T';
    if(gl.btc_price!=null) document.getElementById('btcpx').textContent = '$'+Number(gl.btc_price).toLocaleString();
  }
  const fg=await fetchJSON('/api/fear-greed');
  if(fg && fg.fg!=null) document.getElementById('fg').textContent = fg.fg;
  if(fg && fg.last_update) document.getElementById('lu').textContent = fg.last_update;

  const t=await fetchJSON('/api/trades');
  const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  if(t && Array.isArray(t)){
    document.getElementById('trdcnt').textContent = t.length;
    for(const x of t.slice(-20).reverse()){
      const tr=document.createElement('tr');
      tr.innerHTML = `
        <td class="mono">${x.id}</td>
        <td class="mono">${(x.entry_time||x.created_at||'-')}</td>
        <td>${x.symbol||'-'}</td>
        <td>${x.side||'-'}</td>
        <td>${x.tf||'-'}</td>
        <td class="mono">${money(x.entry)}</td>
        <td class="mono">${money(x.tp1)}</td>
        <td class="mono">${money(x.tp2)}</td>
        <td class="mono">${money(x.tp3)}</td>
        <td class="mono">${money(x.sl)}</td>
        <td>${badge(x.confidence)}</td>`;
      tb.appendChild(tr);
    }
  }
}
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""

TABLE_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Trades</title>
""" + BASE_CSS + """
</head><body>
""" + NAV_HTML + """
<div class="container">
  <div class="card">
    <h1>Trades</h1>
    <table id="t">
      <thead>
        <tr><th>#</th><th>Heure</th><th>Symbole</th><th>Side</th><th>TF</th><th>Entry</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th><th>Conf.</th></tr>
      </thead>
      <tbody></tbody>
      <tfoot><tr><td colspan="11" class="small">Actualisation auto (5s)</td></tr></tfoot>
    </table>
  </div>
</div>
<script>
async function fetchJSON(u){try{const r=await fetch(u);if(!r.ok)return null;return await r.json();}catch(e){return null;}}
function money(x){
  if(x===null||x===undefined||x==='') return '-';
  const n=Number(x); if(!isFinite(n)) return '-';
  if(n>=1) return '$'+n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  let s=n.toFixed(10); s=s.replace(/0+$/,'').replace(/\.$/,''); return '$'+s;
}
function badge(conf){
  if(conf===null||conf===undefined) return '—';
  const v=Number(conf); let cls='warn'; if(v>=70) cls='good'; else if(v<55) cls='bad';
  return '<span class="badge '+cls+'">'+v+'%</span>';
}
async function refresh(){
  const t=await fetchJSON('/api/trades');
  const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  if(t && Array.isArray(t)){
    for(const x of t.slice().reverse()){
      const tr=document.createElement('tr');
      tr.innerHTML = `
        <td class="mono">${x.id}</td>
        <td class="mono">${(x.entry_time||x.created_at||'-')}</td>
        <td>${x.symbol||'-'}</td>
        <td>${x.side||'-'}</td>
        <td>${x.tf||'-'}</td>
        <td class="mono">${money(x.entry)}</td>
        <td class="mono">${money(x.tp1)}</td>
        <td class="mono">${money(x.tp2)}</td>
        <td class="mono">${money(x.tp3)}</td>
        <td class="mono">${money(x.sl)}</td>
        <td>${badge(x.confidence)}</td>`;
      tb.appendChild(tr);
    }
  }
}
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""

SIMPLE_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
""" + BASE_CSS + """
</head><body>
""" + NAV_HTML + """
<div class="container">
  <div class="card"><h1>{title}</h1>
    <div class="small">Disponibile. Les données s’appuient sur /api correspondante.</div>
  </div>
</div>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
def home(): return HTMLResponse(DASH_HTML)

@app.get("/trades", response_class=HTMLResponse)
def page_trades(): return HTMLResponse(TABLE_PAGE)

@app.get("/equity-curve", response_class=HTMLResponse)
def page_equity(): return HTMLResponse(SIMPLE_PAGE.format(title="Equity Curve"))

@app.get("/journal", response_class=HTMLResponse)
def page_journal(): return HTMLResponse(SIMPLE_PAGE.format(title="Journal"))

@app.get("/heatmap", response_class=HTMLResponse)
def page_heatmap(): return HTMLResponse(SIMPLE_PAGE.format(title="Heatmap"))

@app.get("/strategie", response_class=HTMLResponse)
def page_strat(): return HTMLResponse(SIMPLE_PAGE.format(title="Stratégie"))

@app.get("/backtest", response_class=HTMLResponse)
def page_backtest(): return HTMLResponse(SIMPLE_PAGE.format(title="Backtest"))

@app.get("/annonces", response_class=HTMLResponse)
def page_news(): return HTMLResponse(SIMPLE_PAGE.format(title="Annonces"))

@app.get("/patterns", response_class=HTMLResponse)
def page_patterns(): return HTMLResponse(SIMPLE_PAGE.format(title="Patterns"))

@app.get("/advanced-metrics", response_class=HTMLResponse)
def page_adv(): return HTMLResponse(SIMPLE_PAGE.format(title="Métriques avancées"))

@app.get("/healthz")
def healthz(): return {"ok": True, "trades": len(TRADES)}

if __name__ == "__main__":
    uvicorn_run("main:app", host="0.0.0.0", port=int(os.getenv("PORT","8000")), reload=False)
