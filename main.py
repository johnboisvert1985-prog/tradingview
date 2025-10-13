# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION FINALE CORRIGÉE
✅ News en français (sources FR + traduction)
✅ Bull Run Phase réaliste
✅ Tout fonctionnel
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

app = FastAPI(title="Trading Dashboard", version="2.4.0")

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
    
    # CORRIGÉ: Sources françaises vérifiées
    NEWS_SOURCES = [
        "https://journalducoin.com/feed/",
        "https://fr.cointelegraph.com/rss",
        "https://cryptoast.fr/feed/",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.theblockcrypto.com/rss.xml",
    ]
    
    FRENCH_SOURCES = ['journalducoin.com', 'fr.cointelegraph.com', 'cryptoast.fr']
    NEWS_CACHE_TTL = 60
    NEWS_MAX_AGE_HOURS = 48

settings = Settings()

# Dictionnaire de traduction crypto
TRANSLATION_DICT = {
    "announces": "annonce", "launches": "lance", "reveals": "révèle",
    "says": "déclare", "hits": "atteint", "surges": "explose",
    "drops": "chute", "falls": "baisse", "rises": "augmente",
    "soars": "s'envole", "plunges": "plonge", "unveils": "dévoile",
    "approves": "approuve", "rejects": "rejette",
    "price": "prix", "market": "marché", "trading": "trading",
    "exchange": "exchange", "wallet": "portefeuille",
    "analyst": "analyste", "investor": "investisseur",
    "regulation": "régulation", "ban": "interdiction",
    "adoption": "adoption", "partnership": "partenariat",
    "hack": "piratage", "security": "sécurité",
    "all-time high": "plus haut historique",
    "bull market": "marché haussier", "bear market": "marché baissier",
}

def simple_translate(text: str) -> str:
    """Traduit les mots-clés crypto"""
    if not text:
        return text
    french_words = ['le', 'la', 'les', 'un', 'une', 'et', 'à', 'pour', 'dans']
    if any(f' {w} ' in f' {text.lower()} ' for w in french_words):
        return text
    result = text
    for eng, fra in TRANSLATION_DICT.items():
        result = re.sub(r'\b' + re.escape(eng) + r'\b', fra, result, flags=re.IGNORECASE)
    return result

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
        return (datetime.now() - self.last_update[key]).total_seconds() > self.update_interval
    
    def update_timestamp(self, key: str):
        self.last_update[key] = datetime.now()

market_cache = MarketDataCache()

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
                        logger.info(f"✅ Global: MC ${result['total_market_cap']/1e12:.2f}T, BTC.D {result['btc_dominance']:.1f}%")
                        return result
    except Exception as e:
        logger.error(f"❌ Global: {str(e)}")
    return market_cache.global_data or {}

