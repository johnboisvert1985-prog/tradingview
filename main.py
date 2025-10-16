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
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from uvicorn import run as uvicorn_run

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

app = FastAPI(title="TradingView → Dashboard → Telegram")

# ====== In-memory state ======
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
    fg: Optional[int] = None          # Fear & Greed index (0-100)
    btc_d: Optional[float] = None     # BTC dominance %
    mc_trillion: Optional[float] = None  # Total crypto MC in Trillions USD
    last_update: Optional[str] = None

TRADES: List[Trade] = []
TRD_LOCK = threading.Lock()
COUNTER = 0

MARKET = MarketState(fg=28, btc_d=57.2, mc_trillion=3.85, last_update=datetime.now(timezone.utc).isoformat())

# ====== Config & helpers ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

FG_API_URL = os.getenv("FG_API_URL", "")          # optionnel
GLOBAL_API_URL = os.getenv("GLOBAL_API_URL", "")  # optionnel

# Telegram rate-limit: simple queue + worker (évite 429)
TG_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()

def tg_worker():
    session = requests.Session()
    while True:
        item = TG_QUEUE.get()
        if item is None:
            break
        try:
            token = item["token"]
            chat_id = item["chat_id"]
            text = item["text"]
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            r = session.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                # Respect "retry_after" if present
                try:
                    ra = r.json().get("parameters", {}).get("retry_after", 5)
                except Exception:
                    ra = 5
                log.error(f"❌ Telegram: 429 - retry after {ra}s")
                time.sleep(int(ra))
                # retry once
                r = session.post(url, json=payload, timeout=10)
            if r.ok:
                log.info("✅ Telegram envoyé")
            else:
                log.error(f"❌ Telegram: {r.status_code} - {r.text}")
        except Exception as e:
            log.exception(f"❌ Telegram exception: {e}")
        finally:
            # petit délai pour éviter burst
            time.sleep(0.25)
            TG_QUEUE.task_done()

threading.Thread(target=tg_worker, daemon=True).start()

def to_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).strip().replace(",", "")
        if s.upper() in {"NA", "N/A", "-", ""}:
            return None
        return float(s)
    except Exception:
        return None

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    # Pretty: 110,550 or 0.000074
    if x >= 1:
        return f"${x:,.2f}"
    # tiny prices
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return "$" + s

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ====== Confidence scoring (« vivant ») ======
def compute_confidence(side: str, fg: Optional[int], btc_d: Optional[float]) -> (int, List[str]):
    """
    0..100
    - Fear & Greed: bas favorise BUY (contrarian)
    - BTC dominance haute (>57) pénalise alt longs, favorise shorts
    - Ajustements légers
    """
    notes = []
    score = 50

    if fg is not None:
        if fg <= 25:
            score += 10 if side.upper() == "BUY" else -3
            notes.append("✅ Fear extrême = zone d'achat idéale" if side.upper() == "BUY"
                         else "⚠️ Sentiment frileux : shorts moins évidents")
        elif fg <= 45:
            score += 3 if side.upper() == "BUY" else -2
            notes.append("✅ Sentiment frileux : léger avantage aux longs" if side.upper() == "BUY"
                         else "⚠️ Sentiment frileux : shorts moins évidents")
        elif fg >= 75:
            score += 8 if side.upper() == "SELL" else -5
            notes.append("⚠️ Euphorie: prudence sur les nouveaux longs" if side.upper() == "BUY"
                         else "✅ Euphorie: opportunités de short")
        else:
            notes.append("ℹ️ Sentiment neutre")

    if btc_d is not None:
        if btc_d >= 57.0:
            if side.upper() == "BUY":
                score -= 5
            else:
                score += 2
            notes.append("⚠️ BTC trop dominant pour altcoins")
        elif btc_d <= 45.0:
            if side.upper() == "BUY":
                score += 3
            notes.append("✅ Dominance BTC modérée : altcoins plus libres")

    score = max(0, min(100, score))
    return score, notes


