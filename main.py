# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION 2.6.2 COMPLÈTE
✅ Toutes fonctionnalités OK
✅ Webhook SANS secret
✅ RESET + Fear&Greed + Bull Run + Toutes pages
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

app = FastAPI(title="Trading Dashboard", version="2.6.2")

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
        self.equity_curve: List[Dict[str, Any]] = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.journal_entries: List[Dict[str, Any]] = []
    
    def reset_all(self):
        self.trades = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.journal_entries = []
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
                self.equity_curve.append({"equity": self.current_equity, "timestamp": datetime.now()})
                
                logger.info(f"🔒 Trade #{trade_id}: {tp_level.upper()} P&L {pnl_percent:+.2f}%")
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
                'timestamp': trade.get('timestamp').isoformat() if trade.get('timestamp') else None
            }
            trades_json.append(trade_dict)
        return trades_json

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
    
    trades_config = [
        ("BTCUSDT", prices.get('bitcoin', {}).get('price', 65000), 'LONG', 'normal'),
        ("ETHUSDT", prices.get('ethereum', {}).get('price', 3500), 'SHORT', 'normal'),
        ("SOLUSDT", prices.get('solana', {}).get('price', 140), 'LONG', 'normal'),
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
                    logger.error(f"❌ Telegram: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"❌ Telegram: {str(e)}")
        return False

async def notify_new_trade(trade: Dict[str, Any]) -> bool:
    confidence = await calculate_trade_confidence(trade.get('symbol'), trade.get('side'), trade.get('entry'))
    reasons_text = "\n".join([f"  • {r}" for r in confidence['reasons'][:4]])
    
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

💡 Marché: F&amp;G {confidence['fg_value']} | BTC.D {confidence['btc_dominance']:.1f}%"""
    
    return await send_telegram_message(message)

async def notify_tp_hit(trade: Dict[str, Any], tp_level: str) -> bool:
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
    
    return await send_telegram_message(message)

async def notify_sl_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""🛑 <b>STOP LOSS</b> ⚠️

📊 {trade.get('symbol')}
💰 Entry: ${trade.get('entry'):.4f}
🛑 Exit: ${trade.get('exit_price'):.4f}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(message)

async def notify_close(trade: Dict[str, Any], reason: str = "Manuel") -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""⏹️ <b>TRADE FERMÉ</b>

📊 {trade.get('symbol')}
💰 Entry: ${trade.get('entry'):.4f}
⏹️ Exit: ${trade.get('exit_price'):.4f}
💵 P&L: <b>{pnl:+.2f}%</b>
📝 Raison: {reason}"""
    return await send_telegram_message(message)

CSS = """<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
.container { max-width: 1600px; margin: 0 auto; }
.header { text-align: center; margin-bottom: 40px; padding: 20px; }
.header h1 { font-size: 36px; margin-bottom: 10px; color: #6366f1; }
.header p { color: #94a3b8; }
.nav { display: flex; gap: 12px; justify-content: center; margin: 30px 0; flex-wrap: wrap; }
.nav a { padding: 10px 20px; background: rgba(99, 102, 241, 0.2); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 8px; color: #6366f1; text-decoration: none; font-weight: 600; transition: all 0.3s; }
.nav a:hover { background: rgba(99, 102, 241, 0.3); transform: translateY(-2px); }
.card { background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3); }
.card h2 { font-size: 20px; margin-bottom: 16px; color: #6366f1; font-weight: 700; }
.grid { display: grid; gap: 20px; margin-bottom: 20px; }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.metric { background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 12px; padding: 24px; text-align: center; }
.metric-label { font-size: 12px; color: #64748b; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { font-size: 36px; font-weight: bold; color: #6366f1; }
.badge { display: inline-block; padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 700; }
.badge-green { background: rgba(16, 185, 129, 0.2); color: #10b981; }
.badge-red { background: rgba(239, 68, 68, 0.2); color: #ef4444; }
.badge-yellow { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 8px; text-align: left; }
th { color: #64748b; font-weight: 600; border-bottom: 2px solid rgba(99, 102, 241, 0.3); font-size: 11px; }
tr { border-bottom: 1px solid rgba(99, 102, 241, 0.1); }
tr:hover { background: rgba(99, 102, 241, 0.05); }
.tp-cell { display: flex; flex-direction: column; gap: 4px; }
.tp-item { padding: 4px 8px; border-radius: 4px; font-size: 11px; }
.tp-pending { background: rgba(100, 116, 139, 0.2); color: #64748b; }
.tp-hit { background: rgba(16, 185, 129, 0.2); color: #10b981; font-weight: 600; }
.gauge { width: 120px; height: 120px; margin: 0 auto 20px; background: conic-gradient(#6366f1 0deg, #8b5cf6 180deg, #ec4899 360deg); border-radius: 50%; display: flex; align-items: center; justify-content: center; }
.gauge-inner { width: 90px; height: 90px; background: #1e293b; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.gauge-value { font-size: 32px; font-weight: bold; }
.gauge-label { font-size: 12px; color: #64748b; }
.live-badge { display: inline-block; padding: 4px 8px; background: rgba(16, 185, 129, 0.2); color: #10b981; border-radius: 4px; font-size: 10px; font-weight: 700; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
.reset-btn { position: fixed; top: 20px; right: 20px; padding: 12px 24px; background: #ef4444; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; z-index: 1000; }
.reset-btn:hover { background: #dc2626; }
.modal { display: none; position: fixed; z-index: 2000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.7); }
.modal-content { background-color: #1e293b; margin: 15% auto; padding: 30px; border: 2px solid #ef4444; border-radius: 12px; width: 90%; max-width: 500px; text-align: center; }
.modal-buttons { display: flex; gap: 12px; justify-content: center; margin-top: 20px; }
.btn-confirm { padding: 12px 24px; background: #ef4444; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; }
.btn-cancel { padding: 12px 24px; background: #64748b; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; }
</style>"""

NAV = """<div class="nav">
<a href="/">Home</a>
<a href="/trades">Dashboard</a>
</div>"""

@app.get("/api/trades")
async def api_trades():
    return {"ok": True, "trades": trading_state.get_trades_json()}

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
        }
    }