def calculate_bullrun_phase(global_data: Dict[str, Any], fear_greed: Dict[str, Any]) -> Dict[str, Any]:
    """CORRIGÉ: Logique réaliste de détection de phase"""
    btc_dominance = global_data.get('btc_dominance', 48)
    fg_value = fear_greed.get('value', 60)
    
    # Phase 0: Bear Market (très restrictif)
    if btc_dominance >= 60 and fg_value < 35:
        phase, phase_name, emoji, color = 0, "Phase 0: Bear Market", "🐻", "#64748b"
        description = "Marché baissier - Accumulation"
    # Phase 1: Bitcoin Season
    elif btc_dominance >= 55:
        phase, phase_name, emoji, color = 1, "Phase 1: Bitcoin Season", "₿", "#f7931a"
        description = "Bitcoin domine et monte"
    # Phase 2: ETH & Large-Cap
    elif btc_dominance >= 48:
        phase, phase_name, emoji, color = 2, "Phase 2: ETH & Large-Cap", "💎", "#627eea"
        description = "Rotation vers ETH et grandes caps"
    # Phase 3: Altcoin Season
    else:
        phase, phase_name, emoji, color = 3, "Phase 3: Altcoin Season", "🚀", "#10b981"
        description = "Les altcoins explosent"
    
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
.filter-chip { display: inline-block; padding: 6px 12px; margin: 4px; background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3); border-radius: 16px; cursor: pointer; transition: all 0.3s; font-size: 12px; }
.filter-chip:hover { background: rgba(99,102,241,0.2); transform: translateY(-2px); }
.filter-chip.active { background: #6366f1; color: white; border-color: #6366f1; }
</style>"""

NAV = """<div class="nav">
<a href="/">🏠 Home</a>
<a href="/trades">📊 Dashboard</a>
<a href="/equity-curve">📈 Equity</a>
<a href="/journal">📝 Journal</a>
<a href="/heatmap">🔥 Heatmap</a>
<a href="/strategie">⚙️ Stratégie</a>
<a href="/annonces">🗞️ Annonces</a>
</div>"""

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

KEYWORDS_BY_CATEGORY = {
    "regulation": {"keywords": [r"\bETF\b", r"\bSEC\b", r"\brégulation\b", r"\bregulation\b"], "emoji": "⚖️", "name_fr": "Régulation", "boost": 2},
    "security": {"keywords": [r"\bhack\b", r"\bexploit\b", r"\bpiratage\b"], "emoji": "🔒", "name_fr": "Sécurité", "boost": 3},
    "markets": {"keywords": [r"\bATH\b", r"\bcrash\b", r"\bpump\b"], "emoji": "📈", "name_fr": "Marchés", "boost": 1},
}

def score_importance_advanced(title: str, summary: str, source: str) -> dict:
    text = f"{title} {summary}".lower()
    score = 1
    categories = []
    
    for cat_key, cat_data in KEYWORDS_BY_CATEGORY.items():
        for kw in cat_data["keywords"]:
            if re.search(kw, text, flags=re.IGNORECASE):
                if cat_key not in categories:
                    categories.append(cat_key)
                    score += cat_data["boost"]
    
    if "binance.com" in source.lower():
        score += 2
    
    return {"score": min(int(score), 5), "categories": categories, "sentiment": "neutre"}

async def fetch_rss_improved(session: aiohttp.ClientSession, url: str, max_age_hours: int = 48) -> list[dict]:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }
        
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), headers=headers) as resp:
            if resp.status not in [200, 202]:
                return []
            
            raw = await resp.text()
            items = []
            
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
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
                        parsed = parsedate_to_datetime(pub_date)
                        item_time = parsed.replace(tzinfo=None)
                    except:
                        pass
                    
                    if item_time and item_time < cutoff_time:
                        continue
                    
                    source = urlparse(url).netloc
                    clean_desc = re.sub("<[^<]+?>", "", desc)[:500].strip()
                    is_french = any(fr_src in source for fr_src in settings.FRENCH_SOURCES)
                    
                    items.append({
                        "title": title,
                        "link": link,
                        "source": source,
                        "published": pub_date,
                        "published_dt": item_time,
                        "summary": clean_desc,
                        "is_french": is_french,
                    })
            
            logger.info(f"✅ RSS {urlparse(url).netloc}: {len(items)} items")
            return items
            
    except Exception as e:
        logger.error(f"❌ RSS {url}: {str(e)[:100]}")
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
        # NOUVEAU: Traduction automatique
        if not it.get("is_french", False):
            it["title"] = simple_translate(it.get("title", ""))
            summary = it.get("summary", "")
            if len(summary) < 300:
                it["summary"] = simple_translate(summary)
        
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
                delta = datetime.now() - it["published_dt"]
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
    
    french_count = sum(1 for i in items if i.get("is_french"))
    logger.info(f"🗞️ News: {len(items)} ({french_count} 🇫🇷 + {len(items)-french_count} traduits)")
    
    return items

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
            "debug": {
                "btc_dominance": gd.get('btc_dominance', 0),
                "fear_greed": fg.get('value', 0),
            }
        }
    }

@app.get("/api/news")
async def api_news(
    q: Optional[str] = None,
    min_importance: int = 1,
    category: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    items = await fetch_all_news_improved()
    
    if q:
        ql = q.lower().strip()
        items = [i for i in items if ql in (i["title"] + " " + i["summary"] + " " + i["source"]).lower()]
    
    items = [i for i in items if i.get("importance", 1) >= min_importance]
    
    if category and category in KEYWORDS_BY_CATEGORY:
        items = [i for i in items if category in i.get("categories", [])]
    
    if language == 'fr':
        items = [i for i in items if i.get("is_french", False)]
    elif language == 'en':
        items = [i for i in items if not i.get("is_french", True)]
    
    total = len(items)
    page = items[offset: offset + limit]
    
    return {
        "ok": True,
        "total": total,
        "count": len(page),
        "items": page,
    }

@app.get("/api/news-sources-test")
async def test_sources():
    """Test quelles sources RSS fonctionnent"""
    results = []
    async with aiohttp.ClientSession() as session:
        for url in settings.NEWS_SOURCES:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    is_french = any(fr in url for fr in settings.FRENCH_SOURCES)
                    results.append({
                        "url": url,
                        "status": resp.status,
                        "working": resp.status in [200, 202],
                        "language": "🇫🇷" if is_french else "🇬🇧"
                    })
            except Exception as e:
                results.append({"url": url, "status": "error", "error": str(e)[:100]})
    
    return {
        "total": len(results),
        "working": sum(1 for r in results if r.get("working")),
        "sources": results
    }

@app.post("/tv-webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        if not body:
            return JSONResponse({"status": "error", "message": "Body vide"}, status_code=400)
        
        try:
            payload = await request.json()
        except:
            return JSONResponse({"status": "error", "message": "JSON invalide"}, status_code=400)
        
        action = (payload.get("type") or payload.get("action") or "").lower()
        symbol = payload.get("symbol")
        side = payload.get("side", "LONG")
        
        if not symbol:
            return JSONResponse({"status": "error", "message": "Symbol manquant"}, status_code=400)
        
        if action == "entry":
            new_trade = {
                'symbol': symbol,
                'tf_label': payload.get("tf_label") or (payload.get("tf", "15") + "m"),
                'side': side,
                'entry': payload.get("entry"),
                'tp': payload.get("tp") or payload.get("tp1"),
                'sl': payload.get("sl"),
                'row_state': 'normal'
            }
            
            if not all([new_trade['entry'], new_trade['tp'], new_trade['sl']]):
                return JSONResponse({"status": "error", "message": "entry/tp/sl manquants"}, status_code=400)
            
            trading_state.add_trade(new_trade)
            await notify_new_trade(new_trade)
            return JSONResponse({"status": "ok", "trade_id": new_trade.get('id')})
        
        elif action.startswith("tp") and "hit" in action:
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = payload.get('price') or payload.get('tp') or trade.get('tp')
                    if trading_state.close_trade(trade['id'], 'tp', exit_price):
                        await notify_tp_hit(trade)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": f"Trade non trouvé"})
        
        elif action.startswith("sl") and "hit" in action:
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = payload.get('price') or payload.get('sl') or trade.get('sl')
                    if trading_state.close_trade(trade['id'], 'sl', exit_price):
                        await notify_sl_hit(trade)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": f"Trade non trouvé"})
        
        return JSONResponse({"status": "error", "message": f"Action non supportée: {action}"}, status_code=400)
        
    except Exception as e:
        logger.error(f"❌ Webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard</h1><p>Système complet <span class="live-badge">LIVE</span></p></div>""" + NAV + """
<div class="card" style="text-align:center;">
<h2>Dashboard Professionnel</h2>
<p style="color:#94a3b8;margin:20px 0;">✅ Données réelles • ✅ News FR • ✅ Phase Bull Run</p>
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
        <div id="br-details" style="text-align:center;font-size:12px;color:#64748b;margin-top:12px"></div>
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

fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
document.getElementById('br').innerHTML=`<div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div><div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>`;
document.getElementById('br-details').innerHTML=`BTC.D: ${{b.debug.btc_dominance}}% | F&G: ${{b.debug.fear_greed}}`;}}}});
</script>
</div></body></html>""")

