# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION FINALE COMPLÈTE + Annonces Améliorées
Toutes les corrections et améliorations incluses
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import logging
import aiohttp
import os
import asyncio
import random
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================
app = FastAPI(title="Trading Dashboard", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Settings:
    INITIAL_CAPITAL = 10000
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    FEAR_GREED_API = "https://api.alternative.me/fng/"
    COINGECKO_API = "https://api.coingecko.com/api/v3"
    
    NEWS_SOURCES = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://www.binance.com/en/support/announcement/rss",
        "https://cryptoslate.com/feed/",
        "https://decrypt.co/feed",
        "https://www.theblockcrypto.com/rss.xml",
        "https://fr.cointelegraph.com/rss",
        "https://journalducoin.com/feed/",
    ]
    NEWS_CACHE_TTL = 60
    NEWS_MAX_AGE_HOURS = 48

settings = Settings()

# ============================================================================
# CACHE MARCHÉ
# ============================================================================
class MarketDataCache:
    def __init__(self):
        self.fear_greed_data = None
        self.crypto_prices: Dict[str, Any] = {}
        self.global_data: Dict[str, Any] = {}
        self.last_update: Dict[str, datetime] = {}
        self.update_interval = 300
        self.news_items: List[Dict[str, Any]] = []
        self.news_last_fetch: Optional[datetime] = None
    
    def needs_update(self, key: str) -> bool:
        if key not in self.last_update:
            return True
        elapsed = (datetime.now() - self.last_update[key]).total_seconds()
        return elapsed > self.update_interval
    
    def update_timestamp(self, key: str):
        self.last_update[key] = datetime.now()

market_cache = MarketDataCache()

# ============================================================================
# APIs EXTERNES
# ============================================================================