@app.get("/api/stats")
async def api_stats():
    return JSONResponse(trading_state.get_stats())

@app.post("/api/reset")
async def api_reset():
    try:
        trading_state.reset_all()
        return JSONResponse({"ok": True, "message": "Dashboard réinitialisé"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/tv-webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        if not body:
            return JSONResponse({"status": "ok", "message": "Ping"}, status_code=200)
        
        body_text = body.decode('utf-8', errors='ignore')
        logger.info(f"📥 Webhook: {body_text[:200]}")
        
        clean_body = ' '.join(body_text.split())
        payload = json.loads(clean_body)
        logger.info(f"✅ JSON OK")
        
        action = (payload.get("type") or payload.get("action") or "").lower()
        symbol = payload.get("symbol")
        side = payload.get("side", "LONG")
        
        if not symbol:
            return JSONResponse({"status": "error", "message": "Symbol requis"}, status_code=400)
        
        logger.info(f"✅ Action: {action} | {symbol} | {side}")
        
        if action == "entry":
            entry = payload.get("entry")
            tp1 = payload.get("tp1") or payload.get("tp")
            tp2 = payload.get("tp2")
            tp3 = payload.get("tp3")
            sl = payload.get("sl")
            
            if not all([entry, tp1, sl]):
                return JSONResponse({"status": "error", "message": "entry, tp1, sl requis"}, status_code=400)
            
            if not tp2:
                tp2 = float(tp1) * 1.01 if side == 'LONG' else float(tp1) * 0.99
            if not tp3:
                tp3 = float(tp1) * 1.02 if side == 'LONG' else float(tp1) * 0.98
            
            new_trade = {
                'symbol': symbol,
                'tf_label': payload.get("tf_label") or "15m",
                'side': side,
                'entry': float(entry),
                'tp1': float(tp1),
                'tp2': float(tp2),
                'tp3': float(tp3),
                'sl': float(sl),
                'row_state': 'normal'
            }
            
            trading_state.add_trade(new_trade)
            await notify_new_trade(new_trade)
            logger.info(f"✅ TRADE #{new_trade.get('id')}")
            return JSONResponse({"status": "ok", "trade_id": new_trade.get('id')})
        
        elif ("tp" in action) and ("hit" in action):
            tp_level = 'tp1'
            if 'tp3' in action or '3' in action:
                tp_level = 'tp3'
            elif 'tp2' in action or '2' in action:
                tp_level = 'tp2'
            
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(payload.get('price') or trade.get(tp_level))
                    if trading_state.close_trade(trade['id'], tp_level, exit_price):
                        await notify_tp_hit(trade, tp_level)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})
        
        elif ("sl" in action) and ("hit" in action):
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(payload.get('price') or trade.get('sl'))
                    if trading_state.close_trade(trade['id'], 'sl', exit_price):
                        await notify_sl_hit(trade)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})
        
        elif action == "close":
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(payload.get('price', trade.get('entry')))
                    if trading_state.close_trade(trade['id'], 'close', exit_price):
                        await notify_close(trade, payload.get('reason', 'Manuel'))
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})
        
        return JSONResponse({"status": "error", "message": f"Action non supportée: {action}"}, status_code=400)
        
    except Exception as e:
        logger.error(f"❌ Webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>Trading Dashboard</h1><p>v2.6.2 <span class="live-badge">LIVE</span></p></div>""" + NAV + """
<div class="card" style="text-align:center;">
<h2>Dashboard Actif</h2>
<p style="color:#94a3b8;margin:20px 0;">Webhook ouvert • TP1/TP2/TP3 • Fear&Greed • Bull Run</p>
<a href="/trades" style="padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;display:inline-block;margin-top:20px;">Dashboard →</a>
</div></div></body></html>""")

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    stats = trading_state.get_stats()
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
{CSS}
</head>
<body>

