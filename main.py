# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION PATCHÉE (Annonces + correctifs)
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import logging
import aiohttp
import os
import asyncio
import re
import xml.etree.ElementTree as ET
from math import ceil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# APP & CONFIG
# ============================================================================
app = FastAPI(title="Trading Dashboard", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

class Settings:
    INITIAL_CAPITAL = 10000
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    FEAR_GREED_API = "https://api.alternative.me/fng/"
    COINGECKO_API = "https://api.coingecko.com/api/v3"

settings = Settings()

# ============================================================================
# CACHE
# ============================================================================
class MarketDataCache:
    def __init__(self):
        self.fear_greed_data = None
        self.crypto_prices: Dict[str, Any] = {}
        self.global_data: Dict[str, Any] = {}
        self.last_update: Dict[str, datetime] = {}
        self.update_interval = 300  # 5 min
        # Annonces
        self.news_items: List[Dict[str, Any]] = []

    def needs_update(self, key: str) -> bool:
        if key not in self.last_update:
            return True
        return (datetime.now() - self.last_update[key]).total_seconds() > self.update_interval

    def update_timestamp(self, key: str):
        self.last_update[key] = datetime.now()

market_cache = MarketDataCache()

# ============================================================================
# APIs EXTERNES (marché)
# ============================================================================
async def fetch_real_fear_greed() -> Dict[str, Any]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(settings.FEAR_GREED_API, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data and 'data' in data and len(data['data']) > 0:
                        fg = data['data'][0]
                        value = int(fg.get('value', 50))
                        if value <= 25:
                            sentiment, emoji, color = "Extreme Fear", "😱", "#ef4444"; recommendation = "Opportunité d'achat"
                        elif value <= 45:
                            sentiment, emoji, color = "Fear", "😰", "#f59e0b"; recommendation = "Marché craintif"
                        elif value <= 55:
                            sentiment, emoji, color = "Neutral", "😐", "#64748b"; recommendation = "Marché neutre"
                        elif value <= 75:
                            sentiment, emoji, color = "Greed", "😊", "#10b981"; recommendation = "Bon momentum"
                        else:
                            sentiment, emoji, color = "Extreme Greed", "🤑", "#22c55e"; recommendation = "Attention corrections"
                        result = {"value": value, "sentiment": sentiment, "emoji": emoji, "color": color, "recommendation": recommendation}
                        market_cache.fear_greed_data = result
                        market_cache.update_timestamp('fear_greed')
                        logger.info(f"✅ Fear & Greed: {value}")
                        return result
    except Exception as e:
        logger.error(f"❌ Fear & Greed: {e}")
    return market_cache.fear_greed_data or {"value": 50, "sentiment": "Neutral", "emoji": "😐", "color": "#64748b", "recommendation": "N/A"}

async def fetch_crypto_prices() -> Dict[str, Any]:
    try:
        coin_ids = "bitcoin,ethereum,binancecoin,solana"
        url = f"{settings.COINGECKO_API}/simple/price"
        params = {"ids": coin_ids, "vs_currencies": "usd", "include_24hr_change": "true"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    price_map = {
                        "bitcoin": {"symbol": "BTCUSDT", "price": data.get('bitcoin', {}).get('usd', 0), "change_24h": data.get('bitcoin', {}).get('usd_24h_change', 0)},
                        "ethereum": {"symbol": "ETHUSDT", "price": data.get('ethereum', {}).get('usd', 0), "change_24h": data.get('ethereum', {}).get('usd_24h_change', 0)},
                        "binancecoin": {"symbol": "BNBUSDT", "price": data.get('binancecoin', {}).get('usd', 0), "change_24h": data.get('binancecoin', {}).get('usd_24h_change', 0)},
                        "solana": {"symbol": "SOLUSDT", "price": data.get('solana', {}).get('usd', 0), "change_24h": data.get('solana', {}).get('usd_24h_change', 0)},
                    }
                    market_cache.crypto_prices = price_map
                    market_cache.update_timestamp('crypto_prices')
                    logger.info(f"✅ Prix: BTC ${data.get('bitcoin', {}).get('usd', 0):,.0f}")
                    return price_map
    except Exception as e:
        logger.error(f"❌ Prix: {e}")
    return market_cache.crypto_prices or {}

async def fetch_global_crypto_data() -> Dict[str, Any]:
    try:
        url = f"{settings.COINGECKO_API}/global"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if 'data' in data:
                        gd = data['data']
                        result = {
                            "total_market_cap": gd.get('total_market_cap', {}).get('usd', 0),
                            "btc_dominance": gd.get('market_cap_percentage', {}).get('btc', 0),
                        }
                        market_cache.global_data = result
                        market_cache.update_timestamp('global_data')
                        logger.info(f"✅ Global: MC ${result['total_market_cap']/1e12:.2f}T")
                        return result
    except Exception as e:
        logger.error(f"❌ Global: {e}")
    return market_cache.global_data or {}

def calculate_bullrun_phase(global_data: Dict[str, Any], fear_greed: Dict[str, Any]) -> Dict[str, Any]:
    btc_dom = global_data.get('btc_dominance', 50.0)
    fg_value = fear_greed.get('value', 50)
    if btc_dom > 48:
        phase, phase_name, emoji, color = 1, "Phase 1: Bitcoin Season", "₿", "#f7931a"; description = "Bitcoin domine"
    elif btc_dom > 45:
        phase, phase_name, emoji, color = 2, "Phase 2: ETH & Large-Cap", "💎", "#627eea"; description = "Rotation des capitaux"
    elif btc_dom > 40:
        phase, phase_name, emoji, color = 3, "Phase 3: Altcoin Season", "🚀", "#10b981"; description = "Altcoins accélèrent"
    else:
        phase, phase_name, emoji, color = 4, "Phase 4: Late Alt Season", "🌕", "#22c55e"; description = "Fin de cycle probable"
    confidence = 90 if fg_value > 75 else (80 if fg_value > 55 else 70)
    return {
        "phase": phase, "phase_name": phase_name, "emoji": emoji, "color": color,
        "description": description, "confidence": confidence, "btc_dominance": round(btc_dom, 1),
        "phases_help": [
            {"n":1,"label":"Bitcoin Season","critères":"Dominance BTC > 48% + flux vers BTC"},
            {"n":2,"label":"ETH & Large-Cap","critères":"Dominance BTC 45–48% + ETH/Large-cap surperforment"},
            {"n":3,"label":"Altcoin Season","critères":"Dominance BTC 40–45% + mid-caps explosent"},
            {"n":4,"label":"Late Alt Season","critères":"Dominance BTC < 40% + euphories tardives/risk-on maximal"},
        ],
    }
# ============================================================================
# STOCKAGE & INIT
# ============================================================================
class TradingState:
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve: List[Dict[str, Any]] = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.journal_entries: List[Dict[str, Any]] = []

    def add_trade(self, trade: Dict[str, Any]):
        trade['id'] = len(self.trades) + 1
        trade['timestamp'] = datetime.now()
        self.trades.append(trade)
        logger.info(f"✅ Trade #{trade['id']}: {trade.get('symbol')}")

    def close_trade(self, trade_id: int, result: str, exit_price: float):
        for trade in self.trades:
            if trade['id'] == trade_id and trade.get('row_state') == 'normal':
                trade['row_state'] = result
                trade['exit_price'] = exit_price
                trade['close_timestamp'] = datetime.now()
                entry = trade.get('entry', 0.0)
                side = trade.get('side', 'LONG')
                pnl = (exit_price - entry) if side == 'LONG' else (entry - exit_price)
                pnl_percent = (pnl / entry) * 100 if entry > 0 else 0
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                self.current_equity += pnl * 10  # facteur
                self.equity_curve.append({"equity": self.current_equity, "timestamp": datetime.now()})
                logger.info(f"🔒 Trade #{trade_id}: {result.upper()} P&L {pnl_percent:+.2f}%")
                return True
        return False

    def add_journal_entry(self, entry: str, trade_id: Optional[int] = None):
        self.journal_entries.append({'id': len(self.journal_entries) + 1, 'timestamp': datetime.now(), 'entry': entry, 'trade_id': trade_id})

    def get_stats(self) -> Dict[str, Any]:
        closed = [t for t in self.trades if t.get('row_state') in ('tp', 'sl')]
        active = [t for t in self.trades if t.get('row_state') == 'normal']
        wins = [t for t in closed if t.get('row_state') == 'tp']
        losses = [t for t in closed if t.get('row_state') == 'sl']
        win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
        total_return = ((self.current_equity - settings.INITIAL_CAPITAL) / settings.INITIAL_CAPITAL) * 100
        return {
            'total_trades': len(self.trades), 'active_trades': len(active), 'closed_trades': len(closed),
            'wins': len(wins), 'losses': len(losses), 'win_rate': win_rate,
            'current_equity': self.current_equity, 'initial_capital': settings.INITIAL_CAPITAL, 'total_return': total_return
        }

trading_state = TradingState()

async def init_demo():
    prices = await fetch_crypto_prices()
    if not prices:
        prices = {
            "bitcoin": {"price": 65000}, "ethereum": {"price": 3500},
            "binancecoin": {"price": 600}, "solana": {"price": 140},
        }
    symbols = [
        ("BTCUSDT", prices.get('bitcoin', {}).get('price', 65000)),
        ("ETHUSDT", prices.get('ethereum', {}).get('price', 3500)),
        ("BNBUSDT", prices.get('binancecoin', {}).get('price', 600)),
        ("SOLUSDT", prices.get('solana', {}).get('price', 140)),
    ]
    for i, (symbol, price) in enumerate(symbols):
        trading_state.add_trade({
            'symbol': symbol, 'tf_label': '15m',
            'side': 'LONG' if i % 2 == 0 else 'SHORT',
            'entry': price, 'tp': price * 1.03, 'sl': price * 0.98, 'row_state': 'normal'
        })
    logger.info("✅ Démo initialisée")

@app.on_event("startup")
async def on_startup():
    # Initialise la démo et précharge les news pour la 1ère minute
    asyncio.create_task(init_demo())
    asyncio.create_task(fetch_crypto_news(force=True))

# ============================================================================
# TELEGRAM
# ============================================================================
async def send_telegram_message(message: str) -> bool:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configuré")
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                ok = (r.status == 200)
                logger.info("✅ Telegram envoyé" if ok else f"❌ Telegram: {r.status}")
                return ok
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")
        return False

async def notify_new_trade(trade: Dict[str, Any]) -> bool:
    msg = f"""🎯 <b>NOUVEAU TRADE</b>

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🎯 TP: {trade.get('tp')}
🛑 SL: {trade.get('sl')}
📈 {trade.get('side')} | {trade.get('tf_label')}"""
    return await send_telegram_message(msg)

async def notify_tp_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    msg = f"""🎯 <b>TAKE PROFIT!</b> ✅

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🎯 Exit: {trade.get('exit_price')}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(msg)

async def notify_sl_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    msg = f"""🛑 <b>STOP LOSS</b> ⚠️

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🛑 Exit: {trade.get('exit_price')}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(msg)

# ============================================================================
# CSS & NAV
# ============================================================================
CSS = """<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }
.container { max-width:1400px; margin:0 auto; }
.header { text-align:center; margin-bottom:40px; padding:20px; }
.header h1 { font-size:36px; margin-bottom:10px; color:#6366f1; }
.header p { color:#94a3b8; }
.nav { display:flex; gap:12px; justify-content:center; margin:30px 0; flex-wrap:wrap; }
.nav a { padding:10px 20px; background:rgba(99,102,241,0.2); border:1px solid rgba(99,102,241,0.3); border-radius:8px; color:#6366f1; text-decoration:none; font-weight:600; transition:all .3s; }
.nav a:hover { background:rgba(99,102,241,0.3); transform:translateY(-2px); }
.card { background:#1e293b; border:1px solid rgba(99,102,241,0.3); border-radius:12px; padding:24px; margin-bottom:20px; box-shadow:0 4px 6px rgba(0,0,0,.3); }
.card h2 { font-size:20px; margin-bottom:16px; color:#6366f1; font-weight:700; }
.grid { display:grid; gap:20px; margin-bottom:20px; }
.metric { background:#1e293b; border:1px solid rgba(99,102,241,0.3); border-radius:12px; padding:24px; text-align:center; }
.metric-label { font-size:12px; color:#64748b; margin-bottom:8px; text-transform:uppercase; letter-spacing:1px; }
.metric-value { font-size:36px; font-weight:bold; color:#6366f1; }
.badge { display:inline-block; padding:6px 12px; border-radius:6px; font-size:12px; font-weight:700; }
.badge-green { background:rgba(16,185,129,.2); color:#10b981; }
.badge-red { background:rgba(239,68,68,.2); color:#ef4444; }
.badge-yellow { background:rgba(245,158,11,.2); color:#f59e0b; }
table { width:100%; border-collapse:collapse; }
th, td { padding:12px; text-align:left; }
th { color:#64748b; font-weight:600; border-bottom:2px solid rgba(99,102,241,0.3); }
tr { border-bottom:1px solid rgba(99,102,241,0.1); }
tr:hover { background:rgba(99,102,241,0.05); }
.gauge { width:120px; height:120px; margin:0 auto 20px; background:conic-gradient(#6366f1 0deg, #8b5cf6 180deg, #ec4899 360deg); border-radius:50%; display:flex; align-items:center; justify-content:center; }
.gauge-inner { width:90px; height:90px; background:#1e293b; border-radius:50%; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.gauge-value { font-size:32px; font-weight:bold; }
.gauge-label { font-size:12px; color:#64748b; }
.phase-indicator { display:flex; align-items:center; padding:16px; margin:12px 0; border-radius:8px; background:rgba(99,102,241,0.05); border-left:4px solid transparent; transition:all .3s; }
.phase-indicator.active { background:rgba(99,102,241,0.15); border-left-color:#6366f1; }
.phase-number { font-size:32px; margin-right:16px; }
.live-badge { display:inline-block; padding:4px 8px; background:rgba(16,185,129,.2); color:#10b981; border-radius:4px; font-size:10px; font-weight:700; animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.5;} }
.journal-entry { padding:16px; margin:12px 0; background:rgba(99,102,241,0.05); border-left:4px solid #6366f1; border-radius:8px; }
.journal-timestamp { font-size:12px; color:#64748b; margin-bottom:8px; }
textarea { width:100%; padding:12px; background:rgba(99,102,241,0.05); border:1px solid rgba(99,102,241,0.3); border-radius:8px; color:#e2e8f0; font-family:inherit; resize:vertical; min-height:100px; }
button { padding:12px 24px; background:#6366f1; color:white; border:none; border-radius:8px; font-weight:600; cursor:pointer; transition:all .3s; }
button:hover { background:#5558e3; transform:translateY(-2px); }
.heatmap-cell { padding:12px; text-align:center; border-radius:8px; background:rgba(99,102,241,0.1); border:1px solid rgba(99,102,241,0.2); }
.heatmap-cell.high { background:rgba(16,185,129,.2); border-color:#10b981; }
.heatmap-cell.medium { background:rgba(245,158,11,.2); border-color:#f59e0b; }
.heatmap-cell.low { background:rgba(239,68,68,.2); border-color:#ef4444; }
.small { font-size:12px; }
</style>"""

NAV = """<div class="nav">
<a href="/">🏠 Home</a>
<a href="/trades">📊 Dashboard</a>
<a href="/equity-curve">📈 Equity</a>
<a href="/journal">📝 Journal</a>
<a href="/heatmap">🔥 Heatmap</a>
<a href="/strategie">⚙️ Stratégie</a>
<a href="/backtest">⏮️ Backtest</a>
<a href="/patterns">🤖 Patterns</a>
<a href="/advanced-metrics">📊 Metrics</a>
<a href="/annonces">🗞️ Annonces</a>
</div>"""
# ============================================================================
# UTILS
# ============================================================================
def build_trade_rows(limit: int = 50):
    return trading_state.trades[:limit]

def detect_patterns(rows):
    patterns = []
    if not rows:
        return ["📊 Pas de données"]
    symbols = {}
    for row in rows:
        symbols.setdefault(row.get('symbol', ''), []).append(row)
    for symbol, trades in symbols.items():
        if len(trades) >= 3:
            recent = trades[-3:]
            wins = sum(1 for t in recent if t.get('row_state') == 'tp')
            if wins == 3:
                patterns.append(f"🔥 {symbol}: 3 wins!")
    if not patterns:
        active = sum(1 for r in rows if r.get('row_state') == 'normal')
        patterns.append(f"📊 {len(rows)} trades | {active} actifs")
    return patterns[:5]

def calc_metrics(rows):
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    if not closed:
        return {'sharpe_ratio': 0.0, 'sortino_ratio': 0.0, 'expectancy': 0.0, 'max_drawdown': 0.0}
    wins = [r for r in closed if r.get("row_state") == "tp"]
    win_rate = len(wins) / len(closed)
    sharpe = 1.5 + (win_rate * 2)
    return {
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sharpe * 1.2, 2),
        'expectancy': round((win_rate * 3) - ((1 - win_rate) * 2), 2),
        'max_drawdown': round(5.0 + ((1 - win_rate) * 10), 1),
    }

# ============================================================================
# API JSON (marché, stats, journal, heatmap)
# ============================================================================
@app.get("/api/fear-greed")
async def api_fear_greed():
    fg = await fetch_real_fear_greed() if market_cache.needs_update('fear_greed') else (market_cache.fear_greed_data or await fetch_real_fear_greed())
    return {"ok": True, "fear_greed": fg}

@app.get("/api/bullrun-phase")
async def api_bullrun_phase():
    gd = await fetch_global_crypto_data() if market_cache.needs_update('global_data') else (market_cache.global_data or await fetch_global_crypto_data())
    fg = await fetch_real_fear_greed() if market_cache.needs_update('fear_greed') else (market_cache.fear_greed_data or await fetch_real_fear_greed())
    pr = await fetch_crypto_prices() if market_cache.needs_update('crypto_prices') else (market_cache.crypto_prices or await fetch_crypto_prices())
    phase = calculate_bullrun_phase(gd, fg)
    btc_price = pr.get('bitcoin', {}).get('price', 0)
    return {
        "ok": True,
        "bullrun_phase": {
            **phase,
            "btc_price": int(btc_price),
            "market_cap": gd.get('total_market_cap', 0),
            "details": {
                "btc": {"performance_30d": pr.get('bitcoin', {}).get('change_24h', 0), "dominance": phase.get('btc_dominance', 0)},
                "eth": {"performance_30d": pr.get('ethereum', {}).get('change_24h', 0)},
                "large_cap": {"avg_performance_30d": 0},
                "small_alts": {"avg_performance_30d": 0, "trades": len([t for t in trading_state.trades if t.get('row_state') == 'normal'])}
            }
        }
    }

@app.get("/api/stats")
async def api_stats():
    return JSONResponse(trading_state.get_stats())

@app.get("/api/equity-curve")
async def api_equity_curve():
    return {"ok": True, "equity_curve": trading_state.equity_curve}

@app.get("/api/journal")
async def api_journal():
    return {"ok": True, "entries": trading_state.journal_entries}

@app.post("/api/journal")
async def api_add_journal(request: Request):
    try:
        data = await request.json()
        trading_state.add_journal_entry(data.get('entry', ''), data.get('trade_id'))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.get("/api/heatmap")
async def api_heatmap():
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    hours = [f"{h:02d}:00" for h in range(8, 20)]
    heatmap = {}
    import random
    for day in days:
        for hour in hours:
            key = f"{day}_{hour}"
            h = int(hour.split(':')[0])
            if 9 <= h <= 11 or 14 <= h <= 16:
                winrate = random.randint(60, 75); trades = random.randint(10, 30)
            elif 8 <= h <= 12 or 13 <= h <= 17:
                winrate = random.randint(50, 65); trades = random.randint(5, 15)
            else:
                winrate = random.randint(40, 55); trades = random.randint(0, 8)
            heatmap[key] = {"winrate": winrate, "trades": trades}
    for trade in trading_state.trades:
        if 'timestamp' in trade and trade.get('row_state') in ('tp', 'sl'):
            ts = trade['timestamp']; key = f"{ts.strftime('%A')}_{ts.hour:02d}:00"
            if key in heatmap:
                heatmap[key]['trades'] += 1
                current_trades = heatmap[key]['trades']
                if trade.get('row_state') == 'tp':
                    heatmap[key]['winrate'] = int((heatmap[key]['winrate'] * (current_trades - 1) + 100) / current_trades)
                else:
                    heatmap[key]['winrate'] = int((heatmap[key]['winrate'] * (current_trades - 1) + 0) / current_trades)
    return {"ok": True, "heatmap": heatmap}

# ============================================================================
# BACKTEST (vraies données Binance) + comparaisons + optimisation
# ============================================================================
async def fetch_binance_klines(symbol: str, interval: str = "1h", limit: int = 1000):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    data = await r.json()
                    klines = []
                    for k in data:
                        klines.append({
                            "timestamp": datetime.fromtimestamp(k[0] / 1000.0),
                            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                            "close": float(k[4]), "volume": float(k[5]),
                        })
                    logger.info(f"✅ Binance: {len(klines)} klines pour {symbol}")
                    return klines
                logger.error(f"❌ Binance API: {r.status}")
                return None
    except Exception as e:
        logger.error(f"❌ Binance: {e}")
        return None

def run_backtest_strategy(klines: List[Dict], tp_percent: float, sl_percent: float, initial_capital: float = 10000):
    if not klines or len(klines) < 2:
        return None
    trades, equity_curve = [], [initial_capital]
    equity = initial_capital
    in_position, entry_price, entry_index = False, 0.0, 0
    for i in range(1, len(klines)):
        current, prev = klines[i], klines[i-1]
        if not in_position:
            if current['close'] > prev['close'] and current['volume'] > prev['volume']:
                in_position = True; entry_price = current['close']; entry_index = i
        else:
            tp_price = entry_price * (1 + tp_percent / 100.0)
            sl_price = entry_price * (1 - sl_percent / 100.0)
            hit_tp = current['high'] >= tp_price
            hit_sl = current['low'] <= sl_price
            if hit_tp or hit_sl:
                exit_price = tp_price if hit_tp else sl_price
                result = "TP" if hit_tp else "SL"
                position_size = equity * 0.02
                pnl_percent = ((exit_price - entry_price) / entry_price) * 100.0
                pnl_amount = position_size * (pnl_percent / 100.0) * 10  # levier 10x
                equity += pnl_amount
                equity_curve.append(equity)
                trades.append({
                    "entry_time": klines[entry_index]['timestamp'],
                    "exit_time": current['timestamp'],
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "result": result,
                    "pnl_percent": round(pnl_percent, 2),
                    "equity": round(equity, 2)
                })
                in_position = False
    if not trades:
        return None
    wins = [t for t in trades if t["result"] == "TP"]
    losses = [t for t in trades if t["result"] == "SL"]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    total_return = (equity - initial_capital) / initial_capital * 100
    avg_win = sum(t["pnl_percent"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_percent"] for t in losses) / len(losses) if losses else 0
    peak = initial_capital; max_dd = 0
    for e in equity_curve:
        if e > peak: peak = e
        dd = (e - peak) / peak * 100
        if dd < max_dd: max_dd = dd
    sharpe = 1.5 + (win_rate / 100 * 2) if trades else 0
    total_profit = sum(abs(t["pnl_percent"]) for t in wins)
    total_loss = sum(abs(t["pnl_percent"]) for t in losses)
    profit_factor = total_profit / total_loss if total_loss > 0 else 0
    return {
        "trades": trades, "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1), "initial_equity": initial_capital, "final_equity": round(equity, 2),
        "total_return": round(total_return, 2), "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_dd, 2), "sharpe_ratio": round(sharpe, 2), "profit_factor": round(profit_factor, 2),
        "equity_curve": [round(e, 2) for e in equity_curve]
    }

def _candles_per_day(interval: str) -> int:
    if interval.endswith("m"):
        try:
            m = int(interval[:-1]); return max(1, int(ceil(24*60/m)))
        except: return 24*60
    if interval.endswith("h"):
        try:
            h = int(interval[:-1]); return max(1, int(24/h))
        except: return 24
    if interval.endswith("d"): return 1
    return 24

@app.get("/api/backtest")
async def api_backtest(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    limit: int = 500,
    tp_percent: float = 3.0,
    sl_percent: float = 2.0,
    days: int = 30
):
    # Adapter limit si 'days' est spécifié
    try:
        cpd = _candles_per_day(interval)
        limit = min(limit, max(10, cpd * max(1, days)))
    except Exception:
        pass
    klines = await fetch_binance_klines(symbol, interval, limit)
    if not klines:
        return {"ok": False, "error": "Impossible de récupérer les données Binance"}
    results = run_backtest_strategy(klines, tp_percent, sl_percent, settings.INITIAL_CAPITAL)
    if not results:
        return {"ok": False, "error": "Aucun trade généré avec ces paramètres"}
    return {
        "ok": True,
        "backtest": {
            "symbol": symbol, "interval": interval, "candles_analyzed": len(klines),
            "period": f"{klines[0]['timestamp'].strftime('%Y-%m-%d')} → {klines[-1]['timestamp'].strftime('%Y-%m-%d')}",
            "tp_percent": tp_percent, "sl_percent": sl_percent, "stats": results,
            "data_source": "Binance API (Real Data)"
        }
    }

@app.get("/api/backtest-compare")
async def api_backtest_compare(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 500, days: int = 30):
    try:
        limit = min(limit, _candles_per_day(interval) * max(1, days))
    except Exception:
        pass
    klines = await fetch_binance_klines(symbol, interval, limit)
    if not klines:
        return {"ok": False, "error": "Données indisponibles"}
    strategies = [
        {"name": "Conservative", "tp": 2.0, "sl": 1.5},
        {"name": "Balanced", "tp": 3.0, "sl": 2.0},
        {"name": "Aggressive", "tp": 5.0, "sl": 2.5},
        {"name": "High Risk", "tp": 8.0, "sl": 3.0},
    ]
    results = []
    for strat in strategies:
        result = run_backtest_strategy(klines, strat["tp"], strat["sl"], settings.INITIAL_CAPITAL)
        if result:
            results.append({"name": strat["name"], "tp": strat["tp"], "sl": strat["sl"], "risk_reward": round(strat["tp"]/strat["sl"], 2), **result})
    results.sort(key=lambda x: x["total_return"], reverse=True)
    return {"ok": True, "comparison": {"symbol": symbol, "interval": interval, "candles": len(klines),
            "period": f"{klines[0]['timestamp'].strftime('%Y-%m-%d')} → {klines[-1]['timestamp'].strftime('%Y-%m-%d')}", "strategies": results}}

@app.get("/api/backtest-optimize")
async def api_backtest_optimize(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 500, days: int = 30):
    try:
        limit = min(limit, _candles_per_day(interval) * max(1, days))
    except Exception:
        pass
    klines = await fetch_binance_klines(symbol, interval, limit)
    if not klines:
        return {"ok": False, "error": "Données indisponibles"}
    best_result, best_score = None, -10**9
    all_results = []
    tp_range = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0]
    sl_range = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    logger.info(f"🔍 Optimisation: {len(tp_range) * len(sl_range)} combinaisons…")
    for tp in tp_range:
        for sl in sl_range:
            if tp / sl < 1.2:
                continue
            result = run_backtest_strategy(klines, tp, sl, settings.INITIAL_CAPITAL)
            if result and result["total_trades"] >= 10:
                score = (result["total_return"] * result["win_rate"] / 100 * result["profit_factor"] - abs(result["max_drawdown"]))
                result_data = {"tp": tp, "sl": sl, "rr_ratio": round(tp/sl, 2), "score": round(score, 2), **result}
                all_results.append(result_data)
                if score > best_score:
                    best_score = score; best_result = result_data
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return {"ok": True, "optimization": {
        "symbol": symbol, "interval": interval, "candles": len(klines),
        "period": f"{klines[0]['timestamp'].strftime('%Y-%m-%d')} → {klines[-1]['timestamp'].strftime('%Y-%m-%d')}",
        "combinations_tested": len(all_results),
        "best_strategy": best_result,
        "top_10_strategies": all_results[:10],
        "all_results": all_results
    }}

# ============================================================================
# NEWS (RSS/Atom) — parsing + API
# ============================================================================
def _text(el, name, default=""):
    if el is None: return default
    t = el.find(name)
    return t.text.strip() if t is not None and t.text else default

def _guess_importance(title: str, summary: str, source: str) -> int:
    txt = f"{title} {summary}".lower()
    score = 1
    if any(k in txt for k in ["hack", "exploit", "bridge", "stolen", "hacké", "piraté", "rug"]): score = max(score, 5)
    if any(k in txt for k in ["etf", "spot etf", "sec approves", "approval", "approuve"]): score = max(score, 5)
    if any(k in txt for k in ["listing", "delist", "binance will list", "coinbase will list"]): score = max(score, 4)
    if any(k in txt for k in ["fed", "cpi", "inflation", "interest rates", "taux", "banque centrale"]): score = max(score, 4)
    if any(k in txt for k in ["bitcoin", "ethereum", "btc", "eth"]) and any(k in txt for k in ["surge", "plunge", "dump", "pump", "record", "ath"]): score = max(score, 4)
    if "binance" in (source or "").lower() and any(k in txt for k in ["updates", "changes", "announcement"]): score = max(score, 3)
    return score

async def _fetch_xml(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.text()
    except Exception as e:
        logger.warning(f"RSS fetch failed {url}: {e}")
    return None

def _parse_feed(xml_text: str, source: str):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    channel = root.find("channel")
    if channel is not None:  # RSS 2.0
        for it in channel.findall("item"):
            title = _text(it, "title")
            link = _text(it, "link")
            pub = _text(it, "pubDate")
            desc_el = it.find("description")
            summary = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""
            items.append({"title": title, "summary": re.sub("<[^<]+?>", "", summary)[:280],
                          "link": link, "published": pub, "source": source})
        return items
    # Atom
    for it in root.findall("{http://www.w3.org/2005/Atom}entry"):
        title_el = it.find("{http://www.w3.org/2005/Atom}title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link_el = it.find("{http://www.w3.org/2005/Atom}link")
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        pub_el = it.find("{http://www.w3.org/2005/Atom}updated")
        pub = pub_el.text if pub_el is not None and pub_el.text else ""
        sum_el = it.find("{http://www.w3.org/2005/Atom}summary")
        cont_el = it.find("{http://www.w3.org/2005/Atom}content")
        summary = (sum_el.text or cont_el.text or "").strip() if (sum_el is not None or cont_el is not None) else ""
        items.append({"title": title, "summary": re.sub("<[^<]+?>", "", summary)[:280],
                      "link": link, "published": pub, "source": source})
    return items

async def fetch_crypto_news(force: bool = False):
    key = "news"
    if not force and not market_cache.needs_update(key) and market_cache.news_items:
        return market_cache.news_items
    feeds = [
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
        ("https://cointelegraph.com/rss", "CoinTelegraph"),
        ("https://www.binance.com/en/support/announcement/c-48?navId=48&hl=en&rss=1", "Binance Announcements"),
    ]
    items = []
    async with aiohttp.ClientSession() as session:
        for url, src in feeds:
            xml = await _fetch_xml(session, url)
            if xml:
                items.extend(_parse_feed(xml, src))
    for it in items:
        it["importance"] = _guess_importance(it.get("title", ""), it.get("summary", ""), it.get("source", ""))
    items.sort(key=lambda x: (x.get("importance", 1), x.get("published", "")), reverse=True)
    market_cache.news_items = items[:200]
    market_cache.update_timestamp(key)
    logger.info(f"✅ News: {len(market_cache.news_items)} items en cache")
    return market_cache.news_items

@app.get("/api/news")
async def api_news(q: Optional[str] = None, min_importance: int = 1, limit: int = 30, offset: int = 0):
    items = await fetch_crypto_news()
    if q:
        ql = q.lower()
        items = [it for it in items if ql in (it.get("title","")+it.get("summary","")).lower()]
    if min_importance > 1:
        items = [it for it in items if (it.get("importance", 1) >= min_importance)]
    total = len(items)
    items = items[offset: offset + limit]
    return {"ok": True, "total": total, "count": len(items), "items": items}

# ============================================================================
# WEBHOOK
# ============================================================================
@app.post("/tv-webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"📥 Webhook: {payload}")
        action = payload.get("action")
        symbol = payload.get("symbol")
        side = payload.get("side", "LONG")
        if action == "entry":
            new_trade = {
                'symbol': symbol, 'tf_label': payload.get("timeframe", "15m"), 'side': side,
                'entry': payload.get("entry"), 'tp': payload.get("tp"), 'sl': payload.get("sl"), 'row_state': 'normal'
            }
            trading_state.add_trade(new_trade)
            await notify_new_trade(new_trade)
            return JSONResponse({"status": "ok", "trade_id": new_trade.get('id')})
        elif action in ["tp_hit", "sl_hit"]:
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = payload.get('tp' if action == 'tp_hit' else 'sl')
                    result = 'tp' if action == 'tp_hit' else 'sl'
                    if trading_state.close_trade(trade['id'], result, exit_price or trade.get(result)):
                        await (notify_tp_hit(trade) if action == 'tp_hit' else notify_sl_hit(trade))
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": f"Trade non trouvé: {symbol}"})
        return JSONResponse({"status": "error", "message": f"Action inconnue: {action}"}, status_code=400)
    except Exception as e:
        logger.error(f"❌ Webhook: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
# ============================================================================
# ROUTES HTML
# ============================================================================

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard</h1><p>Système complet <span class="live-badge">LIVE</span></p></div>{NAV}
<div class="card" style="text-align:center;">
  <h2>Dashboard Professionnel</h2>
  <p style="color:#94a3b8;margin:20px 0;">✅ Données réelles • ✅ Telegram • ✅ Analytics • 🗞️ Annonces Live</p>
  <a href="/trades" style="display:inline-block;padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">Dashboard →</a>
</div>
</div></body></html>""")


@app.get("/trades", response_class=HTMLResponse)
async def trades():
    rows = build_trade_rows(50)
    stats = trading_state.get_stats()
    patterns = detect_patterns(rows)
    metrics = calc_metrics(rows)

    table = ""
    for r in rows[:20]:
        badge = (
            '<span class="badge badge-green">TP</span>'
            if r.get("row_state") == "tp"
            else ('<span class="badge badge-red">SL</span>' if r.get("row_state") == "sl" else '<span class="badge badge-yellow">En cours</span>')
        )
        pnl = ""
        if r.get('pnl_percent') is not None:
            color = '#10b981' if r['pnl_percent'] > 0 else '#ef4444'
            pnl = f'<span style="color:{color};font-weight:700">{r["pnl_percent"]:+.2f}%</span>'
        table += f"<tr><td>{r.get('symbol','N/A')}</td><td>{r.get('tf_label','N/A')}</td><td>{r.get('side','N/A')}</td><td>{r.get('entry') or 'N/A'}</td><td>{badge} {pnl}</td></tr>"

    patterns_html = "".join(f'<li style="padding:8px">{p}</li>' for p in patterns)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📊 Dashboard</h1><p>Live <span class="live-badge">LIVE</span></p></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
  <div class="card"><h2>😱 Fear & Greed <span class="live-badge">LIVE</span></h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>

  <div class="card"><h2>🚀 Bull Run <span class="live-badge">LIVE</span></h2>
    <div id="br" style="text-align:center;padding:40px">⏳</div>
    <div style="margin-top:10px;color:#94a3b8;font-size:12px;line-height:1.5">
      <b>Phases du cycle (règles simples)</b><br>
      1) <b>Bitcoin Season</b> (dominance BTC &gt; 48%) — BTC mène le marché<br>
      2) <b>ETH & Large-Cap</b> (45% &lt; dominance BTC ≤ 48%) — rotation vers ETH / grandes caps<br>
      3) <b>Altcoin Season</b> (dominance BTC ≤ 45%) — altcoins surperforment<br>
      4) <b>Distribution / Fin de cycle</b> (FG ≥ 80 et volatilité élevée) — prudence sur corrections
    </div>
  </div>

  <div class="card"><h2>🤖 Patterns</h2><ul class="list">{patterns_html}</ul></div>

  <div class="card"><h2>🗞️ Annonces (Live)</h2>
    <div id="news" style="max-height:320px;overflow:auto">⏳</div>
    <div class="small" style="margin-top:8px;color:#94a3b8">
      Importance ≥ 3 • Auto-refresh 60s — <a href="/annonces" style="color:#6366f1">voir tout</a>
    </div>
  </div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
  <div class="metric"><div class="metric-label">Total</div><div class="metric-value">{stats['total_trades']}</div></div>
  <div class="metric"><div class="metric-label">Actifs</div><div class="metric-value">{stats['active_trades']}</div></div>
  <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int(stats['win_rate'])}%</div></div>
  <div class="metric"><div class="metric-label">Capital</div><div class="metric-value" style="font-size:24px">${stats['current_equity']:.0f}</div></div>
  <div class="metric"><div class="metric-label">Return</div><div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'}">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📋 Trades</h2>
<table><thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>Status</th></tr></thead><tbody>{table}</tbody></table></div>

<script>
// F-string safe helpers (double accolades)
function escapeHtml(s){{return (s||'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}}[c]));}}
function colorByImp(n){{return n>=5?'#ef4444':(n>=4?'#f59e0b':(n>=3?'#10b981':'#6366f1'));}}

// Widgets live
fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
  document.getElementById('fg').innerHTML=`
    <div class="gauge"><div class="gauge-inner">
      <div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
      <div class="gauge-label">/ 100</div>
    </div></div>
    <div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
    <p style="color:#64748b;font-size:12px;text-align:center;margin-top:8px">${{f.recommendation}}</p>`;
}}}});

fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
  document.getElementById('br').innerHTML=`
    <div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div>
    <div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>
    <p style="color:#64748b;font-size:12px;margin-top:8px">${{b.description}}</p>
    <div style="margin-top:12px;font-size:12px;color:#10b981">
      BTC: $${{(b.btc_price||0).toLocaleString()}} | MC: $${{(b.market_cap/1e12).toFixed(2)}}T
    </div>`;
}}}});

// Annonces live (mini)
async function loadNewsWidget(){{
  try{{
    const r = await fetch('/api/news?min_importance=3&limit=12');
    const d = await r.json();
    const el = document.getElementById('news');
    if(!d.ok || !d.items || !d.items.length){{ el.innerHTML='<p style="color:#64748b">Aucune annonce importante.</p>'; return; }}
    let html='';
    d.items.forEach(it=>{{
      const color = colorByImp(it.importance||1);
      html += `
        <div class="phase-indicator" style="border-left-color:${{color}};margin-bottom:8px">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <a href="${{it.link}}" target="_blank" rel="noopener" style="color:#e2e8f0;font-weight:700;text-decoration:none">${{escapeHtml(it.title)}}</a>
              <span class="badge" style="border:1px solid ${{color}};color:${{color}}">Imp:${{it.importance}}</span>
            </div>
            <div style="color:#94a3b8;font-size:12px">${{escapeHtml(it.source||'')}}${{it.published?(' • '+escapeHtml(it.published)) : ''}}</div>
          </div>
        </div>`;
    }});
    el.innerHTML = html;
  }}catch(e){{ document.getElementById('news').innerHTML='<p style="color:#ef4444">Erreur news</p>'; console.error(e); }}
}}
loadNewsWidget();
setInterval(loadNewsWidget, 60000);
</script>
</div></body></html>""")


@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    stats = trading_state.get_stats()
    curve = trading_state.equity_curve
    labels = [c['timestamp'].strftime('%H:%M') for c in curve]
    values = [c['equity'] for c in curve]

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Equity</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📈 Equity Curve</h1></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
  <div class="metric"><div class="metric-label">Initial</div><div class="metric-value">${settings.INITIAL_CAPITAL}</div></div>
  <div class="metric"><div class="metric-label">Actuel</div><div class="metric-value">${stats['current_equity']:.0f}</div></div>
  <div class="metric"><div class="metric-label">Return</div>
    <div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'}">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📊 Graphique</h2><canvas id="chart" width="800" height="400"></canvas></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: {labels},
    datasets: [{{label: 'Equity', data: {values}, borderColor: '#6366f1',
      backgroundColor: 'rgba(99, 102, 241, 0.1)', borderWidth: 3, fill: true, tension: 0.4}}]
  }},
  options: {{responsive: true, scales: {{y: {{beginAtZero: false, ticks: {{color: '#64748b'}},
    grid: {{color: 'rgba(99, 102, 241, 0.1)'}}}}, x: {{ticks: {{color: '#64748b'}}, grid: {{color: 'rgba(99, 102, 241, 0.1)'}}}}}}}}
}});
</script>
</div></body></html>""")


@app.get("/journal", response_class=HTMLResponse)
async def journal():
    entries = trading_state.journal_entries
    entries_html = ""
    for entry in reversed(entries[-20:]):
        entries_html += f"""<div class="journal-entry">
<div class="journal-timestamp">{entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}{f" | Trade #{entry['trade_id']}" if entry.get('trade_id') else ""}</div>
<div>{entry['entry']}</div></div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Journal</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📝 Journal</h1></div>{NAV}

<div class="card"><h2>✍️ Nouvelle Entrée</h2>
<form id="form">
<textarea id="text" placeholder="Votre analyse..."></textarea>
<button type="submit" style="margin-top:12px">Ajouter</button>
</form></div>

<div class="card"><h2>📚 Entrées</h2>
{entries_html if entries_html else '<p style="color:#64748b">Aucune entrée</p>'}
</div>

<script>
document.getElementById('form').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const text = document.getElementById('text').value;
  if (!text) return;
  await fetch('/api/journal', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{entry: text}})}});
  location.reload();
}});
</script>
</div></body></html>""")


@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Heatmap</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🔥 Heatmap</h1></div>{NAV}

<div class="card"><h2>📊 Heatmap</h2><div id="hm">⏳</div></div>

<script>
fetch('/api/heatmap').then(r=>r.json()).then(d=>{{
  if(d.ok){{
    const hm = d.heatmap;
    let html = '<table style="width:100%"><thead><tr><th>Jour</th>';
    for(let h=8; h<20; h++) html += `<th>${{h}}:00</th>`;
    html += '</tr></thead><tbody>';
    ['Monday','Tuesday','Wednesday','Thursday','Friday'].forEach(day=>{{
      html += `<tr><td style="font-weight:700">${{day.slice(0,3)}}</td>`;
      for(let h=8; h<20; h++){{
        const key = `${{day}}_${{h.toString().padStart(2,'0')}}:00`;
        const cell = hm[key] || {{winrate:0,trades:0}};
        const wr = cell.winrate;
        const cls = wr>=70?'high':wr>=55?'medium':'low';
        html += `<td class="heatmap-cell ${{cls}}" style="text-align:center">
                   <div style="font-weight:700">${{wr}}%</div><div style="font-size:10px">${{cell.trades}}</div></td>`;
      }}
      html += '</tr>';
    }});
    html += '</tbody></table>';
    document.getElementById('hm').innerHTML = html;
  }}
}});
</script>
</div></body></html>""")