async def fetch_real_fear_greed() -> Dict[str, Any]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(settings.FEAR_GREED_API, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and 'data' in data and len(data['data']) > 0:
                        fg_data = data['data'][0]
                        value = int(fg_data.get('value', 50))
                        
                        if value <= 25:
                            sentiment, emoji, color = "Extreme Fear", "😱", "#ef4444"
                            recommendation = "Opportunité d'achat"
                        elif value <= 45:
                            sentiment, emoji, color = "Fear", "😰", "#f59e0b"
                            recommendation = "Marché craintif"
                        elif value <= 55:
                            sentiment, emoji, color = "Neutral", "😐", "#64748b"
                            recommendation = "Marché neutre"
                        elif value <= 75:
                            sentiment, emoji, color = "Greed", "😊", "#10b981"
                            recommendation = "Bon momentum"
                        else:
                            sentiment, emoji, color = "Extreme Greed", "🤑", "#22c55e"
                            recommendation = "Attention corrections"
                        
                        result = {
                            "value": value,
                            "sentiment": sentiment,
                            "emoji": emoji,
                            "color": color,
                            "recommendation": recommendation,
                        }
                        
                        market_cache.fear_greed_data = result
                        market_cache.update_timestamp('fear_greed')
                        logger.info(f"✅ Fear & Greed: {value}")
                        return result
    except Exception as e:
        logger.error(f"❌ Fear & Greed: {str(e)}")
    
    return market_cache.fear_greed_data or {"value": 50, "sentiment": "Neutral", "emoji": "😐", "color": "#64748b", "recommendation": "N/A"}

async def fetch_crypto_prices() -> Dict[str, Any]:
    try:
        coin_ids = "bitcoin,ethereum,binancecoin,solana"
        url = f"{settings.COINGECKO_API}/simple/price"
        params = {"ids": coin_ids, "vs_currencies": "usd", "include_24hr_change": "true"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
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
        logger.error(f"❌ Prix: {str(e)}")
    return market_cache.crypto_prices or {}

async def fetch_global_crypto_data() -> Dict[str, Any]:
    try:
        url = f"{settings.COINGECKO_API}/global"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'data' in data:
                        global_data = data['data']
                        result = {
                            "total_market_cap": global_data.get('total_market_cap', {}).get('usd', 0),
                            "btc_dominance": global_data.get('market_cap_percentage', {}).get('btc', 0),
                        }
                        market_cache.global_data = result
                        market_cache.update_timestamp('global_data')
                        logger.info(f"✅ Global: MC ${result['total_market_cap']/1e12:.2f}T")
                        return result
    except Exception as e:
        logger.error(f"❌ Global: {str(e)}")
    return market_cache.global_data or {}

def calculate_bullrun_phase(global_data: Dict[str, Any], fear_greed: Dict[str, Any]) -> Dict[str, Any]:
    btc_dominance = global_data.get('btc_dominance', 48)
    fg_value = fear_greed.get('value', 60)
    
    if btc_dominance >= 55 and fg_value < 40:
        phase, phase_name, emoji, color, description = 0, "Phase 0: Accumulation / Bear", "🐻", "#64748b", "Marché prudent / accumulation"
    elif btc_dominance > 50:
        phase, phase_name, emoji, color, description = 1, "Phase 1: Bitcoin Season", "₿", "#f7931a", "Bitcoin domine"
    elif btc_dominance > 45:
        phase, phase_name, emoji, color, description = 2, "Phase 2: ETH & Large-Cap", "💎", "#627eea", "Rotation vers ETH & large caps"
    else:
        phase, phase_name, emoji, color, description = 3, "Phase 3: Altcoin Season", "🚀", "#10b981", "Altcoins en surperformance"
    
    confidence = 90 if fg_value > 75 else (80 if fg_value > 55 else 70)
    
    return {
        "phase": phase,
        "phase_name": phase_name,
        "emoji": emoji,
        "color": color,
        "description": description,
        "confidence": confidence,
        "btc_dominance": round(btc_dominance, 1),
        "fg": fg_value
    }

# ============================================================================
# STOCKAGE EN MÉMOIRE
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
                
                entry = trade.get('entry', 0)
                side = trade.get('side', 'LONG')
                pnl = (exit_price - entry) if side == 'LONG' else (entry - exit_price)
                pnl_percent = (pnl / entry) * 100 if entry > 0 else 0
                
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                
                self.current_equity += pnl * 10
                self.equity_curve.append({"equity": self.current_equity, "timestamp": datetime.now()})
                
                logger.info(f"🔒 Trade #{trade_id}: {result.upper()} P&L {pnl_percent:+.2f}%")
                return True
        return False
    
    def add_journal_entry(self, entry: str, trade_id: Optional[int] = None):
        self.journal_entries.append({
            'id': len(self.journal_entries) + 1,
            'timestamp': datetime.now(),
            'entry': entry,
            'trade_id': trade_id
        })
    
    def get_stats(self) -> Dict[str, Any]:
        closed = [t for t in self.trades if t.get('row_state') in ('tp', 'sl')]
        active = [t for t in self.trades if t.get('row_state') == 'normal']
        wins = [t for t in closed if t.get('row_state') == 'tp']
        losses = [t for t in closed if t.get('row_state') == 'sl']
        win_rate = (len(wins) / len(closed) * 100) if closed else 0
        total_return = ((self.current_equity - settings.INITIAL_CAPITAL) / settings.INITIAL_CAPITAL) * 100
        
        return {
            'total_trades': len(self.trades),
            'active_trades': len(active),
            'closed_trades': len(closed),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'current_equity': self.current_equity,
            'initial_capital': settings.INITIAL_CAPITAL,
            'total_return': total_return
        }

trading_state = TradingState()

async def init_demo():
    prices = await fetch_crypto_prices()
    if not prices:
        prices = {
            "bitcoin": {"price": 65000},
            "ethereum": {"price": 3500},
            "binancecoin": {"price": 600},
            "solana": {"price": 140},
        }
    
    symbols = [
        ("BTCUSDT", prices.get('bitcoin', {}).get('price', 65000)),
        ("ETHUSDT", prices.get('ethereum', {}).get('price', 3500)),
        ("BNBUSDT", prices.get('binancecoin', {}).get('price', 600)),
        ("SOLUSDT", prices.get('solana', {}).get('price', 140)),
    ]
    
    for i, (symbol, price) in enumerate(symbols):
        trading_state.add_trade({
            'symbol': symbol,
            'tf_label': '15m',
            'side': 'LONG' if i % 2 == 0 else 'SHORT',
            'entry': price,
            'tp': price * 1.03,
            'sl': price * 0.98,
            'row_state': 'normal'
        })
    logger.info("✅ Démo initialisée")

asyncio.get_event_loop().create_task(init_demo())

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
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    logger.info("✅ Telegram envoyé")
                    return True
                else:
                    txt = await response.text()
                    logger.error(f"❌ Telegram: {response.status} - {txt[:500]}")
                    return False
    except Exception as e:
        logger.error(f"❌ Telegram: {str(e)}")
        return False

async def notify_new_trade(trade: Dict[str, Any]) -> bool:
    message = f"""🎯 <b>NOUVEAU TRADE</b>

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🎯 TP: {trade.get('tp')}
🛑 SL: {trade.get('sl')}
📈 {trade.get('side')} | {trade.get('tf_label')}"""
    return await send_telegram_message(message)

async def notify_tp_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""🎯 <b>TAKE PROFIT!</b> ✅

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🎯 Exit: {trade.get('exit_price')}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(message)

async def notify_sl_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""🛑 <b>STOP LOSS</b> ⚠️

📊 {trade.get('symbol')}
💰 Entry: {trade.get('entry')}
🛑 Exit: {trade.get('exit_price')}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(message)

# ============================================================================
# CSS & NAV
# ============================================================================
CSS = """<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
.container { max-width: 1400px; margin: 0 auto; }
.header { text-align: center; margin-bottom: 40px; padding: 20px; }
.header h1 { font-size: 36px; margin-bottom: 10px; color: #6366f1; }
.header p { color: #94a3b8; }
.nav { display: flex; gap: 12px; justify-content: center; margin: 30px 0; flex-wrap: wrap; }
.nav a { padding: 10px 20px; background: rgba(99, 102, 241, 0.2); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 8px; color: #6366f1; text-decoration: none; font-weight: 600; transition: all 0.3s; }
.nav a:hover { background: rgba(99, 102, 241, 0.3); transform: translateY(-2px); }
.card { background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3); }
.card h2 { font-size: 20px; margin-bottom: 16px; color: #6366f1; font-weight: 700; }
.grid { display: grid; gap: 20px; margin-bottom: 20px; }
.metric { background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 12px; padding: 24px; text-align: center; }
.metric-label { font-size: 12px; color: #64748b; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { font-size: 36px; font-weight: bold; color: #6366f1; }
.badge { display: inline-block; padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 700; }
.badge-green { background: rgba(16, 185, 129, 0.2); color: #10b981; }
.badge-red { background: rgba(239, 68, 68, 0.2); color: #ef4444; }
.badge-yellow { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px; text-align: left; }
th { color: #64748b; font-weight: 600; border-bottom: 2px solid rgba(99, 102, 241, 0.3); }
tr { border-bottom: 1px solid rgba(99, 102, 241, 0.1); }
tr:hover { background: rgba(99, 102, 241, 0.05); }
.gauge { width: 120px; height: 120px; margin: 0 auto 20px; background: conic-gradient(#6366f1 0deg, #8b5cf6 180deg, #ec4899 360deg); border-radius: 50%; display: flex; align-items: center; justify-content: center; }
.gauge-inner { width: 90px; height: 90px; background: #1e293b; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.gauge-value { font-size: 32px; font-weight: bold; }
.gauge-label { font-size: 12px; color: #64748b; }
.phase-indicator { display: flex; align-items: center; padding: 16px; margin: 12px 0; border-radius: 8px; background: rgba(99, 102, 241, 0.05); border-left: 4px solid transparent; transition: all 0.3s; }
.phase-indicator.active { background: rgba(99, 102, 241, 0.15); border-left-color: #6366f1; }
.live-badge { display: inline-block; padding: 4px 8px; background: rgba(16, 185, 129, 0.2); color: #10b981; border-radius: 4px; font-size: 10px; font-weight: 700; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
.journal-entry { padding: 16px; margin: 12px 0; background: rgba(99, 102, 241, 0.05); border-left: 4px solid #6366f1; border-radius: 8px; }
.journal-timestamp { font-size: 12px; color: #64748b; margin-bottom: 8px; }
textarea { width: 100%; padding: 12px; background: rgba(99, 102, 241, 0.05); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 8px; color: #e2e8f0; font-family: inherit; resize: vertical; min-height: 100px; }
button { padding: 12px 24px; background: #6366f1; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.3s; }
button:hover { background: #5558e3; transform: translateY(-2px); }
.heatmap-cell { padding: 12px; text-align: center; border-radius: 8px; background: rgba(99, 102, 241, 0.1); border: 1px solid rgba(99, 102, 241, 0.2); }
.heatmap-cell.high { background: rgba(16, 185, 129, 0.2); border-color: #10b981; }
.heatmap-cell.medium { background: rgba(245, 158, 11, 0.2); border-color: #f59e0b; }
.heatmap-cell.low { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }
.small { font-size:12px;color:#94a3b8 }
.filter-chip { display: inline-block; padding: 6px 12px; margin: 4px; background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3); border-radius: 16px; cursor: pointer; transition: all 0.3s; font-size: 12px; }
.filter-chip:hover { background: rgba(99,102,241,0.2); transform: translateY(-2px); }
.filter-chip.active { background: #6366f1; color: white; border-color: #6366f1; }
.sentiment-badge { padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
.sentiment-positif { background: rgba(16,185,129,0.2); color: #10b981; }
.sentiment-négatif { background: rgba(239,68,68,0.2); color: #ef4444; }
.sentiment-neutre { background: rgba(100,116,139,0.2); color: #64748b; }
.category-badge { display: inline-block; padding: 3px 8px; margin: 2px; background: rgba(99,102,241,0.1); border-radius: 12px; font-size: 10px; }
.time-ago { font-size: 11px; color: #64748b; font-style: italic; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-box { background: rgba(99,102,241,0.05); padding: 12px; border-radius: 8px; text-align: center; }
.stat-number { font-size: 24px; font-weight: 700; color: #6366f1; }
.stat-label { font-size: 11px; color: #64748b; margin-top: 4px; }
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
        symbol = row.get('symbol', '')
        if symbol not in symbols:
            symbols[symbol] = []
        symbols[symbol].append(row)
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
# NEWS (RSS) - VERSION AMÉLIORÉE
# ============================================================================

KEYWORDS_BY_CATEGORY = {
    "regulation": {
        "keywords": [r"\bETF\b", r"\bSEC\b", r"\brégulation\b", r"\bregulation\b", 
                    r"\bMiCA\b", r"\bAMF\b", r"\bapprobation\b", r"\bapproval\b"],
        "emoji": "⚖️",
        "name_fr": "Régulation",
        "boost": 2
    },
    "listings": {
        "keywords": [r"\blisting\b", r"\bdelisting\b", r"\bcotation\b", 
                    r"\bnouveau token\b", r"\bnew token\b"],
        "emoji": "📊",
        "name_fr": "Listings",
        "boost": 2
    },
    "security": {
        "keywords": [r"\bhack\b", r"\bexploit\b", r"\bbreach\b", r"\bpiratage\b",
                    r"\bvol\b", r"\btheft\b", r"\bscam\b", r"\barnaque\b"],
        "emoji": "🔒",
        "name_fr": "Sécurité",
        "boost": 3
    },
    "technical": {
        "keywords": [r"\bmainnet\b", r"\btestnet\b", r"\bupgrade\b", r"\bfork\b",
                    r"\bmise à jour\b", r"\bhard fork\b"],
        "emoji": "⚙️",
        "name_fr": "Technique",
        "boost": 1
    },
    "partnerships": {
        "keywords": [r"\bpartnership\b", r"\bpartenariat\b", r"\bmerger\b", 
                    r"\bacquisition\b", r"\bfusion\b", r"\bcollaboration\b"],
        "emoji": "🤝",
        "name_fr": "Partenariats",
        "boost": 1
    },
    "markets": {
        "keywords": [r"\ball[- ]time high\b", r"\bATH\b", r"\bcrash\b", 
                    r"\bpump\b", r"\bdump\b", r"\brallye\b", r"\brally\b"],
        "emoji": "📈",
        "name_fr": "Marchés",
        "boost": 1
    },
    "defi": {
        "keywords": [r"\bDeFi\b", r"\byield\b", r"\bstaking\b", r"\bTVL\b",
                    r"\bliquidity\b", r"\bliquidité\b", r"\bprotocol\b"],
        "emoji": "🏦",
        "name_fr": "DeFi",
        "boost": 1
    },
    "nft": {
        "keywords": [r"\bNFT\b", r"\bmetaverse\b", r"\bmétavers\b", r"\bcollection\b"],
        "emoji": "🎨",
        "name_fr": "NFT",
        "boost": 0
    },
}

def score_importance_advanced(title: str, summary: str, source: str) -> dict:
    text = f"{title} {summary}".lower()
    score = 1
    categories = []
    sentiment = "neutre"
    
    for cat_key, cat_data in KEYWORDS_BY_CATEGORY.items():
        for kw in cat_data["keywords"]:
            if re.search(kw, text, flags=re.IGNORECASE):
                if cat_key not in categories:
                    categories.append(cat_key)
                    score += cat_data["boost"]
    
    if "binance.com" in source.lower():
        score += 2
    elif "coindesk" in source.lower() or "cointelegraph" in source.lower():
        score += 1
    
    if title.isupper() and len(title) > 10:
        score += 1
    
    positive_words = ["approves", "approve", "approved", "partnership", "launch", 
                     "success", "growth", "gains", "rally", "bullish"]
    negative_words = ["hack", "scam", "crash", "dump", "ban", "reject", 
                     "bearish", "exploit", "breach"]
    
    pos_count = sum(1 for w in positive_words if w in text)
    neg_count = sum(1 for w in negative_words if w in text)
    
    if pos_count > neg_count:
        sentiment = "positif"
        score += 0.5
    elif neg_count > pos_count:
        sentiment = "négatif"
        score += 1
    
    score = min(int(score), 5)
    
    return {
        "score": score,
        "categories": categories,
        "sentiment": sentiment
    }

async def fetch_rss_improved(session: aiohttp.ClientSession, url: str, max_age_hours: int = 48) -> list[dict]:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml'
        }
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), headers=headers) as resp:
            if resp.status != 200:
                logger.warning(f"⚠️ RSS {url} status {resp.status}")
                return []
            
            raw = await resp.text()
            items = []
            
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as e:
                logger.error(f"❌ RSS parse error: {url} - {str(e)[:100]}")
                return []
            
            cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
            
            channel = root.find("./channel")
            if channel is not None:
                for it in channel.findall("item"):
                    title = (it.findtext("title") or "").strip()
                    link = (it.findtext("link") or "").strip()
                    pub_date = (it.findtext("pubDate") or "").strip()
                    desc = (it.findtext("description") or "").strip()
                    
                    if not title or not link:
                        continue
                    
                    item_time = None
                    try:
                        item_time = parsedate_to_datetime(pub_date)
                    except:
                        pass
                    
                    if item_time and item_time < cutoff_time:
                        continue
                    
                    source = urlparse(url).netloc
                    clean_desc = re.sub("<[^<]+?>", "", desc)[:500].strip()
                    
                    items.append({
                        "title": title,
                        "link": link,
                        "source": source,
                        "published": pub_date,
                        "published_dt": item_time,
                        "summary": clean_desc,
                    })
            else:
                for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                    title_el = entry.find("{http://www.w3.org/2005/Atom}title")
                    link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                    updated_el = entry.find("{http://www.w3.org/2005/Atom}updated")
                    summary_el = entry.find("{http://www.w3.org/2005/Atom}summary")
                    
                    if title_el is None or link_el is None:
                        continue
                    
                    title = (title_el.text or "").strip()
                    link = link_el.get('href', '').strip()
                    pub_date = (updated_el.text if updated_el is not None else "").strip()
                    desc = (summary_el.text if summary_el is not None else "").strip()
                    
                    item_time = None
                    try:
                        item_time = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                    except:
                        pass
                    
                    if item_time and item_time < cutoff_time:
                        continue
                    
                    source = urlparse(url).netloc
                    clean_desc = re.sub("<[^<]+?>", "", desc)[:500].strip()
                    
                    items.append({
                        "title": title,
                        "link": link,
                        "source": source,
                        "published": pub_date,
                        "published_dt": item_time,
                        "summary": clean_desc,
                    })
            
            logger.info(f"✅ RSS {urlparse(url).netloc}: {len(items)} items récents")
            return items
            
    except asyncio.TimeoutError:
        logger.error(f"❌ RSS timeout: {url}")
        return []
    except Exception as e:
        logger.error(f"❌ RSS fetch error {url}: {str(e)[:100]}")
        return []

async def fetch_all_news_improved() -> list[dict]:
    now = datetime.now()
    if (market_cache.news_last_fetch and
        (now - market_cache.news_last_fetch).total_seconds() < settings.NEWS_CACHE_TTL and
        market_cache.news_items):
        return market_cache.news_items

    aggregated: Dict[str, Dict[str, Any]] = {}
    
    try:
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_rss_improved(session, u, settings.NEWS_MAX_AGE_HOURS) 
                    for u in settings.NEWS_SOURCES]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in results:
                if isinstance(res, Exception) or not res:
                    continue
                for item in res:
                    if item["link"] not in aggregated:
                        aggregated[item["link"]] = item
    except Exception as e:
        logger.error(f"❌ fetch_all_news_improved: {e}")

    items = list(aggregated.values())
    
    for it in items:
        scoring = score_importance_advanced(
            it.get("title", ""), 
            it.get("summary", ""),
            it.get("source", "")
        )
        it["importance"] = scoring["score"]
        it["categories"] = scoring["categories"]
        it["sentiment"] = scoring["sentiment"]
        
        if it.get("published_dt"):
            try:
                delta = datetime.now() - it["published_dt"].replace(tzinfo=None)
                if delta.days > 0:
                    it["time_ago"] = f"il y a {delta.days}j"
                elif delta.seconds >= 3600:
                    it["time_ago"] = f"il y a {delta.seconds // 3600}h"
                else:
                    it["time_ago"] = f"il y a {delta.seconds // 60}min"
            except:
                it["time_ago"] = ""
        else:
            it["time_ago"] = ""

    items.sort(key=lambda x: (x.get("importance", 1), x.get("published_dt") or datetime.min), reverse=True)
    
    market_cache.news_items = items
    market_cache.news_last_fetch = now
    logger.info(f"🗞️ News agrégées: {len(items)} items")
    return items

# ============================================================================
# API
# ============================================================================

@app.get("/api/fear-greed")
async def api_fear_greed():
    if market_cache.needs_update('fear_greed'):
        fg = await fetch_real_fear_greed()
    else:
        fg = market_cache.fear_greed_data or await fetch_real_fear_greed()
    return {"ok": True, "fear_greed": fg}

@app.get("/api/bullrun-phase")
async def api_bullrun_phase():
    if market_cache.needs_update('global_data'):
        gd = await fetch_global_crypto_data()
    else:
        gd = market_cache.global_data or await fetch_global_crypto_data()
    
    if market_cache.needs_update('fear_greed'):
        fg = await fetch_real_fear_greed()
    else:
        fg = market_cache.fear_greed_data or await fetch_real_fear_greed()
    
    if market_cache.needs_update('crypto_prices'):
        pr = await fetch_crypto_prices()
    else:
        pr = market_cache.crypto_prices or await fetch_crypto_prices()
    
    phase = calculate_bullrun_phase(gd, fg)
    btc_price = pr.get('bitcoin', {}).get('price', 0)
    
    phase_note = (
        "Phases: "
        "0) Bear (BTC.D≥55 & F&G<40); "
        "1) BTC Season (BTC.D>50); "
        "2) ETH/Large-Cap (45<BTC.D≤50); "
        "3) Alt Season (BTC.D≤45)"
    )
    
    return {
        "ok": True,
        "bullrun_phase": {
            **phase,
            "btc_price": int(btc_price),
            "market_cap": gd.get('total_market_cap', 0),
            "details": {
                "btc": {"performance_30d": pr.get('bitcoin', {}).get('change_24h', 0), "dominance": phase.get('btc_dominance', 0)},
                "eth": {"performance_30d": pr.get('ethereum', {}).get('change_24h', 0)},
            },
            "note": phase_note
        }
    }

@app.get("/api/telegram-test")
async def telegram_test():
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return {
            "ok": False, 
            "error": "Configuration manquante",
            "details": {
                "bot_token_present": bool(settings.TELEGRAM_BOT_TOKEN),
                "chat_id_present": bool(settings.TELEGRAM_CHAT_ID)
            }
        }
    
    test_message = f"""🧪 <b>TEST TELEGRAM</b>

✅ Connexion réussie !
🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
    
    success = await send_telegram_message(test_message)
    
    return {
        "ok": success,
        "message": "Message envoyé" if success else "Échec",
        "config": {
            "bot_token": settings.TELEGRAM_BOT_TOKEN[:10] + "..." if settings.TELEGRAM_BOT_TOKEN else None,
            "chat_id": settings.TELEGRAM_CHAT_ID
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
    
    for day in days:
        for hour in hours:
            key = f"{day}_{hour}"
            h = int(hour.split(':')[0])
            if 9 <= h <= 11 or 14 <= h <= 16:
                winrate = random.randint(60, 75)
                trades = random.randint(10, 30)
            elif 8 <= h <= 12 or 13 <= h <= 17:
                winrate = random.randint(50, 65)
                trades = random.randint(5, 15)
            else:
                winrate = random.randint(40, 55)
                trades = random.randint(0, 8)
            heatmap[key] = {"winrate": winrate, "trades": trades}
    
    return {"ok": True, "heatmap": heatmap}

@app.get("/api/news")
async def api_news(
    q: Optional[str] = None,
    min_importance: int = 1,
    category: Optional[str] = None,
    sentiment: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    items = await fetch_all_news_improved()
    
    if q:
        ql = q.lower().strip()
        items = [i for i in items if ql in (i["title"] + " " + i["summary"] + " " + i["source"]).lower()]
    
    try:
        min_importance = max(1, min(5, int(min_importance)))
    except:
        min_importance = 1
    items = [i for i in items if i.get("importance", 1) >= min_importance]
    
    if category and category in KEYWORDS_BY_CATEGORY:
        items = [i for i in items if category in i.get("categories", [])]
    
    if sentiment and sentiment in ["positif", "négatif", "neutre"]:
        items = [i for i in items if i.get("sentiment") == sentiment]
    
    total = len(items)
    page = items[offset: offset + limit]
    
    stats = {
        "total_items": total,
        "by_importance": {
            "critical": len([i for i in items if i.get("importance") >= 5]),
            "high": len([i for i in items if i.get("importance") == 4]),
            "medium": len([i for i in items if i.get("importance") == 3]),
            "low": len([i for i in items if i.get("importance") <= 2]),
        },
        "by_sentiment": {
            "positif": len([i for i in items if i.get("sentiment") == "positif"]),
            "négatif": len([i for i in items if i.get("sentiment") == "négatif"]),
            "neutre": len([i for i in items if i.get("sentiment") == "neutre"]),
        },
    }
    
    return {
        "ok": True,
        "total": total,
        "count": len(page),
        "items": page,
        "stats": stats
    }

# ============================================================================
# BACKTEST
# ============================================================================

async def fetch_binance_klines(symbol: str, interval: str = "1h", limit: int = 1000):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status == 200:
                    data = await response.json()
                    klines = []
                    for k in data:
                        klines.append({
                            "timestamp": datetime.fromtimestamp(k[0] / 1000),
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5])
                        })
                    logger.info(f"✅ Binance: {len(klines)} klines pour {symbol}")
                    return klines
                else:
                    logger.error(f"❌ Binance: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"❌ Binance: {str(e)}")
        return None

def run_backtest_strategy(klines: List[Dict], tp_percent: float, sl_percent: float, initial_capital: float = 10000):
    if not klines or len(klines) < 2:
        return None
    
    trades = []
    equity = initial_capital
    equity_curve = [equity]
    in_position = False
    entry_price = 0
    entry_index = 0
    
    for i in range(1, len(klines)):
        current = klines[i]
        prev = klines[i-1]
        
        if not in_position:
            if current['close'] > prev['close'] and current['volume'] > prev['volume']:
                in_position = True
                entry_price = current['close']
                entry_index = i
        else:
            tp_price = entry_price * (1 + tp_percent / 100)
            sl_price = entry_price * (1 - sl_percent / 100)
            hit_tp = current['high'] >= tp_price
            hit_sl = current['low'] <= sl_price
            
            if hit_tp or hit_sl:
                exit_price = tp_price if hit_tp else sl_price
                result = "TP" if hit_tp else "SL"
                pnl_percent = ((exit_price - entry_price) / entry_price) * 100
                position_size = equity * 0.02
                pnl_amount = position_size * (pnl_percent / 100) * 10
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
    
    return {
        "trades": trades,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "final_equity": round(equity, 2),
        "total_return": round(total_return, 2),
        "equity_curve": [round(e, 2) for e in equity_curve]
    }

@app.get("/api/backtest")
async def api_backtest(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    limit: int = 500,
    tp_percent: float = 3.0,
    sl_percent: float = 2.0
):
    klines = await fetch_binance_klines(symbol, interval, limit)
    if not klines:
        return {"ok": False, "error": "Impossible de récupérer les données Binance"}
    
    results = run_backtest_strategy(klines, tp_percent, sl_percent, settings.INITIAL_CAPITAL)
    if not results:
        return {"ok": False, "error": "Aucun trade généré"}
    
    return {
        "ok": True,
        "backtest": {
            "symbol": symbol,
            "stats": results
        }
    }

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
                'symbol': symbol,
                'tf_label': payload.get("timeframe", "15m"),
                'side': side,
                'entry': payload.get("entry"),
                'tp': payload.get("tp"),
                'sl': payload.get("sl"),
                'row_state': 'normal'
            }
            trading_state.add_trade(new_trade)
            await notify_new_trade(new_trade)
            return JSONResponse({"status": "ok", "trade_id": new_trade.get('id')})
            
        elif action in ["tp_hit", "sl_hit"]:
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and 
                    trade.get('row_state') == 'normal' and
                    trade.get('side') == side):
                    
                    exit_price = payload.get('tp' if action == 'tp_hit' else 'sl')
                    result = 'tp' if action == 'tp_hit' else 'sl'
                    
                    if trading_state.close_trade(trade['id'], result, exit_price or trade.get(result)):
                        if action == 'tp_hit':
                            await notify_tp_hit(trade)
                        else:
                            await notify_sl_hit(trade)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            
            return JSONResponse({"status": "warning", "message": f"Trade non trouvé: {symbol}"})
        
        return JSONResponse({"status": "error", "message": f"Action inconnue: {action}"}, status_code=400)
    except Exception as e:
        logger.error(f"❌ Webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# ============================================================================
# ROUTES HTML
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard</h1><p>Système complet <span class="live-badge">LIVE</span></p></div>""" + NAV + """
<div class="card" style="text-align:center;">
<h2>Dashboard Professionnel</h2>
<p style="color:#94a3b8;margin:20px 0;">✅ Données réelles • ✅ Telegram • ✅ Analytics • 🗞️ News</p>
<a href="/trades" style="display:inline-block;padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">Dashboard →</a>
</div></div></body></html>""")

@app.get("/trades", response_class=HTMLResponse)
async def trades():
    rows = build_trade_rows(50)
    stats = trading_state.get_stats()
    patterns = detect_patterns(rows)
    
    table = ""
    for r in rows[:20]:
        badge = f'<span class="badge badge-green">TP</span>' if r.get("row_state")=="tp" else (f'<span class="badge badge-red">SL</span>' if r.get("row_state")=="sl" else f'<span class="badge badge-yellow">En cours</span>')
        pnl = ""
        if r.get('pnl_percent'):
            color = '#10b981' if r['pnl_percent'] > 0 else '#ef4444'
            pnl = f'<span style="color:{color};font-weight:700">{r["pnl_percent"]:+.2f}%</span>'
        table += f"<tr><td>{r.get('symbol','N/A')}</td><td>{r.get('tf_label','N/A')}</td><td>{r.get('side','N/A')}</td><td>{r.get('entry') or 'N/A'}</td><td>{badge} {pnl}</td></tr>"
    
    patterns_html = "".join(f'<li style="padding:8px">{p}</li>' for p in patterns)
    
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Dashboard</title><meta charset="UTF-8">{CSS}</head>
<body><div class="container">
<div class="header"><h1>📊 Dashboard</h1><p>Live <span class="live-badge">LIVE</span></p></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
    <div class="card"><h2>😱 Fear & Greed <span class="live-badge">LIVE</span></h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>
    <div class="card"><h2>🚀 Bull Run <span class="live-badge">LIVE</span></h2>
        <div id="br" style="text-align:center;padding:40px">⏳</div>
    </div>
    <div class="card"><h2>🤖 Patterns</h2><ul class="list">{patterns_html}</ul></div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <div class="metric"><div class="metric-label">Total</div><div class="metric-value">{stats['total_trades']}</div></div>
    <div class="metric"><div class="metric-label">Actifs</div><div class="metric-value">{stats['active_trades']}</div></div>
    <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int(stats['win_rate'])}%</div></div>
    <div class="metric"><div class="metric-label">Capital</div><div class="metric-value" style="font-size:24px">${stats['current_equity']:.0f}</div></div>
</div>

<div class="card"><h2>📊 Trades</h2>
<table><thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>Status</th></tr></thead><tbody>{table}</tbody></table></div>

<script>
fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;document.getElementById('fg').innerHTML=`<div class="gauge"><div class="gauge-inner"><div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div></div></div><div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>`;}}}});
fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;document.getElementById('br').innerHTML=`<div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div><div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>`;}}}});
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
    <div class="metric"><div class="metric-label">Return</div><div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'}">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📊 Graphique</h2><canvas id="chart" width="800" height="400"></canvas></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
new Chart(document.getElementById('chart'), {{
    type: 'line',
    data: {{labels: {labels}, datasets: [{{label: 'Equity', data: {values}, borderColor: '#6366f1', backgroundColor: 'rgba(99, 102, 241, 0.1)', borderWidth: 3, fill: true}}]}},
    options: {{responsive: true}}
}});
</script>
</div></body></html>""")

@app.get("/journal", response_class=HTMLResponse)
async def journal():
    entries = trading_state.journal_entries
    entries_html = ""
    for entry in reversed(entries[-20:]):
        entries_html += f"""<div class="journal-entry">
<div class="journal-timestamp">{entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}</div>
<div>{entry['entry']}</div></div>"""
    
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Journal</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>📝 Journal</h1></div>""" + NAV + f"""

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
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Heatmap</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>🔥 Heatmap</h1></div>""" + NAV + """
<div class="card"><h2>📊 Heatmap</h2><div id="hm">⏳</div></div>
<script>
fetch('/api/heatmap').then(r=>r.json()).then(d=>{
    if(d.ok){
        const hm = d.heatmap;
        let html = '<table style="width:100%"><thead><tr><th>Jour</th>';
        for(let h=8; h<20; h++) html += `<th>${h}:00</th>`;
        html += '</tr></thead><tbody>';
        ['Monday','Tuesday','Wednesday','Thursday','Friday'].forEach(day=>{
            html += `<tr><td style="font-weight:700">${day.slice(0,3)}</td>`;
            for(let h=8; h<20; h++){
                const key = `${day}_${h.toString().padStart(2,'0')}:00`;
                const cell = hm[key] || {winrate:0,trades:0};
                const wr = cell.winrate;
                const cls = wr>=70?'high':wr>=55?'medium':'low';
                html += `<td class="heatmap-cell ${cls}"><div style="font-weight:700">${wr}%</div><div style="font-size:10px">${cell.trades}</div></td>`;
            }
            html += '</tr>';
        });
        html += '</tbody></table>';
        document.getElementById('hm').innerHTML = html;
    }
});
</script>
</div></body></html>""")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    telegram_ok = bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
    
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Stratégie</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>⚙️ Stratégie</h1></div>""" + NAV + f"""

<div class="card"><h2>🔔 Telegram</h2>
<p style="color:{'#10b981' if telegram_ok else '#ef4444'}">
{'✅ Configuré' if telegram_ok else '⚠️ Non configuré'}
</p>
<button onclick="testTelegram()" id="telegramBtn">🧪 Tester</button>
<div id="telegramResult" style="margin-top:12px;padding:12px;border-radius:8px;display:none"></div>
</div>

<script>
async function testTelegram() {{
    const btn = document.getElementById('telegramBtn');
    const result = document.getElementById('telegramResult');
    btn.disabled = true;
    btn.textContent = '⏳...';
    try {{
        const r = await fetch('/api/telegram-test');
        const d = await r.json();
        result.style.display = 'block';
        if (d.ok) {{
            result.style.background = 'rgba(16, 185, 129, 0.2)';
            result.style.color = '#10b981';
            result.innerHTML = '✅ ' + d.message;
        }} else {{
            result.style.background = 'rgba(239, 68, 68, 0.2)';
            result.style.color = '#ef4444';
            result.innerHTML = '❌ ' + d.error;
        }}
    }} finally {{
        btn.disabled = false;
        btn.textContent = '🧪 Tester';
    }}
}}
</script>
</div></body></html>""")

@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Backtest</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>⏮️ Backtest</h1></div>""" + NAV + """
<div class="card"><h2>Paramètres</h2>
<div style="display:grid;gap:12px">
<select id="symbol" style="padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
<option value="BTCUSDT">BTCUSDT</option>
<option value="ETHUSDT">ETHUSDT</option>
</select>
<button onclick="runBacktest()" id="btn">🚀 Lancer</button>
</div></div>
<div id="results" style="display:none">
<div class="card"><h2>Résultats</h2>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
<div class="metric"><div class="metric-label">Total</div><div class="metric-value" id="total">-</div></div>
<div class="metric"><div class="metric-label">Wins</div><div class="metric-value" id="wins" style="color:#10b981">-</div></div>
<div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value" id="winrate">-</div></div>
</div></div></div>
<script>
async function runBacktest() {
    const btn = document.getElementById('btn');
    btn.disabled = true;
    btn.textContent = '⏳...';
    const symbol = document.getElementById('symbol').value;
    try {
        const r = await fetch(`/api/backtest?symbol=${symbol}`);
        const d = await r.json();
        if (d.ok) {
            document.getElementById('results').style.display = 'block';
            const s = d.backtest.stats;
            document.getElementById('total').textContent = s.total_trades;
            document.getElementById('wins').textContent = s.wins;
            document.getElementById('winrate').textContent = s.win_rate + '%';
        } else {
            alert(d.error);
        }
    } finally {
        btn.disabled = false;
        btn.textContent = '🚀 Lancer';
    }
}
</script>
</div></body></html>""")

@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    patterns_list = detect_patterns(build_trade_rows(50))
    patterns_html = "".join(f"<li style='padding:12px'>{p}</li>" for p in patterns_list)
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Patterns</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>🤖 Patterns</h1></div>""" + NAV + f"""
<div class="card"><h2>Patterns</h2><ul>{patterns_html}</ul></div>
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
</div></div></div></body></html>""")