<button class="reset-btn" onclick="showResetModal()">RESET</button>

<div id="resetModal" class="modal">
    <div class="modal-content">
        <h2 style="color:#ef4444;margin-bottom:20px;">Confirmation RESET</h2>
        <p style="color:#e2e8f0;margin-bottom:20px;">
            TOUT supprimer ?<br><br>
            <strong>Supprime tous les trades</strong>
        </p>
        <div class="modal-buttons">
            <button class="btn-confirm" onclick="confirmReset()">OUI</button>
            <button class="btn-cancel" onclick="closeResetModal()">ANNULER</button>
        </div>
    </div>
</div>

<div class="container">
<div class="header">
<h1>Trading Dashboard</h1>
<p>TP1/TP2/TP3 <span class="live-badge">LIVE</span></p>
</div>
{NAV}

<div class="grid grid-4">
<div class="metric">
<div class="metric-label">Total Trades</div>
<div class="metric-value">{stats['total_trades']}</div>
</div>
<div class="metric">
<div class="metric-label">Win Rate</div>
<div class="metric-value">{stats['win_rate']:.1f}%</div>
</div>
<div class="metric">
<div class="metric-label">Equity</div>
<div class="metric-value">${stats['current_equity']:,.0f}</div>
</div>
<div class="metric">
<div class="metric-label">Return</div>
<div class="metric-value" style="color:{'#10b981' if stats['total_return'] > 0 else '#ef4444'}">{stats['total_return']:+.1f}%</div>
</div>
</div>

<div class="card">
<h2>Trades</h2>
<table id="tradesTable">
<thead>
<tr>
<th>ID</th>
<th>Symbol</th>
<th>Side</th>
<th>Entry</th>
<th>TP1 / TP2 / TP3</th>
<th>SL</th>
<th>Status</th>
</tr>
</thead>
<tbody></tbody>
</table>
</div>

<div class="card">
<h2>Fear & Greed</h2>
<div id="fearGreedContainer">Chargement...</div>
</div>

<div class="card">
<h2>Bull Run Phase</h2>
<div id="bullrunContainer">Chargement...</div>
</div>

</div>

<script>
function showResetModal() {{
    document.getElementById('resetModal').style.display = 'block';
}}

function closeResetModal() {{
    document.getElementById('resetModal').style.display = 'none';
}}

async function confirmReset() {{
    try {{
        const response = await fetch('/api/reset', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}}
        }});
        
        const data = await response.json();
        
        if (data.ok) {{
            alert('Dashboard réinitialisé !');
            closeResetModal();
            window.location.reload();
        }} else {{
            alert('Erreur: ' + data.error);
        }}
    }} catch (error) {{
        alert('Erreur: ' + error);
    }}
}}

window.onclick = function(event) {{
    const modal = document.getElementById('resetModal');
    if (event.target == modal) closeResetModal();
}}