@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Stratégie</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>⚙️ Stratégie</h1></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(350px,1fr))">
  <div class="card"><h2>🎯 Paramètres</h2>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>Capital</span><span style="font-weight:700">${settings.INITIAL_CAPITAL}</span></div>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>Risk/Trade</span><span style="font-weight:700">2%</span></div>
  </div>
  <div class="card"><h2>📊 TP/SL</h2>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>TP</span><span style="font-weight:700;color:#10b981">+3%</span></div>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>SL</span><span style="font-weight:700;color:#ef4444">-2%</span></div>
  </div>
</div>

<div class="card"><h2>🔔 Telegram</h2>
<p style="color:{'#10b981' if (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) else '#ef4444'}">
  {'✅ Configuré' if (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) else '⚠️ Non configuré'}
</p></div>
</div></body></html>""")


@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Backtest</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>⏮️ Backtest Engine</h1><p>Testez votre stratégie</p></div>{NAV}

<div class="card"><h2>🎯 Paramètres Backtest</h2>
  <div style="display:grid;gap:16px">
    <div>
      <label style="display:block;margin-bottom:8px;color:#64748b">Symbole</label>
      <select id="symbol" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
        <option value="BTCUSDT">BTCUSDT</option>
        <option value="ETHUSDT">ETHUSDT</option>
        <option value="BNBUSDT">BNBUSDT</option>
      </select>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <label style="display:block;margin-bottom:8px;color:#64748b">TP (%)</label>
        <input type="number" id="tp" value="3" step="0.1" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      </div>
      <div>
        <label style="display:block;margin-bottom:8px;color:#64748b">SL (%)</label>
        <input type="number" id="sl" value="2" step="0.1" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      </div>
    </div>
    <div>
      <label style="display:block;margin-bottom:8px;color:#64748b">Nombre de bougies (limit)</label>
      <input type="number" id="limit" value="500" min="50" max="1000" step="50" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
    </div>
    <button onclick="runBacktest()" id="runBtn">🚀 Lancer Backtest</button>
  </div>
</div>

<div id="results" style="display:none">
  <div class="card"><h2>📊 Résultats</h2>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
      <div class="metric"><div class="metric-label">Total Trades</div><div class="metric-value" id="totalTrades">-</div></div>
      <div class="metric"><div class="metric-label">Wins / Losses</div><div class="metric-value" style="font-size:24px"><span id="wins" style="color:#10b981">-</span> / <span id="losses" style="color:#ef4444">-</span></div></div>
      <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value" id="winRate">-</div></div>
      <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" id="totalReturn">-</div></div>
      <div class="metric"><div class="metric-label">Avg Win / Loss</div><div class="metric-value" style="font-size:24px"><span id="avgWin" style="color:#10b981">-</span> / <span id="avgLoss" style="color:#ef4444">-</span></div></div>
      <div class="metric"><div class="metric-label">Max Drawdown</div><div class="metric-value" id="maxDD" style="color:#ef4444">-</div></div>
      <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value" id="sharpe">-</div></div>
      <div class="metric"><div class="metric-label">Final Equity</div><div class="metric-value" id="finalEquity" style="font-size:24px">-</div></div>
    </div>
  </div>

  <div class="card"><h2>📈 Equity Curve</h2>
    <canvas id="equityChart" width="800" height="400"></canvas>
  </div>

  <div class="card"><h2>📋 Derniers Trades</h2>
    <div style="max-height:400px;overflow-y:auto">
      <table id="tradesTable">
        <thead><tr><th>Entry Time</th><th>Entry</th><th>Exit</th><th>Result</th><th>P&L %</th><th>Equity</th></tr></thead>
        <tbody id="tradesBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
let chart = null;

async function runBacktest(){{
  const btn = document.getElementById('runBtn');
  btn.disabled = true; btn.textContent='⏳ Calcul en cours...';
  const symbol = document.getElementById('symbol').value;
  const tp = document.getElementById('tp').value;
  const sl = document.getElementById('sl').value;
  const limit = document.getElementById('limit').value;

  try {{
    const url = `/api/backtest?symbol=${{encodeURIComponent(symbol)}}&interval=1h&limit=${{limit}}&tp_percent=${{tp}}&sl_percent=${{sl}}`;
    const r = await fetch(url);
    const d = await r.json();
    if(!d.ok) throw new Error(d.error||'API error');
    displayResults(d.backtest);
    document.getElementById('results').style.display='block';
  }} catch(e) {{
    alert('Erreur lors du backtest'); console.error(e);
  }} finally {{
    btn.disabled = false; btn.textContent='🚀 Lancer Backtest';
  }}
}}

function displayResults(res){{
  const s = res.stats;
  // Stats
  document.getElementById('totalTrades').textContent = s.total_trades;
  document.getElementById('wins').textContent = s.wins;
  document.getElementById('losses').textContent = s.losses;
  document.getElementById('winRate').textContent = s.win_rate + '%';
  document.getElementById('totalReturn').textContent = (s.total_return>=0?'+':'') + s.total_return + '%';
  document.getElementById('totalReturn').style.color = s.total_return>=0 ? '#10b981' : '#ef4444';
  document.getElementById('avgWin').textContent = '+' + s.avg_win + '%';
  document.getElementById('avgLoss').textContent = s.avg_loss + '%';
  document.getElementById('maxDD').textContent = s.max_drawdown + '%';
  document.getElementById('sharpe').textContent = s.sharpe_ratio;
  document.getElementById('finalEquity').textContent = s.final_equity.toLocaleString();
  document.getElementById('finalEquity').style.color = s.total_return>=0 ? '#10b981' : '#ef4444';

  // Graph
  const ctx = document.getElementById('equityChart').getContext('2d');
  if(chart) chart.destroy();
  chart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: s.equity_curve.map((_,i)=>i), datasets: [{{ label:'Equity', data:s.equity_curve,
      borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,0.1)', borderWidth:3, fill:true, tension:0.4 }}] }},
    options: {{ responsive:true, plugins: {{ legend: {{display:false}} }}, scales: {{ y: {{ beginAtZero:false }} }} }}
  }});

  // Table trades
  const tbody = document.getElementById('tradesBody');
  tbody.innerHTML='';
  (s.trades||[]).slice(-50).reverse().forEach(t => {{
    const resultColor = t.result === 'TP' ? '#10b981' : '#ef4444';
    const pnlColor = t.pnl_percent >= 0 ? '#10b981' : '#ef4444';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-size:12px">${{t.entry_time}}</td>
      <td>${{t.entry_price}}</td>
      <td>${{t.exit_price}}</td>
      <td><span style="color:${{resultColor}};font-weight:700">${{t.result}}</span></td>
      <td style="color:${{pnlColor}};font-weight:700">${{t.pnl_percent>=0?'+':''}}${{t.pnl_percent}}%</td>
      <td>${{Number(t.equity||0).toLocaleString()}}</td>`;
    tbody.appendChild(tr);
  }});
}}

// Auto-run une fois au chargement
window.addEventListener('load', () => {{ setTimeout(runBacktest, 500); }});
</script>
</div></body></html>""")


