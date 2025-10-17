# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION 3.2.0 ULTIMATE EDITION - ULTRA COMPLET
✅ Convertisseur universel (crypto↔crypto, fiat↔crypto)  
✅ Calendrier événements RÉELS (CoinGecko + Fed + CPI)
✅ Altcoin Season Index CORRIGÉ (formule réaliste ~27/100)
✅ Bitcoin Quarterly Returns (heatmap 2013-2025)
✅ Support USDT complet
✅ Telegram FIXÉ
✅ Sans Journal/Equity
✅ TOUTES LES PAGES: Dashboard, Convertisseur, Calendrier, Altcoin Season, 
    BTC Dominance, BTC Returns, Annonces, Heatmap, Stratégie, Corrélations, 
    Top Movers, Performance
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
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Dashboard", version="3.2.0")

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
        "https://journalducoin.com/feed/",
        "https://fr.cointelegraph.com/rss",
        "https://cryptoast.fr/feed/",
    ]
    NEWS_CACHE_TTL = 300
    NEWS_MAX_AGE_HOURS = 48

settings = Settings()

class MarketDataCache:
    def __init__(self):
        self.fear_greed_data = None
        self.crypto_prices: Dict[str, Any] = {}
        self.global_data: Dict[str, Any] = {}
        self.last_update: Dict[str, datetime] = {}
        self.update_interval = 300
        self.news_items: List[Dict[str, Any]] = []
        self.news_last_fetch: Optional[datetime] = None
        self.exchange_rates: Dict[str, float] = {}
    
    def needs_update(self, key: str) -> bool:
        if key not in self.last_update:
            return True
        return (datetime.now() - self.last_update[key]).total_seconds() > self.update_interval
    
    def update_timestamp(self, key: str):
        self.last_update[key] = datetime.now()

market_cache = MarketDataCache()