async function loadDashboard() {{
    try {{
        const res = await fetch('/api/trades');
        const data = await res.json();
        
        if (!data.ok) return;
        
        const tbody = document.querySelector('#tradesTable tbody');
        tbody.innerHTML = '';
        
        const trades = data.trades.slice().reverse();
        trades.forEach(trade => {{
            const row = document.createElement('tr');
            
            let statusBadge = '';
            if (trade.row_state === 'normal') {{
                statusBadge = '<span class="badge badge-yellow">ACTIF</span>';
            }} else if (trade.row_state === 'tp1') {{
                statusBadge = '<span class="badge badge-green">TP1</span>';
            }} else if (trade.row_state === 'tp2') {{
                statusBadge = '<span class="badge badge-green">TP2</span>';
            }} else if (trade.row_state === 'tp3') {{
                statusBadge = '<span class="badge badge-green">TP3</span>';
            }} else if (trade.row_state === 'closed') {{
                statusBadge = '<span class="badge badge-yellow">FERME</span>';
            }} else {{
                statusBadge = '<span class="badge badge-red">SL</span>';
            }}
            
            const tp1Class = trade.tp1_hit ? 'tp-hit' : 'tp-pending';
            const tp2Class = trade.tp2_hit ? 'tp-hit' : 'tp-pending';
            const tp3Class = trade.tp3_hit ? 'tp-hit' : 'tp-pending';
            
            const formatPrice = (p) => {{
                if (p >= 1) return p.toFixed(2);
                if (p >= 0.01) return p.toFixed(4);
                return p.toFixed(6);
            }};
            
            row.innerHTML = `
                <td>#${{trade.id}}</td>
                <td><strong>${{trade.symbol}}</strong></td>
                <td>${{trade.side}}</td>
                <td>${{formatPrice(trade.entry)}}</td>
                <td>
                    <div class="tp-cell">
                        <div class="${{tp1Class}} tp-item">${{trade.tp1_hit ? 'OK' : '--'}} TP1: ${{formatPrice(trade.tp1)}}</div>
                        <div class="${{tp2Class}} tp-item">${{trade.tp2_hit ? 'OK' : '--'}} TP2: ${{formatPrice(trade.tp2)}}</div>
                        <div class="${{tp3Class}} tp-item">${{trade.tp3_hit ? 'OK' : '--'}} TP3: ${{formatPrice(trade.tp3)}}</div>
                    </div>
                </td>
                <td>${{formatPrice(trade.sl)}}</td>
                <td>${{statusBadge}}</td>
            `;
            tbody.appendChild(row);
        }});
        
        const fgRes = await fetch('/api/fear-greed');
        const fgData = await fgRes.json();
        
        if (fgData.ok) {{
            const fg = fgData.fear_greed;
            document.getElementById('fearGreedContainer').innerHTML = `
                <div class="gauge"><div class="gauge-inner">
                    <div class="gauge-value">${{fg.value}}</div>
                    <div class="gauge-label">${{fg.sentiment}}</div>
                </div></div>
                <p style="text-align:center;margin-top:15px;">${{fg.emoji}} ${{fg.recommendation}}</p>
            `;
        }}
        
        const brRes = await fetch('/api/bullrun-phase');
        const brData = await brRes.json();
        
        if (brData.ok) {{
            const phase = brData.bullrun_phase;
            document.getElementById('bullrunContainer').innerHTML = `
                <div style="text-align:center;padding:20px;">
                    <div style="font-size:48px;">${{phase.emoji}}</div>
                    <h3 style="color:${{phase.color}};margin:15px 0;">${{phase.phase_name}}</h3>
                    <p style="color:#94a3b8;margin-bottom:15px;">${{phase.description}}</p>
                    <div style="display:flex;gap:20px;justify-content:center;">
                        <div><strong>BTC.D:</strong> ${{phase.btc_dominance}}%</div>
                        <div><strong>F&G:</strong> ${{phase.fg}}</div>
                        <div><strong>Confiance:</strong> ${{phase.confidence}}%</div>
                    </div>
                </div>
            `;
        }}
        
    }} catch(e) {{
        console.error('Erreur:', e);
    }}
}}

loadDashboard();
setInterval(loadDashboard, 30000);
</script>
</body></html>"""
    
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD v2.6.2 COMPLET")
    print("="*70)
    print("✅ Webhook OUVERT")
    print("✅ Fear & Greed")
    print("✅ Bull Run Phase")
    print("✅ TP1/TP2/TP3")
    print("✅ RESET")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