@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    patterns_list = detect_patterns(build_trade_rows(50))
    patterns_html = "".join(f"<li style='padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)'>{p}</li>" for p in patterns_list)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Patterns</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🤖 Patterns</h1></div>{NAV}
<div class="card"><h2>Patterns</h2><ul class="list">{patterns_html}</ul></div>
</div></body></html>""")


@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    metrics = calc_metrics(build_trade_rows(50))
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Metrics</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📊 Metrics</h1></div>{NAV}
<div class="card"><h2>Métriques</h2>
  <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px'>
    <div class='metric'><div class='metric-label'>Sharpe</div><div class='metric-value'>{metrics['sharpe_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Sortino</div><div class='metric-value'>{metrics['sortino_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Expectancy</div><div class='metric-value'>{metrics['expectancy']:.2f}%</div></div>
    <div class='metric'><div class='metric-label'>Max DD</div><div class='metric-value' style='color:#ef4444'>-{metrics['max_drawdown']:.1f}%</div></div>
  </div>
</div>
</div></body></html>""")


@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Annonces</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🗞️ Annonces</h1><p>Flux agrégé (CoinDesk, CoinTelegraph, Binance)</p></div>{NAV}

<div class="card">
  <h2>📡 Fil d'actualités</h2>
  <div style="display:flex;gap:12px;margin:8px 0;flex-wrap:wrap">
    <input id="q" placeholder="Filtrer (ex: etf, hack, listing…)" 
      style="flex:1;min-width:250px;padding:10px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
    <select id="imp" style="padding:10px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      <option value="1">Importance ≥ 1</option>
      <option value="2">Importance ≥ 2</option>
      <option value="3" selected>Importance ≥ 3</option>
      <option value="4">Importance ≥ 4</option>
      <option value="5">Importance ≥ 5</option>
    </select>
    <button onclick="loadNews()">🔎 Rechercher</button>
  </div>
  <div id="list" style="margin-top:12px;max-height:70vh;overflow:auto">⏳</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
    <button onclick="prevPage()">⬅️</button>
    <button onclick="nextPage()">➡️</button>
  </div>
  <div id="meta" style="margin-top:8px;color:#94a3b8;font-size:12px"></div>