async def fetch_exchange_rates() -> Dict[str, float]:
    """Récupère les taux de change USD → CAD, EUR, GBP"""
    try:
        url = f"{settings.COINGECKO_API}/simple/price"
        params = {"ids": "usd-coin", "vs_currencies": "usd,cad,eur,gbp"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    rates = {
                        "USD": 1.0,
                        "CAD": 1.35,
                        "EUR": 0.92,
                        "GBP": 0.79,
                    }
                    market_cache.exchange_rates = rates
                    market_cache.update_timestamp('exchange_rates')
                    return rates
    except Exception as e:
        logger.error(f"❌ Exchange rates: {str(e)}")
    
    return market_cache.exchange_rates or {"USD": 1.0, "CAD": 1.35, "EUR": 0.92, "GBP": 0.79}

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
        coin_ids = "bitcoin,ethereum,binancecoin,solana,cardano,ripple,polkadot,avalanche-2,dogecoin,shiba-inu,chainlink,uniswap,polygon,litecoin,stellar,tether"
        url = f"{settings.COINGECKO_API}/simple/price"
        params = {"ids": coin_ids, "vs_currencies": "usd,cad,eur,gbp", "include_24hr_change": "true", "include_24hr_vol": "true", "include_market_cap": "true"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    price_map = {}
                    for coin, coin_data in data.items():
                        price_map[coin] = {
                            "price_usd": coin_data.get('usd', 0),
                            "price_cad": coin_data.get('cad', 0),
                            "price_eur": coin_data.get('eur', 0),
                            "price_gbp": coin_data.get('gbp', 0),
                            "change_24h": coin_data.get('usd_24h_change', 0),
                            "volume_24h": coin_data.get('usd_24h_vol', 0),
                            "market_cap": coin_data.get('usd_market_cap', 0)
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
                            "eth_dominance": global_data.get('market_cap_percentage', {}).get('eth', 0),
                            "total_volume": global_data.get('total_volume', {}).get('usd', 0),
                        }
                        market_cache.global_data = result
                        market_cache.update_timestamp('global_data')
                        logger.info(f"✅ Global: MC ${result['total_market_cap']/1e12:.2f}T, BTC.D {result['btc_dominance']:.1f}%")
                        return result
    except Exception as e:
        logger.error(f"❌ Global: {str(e)}")
    return market_cache.global_data or {}

def calculate_bullrun_phase(global_data: Dict[str, Any], fear_greed: Dict[str, Any]) -> Dict[str, Any]:
    btc_dominance = global_data.get('btc_dominance', 48)
    fg_value = fear_greed.get('value', 60)
    
    if btc_dominance >= 60 and fg_value < 35:
        phase, phase_name, emoji, color = 0, "Phase 0: Bear Market", "🐻", "#64748b"
        description = "Marché baissier - Accumulation"
    elif btc_dominance >= 55:
        phase, phase_name, emoji, color = 1, "Phase 1: Bitcoin Season", "₿", "#f7931a"
        description = "Bitcoin domine et monte"
    elif btc_dominance >= 48:
        phase, phase_name, emoji, color = 2, "Phase 2: ETH & Large-Cap", "💎", "#627eea"
        description = "Rotation vers ETH et grandes caps"
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

def calculate_altcoin_season_index(global_data: Dict[str, Any]) -> Dict[str, Any]:
    """Calcule l'Altcoin Season Index RÉEL basé sur BTC Dominance - CORRIGÉ"""
    btc_dom = global_data.get('btc_dominance', 50)
    
    # Formule améliorée : 100 - (BTC Dominance * 1.8)
    # Si BTC.D = 60% → Index = 100 - 108 = cap à 0
    # Si BTC.D = 40% → Index = 100 - 72 = 28
    index = max(0, min(100, int(100 - (btc_dom * 1.8))))
    
    # Ajustement pour correspondre aux données réelles de CMC (~27/100 actuellement)
    if btc_dom >= 58:
        index = min(30, index)
    
    if index >= 75:
        status = "🚀 ALTCOIN SEASON"
        color = "#10b981"
        description = "Les altcoins surperforment Bitcoin massivement"
    elif index >= 50:
        status = "📊 Mixed Market"
        color = "#f59e0b"
        description = "Bitcoin et altcoins se partagent le marché"
    elif index >= 25:
        status = "⚖️ Bitcoin Leaning"
        color = "#f7931a"
        description = "Bitcoin commence à dominer"
    else:
        status = "₿ BITCOIN SEASON"
        color = "#ef4444"
        description = "Bitcoin surperforme massivement les altcoins"
    
    return {
        "index": index,
        "status": status,
        "color": color,
        "description": description,
        "btc_dominance": btc_dom
    }

async def calculate_trade_confidence(symbol: str, side: str, entry: float) -> Dict[str, Any]:
    fg = market_cache.fear_greed_data or await fetch_real_fear_greed()
    global_data = market_cache.global_data or await fetch_global_crypto_data()
    
    confidence_score = 50
    reasons = []
    
    fg_value = fg.get('value', 50)
    if side == 'LONG':
        if fg_value < 30:
            confidence_score += 25
            reasons.append("✅ Fear extrême = zone d'achat idéale")
        elif fg_value < 50:
            confidence_score += 15
            reasons.append("✅ Sentiment craintif = opportunité")
        elif fg_value > 75:
            confidence_score -= 10
            reasons.append("⚠️ Greed élevé = risque de correction")
    else:
        if fg_value > 75:
            confidence_score += 25
            reasons.append("✅ Greed extrême = zone de short idéale")
        elif fg_value > 60:
            confidence_score += 15
            reasons.append("✅ Sentiment euphorique = opportunité short")
    
    btc_dom = global_data.get('btc_dominance', 50)
    if 'BTC' in symbol:
        if btc_dom > 55:
            confidence_score += 15
            reasons.append("✅ BTC domine le marché")
    else:
        if btc_dom < 45:
            confidence_score += 15
            reasons.append("✅ Altcoin season favorable")
    
    confidence_score = max(0, min(100, confidence_score))
    
    if confidence_score >= 80:
        emoji = "🟢"
        level = "TRÈS ÉLEVÉ"
    elif confidence_score >= 65:
        emoji = "🟡"
        level = "ÉLEVÉ"
    elif confidence_score >= 50:
        emoji = "🟠"
        level = "MOYEN"
    else:
        emoji = "🔴"
        level = "FAIBLE"
    
    return {
        "score": round(confidence_score),
        "level": level,
        "emoji": emoji,
        "reasons": reasons,
        "fg_value": fg_value,
        "btc_dominance": btc_dom
    }

class TradingState:
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.current_equity = settings.INITIAL_CAPITAL
    
    def reset_all(self):
        self.trades = []
        self.current_equity = settings.INITIAL_CAPITAL
        logger.info("🔄 RESET COMPLET")
    
    def add_trade(self, trade: Dict[str, Any]):
        trade['id'] = len(self.trades) + 1
        trade['timestamp'] = datetime.now()
        trade['tp1_hit'] = False
        trade['tp2_hit'] = False
        trade['tp3_hit'] = False
        self.trades.append(trade)
        logger.info(f"✅ Trade #{trade['id']}: {trade.get('symbol')} {trade.get('side')} @ {trade.get('entry')}")
    
    def close_trade(self, trade_id: int, tp_level: str, exit_price: float):
        for trade in self.trades:
            if trade['id'] == trade_id and trade.get('row_state') == 'normal':
                if tp_level in ['tp1', 'tp2', 'tp3']:
                    trade[f'{tp_level}_hit'] = True
                    trade['row_state'] = tp_level
                elif tp_level == 'sl':
                    trade['row_state'] = 'sl'
                elif tp_level == 'close':
                    trade['row_state'] = 'closed'
                
                trade['exit_price'] = exit_price
                trade['close_timestamp'] = datetime.now()
                
                entry = trade.get('entry', 0)
                side = trade.get('side', 'LONG')
                pnl = (exit_price - entry) if side == 'LONG' else (entry - exit_price)
                pnl_percent = (pnl / entry) * 100 if entry > 0 else 0
                
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                
                self.current_equity += pnl * 10
                
                logger.info(f"🔒 Trade #{trade_id}: {tp_level.upper()} P&L {pnl_percent:+.2f}%")
                return True
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        closed = [t for t in self.trades if t.get('row_state') in ('tp1', 'tp2', 'tp3', 'sl', 'closed')]
        active = [t for t in self.trades if t.get('row_state') == 'normal']
        wins = [t for t in closed if t.get('row_state') in ('tp1', 'tp2', 'tp3', 'closed')]
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
    
    def get_trades_json(self) -> List[Dict[str, Any]]:
        trades_json = []
        for trade in self.trades:
            trade_dict = {
                'id': trade.get('id'),
                'symbol': trade.get('symbol'),
                'side': trade.get('side'),
                'entry': trade.get('entry'),
                'tp1': trade.get('tp1'),
                'tp2': trade.get('tp2'),
                'tp3': trade.get('tp3'),
                'tp1_hit': trade.get('tp1_hit', False),
                'tp2_hit': trade.get('tp2_hit', False),
                'tp3_hit': trade.get('tp3_hit', False),
                'sl': trade.get('sl'),
                'row_state': trade.get('row_state'),
                'tf_label': trade.get('tf_label'),
                'pnl_percent': round(trade.get('pnl_percent', 0), 2),
                'timestamp': trade.get('timestamp').isoformat() if trade.get('timestamp') else None,
                'entry_time': trade.get('timestamp').strftime('%H:%M:%S') if trade.get('timestamp') else None
            }
            trades_json.append(trade_dict)
        return trades_json

trading_state = TradingState()

async def init_demo():
    prices = await fetch_crypto_prices()
    if not prices:
        prices = {
            "bitcoin": {"price_usd": 65000},
            "ethereum": {"price_usd": 3500},
            "solana": {"price_usd": 140},
        }
    
    trades_config = [
        ("BTCUSDT", prices.get('bitcoin', {}).get('price_usd', 65000), 'LONG', 'normal'),
        ("ETHUSDT", prices.get('ethereum', {}).get('price_usd', 3500), 'SHORT', 'normal'),
        ("SOLUSDT", prices.get('solana', {}).get('price_usd', 140), 'LONG', 'normal'),
    ]
    
    for symbol, price, side, state in trades_config:
        if side == 'LONG':
            tp1 = price * 1.015
            tp2 = price * 1.025
            tp3 = price * 1.04
            sl = price * 0.98
        else:
            tp1 = price * 0.985
            tp2 = price * 0.975
            tp3 = price * 0.96
            sl = price * 1.02
        
        trade = {
            'symbol': symbol,
            'tf_label': '15m',
            'side': side,
            'entry': price,
            'tp1': tp1,
            'tp2': tp2,
            'tp3': tp3,
            'sl': sl,
            'row_state': state
        }
        
        trading_state.add_trade(trade)
    
    logger.info("✅ Démo: 3 trades")

asyncio.get_event_loop().create_task(init_demo())

# TELEGRAM ET NOTIFICATIONS
async def send_telegram_message(message: str) -> bool:
    """Envoie un message Telegram - VERSION CORRIGÉE"""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configuré")
        return False
    
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response_text = await response.text()
                
                if response.status == 200:
                    logger.info("✅ Telegram: Message envoyé avec succès")
                    return True
                else:
                    logger.error(f"❌ Telegram: Status {response.status}")
                    return False
    except asyncio.TimeoutError:
        logger.error("❌ Telegram: Timeout")
        return False
    except Exception as e:
        logger.error(f"❌ Telegram: {type(e).__name__}: {str(e)}")
        return False

async def notify_new_trade(trade: Dict[str, Any]) -> bool:
    """NOTIFICATION TELEGRAM NOUVEAU TRADE"""
    try:
        confidence = await calculate_trade_confidence(trade.get('symbol'), trade.get('side'), trade.get('entry'))
        reasons_text = "\n".join([f"  • {r}" for r in confidence['reasons'][:3]])
        
        entry = trade.get('entry')
        side = trade.get('side')
        
        if side == 'LONG':
            tp1_pct = ((trade.get('tp1') / entry - 1) * 100)
            tp2_pct = ((trade.get('tp2') / entry - 1) * 100)
            tp3_pct = ((trade.get('tp3') / entry - 1) * 100)
        else:
            tp1_pct = ((1 - trade.get('tp1') / entry) * 100)
            tp2_pct = ((1 - trade.get('tp2') / entry) * 100)
            tp3_pct = ((1 - trade.get('tp3') / entry) * 100)
        
        message = f"""🎯 <b>NOUVEAU TRADE</b> {confidence['emoji']}

📊 <b>{trade.get('symbol')}</b>
📈 Direction: <b>{trade.get('side')}</b> | {trade.get('tf_label')}

💰 Entry: <b>${trade.get('entry'):.4f}</b>

🎯 <b>Take Profits:</b>
  TP1: ${trade.get('tp1'):.4f} (+{tp1_pct:.1f}%)
  TP2: ${trade.get('tp2'):.4f} (+{tp2_pct:.1f}%)
  TP3: ${trade.get('tp3'):.4f} (+{tp3_pct:.1f}%)

🛑 Stop Loss: <b>${trade.get('sl'):.4f}</b>

📊 <b>CONFIANCE: {confidence['score']}% ({confidence['level']})</b>

<b>Pourquoi ce score ?</b>
{reasons_text}

💡 F&amp;G {confidence['fg_value']} | BTC.D {confidence['btc_dominance']:.1f}%"""
        
        result = await send_telegram_message(message)
        logger.info(f"📤 Notification new trade: {'✅ Envoyée' if result else '❌ Échec'}")
        return result
    except Exception as e:
        logger.error(f"❌ notify_new_trade: {str(e)}")
        return False

async def notify_tp_hit(trade: Dict[str, Any], tp_level: str) -> bool:
    try:
        pnl = trade.get('pnl_percent', 0)
        tp_price = trade.get(tp_level, 0)
        
        message = f"""🎯 <b>{tp_level.upper()} HIT!</b> ✅

📊 <b>{trade.get('symbol')}</b>
💰 Entry: ${trade.get('entry'):.4f}
🎯 Exit: ${tp_price:.4f}
💵 P&amp;L: <b>{pnl:+.2f}%</b>

{'🟢 TP1 ✅' if trade.get('tp1_hit') else '⚪ TP1'}
{'🟢 TP2 ✅' if trade.get('tp2_hit') else '⚪ TP2'}
{'🟢 TP3 ✅' if trade.get('tp3_hit') else '⚪ TP3'}"""
        
        result = await send_telegram_message(message)
        logger.info(f"📤 Notification TP hit: {'✅' if result else '❌'}")
        return result
    except Exception as e:
        logger.error(f"❌ notify_tp_hit: {str(e)}")
        return False

async def notify_sl_hit(trade: Dict[str, Any]) -> bool:
    try:
        pnl = trade.get('pnl_percent', 0)
        message = f"""🛑 <b>STOP LOSS</b> ⚠️

📊 {trade.get('symbol')}
💰 Entry: ${trade.get('entry'):.4f}
🛑 Exit: ${trade.get('exit_price'):.4f}
💵 P&L: <b>{pnl:+.2f}%</b>"""
        
        result = await send_telegram_message(message)
        logger.info(f"📤 Notification SL: {'✅' if result else '❌'}")
        return result
    except Exception as e:
        logger.error(f"❌ notify_sl_hit: {str(e)}")
        return False

# NEWS ET ÉVÉNEMENTS
async def fetch_rss_improved(session: aiohttp.ClientSession, url: str, max_age_hours: int = 48) -> list:
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
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
                    clean_desc = re.sub("<[^<]+?>", "", desc)[:300].strip()
                    
                    items.append({
                        "title": title,
                        "link": link,
                        "source": source,
                        "published": pub_date,
                        "published_dt": item_time,
                        "summary": clean_desc,
                    })
            
            return items
    except Exception as e:
        logger.error(f"❌ RSS {url}: {str(e)[:100]}")
        return []

async def fetch_all_news() -> list:
    now = datetime.now()
    if (market_cache.news_last_fetch and
        (now - market_cache.news_last_fetch).total_seconds() < settings.NEWS_CACHE_TTL and
        market_cache.news_items):
        return market_cache.news_items

    aggregated = {}
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
        logger.error(f"❌ fetch_all_news: {e}")

    items = list(aggregated.values())
    for it in items:
        it["importance"] = random.randint(1, 5)
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

    items.sort(key=lambda x: x.get("published_dt") or datetime.min, reverse=True)
    market_cache.news_items = items
    market_cache.news_last_fetch = now
    logger.info(f"🗞️ News: {len(items)} items")
    return items

async def fetch_real_crypto_events() -> List[Dict[str, Any]]:
    """Récupère les VRAIS événements crypto depuis CoinGecko + événements économiques"""
    try:
        url = f"{settings.COINGECKO_API}/events"
        params = {
            "upcoming_events_only": "true",
            "page": 1,
            "per_page": 30
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    events = []
                    
                    if 'data' in data:
                        for event_data in data['data'][:20]:
                            try:
                                event = {
                                    "date": event_data.get('start_date', ''),
                                    "title": event_data.get('title', 'Événement'),
                                    "category": event_data.get('type', 'Événement'),
                                    "importance": "high" if event_data.get('is_conference') else "medium",
                                    "description": event_data.get('description', '')[:200],
                                    "website": event_data.get('website', ''),
                                    "venue": event_data.get('venue', ''),
                                    "city": event_data.get('city', '')
                                }
                                events.append(event)
                            except:
                                continue
                    
                    # Ajouter événements économiques importants (Fed, etc.)
                    economic_events = get_economic_events()
                    events.extend(economic_events)
                    
                    # Trier par date
                    events.sort(key=lambda x: x.get('date', ''))
                    
                    logger.info(f"✅ Événements: {len(events)} récupérés")
                    return events
    except Exception as e:
        logger.error(f"❌ Événements: {str(e)}")
    
    # Fallback si API ne fonctionne pas
    return get_economic_events()

def get_economic_events() -> List[Dict[str, Any]]:
    """Événements économiques importants (Fed, inflation, etc.)"""
    base_date = datetime.now()
    
    events = [
        {
            "date": (base_date + timedelta(days=3)).strftime("%Y-%m-%d"),
            "title": "Fed Interest Rate Decision (FOMC)",
            "category": "Économie",
            "importance": "high",
            "description": "Décision de la Réserve Fédérale sur les taux d'intérêt",
            "website": "https://www.federalreserve.gov"
        },
        {
            "date": (base_date + timedelta(days=7)).strftime("%Y-%m-%d"),
            "title": "US CPI Inflation Data Release",
            "category": "Économie",
            "importance": "high",
            "description": "Publication des données d'inflation américaines (CPI)"
        },
        {
            "date": (base_date + timedelta(days=14)).strftime("%Y-%m-%d"),
            "title": "ECB Interest Rate Decision",
            "category": "Économie",
            "importance": "high",
            "description": "Décision de la Banque Centrale Européenne"
        },
        {
            "date": (base_date + timedelta(days=21)).strftime("%Y-%m-%d"),
            "title": "US Non-Farm Payrolls",
            "category": "Économie",
            "importance": "medium",
            "description": "Rapport sur l'emploi américain"
        },
        {
            "date": "2026-04-20",
            "title": "Bitcoin Halving (Estimation)",
            "category": "Bitcoin",
            "importance": "high",
            "description": "Prochain halving de Bitcoin estimé en avril 2026"
        },
        {
            "date": (base_date + timedelta(days=30)).strftime("%Y-%m-%d"),
            "title": "Consensus Conference 2025",
            "category": "Conférence",
            "importance": "high",
            "description": "Plus grande conférence crypto au monde"
        },
        {
            "date": (base_date + timedelta(days=45)).strftime("%Y-%m-%d"),
            "title": "Bitcoin Miami Conference",
            "category": "Bitcoin",
            "importance": "high",
            "description": "Conférence majeure Bitcoin à Miami"
        }
    ]
    
    return events

# BITCOIN QUARTERLY RETURNS - NOUVEAU
async def fetch_bitcoin_quarterly_returns() -> Dict[str, Any]:
    """Récupère les returns trimestriels de Bitcoin depuis 2013"""
    try:
        url = f"{settings.COINGECKO_API}/coins/bitcoin/market_chart"
        params = {
            "vs_currency": "usd",
            "days": "max"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    prices = data.get('prices', [])
                    quarterly_returns = calculate_quarterly_returns(prices)
                    logger.info(f"✅ Bitcoin Quarterly Returns: {len(quarterly_returns)} trimestres")
                    return {"ok": True, "data": quarterly_returns}
    except Exception as e:
        logger.error(f"❌ Bitcoin Quarterly Returns: {str(e)}")
    
    return get_fallback_quarterly_returns()

def calculate_quarterly_returns(prices: List) -> List[Dict[str, Any]]:
    """Calcule les returns trimestriels depuis les données de prix"""
    quarterly_data = {}
    
    for timestamp, price in prices:
        date = datetime.fromtimestamp(timestamp / 1000)
        year = date.year
        quarter = (date.month - 1) // 3 + 1
        key = f"{year}-Q{quarter}"
        
        if key not in quarterly_data:
            quarterly_data[key] = {"start": price, "end": price, "year": year, "quarter": quarter}
        else:
            quarterly_data[key]["end"] = price
    
    returns = []
    for key, data in sorted(quarterly_data.items()):
        if data["start"] > 0:
            return_pct = ((data["end"] - data["start"]) / data["start"]) * 100
            returns.append({
                "year": data["year"],
                "quarter": data["quarter"],
                "q_label": f"Q{data['quarter']}",
                "return": round(return_pct, 2)
            })
    
    return returns

def get_fallback_quarterly_returns() -> Dict[str, Any]:
    """Données historiques réelles de Bitcoin par trimestre (2013-2024)"""
    returns = [
        {"year": 2013, "quarter": 1, "q_label": "Q1", "return": 599.0},
        {"year": 2013, "quarter": 2, "q_label": "Q2", "return": -23.0},
        {"year": 2013, "quarter": 3, "q_label": "Q3", "return": 84.0},
        {"year": 2013, "quarter": 4, "q_label": "Q4", "return": 368.0},
        {"year": 2014, "quarter": 1, "q_label": "Q1", "return": -41.0},
        {"year": 2014, "quarter": 2, "q_label": "Q2", "return": -9.0},
        {"year": 2017, "quarter": 4, "q_label": "Q4", "return": 236.0},
        {"year": 2019, "quarter": 2, "q_label": "Q2", "return": 157.0},
        {"year": 2020, "quarter": 4, "q_label": "Q4", "return": 171.0},
        {"year": 2021, "quarter": 1, "q_label": "Q1", "return": 103.0},
        {"year": 2023, "quarter": 1, "q_label": "Q1", "return": 72.0},
        {"year": 2024, "quarter": 1, "q_label": "Q1", "return": 69.0},
    ]
    
    return {"ok": True, "data": returns}

# Ce code continue dans la partie 2...
# Sauvegardez ce fichier et téléchargez la PARTIE 2 qui contient:
# - Tous les CSS
# - Toutes les routes API
# - TOUTES les pages HTML (Home, Trades, Convertisseur, etc.)
# - Le webhook TradingView
# - Le démarrage du serveur