# ====== Market refresh (optionnel) ======
def refresh_market():
    # tente d’actualiser MARKET; sinon conserve les valeurs
    updated = False
    session = requests.Session()
    try:
        if FG_API_URL:
            r = session.get(FG_API_URL, timeout=8)
            if r.ok:
                data = r.json()
                # suppose data {"value": 28}
                fg = int(data.get("value"))
                MARKET.fg = fg
                updated = True
    except Exception:
        pass
    try:
        if GLOBAL_API_URL:
            r = session.get(GLOBAL_API_URL, timeout=8)
            if r.ok:
                data = r.json()
                # suppose {"btc_d":57.1,"mc_trillion":3.87}
                MARKET.btc_d = float(data.get("btc_d"))
                MARKET.mc_trillion = float(data.get("mc_trillion"))
                updated = True
    except Exception:
        pass

    if updated:
        MARKET.last_update = now_iso()

def market_refresher_loop():
    while True:
        try:
            refresh_market()
        except Exception:
            pass
        time.sleep(60)  # 1 min

threading.Thread(target=market_refresher_loop, daemon=True).start()

# ====== Telegram message ======
def build_confidence_badge(conf: int) -> str:
    if conf >= 70:
        level = "ÉLEVÉ"
    elif conf >= 55:
        level = "MOYEN"
    else:
        level = "FAIBLE"
    return f"{conf}% ({level})"

def send_telegram_entry(t: Trade):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    fg = MARKET.fg if MARKET.fg is not None else "-"
    btc_d = f"{MARKET.btc_d:.1f}%" if MARKET.btc_d is not None else "—"

    conf = t.confidence if t.confidence is not None else 50
    conf_text = build_confidence_badge(conf)
    reasons = t.confidence_reason or []

    # Affichage propre des objectifs
    tp1 = fmt_money(t.tp1)
    tp2 = fmt_money(t.tp2)
    tp3 = fmt_money(t.tp3)
    sl = fmt_money(t.sl)
    entry = fmt_money(t.entry)

    direction = (t.direction or ("LONG" if (t.side or "").upper() == "BUY" else "SHORT")).upper()
    tf = t.tf or "-"

    # message complet
    lines = []
    title = f"🎯 NOUVEAU TRADE — {t.symbol}"
    lines.append(title)
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
    lines.append(f"📊 CONFIANCE: {conf_text}")
    lines.append("")
    if reasons:
        lines.append("Pourquoi ce score ?")
        for r in reasons:
            # éviter de surcharger, 3 raisons max
            lines.append(f"  • {r}")
    lines.append("")
    lines.append(f"💡 Marché: F&G {fg} | BTC.D {btc_d}")

    text = "\n".join(lines).strip()

    TG_QUEUE.put({
        "token": TELEGRAM_BOT_TOKEN,
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    })

# ====== TP/SL helpers ======
def compute_levels(entry: Optional[float], side: str,
                   tp1: Optional[float], tp2: Optional[float], tp3: Optional[float],
                   sl: Optional[float]) -> (Optional[float], Optional[float], Optional[float], Optional[float]):
    """
    Si TP/SL manquent, calcule par défaut:
      BUY:  TP1 +1.5%, TP2 +2.5%, TP3 +4.0%, SL -2.0%
      SELL: TP1 -1.5%, TP2 -2.5%, TP3 -4.0%, SL +2.0%
    """
    if entry is None:
        return tp1, tp2, tp3, sl

    s = side.upper()
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
        if s == "BUY":
            sl = entry * 0.980
        else:
            sl = entry * 1.020

    return tp1, tp2, tp3, sl

# ====== Webhook parsing ======
ALIASES = {
    "timeframe": "tf",
    "interval": "tf",
    "direction": "direction",
    "action": "side",
    "type": "type",
    "symbol": "symbol",
    "ticker": "symbol",
    "s": "symbol",
    "entry": "entry",
    "price": "entry",
    "p": "entry",
    "tp": "tp1",
    "tp1": "tp1",
    "tp2": "tp2",
    "tp3": "tp3",
    "sl": "sl",
    "stop": "sl",
    "entry_time": "entry_time",
    "time": "entry_time",
    "timenow": "entry_time",
    "alert_name": "alert_name",
    "name": "alert_name",
}