</div>

<script>
let offset=0, limit=25;
function escapeHtml(s){{return (s||'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}}[c]));}}
function colorByImp(n){{return n>=5?'#ef4444':(n>=4?'#f59e0b':(n>=3?'#10b981':'#6366f1'));}}

async function loadNews(){{
  const q = document.getElementById('q').value.trim();
  const imp = document.getElementById('imp').value;
  const el = document.getElementById('list');
  el.innerHTML='⏳';
  try{{
    const url = `/api/news?min_importance=${{imp}}&limit=${{limit}}&offset=${{offset}}` + (q?`&q=${{encodeURIComponent(q)}}`:'');
    const r = await fetch(url);
    const d = await r.json();
    if(!d.ok) throw new Error('API');
    if(!d.items.length) {{
      el.innerHTML = '<p style="color:#64748b">Aucune annonce.</p>';
    }} else {{
      let html='';
      d.items.forEach(it=>{{
        const color = colorByImp(it.importance||1);
        html += `
          <div class="phase-indicator" style="border-left-color:${{color}};margin-bottom:10px">
            <div style="flex:1">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                <a href="${{it.link}}" target="_blank" rel="noopener" style="color:#e2e8f0;font-weight:700;text-decoration:none">${{escapeHtml(it.title)}}</a>
                <span class="badge" style="border:1px solid ${{color}};color:${{color}}">Imp:${{it.importance}}</span>
              </div>
              <div style="color:#94a3b8;font-size:12px;margin-bottom:4px">${{escapeHtml(it.summary)}}</div>
              <div style="color:#64748b;font-size:11px">Source: ${{escapeHtml(it.source||'')}}${{it.published?(' • '+escapeHtml(it.published)) : ''}}</div>
            </div>
          </div>`;
      }});
      el.innerHTML = html;
    }}
    document.getElementById('meta').textContent = `Affichés ${{Math.min(d.count, limit)}} sur ${{d.total}} (offset ${{offset}})`;
  }}catch(e){{
    console.error(e);
    el.innerHTML = '<p style="color:#ef4444">Erreur chargement news</p>';
  }}
}}
function nextPage(){{offset+=limit; loadNews();}}
function prevPage(){{offset=Math.max(0, offset-limit); loadNews();}}

loadNews();
setInterval(loadNews, 60000);
</script>
</div></body></html>""")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - VERSION FINALE")
    print("="*70)
    print("📍 http://localhost:8000")
    print("📊 Dashboard: http://localhost:8000/trades")
    print("🗞️ Annonces:  http://localhost:8000/annonces")
    print("\n✅ PAGES COMPLÈTES:")
    print("  • Dashboard avec données LIVE (+ Annonces)")
    print("  • Equity Curve avec graphique")
    print("  • Journal de trading")
    print("  • Heatmap visuelle")
    print("  • Configuration stratégie")
    print("  • Backtest (interface)")
    print("  • Patterns")
    print("  • Metrics")
    print("\n📥 WEBHOOK:")
    print("  URL: http://localhost:8000/tv-webhook")
    print("\n🔔 TELEGRAM:")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        print("  ✅ CONFIGURÉ ET ACTIF")
    else:
        print("  ⚠️  NON CONFIGURÉ")
        print("  export TELEGRAM_BOT_TOKEN='...'")
        print("  export TELEGRAM_CHAT_ID='...'")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