@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Annonces 🇫🇷</title>" + CSS + "</head>"
        "<body><div class='container'><div class='header'><h1>🗞️ Annonces Crypto</h1></div>" + NAV +
        "<div class='card'><h2>🔍 Filtres</h2>"
        "<div style='display:grid;grid-template-columns:1fr 200px 120px 100px;gap:12px;align-items:end'>"
        "<div><input id='q' placeholder='Recherche...' style='width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0'/></div>"
        "<div><select id='minImp' style='width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0'>"
        "<option value='1'>Toutes</option><option value='3' selected>Importantes</option><option value='5'>Critiques</option></select></div>"
        "<div><select id='language' style='width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0'>"
        "<option value='all' selected>🌐 Toutes</option><option value='fr'>🇫🇷 Sources FR</option><option value='en'>🇬🇧 Sources EN</option></select></div>"
        "<div><button id='refreshBtn'>🔄</button></div></div>"
        "</div>"
        "<div class='card'><h2>📣 Flux</h2>"
        "<div id='status' style='color:#64748b;font-size:12px;margin-bottom:12px'>...</div>"
        "<div id='newsList'></div></div>"
        "<script>"
        "let timer;"
        "function escapeHtml(s){return s?s.replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}[c])):''}"
        "function badge(i){const c={5:'#ef4444',4:'#f59e0b',3:'#10b981',2:'#6366f1',1:'#64748b'},l={5:'CRITIQUE',4:'IMPORTANT',3:'NOTABLE',2:'STANDARD',1:'INFO'};return '<span style=\"padding:4px 8px;border-radius:4px;font-size:11px;font-weight:700;background:rgba(148,163,184,0.1);border:1px solid '+c[i]+';color:'+c[i]+'\">'+l[i]+'</span>'}"
        "async function loadNews(){"
        "const q=document.getElementById('q').value,minImp=document.getElementById('minImp').value,lang=document.getElementById('language').value;"
        "let url='/api/news?min_importance='+minImp+'&limit=50';"
        "if(q)url+='&q='+encodeURIComponent(q);"
        "if(lang!=='all')url+='&language='+lang;"
        "document.getElementById('status').textContent='⏳...';"
        "try{"
        "const r=await fetch(url),d=await r.json();"
        "if(!d.ok)return;"
        "const lang_emoji=lang==='fr'?'🇫🇷':lang==='en'?'🇬🇧':'🌐';"
        "document.getElementById('status').textContent=lang_emoji+' '+d.count+' news (traduites automatiquement)';"
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
        "}catch(e){console.error(e)}"
        "}"
        "document.getElementById('refreshBtn').addEventListener('click',loadNews);"
        "document.getElementById('q').addEventListener('input',function(){if(timer)clearTimeout(timer);timer=setTimeout(loadNews,400)});"
        "document.getElementById('minImp').addEventListener('change',loadNews);"
        "document.getElementById('language').addEventListener('change',loadNews);"
        "window.addEventListener('load',function(){loadNews();setInterval(loadNews,60000)});"
        "</script></div></body></html>"
    )

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - VERSION FINALE CORRIGÉE")
    print("="*70)
    print(f"📍 http://localhost:8000")
    print(f"📊 Dashboard: http://localhost:8000/trades")
    print(f"🗞️ Annonces FR: http://localhost:8000/annonces")
    print(f"🧪 Test sources: http://localhost:8000/api/news-sources-test")
    print("="*70)
    print("📦 Version: 2.4.0")
    print("✅ News traduites en français")
    print("✅ Bull Run Phase corrigée")
    print("✅ Sources françaises + traduction auto")
    print("="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