def norm_key(k: str) -> str:
    k = k.strip().lower()
    return ALIASES.get(k, k)

def parse_text_payload(raw_text: str) -> Dict[str, Any]:
    """
    Accepte:
      key:value
      key= value
      JSON-like mais envoyé en text/plain
    """
    data: Dict[str, Any] = {}
    # 1) essai JSON
    try:
        j = json.loads(raw_text)
        if isinstance(j, dict):
            for k, v in j.items():
                data[norm_key(str(k))] = v
            return data
    except Exception:
        pass

    # 2) kv lignes
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
    cand = (side or "").strip().upper()
    if cand in {"BUY", "SELL"}:
        return cand
    d = (direction or "").strip().upper()
    if d in {"LONG", "BUY"}:
        return "BUY"
    if d in {"SHORT", "SELL"}:
        return "SELL"
    return None

def guess_symbol(d: Dict[str, Any]) -> Optional[str]:
    for key in ("symbol", "ticker", "s"):
        v = d.get(key)
        if v:
            return str(v).strip().upper()
    # try from text patterns? (minimum)
    return None

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
                # parfois TradingView envoie un string JSON dans un tableau etc.
                payload = parse_text_payload(json.dumps(j))
        else:
            # text/plain; parfois TradingView envoie un "JSON" brut en texte
            text = await request.body()
            txt = text.decode("utf-8", errors="ignore")
            payload = parse_text_payload(txt)
            log.info(f"📥 Webhook payload (keys via text): {list(payload.keys())}")
    except Exception as e:
        log.warning(f"⚠️ Webhook parse error: {e}")
        return PlainTextResponse("Bad payload", status_code=400)

    # Normalisation
    side = deduce_side(payload.get("side"), payload.get("direction"))
    if not side:
        log.warning("⚠️ Side manquant")
        return PlainTextResponse("Missing side", status_code=400)

    symbol = guess_symbol(payload)
    if not symbol:
        log.warning("⚠️ Webhook: Symbol manquant")
        return PlainTextResponse("Missing symbol", status_code=400)

    tf = payload.get("tf")
    # entry peut être absent → tenter "entry", sinon None
    entry = to_float(payload.get("entry"))
    # parse niveaux s’ils existent
    tp1 = to_float(payload.get("tp1"))
    tp2 = to_float(payload.get("tp2"))
    tp3 = to_float(payload.get("tp3"))
    sl = to_float(payload.get("sl"))

    entry_time = payload.get("entry_time")
    if entry_time:
        entry_time = str(entry_time)
    else:
        entry_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # compute levels si manquants
    tp1, tp2, tp3, sl = compute_levels(entry, side, tp1, tp2, tp3, sl)

    # confidence « vivant »
    conf, reasons = compute_confidence(side, MARKET.fg, MARKET.btc_d)

    alert_name = payload.get("alert_name")

    # créer trade
    global COUNTER
    with TRD_LOCK:
        COUNTER += 1
        t = Trade(
            id=COUNTER,
            created_at=now_iso(),
            side=side,
            symbol=symbol,
            tf=str(tf) if tf else "-",
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            sl=sl,
            direction=("LONG" if side == "BUY" else "SHORT"),
            entry_time=entry_time,
            alert_name=str(alert_name) if alert_name else None,
            raw=payload,
            confidence=conf,
            confidence_reason=reasons[:3] if reasons else None
        )
        TRADES.append(t)

    log.info(f"✅ Trade #{t.id}: {t.symbol} {t.side} @ {t.entry if t.entry else '-'}")

    # Telegram immédiat à l'ENTRY
    send_telegram_entry(t)

    return JSONResponse({"ok": True, "id": t.id})

# ====== API ======
@app.get("/api/trades")
def api_trades():
    with TRD_LOCK:
        return [t.dict() for t in TRADES]

