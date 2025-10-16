# main.py
import os
import json
import re
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

app = FastAPI(title="TradingView Webhook → Dashboard & Telegram")

# --- Config Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHANNEL_USERNAME = os.getenv("TELEGRAM_CHANNEL_USERNAME", "").strip()

# --- Cibles par défaut (si TP/SL absents) ---
TP1_PCT = float(os.getenv("TP1_PCT", "1.5"))   # %
TP2_PCT = float(os.getenv("TP2_PCT", "2.5"))   # %
TP3_PCT = float(os.getenv("TP3_PCT", "4.0"))   # %
SL_PCT  = float(os.getenv("SL_PCT",  "2.0"))   # %

# --- State in-memory ---
class TradingState:
    def __init__(self):
        self.reset()
    def reset(self):
        self.trades: List[Dict[str, Any]] = []
        self.next_id = 1
        self.market = {
            "fear_greed": 28,
            "fear_greed_display": "28",
            "btc_dominance": 57.1,
            "btc_dominance_display": "57.1%",
            "market_cap_display": "$3.87T",
            "btc_price_display": "$110,900",
        }
        log.info("♻️ TradingState reset")
    def add_trade(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        trade = dict(trade)
        trade["id"] = self.next_id
        self.next_id += 1
        self.trades.append(trade)
        return trade

STATE = TradingState()

# --- Utils ---
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None

def fmt_price(v: Optional[float]) -> str:
    if v is None: return "-"
    mag = abs(v)
    if mag >= 100:   return f"{v:,.2f}".replace(",", " ")
    if mag >= 1:     return f"{v:,.4f}".replace(",", " ")
    if mag >= 0.01:  return f"{v:,.6f}".replace(",", " ")
    return f"{v:.8f}"

def label_conf(score: int) -> str:
    if score >= 70: return "ÉLEVÉ"
    if score >= 55: return "MOYEN"
    return "FAIBLE"

def compute_confidence(trade: Dict[str, Any], market: Dict[str, Any]) -> Tuple[int, str, List[str]]:
    bullets = []
    score = 50
    fg = market.get("fear_greed", 50)
    btcd = market.get("btc_dominance", 50.0)
    side = (trade.get("side") or "").upper()

    if fg <= 25 and side == "BUY":
        score += 8; bullets.append("✅ Sentiment très bas : opportunité d'achat")
    elif fg >= 75 and side == "SELL":
        score += 6; bullets.append("✅ Euphorie élevée : vente opportuniste")
    else:
        bullets.append("⚠️ Sentiment frileux : avantage modéré")

    sym = (trade.get("symbol") or "").upper()
    is_alt = not (sym.startswith("BTC") or sym.startswith("BTCUSD") or sym == "BTC")
    if is_alt and btcd >= 57.0 and side == "BUY":
        score -= 8; bullets.append("⚠️ BTC.D élevée : pression sur altcoins")
    elif is_alt and btcd < 52.0 and side == "BUY":
        score += 4; bullets.append("✅ BTC.D en baisse : meilleur climat altcoins")

    tf = str(trade.get("tf") or trade.get("timeframe") or "").lower()
    if tf in ("1m","3m","5m"):
        score -= 3; bullets.append("⚠️ TF courte : bruit élevé")
    elif tf in ("1h","4h","240"):
        score += 2; bullets.append("✅ TF plus stable")

    score = max(0, min(100, int(round(score))))
    return score, label_conf(score), bullets

# --- Extraction symbole ---
TICKER_RE = re.compile(r"\b([A-Z0-9]{2,20}(?:USDT|USDC|USD|BTC)(?:\.[PS])?)\b", re.I)

def guess_symbol(payload: Dict[str, Any], raw_text: Optional[str]) -> str:
    sym = (payload.get("symbol") or payload.get("ticker") or "").strip()
    if sym: return sym.upper()
    if raw_text:
        m = re.search(r"—\s*<b>\s*([A-Z0-9\.\-:_/]+)\s*</b>", raw_text, re.I)
        if m: return m.group(1).upper()
        m2 = TICKER_RE.search(raw_text)
        if m2: return m2.group(1).upper()
    return "UNKNOWN"

# --- Parse webhook ---
async def parse_webhook(request: Request) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ctype = request.headers.get("content-type", "")
    raw = (await request.body()).decode(errors="ignore").strip()
    log.info(f"{now_iso()} INFO:main:📥 Webhook content-type: {ctype}")
    data: Dict[str, Any] = {}

    if "application/json" in ctype:
        try:
            data = json.loads(raw or "{}")
            log.info(f"{now_iso()} INFO:main:📥 Webhook payload (keys): {sorted(list(data.keys()))}")
        except Exception:
            log.warning("⚠️ Webhook: JSON invalide")
            return None, raw
    else:
        # text/plain — tenter JSON d'abord
        try:
            data = json.loads(raw)
            log.info(f"{now_iso()} INFO:main:📥 Webhook payload (keys via text->json): {sorted(list(data.keys()))}")
        except Exception:
            # heuristiques sur texte libre
            keys = []
            m = re.search(r"\b(BUY|SELL)\b", raw, re.I)
            if m: data["side"] = m.group(1).upper(); keys.append("side")
            m = re.search(r"\b(1m|3m|5m|15m|30m|1h|4h|D|W)\b", raw, re.I)
            if m: data["tf"] = m.group(1); keys.append("tf")
            # prix : “Entry:”, “price:”, “ix:”, “P ix:”
            m = re.search(r"(?:Entry|price|ix|P\s*ix)\s*[:=]\s*[$]?\s*([0-9]*\.?[0-9]+(?:e-?\d+)?)", raw, re.I)
            if m: data["entry"] = safe_float(m.group(1)); keys.append("entry")
            # stop / take profits éventuels dans le texte
            m = re.search(r"SL\s*[:=]\s*[$]?\s*([0-9]*\.?[0-9]+)", raw, re.I)
            if m: data["sl"] = safe_float(m.group(1)); keys.append("sl")
            m = re.search(r"TP1\s*[:=]\s*[$]?\s*([0-9]*\.?[0-9]+)", raw, re.I)
            if m: data["tp1"] = safe_float(m.group(1)); keys.append("tp1")
            m = re.search(r"TP2\s*[:=]\s*[$]?\s*([0-9]*\.?[0-9]+)", raw, re.I)
            if m: data["tp2"] = safe_float(m.group(1)); keys.append("tp2")
            m = re.search(r"TP3\s*[:=]\s*[$]?\s*([0-9]*\.?[0-9]+)", raw, re.I)
            if m: data["tp3"] = safe_float(m.group(1)); keys.append("tp3")

            m = re.search(r"(?:Heure|Time)\s*[:=]\s*([0-9:\- ]{10,})", raw, re.I)
            if m: data["entry_time"] = m.group(1); keys.append("entry_time")

            data["symbol"] = guess_symbol(data, raw); keys.append("symbol(guessed)")
            log.info(f"{now_iso()} INFO:main:📥 Webhook payload (keys via text): {keys}")

    if not data:
        return None, raw

    action = (data.get("type") or data.get("action") or "").lower()
    if not action:
        # si on voit des indices d'une “entrée”, on force
        if any(k in data for k in ("entry","entry_time","side")):
            action = "entry"
        else:
            log.warning("⚠️ Action inconnue: ''")
            return None, raw
    data["type"] = action
    return data, raw

# --- Telegram ---
def telegram_destination() -> Optional[str]:
    if TELEGRAM_CHAT_ID: return TELEGRAM_CHAT_ID
    if TELEGRAM_CHANNEL_USERNAME: return TELEGRAM_CHANNEL_USERNAME
    return None

def send_telegram(text: str) -> bool:
    dest = telegram_destination()
    if not (TELEGRAM_BOT_TOKEN and dest):
        log.warning("⚠️ Telegram non configuré (TOKEN/CHAT_ID manquant)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": dest, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(3):
        r = httpx.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("✅ Telegram envoyé"); return True
        if r.status_code == 429:
            try: retry = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception: retry = 5
            log.error(f"❌ Telegram: 429 - {r.text}")
            time.sleep(retry + 1); continue
        log.error(f"❌ Telegram: {r.status_code} - {r.text}")
        break
    return False

def compute_missing_targets(side: str, entry: Optional[float],
                            tp1: Optional[float], tp2: Optional[float], tp3: Optional[float],
                            sl: Optional[float]) -> Tuple[Optional[float],Optional[float],Optional[float],Optional[float]]:
    if entry is None:
        return tp1, tp2, tp3, sl
    up = (side.upper() == "BUY")
    if tp1 is None: tp1 = entry * (1 + TP1_PCT/100) if up else entry * (1 - TP1_PCT/100)
    if tp2 is None: tp2 = entry * (1 + TP2_PCT/100) if up else entry * (1 - TP2_PCT/100)
    if tp3 is None: tp3 = entry * (1 + TP3_PCT/100) if up else entry * (1 - TP3_PCT/100)
    if sl  is None: sl  = entry * (1 - SL_PCT/100)  if up else entry * (1 + SL_PCT/100)
    return tp1, tp2, tp3, sl

def format_telegram_trade(trade: Dict[str, Any], market: Dict[str, Any]) -> str:
    side = (trade.get("side") or "").upper()
    direction = "LONG" if side == "BUY" else ("SHORT" if side == "SELL" else "-")
    tf = trade.get("tf") or trade.get("timeframe") or "-"
    sym = (trade.get("symbol") or "UNKNOWN").upper()

    entry = trade.get("entry")
    tp1, tp2, tp3 = trade.get("tp1"), trade.get("tp2"), trade.get("tp3")
    sl = trade.get("sl")
    entry_time = trade.get("entry_time")

    # Fallback : calcule TP/SL si absents et entry dispo
    tp1, tp2, tp3, sl = compute_missing_targets(side, entry, tp1, tp2, tp3, sl)

    score, label, bullets = compute_confidence(trade, market)
    fg = market.get("fear_greed_display", str(market.get("fear_greed", "-")))
    btcd = market.get("btc_dominance_display", str(market.get("btc_dominance", "-")))

    lines = []
    lines.append(f"🎯 NOUVEAU TRADE — <b>{sym}</b>")
    lines.append("")
    lines.append(f"📊 <b>{side}</b>")
    lines.append(f"📈 Direction: <b>{direction}</b> | {tf}")
    if entry_time:
        lines.append(f"🕒 Heure: <code>{entry_time}</code>")
    lines.append("")
    lines.append(f"💰 Entry: ${fmt_price(entry)}")
    # Toujours afficher TP/SL (calculés si besoin)
    lines.append("")
    lines.append("🎯 Take Profits:")
    lines.append(f"  TP1: ${fmt_price(tp1)}")
    lines.append(f"  TP2: ${fmt_price(tp2)}")
    lines.append(f"  TP3: ${fmt_price(tp3)}")
    lines.append(f"\n🛑 Stop Loss: ${fmt_price(sl)}")

    lines.append("")
    lines.append(f"📊 CONFIANCE: <b>{score}% ({label})</b>")
    if bullets:
        lines.append("")
        lines.append("Pourquoi ce score ?")
        for b in bullets:
            lines.append(f"  • {b}")

    lines.append("")
    lines.append(f"💡 Marché: F&G {fg} | BTC.D {btcd}")
    return "\n".join(lines)

# --- API ---
@app.get("/api/trades")
def api_trades():
    return {"trades": STATE.trades, "count": len(STATE.trades)}

@app.post("/api/reset")
def api_reset():
    STATE.reset()
    return {"ok": True}

@app.get("/api/fear-greed")
def api_fg():
    log.info(f"✅ Fear & Greed: {STATE.market['fear_greed']}")
    return {"value": STATE.market["fear_greed"], "display": STATE.market["fear_greed_display"]}

@app.get("/api/bullrun-phase")
def api_bullrun():
    log.info(f"✅ Global: MC {STATE.market['market_cap_display']}, BTC.D {STATE.market['btc_dominance_display']}")
    log.info(f"✅ Prix: BTC {STATE.market['btc_price_display']}")
    return {
        "market_cap": STATE.market["market_cap_display"],
        "btc_dominance": STATE.market["btc_dominance_display"],
        "btc_price": STATE.market["btc_price_display"],
    }

# --- Webhook ---
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    data, raw = await parse_webhook(request)
    if not data:
        return PlainTextResponse("Bad payload", status_code=400)

    action = (data.get("type") or "").lower()
    if action != "entry":
        log.warning(f"⚠️ Action inconnue: '{action}'")
        return PlainTextResponse("Unknown action", status_code=400)

    side = (data.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        direction = (data.get("direction") or "").upper()
        if direction in ("LONG", "SHORT"):
            side = "BUY" if direction == "LONG" else "SELL"
        else:
            return PlainTextResponse("Missing side", status_code=400)

    trade: Dict[str, Any] = {
        "type": "entry",
        "side": side,
        "symbol": guess_symbol(data, raw),
        "tf": data.get("tf") or data.get("timeframe"),
        "entry": safe_float(data.get("entry") or data.get("price") or data.get("px") or data.get("p") or data.get("ix")),
        "tp1": safe_float(data.get("tp1")),
        "tp2": safe_float(data.get("tp2")),
        "tp3": safe_float(data.get("tp3")),
        "sl":  safe_float(data.get("sl")),
        "created_at": data.get("created_at") or now_iso(),
        "entry_time": data.get("entry_time") or data.get("created_at") or now_iso(),
    }

    saved = STATE.add_trade(trade)
    log.info(f"✅ Trade #{saved['id']}: {saved['symbol']} {saved['side']} @ {saved['entry']}")

    text = format_telegram_trade(saved, STATE.market)
    send_telegram(text)
    return JSONResponse({"ok": True, "id": saved["id"]})

# ---- Pages (UI rapide) ----
NAV = """
<nav style="display:flex;gap:10px;margin-bottom:14px">
  <a href="/">🏠 Accueil</a>
  <a href="/trades">📋 Trades</a>
  <a href="/equity-curve">📈 Equity</a>
  <a href="/journal">📝 Journal</a>
  <a href="/heatmap">🔥 Heatmap</a>
  <a href="/strategie">⚙️ Stratégie</a>
  <a href="/backtest">⏮️ Backtest</a>
  <a href="/advanced-metrics">📊 Advanced</a>
  <a href="/annonces">🗞️ Annonces</a>
</nav>
"""

RESET_BTN = """
<button id="resetBtn">♻️ Reset</button>
<script>
document.getElementById('resetBtn').onclick = async () => {
  if (!confirm('Réinitialiser les trades ?')) return;
  await fetch('/api/reset', {method:'POST'});
  location.reload();
};
</script>
"""

@app.get("/")
def home():
    html = f"""
    <html><head><meta charset="utf-8"><title>Dashboard</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h1>Dashboard</h1>
      <p>F&G: <b>{STATE.market['fear_greed_display']}</b> | BTC.D: <b>{STATE.market['btc_dominance_display']}</b> | MC: <b>{STATE.market['market_cap_display']}</b> | BTC: <b>{STATE.market['btc_price_display']}</b></p>
      {RESET_BTN}
      <p>Utilise le menu pour naviguer.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/trades")
def page_trades():
    rows = ["<tr><th>#</th><th>Symbole</th><th>Side</th><th>TF</th><th>Entrée</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th><th>Heure entrée</th><th>Créé</th></tr>"]
    for t in STATE.trades:
        rows.append(
            f"<tr>"
            f"<td>{t['id']}</td>"
            f"<td>{(t.get('symbol') or '-')}</td>"
            f"<td>{(t.get('side') or '-')}</td>"
            f"<td>{(t.get('tf') or '-')}</td>"
            f"<td>{fmt_price(t.get('entry'))}</td>"
            f"<td>{fmt_price(t.get('tp1'))}</td>"
            f"<td>{fmt_price(t.get('tp2'))}</td>"
            f"<td>{fmt_price(t.get('tp3'))}</td>"
            f"<td>{fmt_price(t.get('sl'))}</td>"
            f"<td>{t.get('entry_time') or '-'}</td>"
            f"<td>{t.get('created_at') or '-'}</td>"
            f"</tr>"
        )
    html = f"""
    <html><head><meta charset="utf-8"><title>Trades</title>
    <style>
      table{{border-collapse:collapse;width:100%}}
      th,td{{border:1px solid #ddd;padding:6px;text-align:left}}
      th{{background:#f5f5f5}}
    </style>
    </head>
    <body style="font-family:system-ui;max-width:1200px;margin:20px auto">
      {NAV}
      <h2>Trades</h2>
      {RESET_BTN}
      <table>{''.join(rows)}</table>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/equity-curve")
def page_equity():
    html = f"""
    <html><head><meta charset="utf-8"><title>Equity</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Courbe d'Equity</h2>
      <p>À intégrer avec vos PnL/équity calculés.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/journal")
def page_journal():
    html = f"""
    <html><head><meta charset="utf-8"><title>Journal</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Journal</h2>
      <p>Notes de trading et captures à venir.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/heatmap")
def page_heatmap():
    html = f"""
    <html><head><meta charset="utf-8"><title>Heatmap</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Heatmap</h2>
      <p>Heatmap des perfs à intégrer.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/strategie")
def page_strategie():
    html = f"""
    <html><head><meta charset="utf-8"><title>Stratégie</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Stratégie</h2>
      <p>Paramétrage de la stratégie à afficher ici.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/backtest")
def page_backtest():
    html = f"""
    <html><head><meta charset="utf-8"><title>Backtest</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Backtest</h2>
      <p>Résultats de backtest (tableaux/graphes) à connecter.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/advanced-metrics")
def page_adv():
    html = f"""
    <html><head><meta charset="utf-8"><title>Advanced</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Advanced Metrics</h2>
      <p>Sharpe, Sortino, Max DD, etc.</p>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/annonces")
def page_news():
    html = f"""
    <html><head><meta charset="utf-8"><title>Annonces</title></head>
    <body style="font-family:system-ui;max-width:1000px;margin:20px auto">
      {NAV}
      <h2>Annonces / News 🇫🇷</h2>
      <p>Flux RSS branchés côté backend — à afficher ici si besoin.</p>
    </body></html>
    """
    return HTMLResponse(html)

# --- Seed démo (facultatif) ---
def seed_demo():
    if STATE.trades: return
    demo = [
        {"type":"entry","side":"BUY","symbol":"BTCUSDT","tf":"1h","entry":65000,"tp1":66000,"tp2":67000,"tp3":69000,"sl":63000,"created_at":now_iso(),"entry_time":now_iso()},
        {"type":"entry","side":"SELL","symbol":"ETHUSDT","tf":"1h","entry":3500,"tp1":3400,"tp2":3300,"tp3":3200,"sl":3600,"created_at":now_iso(),"entry_time":now_iso()},
        {"type":"entry","side":"BUY","symbol":"SOLUSDT","tf":"1h","entry":140,"tp1":144,"tp2":147,"tp3":150,"sl":134,"created_at":now_iso(),"entry_time":now_iso()},
    ]
    for d in demo:
        STATE.add_trade(d)
        log.info(f"✅ Trade #{STATE.next_id-1}: {d['symbol']} {d['side']} @ {d['entry']}")
    log.info(f"✅ Démo initialisée avec {len(demo)} trades")

@app.on_event("startup")
def on_start():
    seed_demo()
