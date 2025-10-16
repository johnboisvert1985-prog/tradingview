# main.py
import os
import re
import math
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ------------------------------------------------------------------------------
# Config & logger
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("main")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Pour Render / CORS
app = FastAPI(title="TradingView Webhook Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# ------------------------------------------------------------------------------
# État en mémoire (démos + historique minimal)
# ------------------------------------------------------------------------------
class TradingState:
    def __init__(self) -> None:
        self.trades: List[Dict[str, Any]] = []
        self.journal: List[Dict[str, Any]] = []
        self.created_at = datetime.utcnow().isoformat()

    def reset(self):
        self.trades.clear()
        self.journal.clear()
        logger.info("♻️ TradingState reset")

STATE = TradingState()

# Démo de trades au démarrage (si tu veux, sinon commente)
DEMO = [
    {"symbol": "BTCUSDT", "side": "BUY",  "direction": "LONG",  "entry": 65000, "tf": "15m"},
    {"symbol": "ETHUSDT", "side": "SELL", "direction": "SHORT", "entry": 3500,  "tf": "15m"},
    {"symbol": "SOLUSDT", "side": "BUY",  "direction": "LONG",  "entry": 140,   "tf": "15m"},
]
for i, t in enumerate(DEMO, start=1):
    t["tp1"] = round(t["entry"] * (1.015 if t["side"] == "BUY" else 0.985), 6)
    t["tp2"] = round(t["entry"] * (1.025 if t["side"] == "BUY" else 0.975), 6)
    t["tp3"] = round(t["entry"] * (1.040 if t["side"] == "BUY" else 0.960), 6)
    t["sl"]  = round(t["entry"] * (0.98 if t["side"] == "BUY" else 1.02), 6)
    t["created_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    t["entry_time"] = t["created_at"]
    t["confidence"] = {"score": 60, "label": "MOYEN", "reasons": "Démo"}
    STATE.trades.append(t)
logger.info("✅ Démo initialisée avec %d trades", len(STATE.trades))

# ------------------------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------------------------
ALIASES = {
    # symbol
    "symbol": "symbol", "ticker": "symbol", "pair": "symbol",
    # timeframe
    "tf": "tf", "interval": "tf", "timeframe": "tf",
    # side/direction
    "side": "side", "direction": "direction",
    # entry
    "entry": "entry", "price": "entry", "close": "entry",
    # entry time
    "entry_time": "entry_time", "heure": "entry_time", "created_at": "entry_time", "time": "entry_time",
    # extras
    "alert_name": "alert_name", "name": "alert_name",
    # targets
    "tp1": "tp1", "tp2": "tp2", "tp3": "tp3", "sl": "sl"
}

def as_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except Exception:
        return None

async def fetch_spot_price(symbol: str) -> Optional[float]:
    """Fetch spot price from Binance public API."""
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            if r.status_code == 200:
                j = r.json()
                return float(j.get("price"))
    except Exception as e:
        logger.warning(f"⚠️ Binance fetch price failed for {symbol}: {e}")
    return None

async def fetch_fear_greed() -> Optional[int]:
    """Stub rapide. Remplace par ton fetch réel si besoin."""
    return 28

async def fetch_btc_dominance() -> Optional[float]:
    """Stub rapide. Remplace par ton fetch réel si besoin."""
    return 57.1

def confidence_score(fng: Optional[int], btc_d: Optional[float], side: str) -> Tuple[int, str, str]:
    """Retourne (score, label, raisons)"""
    score = 50
    reasons = []

    if fng is not None:
        if fng <= 25:
            score += 10; reasons.append("✅ Fear bas : opportunité")
        elif fng >= 75:
            score -= 10; reasons.append("⚠️ Greed élevé : prudence")

    if btc_d is not None:
        if side == "BUY":
            if btc_d > 55: score -= 5; reasons.append("⚠️ BTC.D élevé (altcoins défavorisés)")
            else: score += 5; reasons.append("✅ BTC.D modéré (altcoins OK)")
        else:  # SELL
            if btc_d > 55: score += 5; reasons.append("✅ BTC.D élevé (altcoins fragiles)")
            else: score -= 3; reasons.append("⚠️ BTC.D bas (shorts moins évidents)")

    score = max(0, min(100, score))
    if score >= 70: label = "ÉLEVÉ"
    elif score >= 55: label = "MOYEN"
    elif score >= 45: label = "FAIBLE"
    else: label = "TRÈS FAIBLE"

    return score, label, " • ".join(reasons) if reasons else "—"

async def telegram_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configuré (TOKEN/CHAT_ID manquants).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=data)
        if r.status_code == 429:
            j = {}
            try:
                j = r.json()
            except Exception:
                pass
            wait = int((j.get("parameters") or {}).get("retry_after", 1))
            logger.error(f"❌ Telegram: 429 - retry_after={wait}s")
            await asyncio.sleep(wait)
            await client.post(url, json=data)
        else:
            try:
                r.raise_for_status()
                logger.info("✅ Telegram envoyé")
            except Exception as e:
                logger.error(f"❌ Telegram: {r.status_code} - {r.text} - {e}")

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "$-"
    # format adaptatif
    if abs(x) >= 1:
        return f"${x:,.3f}".replace(",", " ")
    if abs(x) >= 0.01:
        return f"${x:.4f}"
    return f"${x:.6f}"

# ------------------------------------------------------------------------------
# Parser TradingView tolérant (JSON ou texte)
# ------------------------------------------------------------------------------
async def parse_tv_payload(request: Request) -> Dict[str, Any]:
    ct = (request.headers.get("content-type") or "").lower()

    # 1) JSON propre
    if "application/json" in ct:
        try:
            data = await request.json()
            if isinstance(data, dict) and "chat_id" in data and "text" in data:
                # On dirait un payload Telegram => refuser proprement
                raise HTTPException(status_code=400, detail="Payload ressemble à un JSON Telegram. Envoie les champs de trade (side, symbol, entry...).")
            return data if isinstance(data, dict) else {}
        except Exception:
            pass

    # 2) Texte brut: lignes "clé: valeur"
    raw = (await request.body()).decode("utf-8", errors="ignore").strip()
    data: Dict[str, Any] = {}
    if raw:
        # Si contient chat_id, c'est un message Telegram
        if "chat_id" in raw and "text" in raw:
            raise HTTPException(status_code=400, detail="Payload semble être un message Telegram. Envoie les champs de trade (side, symbol, entry...).")
        # JSON collé en texte ? on tente
        if raw.startswith("{") and raw.endswith("}"):
            try:
                j = json.loads(raw)
                if isinstance(j, dict):
                    return j
            except Exception:
                pass
        # Sinon parse "clé: valeur"
        keys_seen = []
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k:
                    data[k] = v
                    keys_seen.append(k)
        logger.info(f"📥 Webhook payload (keys via text): {keys_seen if keys_seen else list(data.keys())}")
    else:
        logger.info("📥 Webhook: payload vide")

    return data

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    html = """
    <!doctype html>
    <html lang="fr">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Dashboard Trading</title>
      <style>
        body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif; margin:24px;}
        a{color:#0b74de; text-decoration:none}
        .grid{display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
        .card{border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 1px 2px rgba(0,0,0,.04)}
        .muted{color:#6b7280}
        .btn{display:inline-block; padding:8px 12px; border-radius:8px; border:1px solid #ddd}
      </style>
    </head>
    <body>
      <h1>📊 Dashboard Trading</h1>
      <div class="grid">
        <div class="card">
          <h3>Trades</h3>
          <p class="muted">Voir la liste des entrées</p>
          <a class="btn" href="/trades">Ouvrir</a>
        </div>
        <div class="card">
          <h3>Équity curve</h3>
          <p class="muted">Courbe simple (placeholder)</p>
          <a class="btn" href="/equity-curve">Ouvrir</a>
        </div>
        <div class="card">
          <h3>Journal</h3>
          <p class="muted">Notes rapides</p>
          <a class="btn" href="/journal">Ouvrir</a>
        </div>
        <div class="card">
          <h3>Heatmap</h3>
          <p class="muted">Aperçu (placeholder)</p>
          <a class="btn" href="/heatmap">Ouvrir</a>
        </div>
      </div>
      <p style="margin-top:24px"><a class="btn" href="/annonces">🗞️ Annonces</a>
         <a class="btn" href="/strategie">🧠 Stratégie</a>
         <a class="btn" href="/backtest">🧪 Backtest</a>
         <a class="btn" href="/patterns">📐 Patterns</a></p>
    </body>
    </html>
    """
    return HTMLResponse(html)

TRADES_PAGE_JS = r"""
function formatNumber(x) {
  if (x === null || x === undefined || x === '') return '-';
  const n = Number(x);
  if (Number.isNaN(n)) return '-';
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (n >= 1) return n.toFixed(3);
  if (n >= 0.01) return n.toFixed(4);
  return n.toFixed(6);
}
"""

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    html = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trades</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif; margin:24px;}
    table{border-collapse:collapse; width:100%}
    th,td{border:1px solid #e5e7eb; padding:8px; text-align:left}
    th{background:#f3f4f6}
    .muted{color:#6b7280}
  </style>
</head>
<body>
  <h1>📈 Trades</h1>
  <p class="muted">Actualisation auto toutes les 10s</p>
  <table id="t">
    <thead>
      <tr>
        <th>Heure</th><th>Symbole</th><th>Side</th><th>TF</th>
        <th>Entry</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th>
        <th>Confiance</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  <script>""" + TRADES_PAGE_JS + """</script>
  <script>
    async function load(){ 
      const r = await fetch('/api/trades'); 
      const js = await r.json();
      const tbody = document.querySelector('#t tbody');
      tbody.innerHTML = '';
      js.forEach(tr => {
        const trEl = document.createElement('tr');
        const conf = tr.confidence ? (tr.confidence.score + '% ' + '(' + tr.confidence.label + ')') : '-';
        trEl.innerHTML = `
          <td>${tr.entry_time || tr.created_at || '-'}</td>
          <td>${tr.symbol || '-'}</td>
          <td>${tr.side || '-'}</td>
          <td>${tr.tf || '-'}</td>
          <td>${formatNumber(tr.entry)}</td>
          <td>${formatNumber(tr.tp1)}</td>
          <td>${formatNumber(tr.tp2)}</td>
          <td>${formatNumber(tr.tp3)}</td>
          <td>${formatNumber(tr.sl)}</td>
          <td>${conf}</td>
        `;
        tbody.appendChild(trEl);
      });
    }
    load(); setInterval(load, 10000);
  </script>
</body>
</html>"""
    return HTMLResponse(html)

@app.get("/api/trades", response_class=JSONResponse)
async def api_trades():
    return JSONResponse(STATE.trades)

@app.post("/api/reset", response_class=PlainTextResponse)
async def api_reset():
    STATE.reset()
    return PlainTextResponse("OK")

@app.get("/api/fear-greed", response_class=JSONResponse)
async def api_fng():
    fng = await fetch_fear_greed()
    logger.info(f"✅ Fear & Greed: {fng}")
    return {"value": fng}

@app.get("/api/bullrun-phase", response_class=JSONResponse)
async def api_bullrun():
    btc_price = await fetch_spot_price("BTCUSDT")
    btc_d = await fetch_btc_dominance()
    mc = 3.87  # T (placeholder)
    if btc_d is not None:
        logger.info(f"✅ Global: MC ${mc:.2f}T, BTC.D {btc_d:.1f}%")
    if btc_price is not None:
        logger.info(f"✅ Prix: BTC ${btc_price:,.0f}".replace(",", " "))
    return {
        "market_cap_trillions": mc,
        "btc_dominance": btc_d,
        "btc_price": btc_price
    }

# Pages placeholders pour éviter “rien n’est beau”
@app.get("/equity-curve", response_class=HTMLResponse)
async def page_equity():
    return HTMLResponse("<h1>Équity curve</h1><p>Placeholder simple.</p>")

@app.get("/journal", response_class=HTMLResponse)
async def page_journal():
    return HTMLResponse("<h1>Journal</h1><p>Placeholder simple.</p>")

@app.get("/heatmap", response_class=HTMLResponse)
async def page_heatmap():
    return HTMLResponse("<h1>Heatmap</h1><p>Placeholder simple.</p>")

@app.get("/strategie", response_class=HTMLResponse)
async def page_strat():
    return HTMLResponse("<h1>Stratégie</h1><p>Placeholder simple.</p>")

@app.get("/backtest", response_class=HTMLResponse)
async def page_backtest():
    return HTMLResponse("<h1>Backtest</h1><p>Placeholder simple.</p>")

@app.get("/patterns", response_class=HTMLResponse)
async def page_patterns():
    return HTMLResponse("<h1>Patterns</h1><p>Placeholder simple.</p>")

@app.get("/annonces", response_class=HTMLResponse)
async def page_news():
    return HTMLResponse("<h1>Annonces</h1><p>Placeholder simple.</p>")

# ------------------------------------------------------------------------------
# Webhook TradingView TOLÉRANT
# ------------------------------------------------------------------------------
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    # Parse "intelligent"
    payload = await parse_tv_payload(request)

    # Normalise clés via ALIASES
    norm: Dict[str, Any] = {}
    for k, v in payload.items():
        k2 = ALIASES.get(str(k).strip().lower())
        if k2:
            norm[k2] = v

    alert_name = str(norm.get("alert_name", "")).upper()
    direction  = str(norm.get("direction", "")).upper()
    side       = str(norm.get("side", "")).upper()

    # Déduction du side si absent
    if side not in {"BUY", "SELL"}:
        if direction in {"LONG", "BUY"}:
            side = "BUY"
        elif direction in {"SHORT", "SELL"}:
            side = "SELL"
        elif "LONG" in alert_name or "BUY" in alert_name:
            side = "BUY"
        elif "SHORT" in alert_name or "SELL" in alert_name:
            side = "SELL"

    if not side:
        logger.warning("⚠️ Side manquant")
        raise HTTPException(status_code=400, detail="side manquant")

    # Symbol
    symbol = norm.get("symbol") or ""
    symbol = str(symbol).upper().replace("PERP", "USDT").replace(".P", "USDT").replace("-PERP", "USDT")

    if not symbol:
        # Essaie depuis alert_name : ex "NOUVEAU TRADE — FLUXUSDT.P"
        m = re.search(r"([A-Z]{2,}USDT(?:\.P)?)", alert_name)
        if m:
            symbol = m.group(1).replace(".P", "USDT")

    if not symbol:
        raise HTTPException(status_code=400, detail="symbol manquant")

    # Entry
    entry = as_float(norm.get("entry"))
    if entry is None:
        entry = await fetch_spot_price(symbol)
    if entry is None:
        raise HTTPException(status_code=400, detail="entry manquant et fetch spot impossible")

    # Timeframe & heure
    tf = norm.get("tf") or "-"
    entry_time = norm.get("entry_time") or datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # TP/SL
    tp1 = as_float(norm.get("tp1"))
    tp2 = as_float(norm.get("tp2"))
    tp3 = as_float(norm.get("tp3"))
    sl  = as_float(norm.get("sl"))

    TP_LONG  = (0.015, 0.025, 0.040)
    TP_SHORT = (0.015, 0.025, 0.040)
    SL_LONG  = 0.02
    SL_SHORT = 0.02

    if tp1 is None or tp2 is None or tp3 is None or sl is None:
        if side == "BUY":
            if tp1 is None: tp1 = entry * (1 + TP_LONG[0])
            if tp2 is None: tp2 = entry * (1 + TP_LONG[1])
            if tp3 is None: tp3 = entry * (1 + TP_LONG[2])
            if sl  is None: sl  = entry * (1 - SL_LONG)
        else:
            if tp1 is None: tp1 = entry * (1 - TP_SHORT[0])
            if tp2 is None: tp2 = entry * (1 - TP_SHORT[1])
            if tp3 is None: tp3 = entry * (1 - TP_SHORT[2])
            if sl  is None: sl  = entry * (1 + SL_SHORT)

    # Score confiance vivant
    fng = await fetch_fear_greed()
    btc_d = await fetch_btc_dominance()
    score, label, reasons = confidence_score(fng, btc_d, side)

    trade = {
        "symbol": symbol,
        "side": side,
        "direction": "LONG" if side == "BUY" else "SHORT",
        "tf": tf,
        "entry": round(entry, 10),
        "tp1": round(tp1, 10),
        "tp2": round(tp2, 10),
        "tp3": round(tp3, 10),
        "sl":  round(sl,  10),
        "entry_time": entry_time,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "confidence": {"score": score, "label": label, "reasons": reasons},
    }

    STATE.trades.append(trade)

    # Message Telegram
    # Titre inclut le symbole pour régler "ne dit pas c'est quoi la crypto"
    parts = []
    parts.append(f"🎯 <b>NOUVEAU TRADE — {symbol}</b>")
    parts.append("")
    parts.append(f"📊 <b>{'BUY' if side=='BUY' else 'SELL'}</b>")
    parts.append(f"📈 Direction: {trade['direction']} | {tf}")
    parts.append("")
    parts.append(f"💰 Entry: {fmt_money(entry)}")
    parts.append("")
    parts.append("🎯 <b>Take Profits:</b>")
    parts.append(f"  TP1: {fmt_money(tp1)}")
    parts.append(f"  TP2: {fmt_money(tp2)}")
    parts.append(f"  TP3: {fmt_money(tp3)}")
    parts.append("")
    parts.append(f"🛑 Stop Loss: {fmt_money(sl)}")
    parts.append("")
    parts.append(f"📊 <b>CONFIANCE:</b> {score}% ({label})")
    parts.append("")
    why = reasons if reasons else "—"
    parts.append("Pourquoi ce score ?")
    for bullet in why.split(" • "):
        if bullet.strip():
            parts.append(f"  • {bullet.strip()}")

    fg_str = f"{fng}" if fng is not None else "—"
    btd_str = f"{btc_d:.1f}%" if btc_d is not None else "—"
    parts.append("")
    parts.append(f"💡 Marché: F&G {fg_str} | BTC.D {btd_str}")

    text = "\n".join(parts)
    await telegram_send(text)

    return JSONResponse({"ok": True})

# ------------------------------------------------------------------------------
# Uvicorn entrypoint
# ------------------------------------------------------------------------------
# Laisse 'app' au niveau global pour uvicorn: `uvicorn main:app --host 0.0.0.0 --port 8000`