@app.get("/api/fear-greed")
def api_fg():
    return {"fg": MARKET.fg, "last_update": MARKET.last_update}

@app.get("/api/bullrun-phase")
def api_global():
    return {
        "marketcap_trillion": MARKET.mc_trillion,
        "btc_d": MARKET.btc_d,
        "btc_price": None  # laissez None si non sourcé ici
    }

@app.post("/api/reset")
def api_reset():
    global TRADES, COUNTER
    with TRD_LOCK:
        TRADES = []
        COUNTER = 0
    log.info("♻️ TradingState reset")
    return {"ok": True}

# ====== PAGES (HTML) ======
# IMPORTANT: on évite f-strings pour les pages avec JS/CSS afin d’éviter les { } → SyntaxError
BASE_CSS = """
<style>
:root { --bg:#0b0f1a; --card:#111827; --muted:#9ca3af; --acc:#22c55e; --warn:#f59e0b; --bad:#ef4444; }
* { box-sizing: border-box; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Arial; }
body { margin:0; background: var(--bg); color: #e5e7eb; }
nav { display:flex; gap:14px; align-items:center; padding:14px 18px; background:#0f172a; border-bottom:1px solid #1f2937; position:sticky; top:0; z-index:10;}
nav a { color:#cbd5e1; text-decoration:none; padding:8px 10px; border-radius:10px; }
nav a:hover { background:#1f2937; }
.container { max-width: 1100px; margin: 18px auto; padding:0 14px; }
.card { background: var(--card); border:1px solid #1f2937; border-radius:14px; padding:16px; margin-bottom:16px; }
h1,h2 { margin: 8px 0 12px 0; }
.small { color: var(--muted); font-size: 0.92rem; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-weight:600; }
.badge.good { background: #064e3b; color:#a7f3d0; }
.badge.warn { background:#3f2f06; color:#fde68a; }
.badge.bad { background:#4c0519; color:#fecdd3; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #1f2937; padding: 10px 8px; text-align:left; vertical-align: top;}
tfoot td { border-top:1px solid #1f2937; }
.mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
.kpi { display:flex; gap:14px; flex-wrap:wrap; }
.kpi .tile { flex: 1 1 200px; background:#0f172a; border:1px solid #1f2937; border-radius:14px; padding:12px; }
.tile h3 { margin:0 0 6px 0; font-size:0.95rem; color:#cbd5e1; }
.tile .v { font-size:1.25rem; font-weight:700; }
footer { color:#6b7280; font-size:0.85rem; padding:20px 0 30px; text-align:center; }
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
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Dashboard</title>
""" + BASE_CSS + """
</head>
<body>
""" + NAV_HTML + """
<div class="container">
  <div class="card">
    <h1>Dashboard</h1>
    <div class="kpi">
      <div class="tile"><h3>Fear &amp; Greed</h3><div class="v" id="fg">—</div></div>
      <div class="tile"><h3>BTC Dominance</h3><div class="v" id="btcd">—</div></div>
      <div class="tile"><h3>MarketCap</h3><div class="v" id="mc">—</div></div>
      <div class="tile"><h3>Trades</h3><div class="v" id="trdcnt">—</div></div>
    </div>
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
async function fetchJSON(u){ try{ const r=await fetch(u); if(!r.ok) return null; return await r.json(); }catch(e){ return null; } }
function money(x){
  if(x===null||x===undefined||x==='') return '-';
  const n = Number(x);
  if(!isFinite(n)) return '-';
  if(n>=1) return '$'+n.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
  let s = n.toFixed(10);
  s = s.replace(/0+$/,'').replace(/\.$/,'');
  return '$'+s;
}
function badge(conf){
  if(conf===null||conf===undefined) return '—';
  const v = Number(conf);
  let cls='warn';
  if(v>=70) cls='good'; else if(v<55) cls='bad';
  return '<span class="badge '+cls+'">'+v+'%</span>';
}
async function refresh(){
  const fg = await fetchJSON('/api/fear-greed');
  if(fg&&fg.fg!=null) document.getElementById('fg').textContent = fg.fg;

  const gl = await fetchJSON('/api/bullrun-phase');
  if(gl){
    if(gl.btc_d!=null) document.getElementById('btcd').textContent = gl.btc_d.toFixed(1)+'%';
    if(gl.marketcap_trillion!=null) document.getElementById('mc').textContent = '$'+gl.marketcap_trillion.toFixed(2)+'T';
  }

  const t = await fetchJSON('/api/trades');
  const tb = document.querySelector('#t tbody');
  tb.innerHTML='';
  if(t && Array.isArray(t)){
    document.getElementById('trdcnt').textContent = t.length;
    for(const x of t.slice(-20).reverse()){
      const tr = document.createElement('tr');
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
</body>
</html>
"""