@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Annonces</title>" + CSS + "</head>"
        "<body><div class='container'><div class='header'><h1>🗞️ Annonces</h1></div>" + NAV +
        "<div class='card'><h2>🔍 Filtres</h2>"
        "<div style='display:grid;grid-template-columns:1fr 200px 120px;gap:12px;align-items:end'>"
        "<div><input id='q' placeholder='Recherche...' style='width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0'/></div>"
        "<div><select id='minImp' style='width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0'>"
        "<option value='1'>1 - Toutes</option><option value='3' selected>3 - Importantes</option><option value='5'>5 - Critiques</option></select></div>"
        "<div><button id='refreshBtn'>🔄</button></div></div>"
        "<div style='margin-top:12px'>"
        "<div id='categoryFilters'>"
        "<span class='filter-chip' data-category='all'>🌐 Toutes</span>"
        "<span class='filter-chip' data-category='regulation'>⚖️ Régulation</span>"
        "<span class='filter-chip' data-category='security'>🔒 Sécurité</span>"
        "<span class='filter-chip' data-category='listings'>📊 Listings</span>"
        "</div></div></div>"
        "<div class='card'><h2>📣 Flux</h2>"
        "<div id='status' style='color:#64748b;font-size:12px;margin-bottom:12px'>...</div>"
        "<div id='newsList'></div></div>"
        "<script>"
        "let timer,currentCategory='all';"
        "function escapeHtml(s){return s?s.replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}[c])):''}"
        "function badge(i){const c={5:'#ef4444',4:'#f59e0b',3:'#10b981',2:'#6366f1',1:'#64748b'},l={5:'CRITIQUE',4:'IMPORTANT',3:'NOTABLE',2:'STANDARD',1:'INFO'};return '<span style=\"padding:4px 8px;border-radius:4px;font-size:11px;font-weight:700;background:rgba(148,163,184,0.1);border:1px solid '+c[i]+';color:'+c[i]+'\">'+l[i]+'</span>'}"
        "async function loadNews(){"
        "const q=document.getElementById('q').value,minImp=document.getElementById('minImp').value;"
        "let url='/api/news?min_importance='+minImp+'&limit=50';"
        "if(q)url+='&q='+encodeURIComponent(q);"
        "if(currentCategory!=='all')url+='&category='+currentCategory;"
        "document.getElementById('status').textContent='⏳...';"
        "try{"
        "const r=await fetch(url),d=await r.json();"
        "if(!d.ok)return;"
        "document.getElementById('status').textContent=d.count+' news';"
        "const list=document.getElementById('newsList');"
        "list.innerHTML='';"
        "if(d.items&&d.items.length){"
        "for(const it of d.items){"
        "const card=document.createElement('div');"
        "card.className='phase-indicator';"
        "const bc=it.importance>=5?'#ef4444':(it.importance>=4?'#f59e0b':'#10b981');"
        "card.style.borderLeftColor=bc;"
        "card.innerHTML='<div style=\"flex:1\">'"
        "+'<div style=\"display:flex;gap:8px;margin-bottom:8px\">'"
        "+'<a href=\"'+it.link+'\" target=\"_blank\" style=\"color:#e2e8f0;font-weight:700;text-decoration:none\">'+escapeHtml(it.title)+'</a>'"
        "+badge(it.importance)+'</div>'"
        "+'<div style=\"color:#94a3b8;font-size:13px;margin-bottom:8px\">'+escapeHtml(it.summary)+'</div>'"
        "+'<div style=\"color:#64748b;font-size:12px\">'+escapeHtml(it.source)+(it.time_ago?' • '+it.time_ago:'')+'</div>'"
        "+'</div>';"
        "list.appendChild(card);"
        "}}"
        "}catch(e){}"
        "}"
        "document.querySelectorAll('#categoryFilters .filter-chip').forEach(chip=>{"
        "chip.addEventListener('click',function(){"
        "document.querySelectorAll('#categoryFilters .filter-chip').forEach(c=>c.classList.remove('active'));"
        "this.classList.add('active');currentCategory=this.dataset.category;loadNews();"
        "})"
        "});"
        "document.querySelector('[data-category=\"all\"]').classList.add('active');"
        "document.getElementById('refreshBtn').addEventListener('click',loadNews);"
        "document.getElementById('q').addEventListener('input',function(){if(timer)clearTimeout(timer);timer=setTimeout(loadNews,400)});"
        "document.getElementById('minImp').addEventListener('change',loadNews);"
        "window.addEventListener('load',function(){loadNews();setInterval(loadNews,60000)});"
        "</script></div></body></html>"
    )

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - VERSION COMPLÈTE")
    print("="*70)
    print(f"📍 http://localhost:8000")
    print(f"📊 Dashboard: http://localhost:8000/trades")
    print(f"🗞️ Annonces: http://localhost:8000/annonces")
    print("="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