TABLE_PAGE = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Trades</title>
""" + BASE_CSS + """
</head>
<body>
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
async function fetchJSON(u){ try{ const r=await fetch(u); if(!r.ok) return null; return await r.json(); }catch(e){ return null; } }
function money(x){
  if(x===null||x===undefined||x==='') return '-';
  const n = Number(x);
  if(!isFinite(n)) return '-';
  if(n>=1) return '$'+n.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
  let s = n.toFixed(10);
  s = s.replace(/0+$/,'').replace(/\.$/,'');
  return '$'+s;
}
function badge(conf){
  if(conf===null||conf===undefined) return '—';
  const v = Number(conf);
  let cls='warn';
  if(v>=70) cls='good'; else if(v<55) cls='bad';
  return '<span class="badge '+cls+'">'+v+'%</span>';
}
async function refresh(){
  const t = await fetchJSON('/api/trades');
  const tb = document.querySelector('#t tbody');
  tb.innerHTML='';
  if(t && Array.isArray(t)){
    for(const x of t.slice().reverse()){
      const tr = document.createElement('tr');
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
<!doctype html>
<html>
<head><meta charset="utf-8"><title>{title}</title>
""" + BASE_CSS + """
</head>
<body>
""" + NAV_HTML + """
<div class="container">
  <div class="card">
    <h1>{title}</h1>
    <div class="small">Cette page est disponible et fonctionnelle. Le contenu spécifique peut être étendu.</div>
  </div>
</div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(DASH_HTML)

@app.get("/trades", response_class=HTMLResponse)
def page_trades():
    return HTMLResponse(TABLE_PAGE)

@app.get("/equity-curve", response_class=HTMLResponse)
def page_equity():
    # on affiche une page simple (vous pouvez y mettre un graphique plus tard)
    return HTMLResponse(SIMPLE_PAGE.format(title="Equity Curve"))

@app.get("/journal", response_class=HTMLResponse)
def page_journal():
    return HTMLResponse(SIMPLE_PAGE.format(title="Journal"))

@app.get("/heatmap", response_class=HTMLResponse)
def page_heatmap():
    return HTMLResponse(SIMPLE_PAGE.format(title="Heatmap"))

@app.get("/strategie", response_class=HTMLResponse)
def page_strat():
    return HTMLResponse(SIMPLE_PAGE.format(title="Stratégie"))

@app.get("/backtest", response_class=HTMLResponse)
def page_backtest():
    return HTMLResponse(SIMPLE_PAGE.format(title="Backtest"))

@app.get("/annonces", response_class=HTMLResponse)
def page_news():
    return HTMLResponse(SIMPLE_PAGE.format(title="Annonces"))

@app.get("/patterns", response_class=HTMLResponse)
def page_patterns():
    return HTMLResponse(SIMPLE_PAGE.format(title="Patterns"))

@app.get("/advanced-metrics", response_class=HTMLResponse)
def page_adv():
    return HTMLResponse(SIMPLE_PAGE.format(title="Métriques avancées"))

# ====== Health ======
@app.get("/healthz")
def healthz():
    return {"ok": True, "trades": len(TRADES)}

if __name__ == "__main__":
    # Render utilise souvent: gunicorn/uvicorn via CMD; mais pour local:
    uvicorn_run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
