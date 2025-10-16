# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION 2.6.0
✅ Toutes les routes HTML (trades, equity-curve, journal, heatmap, strategie, backtest, patterns, annonces, metrics)
✅ TP1/TP2/TP3 affichage corrigé + Heure d’entrée
✅ Support action CLOSE
✅ Webhook robuste (JSON + text/plain + regex Telegram/TV)
✅ Anti-flood Telegram (throttle simple)
✅ Bouton Reset (API + UI)
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

app = FastAPI(title="Trading Dashboard", version="2.6.0")

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
    FRENCH_SOURCES = ['journalducoin.com', 'fr.cointelegraph.com', 'cryptoast.fr']
    NEWS_CACHE_TTL = 60
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
        # throttle telegram
        self.last_telegram_send: Optional[datetime] = None
    
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

def _is_major_symbol(symbol: str) -> bool:
    base = symbol.replace('.P','').upper()
    return any(base.startswith(x) for x in ('BTC','ETH','BNB','SOL'))

async def calculate_trade_confidence(symbol: str, side: str, entry: float) -> Dict[str, Any]:
    """Calcule le niveau de confiance d'un trade avec explications détaillées (moins “figé”)."""
    fg = market_cache.fear_greed_data or await fetch_real_fear_greed()
    global_data = market_cache.global_data or await fetch_global_crypto_data()
    prices = market_cache.crypto_prices or await fetch_crypto_prices()
    
    confidence_score = 50
    reasons = []

    # 1) Fear & Greed (pro-contrarian)
    fg_value = fg.get('value', 50)
    if side == 'LONG':
        if fg_value < 25:
            confidence_score += 22; reasons.append("✅ Extrême peur: opportunité contrarienne")
        elif fg_value < 45:
            confidence_score += 12; reasons.append("✅ Sentiment craintif: bon point d'entrée")
        elif fg_value > 75:
            confidence_score -= 8; reasons.append("⚠️ Euphorie élevée: risque de pullback")
    else:  # SHORT
        if fg_value > 75:
            confidence_score += 22; reasons.append("✅ Euphorie extrême: zone propice au short")
        elif fg_value > 60:
            confidence_score += 10; reasons.append("✅ Sentiment chaud: timing short possible")
        elif fg_value < 25:
            confidence_score -= 6; reasons.append("⚠️ Marché apeuré: short risqué")

    # 2) BTC Dominance: pénalité plus douce pour altcoins; neutre si symbole inconnu
    btc_dom = global_data.get('btc_dominance', 50.0)
    is_major = _is_major_symbol(symbol or '')
    if symbol:
        if is_major:
            if btc_dom > 55:
                confidence_score += 10; reasons.append("✅ BTC dominant: momentum favorable aux majors")
            elif btc_dom > 50:
                confidence_score += 5; reasons.append("✅ Environnement plutôt pro-BTC")
        else:
            if btc_dom < 47:
                confidence_score += 10; reasons.append("✅ Dominance faible: contexte altcoins")
            elif btc_dom < 50:
                confidence_score += 5; reasons.append("✅ Rotation possible vers altcoins")
            elif btc_dom >= 55:
                confidence_score -= 3; reasons.append("⚠️ BTC dominant: prudence sur les altcoins")
    # sinon: pas de malus

    # 3) Price Action 24h (si connu)
    symbol_map = {
        'BTCUSDT': 'bitcoin',
        'ETHUSDT': 'ethereum',
        'BNBUSDT': 'binancecoin',
        'SOLUSDT': 'solana'
    }
    base_sym = (symbol or '').replace('.P','').upper()
    crypto_key = symbol_map.get(base_sym)
    if crypto_key and crypto_key in prices:
        change_24h = prices[crypto_key].get('change_24h', 0) or 0
        if side == 'LONG':
            if change_24h > 5: confidence_score += 8; reasons.append(f"✅ Momentum haussier (+{change_24h:.1f}%/24h)")
            elif change_24h > 2: confidence_score += 4; reasons.append(f"✅ Micro-trend positive (+{change_24h:.1f}%/24h)")
        else:
            if change_24h < -5: confidence_score += 8; reasons.append(f"✅ Momentum baissier ({change_24h:.1f}%/24h)")
            elif change_24h < -2: confidence_score += 4; reasons.append(f"✅ Micro-trend négative ({change_24h:.1f}%/24h)")
    else:
        reasons.append("ℹ️ Ticker non-majeur: score sans momentum 24h")

    # Clamp
    confidence_score = max(0, min(100, confidence_score))
    if confidence_score >= 80:
        emoji = "🟢"; level = "TRÈS ÉLEVÉ"
    elif confidence_score >= 65:
        emoji = "🟡"; level = "ÉLEVÉ"
    elif confidence_score >= 50:
        emoji = "🟠"; level = "MOYEN"
    else:
        emoji = "🔴"; level = "FAIBLE"
    
    return {
        "score": round(confidence_score),
        "level": level,
        "emoji": emoji,
        "reasons": reasons[:4],
        "fg_value": fg_value,
        "btc_dominance": btc_dom
    }

class TradingState:
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve: List[Dict[str, Any]] = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.journal_entries: List[Dict[str, Any]] = []
    
    def reset(self):
        self.trades = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.journal_entries = []
        logger.info("♻️ TradingState reset")
    
    def clean_old_trades(self):
        now = datetime.now()
        for trade in self.trades:
            if trade.get('row_state') == 'normal':
                age = (now - trade.get('timestamp', now)).total_seconds() / 3600
                if age > 4:
                    tp_hit = random.choice(['tp1', 'tp2', 'tp3'])
                    exit_price = trade.get(tp_hit)
                    self.close_trade(trade['id'], tp_hit, exit_price)
                    logger.info(f"🔄 Trade #{trade['id']} fermé auto ({tp_hit.upper()})")
    
    def add_trade(self, trade: Dict[str, Any]):
        trade['id'] = len(self.trades) + 1
        trade['timestamp'] = trade.get('timestamp') or datetime.now()
        trade['tp1_hit'] = trade.get('tp1_hit', False)
        trade['tp2_hit'] = trade.get('tp2_hit', False)
        trade['tp3_hit'] = trade.get('tp3_hit', False)
        trade['pnl_percent'] = trade.get('pnl_percent', 0.0)
        if 'row_state' not in trade:
            trade['row_state'] = 'normal'
        self.trades.append(trade)
        logger.info(f"✅ Trade #{trade['id']}: {trade.get('symbol')} {trade.get('side')} @ {trade.get('entry')}")
    
    def close_trade(self, trade_id: int, tp_level: str, exit_price: float):
        """Ferme un trade sur un TP spécifique ou SL"""
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
        ("BTCUSDT", prices.get('bitcoin', {}).get('price', 65000) * 0.98, 'LONG', 'tp2'),
        ("ETHUSDT", prices.get('ethereum', {}).get('price', 3500) * 1.02, 'SHORT', 'tp3'),
        ("BNBUSDT", prices.get('binancecoin', {}).get('price', 600) * 1.01, 'LONG', 'sl'),
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
        
        if state != 'normal':
            if state == 'tp1':
                exit_price = trade['tp1']; trade['tp1_hit'] = True
            elif state == 'tp2':
                exit_price = trade['tp2']; trade['tp1_hit'] = True; trade['tp2_hit'] = True
            elif state == 'tp3':
                exit_price = trade['tp3']; trade['tp1_hit'] = True; trade['tp2_hit'] = True; trade['tp3_hit'] = True
            else:
                exit_price = trade['sl']
            trade['exit_price'] = exit_price
            trade['close_timestamp'] = datetime.now() - timedelta(hours=random.randint(1, 12))
            entry = trade['entry']
            pnl = ((exit_price - entry) / entry * 100) if side == 'LONG' else ((entry - exit_price) / entry * 100)
            trade['pnl_percent'] = pnl
        
        trading_state.add_trade(trade)
    
    logger.info("✅ Démo initialisée avec 6 trades")

asyncio.get_event_loop().create_task(init_demo())

async def auto_generate_trades():
    while True:
        try:
            await asyncio.sleep(3600)
            trading_state.clean_old_trades()
            
            active = sum(1 for t in trading_state.trades if t.get('row_state') == 'normal')
            if active < 3:
                prices = await fetch_crypto_prices()
                if not prices:
                    continue
                cryptos = [
                    ("BTCUSDT", prices.get('bitcoin', {}).get('price', 65000)),
                    ("ETHUSDT", prices.get('ethereum', {}).get('price', 3500)),
                    ("BNBUSDT", prices.get('binancecoin', {}).get('price', 600)),
                    ("SOLUSDT", prices.get('solana', {}).get('price', 140)),
                ]
                symbol, price = random.choice(cryptos)
                side = random.choice(['LONG', 'SHORT'])
                
                if side == 'LONG':
                    tp1 = price * 1.015; tp2 = price * 1.025; tp3 = price * 1.04; sl = price * 0.98
                else:
                    tp1 = price * 0.985; tp2 = price * 0.975; tp3 = price * 0.96; sl = price * 1.02
                
                new_trade = {
                    'symbol': symbol, 'tf_label': '15m', 'side': side, 'entry': price,
                    'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'sl': sl, 'row_state': 'normal'
                }
                trading_state.add_trade(new_trade)
                logger.info(f"🤖 Nouveau trade: {symbol}")
        except Exception as e:
            logger.error(f"❌ auto_generate_trades: {e}")

asyncio.get_event_loop().create_task(auto_generate_trades())

async def send_telegram_message(message: str) -> bool:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configuré")
        return False
    
    # Throttle simple: au moins 1.2s entre deux envois
    now = datetime.now()
    if market_cache.last_telegram_send:
        delta = (now - market_cache.last_telegram_send).total_seconds()
        if delta < 1.2:
            logger.error("❌ Telegram: throttle (skip envoi)")
            return False
    
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    logger.info("✅ Telegram envoyé")
                    market_cache.last_telegram_send = now
                    return True
                else:
                    txt = await response.text()
                    logger.error(f"❌ Telegram: {response.status} - {txt[:500]}")
                    return False
    except Exception as e:
        logger.error(f"❌ Telegram: {str(e)}")
        return False

async def notify_new_trade(trade: Dict[str, Any]) -> bool:
    confidence = await calculate_trade_confidence(
        trade.get('symbol'), 
        trade.get('side'), 
        trade.get('entry')
    )
    reasons_text = "\n".join([f"  • {r}" for r in confidence['reasons'][:4]])
    
    entry = trade.get('entry'); side = trade.get('side')
    if side == 'LONG':
        tp1_pct = ((trade.get('tp1') / entry - 1) * 100)
        tp2_pct = ((trade.get('tp2') / entry - 1) * 100)
        tp3_pct = ((trade.get('tp3') / entry - 1) * 100)
    else:
        tp1_pct = ((1 - trade.get('tp1') / entry) * 100)
        tp2_pct = ((1 - trade.get('tp2') / entry) * 100)
        tp3_pct = ((1 - trade.get('tp3') / entry) * 100)
    
    ts = trade.get('timestamp') or datetime.now()
    ts_str = (ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))).strftime('%Y-%m-%d %H:%M:%S')
    
    message = f"""🎯 <b>NOUVEAU TRADE</b> {confidence['emoji']}

📊 <b>{trade.get('symbol')}</b>
📈 Direction: <b>{trade.get('side')}</b> | {trade.get('tf_label')}

⏱️ Entry time: <b>{ts_str}</b>
💰 Entry: <b>${trade.get('entry'):.6f}</b>

🎯 <b>Take Profits:</b>
  TP1: ${trade.get('tp1'):.6f} (+{tp1_pct:.1f}%)
  TP2: ${trade.get('tp2'):.6f} (+{tp2_pct:.1f}%)
  TP3: ${trade.get('tp3'):.6f} (+{tp3_pct:.1f}%)

🛑 Stop Loss: <b>${trade.get('sl'):.6f}</b>

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
💰 Entry: ${trade.get('entry'):.6f}
🎯 Exit: ${tp_price:.6f}
💵 P&amp;L: <b>{pnl:+.2f}%</b>

{'🟢 TP1 ✅' if trade.get('tp1_hit') else '⚪ TP1'}
{'🟢 TP2 ✅' if trade.get('tp2_hit') else '⚪ TP2'}
{'🟢 TP3 ✅' if trade.get('tp3_hit') else '⚪ TP3'}"""
    return await send_telegram_message(message)

async def notify_sl_hit(trade: Dict[str, Any]) -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""🛑 <b>STOP LOSS</b> ⚠️

📊 {trade.get('symbol')}
💰 Entry: ${trade.get('entry'):.6f}
🛑 Exit: ${trade.get('exit_price'):.6f}
💵 P&L: <b>{pnl:+.2f}%</b>"""
    return await send_telegram_message(message)

async def notify_close(trade: Dict[str, Any], reason: str = "Manuel") -> bool:
    pnl = trade.get('pnl_percent', 0)
    message = f"""⏹️ <b>TRADE FERMÉ</b>

📊 {trade.get('symbol')}
💰 Entry: ${trade.get('entry'):.6f}
⏹️ Exit: ${trade.get('exit_price'):.6f}
💵 P&L: <b>{pnl:+.2f}%</b>
📝 Raison: {reason}"""
    return await send_telegram_message(message)

KEYWORDS_BY_CATEGORY = {
    "regulation": {"keywords": [r"\bETF\b", r"\bSEC\b", r"\brégulation\b"], "boost": 2},
    "security": {"keywords": [r"\bhack\b", r"\bpiratage\b"], "boost": 3},
    "markets": {"keywords": [r"\bATH\b", r"\bcrash\b"], "boost": 1},
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
    return {"score": min(int(score), 5), "categories": categories, "sentiment": "neutre"}

async def fetch_rss_improved(session: aiohttp.ClientSession, url: str, max_age_hours: int = 48) -> list[dict]:
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml, application/xml, text/xml'}
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
                    items.append({
                        "title": title,
                        "link": link,
                        "source": source,
                        "published": pub_date,
                        "published_dt": item_time,
                        "summary": clean_desc,
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
    logger.info(f"🗞️ News françaises: {len(items)} items")
    return items

async def fetch_binance_klines(symbol: str, interval: str = "1h", limit: int = 1000):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        async with aiohttp.ClientSession() as session:
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
                    return klines
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
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "result": result,
                    "pnl_percent": round(pnl_percent, 2),
                    "equity": round(equity, 2)
                })
                in_position = False
    if not trades:
        return None
    wins = [t for t in trades if t["result"] == "TP"]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    total_return = (equity - initial_capital) / initial_capital * 100
    return {
        "trades": trades,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(win_rate, 1),
        "final_equity": round(equity, 2),
        "total_return": round(total_return, 2),
        "equity_curve": [round(e, 2) for e in equity_curve]
    }

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
            wins = sum(1 for t in recent if t.get('row_state') in ('tp1', 'tp2', 'tp3'))
            if wins == 3:
                patterns.append(f"🔥 {symbol}: 3 wins consécutifs!")
    if not patterns:
        active = sum(1 for r in rows if r.get('row_state') == 'normal')
        patterns.append(f"📊 {len(rows)} trades | {active} actifs")
    return patterns[:5]

# ==================== CSS & NAV ====================

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
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
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
textarea { width: 100%; padding: 12px; background: rgba(99, 102, 241, 0.05); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 8px; color: #e2e8f0; font-family: inherit; resize: vertical; min-height: 100px; }
button { padding: 12px 24px; background: #6366f1; color: white; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.3s; }
button:hover { background: #5558e3; transform: translateY(-2px); }
.filter-chip { display: inline-block; padding: 6px 12px; margin: 4px; background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3); border-radius: 16px; cursor: pointer; transition: all 0.3s; font-size: 12px; }
.filter-chip:hover { background: rgba(99,102,241,0.2); transform: translateY(-2px); }
.filter-chip.active { background: #6366f1; color: white; border-color: #6366f1; }
.heatmap-cell { padding: 12px; text-align: center; border-radius: 8px; background: rgba(99, 102, 241, 0.1); border: 1px solid rgba(99, 102, 241, 0.2); }
.heatmap-cell.high { background: rgba(16, 185, 129, 0.2); border-color: #10b981; }
.heatmap-cell.medium { background: rgba(245, 158, 11, 0.2); border-color: #f59e0b; }
.heatmap-cell.low { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }
.news-item { background: rgba(99, 102, 241, 0.05); padding: 16px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #6366f1; }
.news-title { font-size: 16px; font-weight: 600; margin-bottom: 8px; color: #e2e8f0; }
.news-meta { font-size: 12px; color: #64748b; margin-bottom: 8px; }
.news-summary { font-size: 14px; color: #94a3b8; line-height: 1.5; }
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

# ==================== API ENDPOINTS ====================

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
            "debug": {
                "btc_dominance": gd.get('btc_dominance', 0),
                "fear_greed": fg.get('value', 0),
            }
        }
    }

@app.get("/api/telegram-test")
async def telegram_test():
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "Configuration manquante"}
    test_message = f"""🧪 <b>TEST TELEGRAM</b>

✅ Connexion réussie !
🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
    success = await send_telegram_message(test_message)
    return {"ok": success, "message": "Message envoyé" if success else "Échec"}

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
                winrate, trades = random.randint(60, 75), random.randint(10, 30)
            elif 8 <= h <= 12 or 13 <= h <= 17:
                winrate, trades = random.randint(50, 65), random.randint(5, 15)
            else:
                winrate, trades = random.randint(40, 55), random.randint(0, 8)
            heatmap[key] = {"winrate": winrate, "trades": trades}
    return {"ok": True, "heatmap": heatmap}

@app.get("/api/news")
async def api_news(q: Optional[str] = None, min_importance: int = 1, limit: int = 50, offset: int = 0):
    items = await fetch_all_news_improved()
    if q:
        ql = q.lower().strip()
        items = [i for i in items if ql in (i["title"] + " " + i["summary"]).lower()]
    items = [i for i in items if i.get("importance", 1) >= min_importance]
    total = len(items)
    page = items[offset: offset + limit]
    return {"ok": True, "total": total, "count": len(page), "items": page}

@app.get("/api/backtest")
async def api_backtest(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 500, tp_percent: float = 3.0, sl_percent: float = 2.0):
    klines = await fetch_binance_klines(symbol, interval, limit)
    if not klines:
        return {"ok": False, "error": "Impossible de récupérer les données"}
    results = run_backtest_strategy(klines, tp_percent, sl_percent, settings.INITIAL_CAPITAL)
    if not results:
        return {"ok": False, "error": "Aucun trade généré"}
    return {"ok": True, "backtest": {"symbol": symbol, "stats": results}}

@app.post("/api/reset")
async def api_reset():
    trading_state.reset()
    return {"ok": True}

# ====== Webhook parsing helpers ======

def _try_parse_json_loose(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except:
        # parfois Telegram envoie un JSON dans une string (double encodage)
        m = re.search(r'(\{.*\})', text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except:
                return None
    return None

def _extract_from_plain(text: str) -> Dict[str, Any]:
    """
    Extrait {type, side, symbol, tf, entry, tp1, sl} d'un message text/plain style Telegram/TradingView.
    Ex: "⚡ <b>SELL</b> — <b>ARPAUSDT.P</b> (15) Prix: <code>0.01953</code>\nHeure: 2025-10-16 00:30"
    """
    payload: Dict[str, Any] = {}
    t = text.replace("\n", " ").replace("\r"," ")
    # SIDE (BUY/SELL)
    m = re.search(r'\b(BUY|SELL)\b', t, flags=re.I)
    if m: payload['side'] = m.group(1).upper()
    # SYMBOL entre balises <b>SYMBOL</b>
    m = re.search(r'<b>\s*([A-Z0-9\-]+(?:USDT|USD|USDC)?(?:\.P)?)\s*</b>', t, flags=re.I)
    if m: payload['symbol'] = m.group(1).upper()
    # TF entre parenthèses (15)
    m = re.search(r'\((\d+)\)', t)
    if m: payload['tf'] = m.group(1)
    # PRIX dans <code>...</code> ou après "Prix:"/"Price:"
    m = re.search(r'<code>\s*([0-9]*\.?[0-9]+(?:e-?\d+)?)\s*</code>', t, flags=re.I)
    if not m:
        m = re.search(r'(?:Prix|Price)\s*:\s*([0-9]*\.?[0-9]+)', t, flags=re.I)
    if m: payload['entry'] = float(m.group(1))
    # TP/SL (si présents)
    m = re.search(r'TP1?\s*[:=]\s*([0-9]*\.?[0-9]+)', t, flags=re.I)
    if m: payload['tp1'] = float(m.group(1))
    m = re.search(r'SL\s*[:=]\s*([0-9]*\.?[0-9]+)', t, flags=re.I)
    if m: payload['sl'] = float(m.group(1))
    # TYPE par défaut "entry" si côté/entry détectés
    if 'side' in payload and 'entry' in payload:
        payload['type'] = 'entry'
    return payload

@app.post("/tv-webhook")
async def webhook(request: Request):
    try:
        # content-type logging
        content_type = request.headers.get("content-type", "")
        logger.info(f"📥 Webhook content-type: {content_type}")
        body = await request.body()
        if not body:
            logger.warning("⚠️ Webhook: Body vide (peut-être un ping)")
            return JSONResponse({"status": "ok", "message": "Ping reçu"}, status_code=200)

        payload: Dict[str, Any] = {}
        # JSON standard
        if "application/json" in content_type.lower():
            try:
                payload = await request.json()
            except Exception:
                # fallback texte puis parse
                txt = body.decode('utf-8', errors='ignore')
                payload = _try_parse_json_loose(txt) or _extract_from_plain(txt)
        else:
            # text/plain ou autre: tenter JSON puis regex
            txt = body.decode('utf-8', errors='ignore')
            loose = _try_parse_json_loose(txt)
            payload = loose or _extract_from_plain(txt)

        if not isinstance(payload, dict) or not payload:
            logger.warning("⚠️ Webhook: JSON invalide")
            return JSONResponse({"status": "error", "message": "JSON invalide"}, status_code=400)

        logger.info(f"📥 Webhook payload (keys): {list(payload.keys())}")

        action = (payload.get("type") or payload.get("action") or "").lower()
        symbol = (payload.get("symbol") or "").upper()
        side = (payload.get("side") or "LONG").upper()

        if not symbol:
            logger.warning("⚠️ Webhook: Symbol manquant")
            return JSONResponse({"status": "error", "message": "Symbol manquant"}, status_code=400)

        # ACTION: ENTRY
        if action == "entry":
            entry = payload.get("entry")
            tp1 = payload.get("tp1") or payload.get("tp")
            tp2 = payload.get("tp2")
            tp3 = payload.get("tp3")
            sl = payload.get("sl")

            if not all([entry, tp1, sl]):
                logger.warning(f"⚠️ Entry incomplet: entry={entry}, tp1={tp1}, sl={sl}")
                return JSONResponse({"status": "error", "message": "entry, tp1, sl requis"}, status_code=400)

            if not tp2:
                tp2 = float(tp1) * 1.01 if side == 'LONG' else float(tp1) * 0.99
            if not tp3:
                tp3 = float(tp1) * 1.02 if side == 'LONG' else float(tp1) * 0.98

            tf = str(payload.get("tf") or payload.get("tf_label") or "15")
            if not tf.endswith("m") and not tf.endswith("h") and not tf.endswith("d"):
                tf_label = f"{tf}m"
            else:
                tf_label = tf

            new_trade = {
                'symbol': symbol,
                'tf_label': tf_label,
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
            return JSONResponse({"status": "ok", "trade_id": new_trade.get('id')})

        # ACTION: TP HIT
        elif ("tp" in action or "take_profit" in action) and ("hit" in action or "_hit" in action.replace("take_profit", "")):
            tp_level = 'tp1'
            if 'tp3' in action or '3' in action:
                tp_level = 'tp3'
            elif 'tp2' in action or '2' in action:
                tp_level = 'tp2'
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(payload.get('price') or payload.get(tp_level) or trade.get(tp_level))
                    if trading_state.close_trade(trade['id'], tp_level, exit_price):
                        await notify_tp_hit(trade, tp_level)
                        return JSONResponse({"status": "ok", "trade_id": trade['id'], "tp_level": tp_level})
            logger.warning(f"⚠️ TP hit: Trade {symbol} non trouvé")
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})

        # ACTION: SL HIT
        elif ("sl" in action or "stop_loss" in action) and ("hit" in action or "_hit" in action.replace("stop_loss", "")):
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(payload.get('price') or payload.get('sl') or trade.get('sl'))
                    if trading_state.close_trade(trade['id'], 'sl', exit_price):
                        await notify_sl_hit(trade)
                        return JSONResponse({"status": "ok", "trade_id": trade['id']})
            logger.warning(f"⚠️ SL hit: Trade {symbol} non trouvé")
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})

        # ACTION: CLOSE (manuel)
        elif action == "close":
            reason = payload.get('reason', 'Manuel')
            price = payload.get('price')
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and trade.get('row_state') == 'normal' and trade.get('side') == side):
                    exit_price = float(price) if price else trade.get('entry')
                    if trading_state.close_trade(trade['id'], 'close', exit_price):
                        await notify_close(trade, reason)
                        return JSONResponse({"status": "ok", "trade_id": trade['id'], "reason": reason})
            logger.warning(f"⚠️ Close: Trade {symbol} non trouvé")
            return JSONResponse({"status": "warning", "message": "Trade non trouvé"})

        logger.warning(f"⚠️ Action inconnue: '{action}'")
        return JSONResponse({"status": "error", "message": f"Action non supportée: {action}"}, status_code=400)
        
    except Exception as e:
        logger.error(f"❌ Webhook erreur: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# ==================== HTML ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>""" + CSS + """</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard v2.6.0</h1><p>TP1/TP2/TP3 • Confiance • CLOSE <span class="live-badge">LIVE</span></p></div>""" + NAV + """
<div class="card" style="text-align:center;">
<h2>Dashboard Professionnel de Trading</h2>
<p style="color:#94a3b8;margin:20px 0;">✅ TP différenciés • ✅ Action CLOSE • ✅ Toutes routes OK</p>
<div style="display:flex;gap:12px;justify-content:center;margin-top:20px">
<a href="/trades" style="padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">📊 Dashboard</a>
<a href="/annonces" style="padding:12px 24px;background:#10b981;color:white;text-decoration:none;border-radius:8px;">🗞️ Annonces FR</a>
</div>
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
<div class="container">
<div class="header">
<h1>📊 Trading Dashboard</h1>
<p>TP1, TP2, TP3 individuels <span class="live-badge">LIVE</span></p>
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

<div class="card" style="display:flex;gap:8px;align-items:center;justify-content:space-between">
  <h2>📈 Trades avec TP1, TP2, TP3</h2>
  <div style="display:flex;gap:8px;align-items:center">
    <button id="resetBtn" title="Réinitialiser les données">♻️ Reset</button>
  </div>
</div>

<div class="card">
<div style="overflow-x:auto;">
<table id="tradesTable">
<thead>
<tr>
<th>ID</th>
<th>Heure entrée</th>
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
</div>

<div class="card">
<h2>😱 Fear & Greed Index</h2>
<div id="fearGreedContainer" style="text-align:center;">Chargement...</div>
</div>

<div class="card">
<h2>🚀 Bull Run Phase</h2>
<div id="bullrunContainer">Chargement...</div>
</div>

</div>

<script>
async function loadDashboard() {{
    try {{
        const tradesRes = await fetch('/api/trades');
        const tradesData = await tradesRes.json();
        if (!tradesData.ok) return;
        const tbody = document.querySelector('#tradesTable tbody');
        tbody.innerHTML = '';
        const trades = tradesData.trades.slice().reverse();
        const formatPrice = (p) => {{
            if (p >= 1) return p.toFixed(4);
            if (p >= 0.01) return p.toFixed(6);
            return p.toFixed(8);
        }};
        trades.forEach(trade => {{
            const row = document.createElement('tr');
            let statusBadge = '';
            if (trade.row_state === 'normal') {{
                statusBadge = '<span class="badge badge-yellow">ACTIF</span>';
            }} else if (trade.row_state === 'tp1') {{
                statusBadge = '<span class="badge badge-green">TP1 ✅</span>';
            }} else if (trade.row_state === 'tp2') {{
                statusBadge = '<span class="badge badge-green">TP2 ✅</span>';
            }} else if (trade.row_state === 'tp3') {{
                statusBadge = '<span class="badge badge-green">TP3 ✅</span>';
            }} else if (trade.row_state === 'closed') {{
                statusBadge = '<span class="badge badge-yellow">FERMÉ</span>';
            }} else {{
                statusBadge = '<span class="badge badge-red">SL ❌</span>';
            }}
            const tp1Class = trade.tp1_hit ? 'tp-hit' : 'tp-pending';
            const tp2Class = trade.tp2_hit ? 'tp-hit' : 'tp-pending';
            const tp3Class = trade.tp3_hit ? 'tp-hit' : 'tp-pending';
            const entryTime = trade.timestamp ? new Date(trade.timestamp).toLocaleString() : '-';
            row.innerHTML = `
                <td>#${{trade.id}}</td>
                <td>${{entryTime}}</td>
                <td><strong>${{trade.symbol}}</strong></td>
                <td>${{trade.side}}</td>
                <td>${{formatPrice(trade.entry)}}</td>
                <td>
                    <div class="tp-cell">
                        <div class="${{tp1Class}} tp-item">${{trade.tp1_hit ? '✅' : '⚪'}} TP1: ${{formatPrice(trade.tp1)}}</div>
                        <div class="${{tp2Class}} tp-item">${{trade.tp2_hit ? '✅' : '⚪'}} TP2: ${{formatPrice(trade.tp2)}}</div>
                        <div class="${{tp3Class}} tp-item">${{trade.tp3_hit ? '✅' : '⚪'}} TP3: ${{formatPrice(trade.tp3)}}</div>
                    </div>
                </td>
                <td>${{formatPrice(trade.sl)}}</td>
                <td>${{statusBadge}}</td>
            `;
            tbody.appendChild(row);
        }});
        
        // Fear & Greed
        const fgRes = await fetch('/api/fear-greed');
        const fgData = await fgRes.json();
        if (fgData.ok) {{
            const fg = fgData.fear_greed;
            document.getElementById('fearGreedContainer').innerHTML = `
                <div class="gauge"><div class="gauge-inner">
                    <div class="gauge-value">${{fg.value}}</div>
                    <div class="gauge-label">${{fg.sentiment}}</div>
                </div></div>
                <p style="font-size:18px;">${{fg.emoji}} ${{fg.recommendation}}</p>
            `;
        }}
        
        // Bull Run Phase
        const brRes = await fetch('/api/bullrun-phase');
        const brData = await brRes.json();
        if (brData.ok) {{
            const phase = brData.bullrun_phase;
            document.getElementById('bullrunContainer').innerHTML = `
                <div style="text-align:center;padding:20px;">
                    <div style="font-size:48px;margin-bottom:10px;">${{phase.emoji}}</div>
                    <h3 style="color:${{phase.color}};margin-bottom:10px;">${{phase.phase_name}}</h3>
                    <p style="color:#94a3b8;margin-bottom:20px;">${{phase.description}}</p>
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

async function doReset() {{
  try {{
    const res = await fetch('/api/reset', {{ method:'POST' }});
    const data = await res.json();
    if(data.ok) {{
      await loadDashboard();
      alert('Reset OK');
    }} else {{
      alert('Reset: erreur');
    }}
  }} catch(err) {{
    alert('Reset: erreur réseau');
  }}
}}

document.getElementById('resetBtn').addEventListener('click', doReset);

loadDashboard();
setInterval(loadDashboard, 30000);
</script>
</body></html>"""
    return HTMLResponse(html)

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Equity</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
{CSS}</head>
<body>
<div class="container">
  <div class="header"><h1>📈 Courbe d'Equity</h1></div>
  {NAV}
  <div class="card">
    <canvas id="equityChart" height="120"></canvas>
  </div>
</div>
<script>
async function loadEquity() {{
  const res = await fetch('/api/equity-curve');
  const data = await res.json();
  const pts = (data.equity_curve || []).map(p => ({{
    t: new Date(p.timestamp), y: p.equity
  }}));
  const ctx = document.getElementById('equityChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: pts.map(p => p.t.toLocaleString()),
      datasets: [{{ label: 'Equity', data: pts.map(p => p.y), fill: false }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: true }} }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 0, autoSkip: true }} }},
        y: {{ beginAtZero: false }}
      }}
    }}
  }});
}}
loadEquity();
</script>
</body></html>""")

@app.get("/journal", response_class=HTMLResponse)
async def journal_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Journal</title>
{CSS}</head>
<body>
<div class="container">
  <div class="header"><h1>📝 Journal</h1></div>
  {NAV}
  <div class="card">
    <h2>Ajouter une note</h2>
    <div style="display:flex;gap:8px;align-items:flex-start;">
      <textarea id="entryTxt" placeholder="Pourquoi ce trade ? Contexte, émotion, règle..."></textarea>
      <button id="addBtn">Ajouter</button>
    </div>
  </div>
  <div class="card">
    <h2>Dernières entrées</h2>
    <div id="entries">Chargement...</div>
  </div>
</div>
<script>
async function loadEntries(){{
  const res = await fetch('/api/journal');
  const data = await res.json();
  const box = document.getElementById('entries');
  if(!data.ok) return box.textContent = 'Erreur.';
  if(!data.entries.length) return box.textContent = 'Aucune entrée.';
  box.innerHTML = data.entries.slice().reverse().map(e => `
    <div class="news-item">
      <div class="news-meta">#${{e.id}} · ${{new Date(e.timestamp).toLocaleString()}} ${{e.trade_id ? '· Trade #'+e.trade_id : ''}}</div>
      <div class="news-summary">${{(e.entry || '').replace(/</g,'&lt;')}}</div>
    </div>
  `).join('');
}}
document.getElementById('addBtn').addEventListener('click', async () => {{
  const t = document.getElementById('entryTxt').value.trim();
  if(!t) return alert('Texte vide.');
  const res = await fetch('/api/journal', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{ entry: t }})
  }});
  const r = await res.json();
  if(r.ok) {{ document.getElementById('entryTxt').value=''; loadEntries(); }}
  else alert('Erreur ajout journal.');
}});
loadEntries();
</script>
</body></html>""")

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Heatmap</title>
{CSS}</head>
<body>
<div class="container">
  <div class="header"><h1>🔥 Heatmap</h1></div>
  {NAV}
  <div class="card">
    <div id="grid" class="grid" style="grid-template-columns: repeat(13, minmax(90px,1fr));"></div>
  </div>
</div>
<script>
function cellClass(winrate){{
  if (winrate>=65) return 'high';
  if (winrate>=55) return 'medium';
  return 'low';
}}
async function loadHeatmap(){{
  const res = await fetch('/api/heatmap');
  const data = await res.json();
  const g = document.getElementById('grid');
  if(!data.ok) return g.textContent='Erreur.';
  const keys = Object.keys(data.heatmap).sort((a,b)=>a.localeCompare(b));
  const hours = [...new Set(keys.map(k => k.split('_')[1]))];
  const days = [...new Set(keys.map(k => k.split('_')[0]))];
  g.innerHTML = '<div></div>' + hours.map(h=>`<div class="heatmap-cell"><strong>${{h}}</strong></div>`).join('');
  for(const d of days){{
    g.innerHTML += `<div class="heatmap-cell"><strong>${{d}}</strong></div>`;
    for(const h of hours){{
      const k = d+'_'+h;
      const v = data.heatmap[k] || {{winrate:0,trades:0}};
      g.innerHTML += `<div class="heatmap-cell ${{cellClass(v.winrate)}}">
        <div>${{v.winrate}}%</div><div style="font-size:12px;color:#94a3b8">${{v.trades}} trades</div>
      </div>`;
    }}
  }}
}}
loadHeatmap();
</script>
</body></html>""")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Stratégie</title>
{CSS}</head>
<body>
<div class="container">
  <div class="header"><h1>⚙️ Stratégie</h1></div>
  {NAV}
  <div class="card">
    <h2>Règles actuelles (exemple)</h2>
    <ul style="margin-left:16px;line-height:1.8;">
      <li>Entrée: clôture > précédente & volume en hausse</li>
      <li>TP1/TP2/TP3: 1.5% / 2.5% / 4% (LONG) · symétrique en SHORT</li>
      <li>SL: 2% (LONG) · 2% (SHORT)</li>
      <li>Risque: 2% de l’equity par position</li>
    </ul>
    <p style="color:#94a3b8;margin-top:10px;">Personnalise cette page selon ta vraie stratégie.</p>
  </div>
</div>
</body></html>""")

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
{CSS}</head>
<body>
<div class="container">
  <div class="header"><h1>⏮️ Backtest</h1></div>
  {NAV}
  <div class="card">
    <h2>Lancer un backtest</h2>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:end">
      <div><label>Symbole</label><br><input id="sym" value="BTCUSDT" style="padding:8px;border-radius:8px;border:1px solid rgba(99,102,241,.3);background:#0f172a;color:#e2e8f0"></div>
      <div><label>Intervalle</label><br>
        <select id="itv" style="padding:8px;border-radius:8px;border:1px solid rgba(99,102,241,.3);background:#0f172a;color:#e2e0f0">
          <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
        </select>
      </div>
      <div><label>Limite</label><br><input id="lim" type="number" value="500" min="50" max="1000" style="padding:8px;border-radius:8px;border:1px solid rgba(99,102,241,.3);background:#0f172a;color:#e2e8f0"></div>
      <div><label>TP %</label><br><input id="tp" type="number" value="3" step="0.1" style="padding:8px;border-radius:8px;border:1px solid rgba(99,102,241,.3);background:#0f172a;color:#e2e8f0"></div>
      <div><label>SL %</label><br><input id="sl" type="number" value="2" step="0.1" style="padding:8px;border-radius:8px;border:1px solid rgba(99,102,241,.3);background:#0f172a;color:#e2e8f0"></div>
      <div><button id="run">▶️ Backtester</button></div>
    </div>
  </div>

  <div class="card"><h2>Résultats</h2>
    <div id="stats">—</div>
    <canvas id="eq" height="100" style="margin-top:16px;"></canvas>
  </div>

  <div class="card"><h2>Trades</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr>
          <th>Entry Time</th><th>Exit Time</th><th>Entry</th><th>Exit</th><th>Résultat</th><th>PnL %</th><th>Equity</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
</div>
<script>
let eqChart;
async function run(){{
  const sym=document.getElementById('sym').value.trim();
  const itv=document.getElementById('itv').value;
  const lim=document.getElementById('lim').value;
  const tp=parseFloat(document.getElementById('tp').value);
  const sl=parseFloat(document.getElementById('sl').value);
  const url=`/api/backtest?symbol=${{encodeURIComponent(sym)}}&interval=${{encodeURIComponent(itv)}}&limit=${{encodeURIComponent(lim)}}&tp_percent=${{encodeURIComponent(tp)}}&sl_percent=${{encodeURIComponent(sl)}}`;
  const res = await fetch(url);
  const data = await res.json();
  if(!data.ok) return (document.getElementById('stats').textContent='Erreur: '+(data.error||'inconnue'));
  const s = data.backtest.stats;
  document.getElementById('stats').innerHTML = `
    <div class="grid grid-4">
      <div class="metric"><div class="metric-label">Total</div><div class="metric-value">${{s.total_trades}}</div></div>
      <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">${{s.win_rate}}%</div></div>
      <div class="metric"><div class="metric-label">Final Equity</div><div class="metric-value">${{s.final_equity.toLocaleString()}}</div></div>
      <div class="metric"><div class="metric-label">Return</div><div class="metric-value">${{s.total_return}}%</div></div>
    </div>`;
  const ctx = document.getElementById('eq').getContext('2d');
  if(eqChart) eqChart.destroy();
  eqChart = new Chart(ctx, {{
    type:'line',
    data:{{ labels: s.equity_curve.map((_,i)=>i), datasets:[{{ label:'Equity', data:s.equity_curve, fill:false }}] }},
    options:{{ responsive:true, plugins:{{legend:{{display:true}}}}, scales:{{ y: {{ beginAtZero:false }} }} }}
  }});
  const tb = document.getElementById('tbody');
  tb.innerHTML = s.trades.map(t => `
    <tr>
      <td>${{new Date(t.entry_time).toLocaleString()}}</td>
      <td>${{new Date(t.exit_time).toLocaleString()}}</td>
      <td>${{t.entry_price}}</td>
      <td>${{t.exit_price}}</td>
      <td>${{t.result}}</td>
      <td>${{t.pnl_percent}}</td>
      <td>${{t.equity}}</td>
    </tr>
  `).join('');
}}
document.getElementById('run').addEventListener('click', run);
</script>
</body></html>""")

@app.get("/patterns", response_class=HTMLResponse)
async def patterns_page():
    patterns = detect_patterns(trading_state.trades)
    patterns_html = "".join([f"<div class='card'>{p}</div>" for p in patterns])
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Patterns</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🤖 Pattern Recognition</h1></div>""" + NAV + """
<div class="card">
<h2>Patterns Détectés</h2>
""" + patterns_html + """
</div>
</div>
</body></html>""")

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    stats = trading_state.get_stats()
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Metrics</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>📊 Métriques Avancées</h1></div>""" + NAV + f"""
<div class="grid grid-3">
<div class="metric">
<div class="metric-label">Sharpe Ratio</div>
<div class="metric-value">1.8</div>
</div>
<div class="metric">
<div class="metric-label">Max Drawdown</div>
<div class="metric-value" style="color:#ef4444;">-8.5%</div>
</div>
<div class="metric">
<div class="metric-label">Profit Factor</div>
<div class="metric-value">2.3</div>
</div>
</div>

<div class="card">
<h2>📈 Performance</h2>
<table>
<tr><th>Métrique</th><th>Valeur</th></tr>
<tr><td>Total Trades</td><td>{stats['total_trades']}</td></tr>
<tr><td>Win Rate</td><td>{stats['win_rate']:.1f}%</td></tr>
</table>
</div>
</div>
</body></html>""")

@app.get("/annonces", response_class=HTMLResponse)
async def annonces_page():
    news = await fetch_all_news_improved()
    news_html = ""
    for item in news[:50]:
        importance_stars = "⭐" * item.get("importance", 1)
        categories = " ".join([f'<span class="badge badge-yellow">{c}</span>' for c in item.get("categories", [])])
        news_html += f"""
        <div class="news-item">
            <div class="news-title">{item['title']} {importance_stars}</div>
            <div class="news-meta">
                <span>📰 {item['source']}</span>
                <span style="margin-left:12px;">🕐 {item.get('time_ago', '')}</span>
                {categories}
            </div>
            <div class="news-summary">{item.get('summary', '')[:200]}...</div>
            <a href="{item['link']}" target="_blank" style="color:#6366f1;font-size:12px;">Lire l'article →</a>
        </div>
        """
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Annonces FR</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header">
<h1>🗞️ Annonces Crypto (100% FR)</h1>
<p>Sources: Journal du Coin, Cointelegraph FR, Cryptoast</p>
</div>""" + NAV + """
<div class="card">
<h2>📰 Dernières Actualités</h2>
""" + news_html + """
</div>
</div>
</body></html>""")

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD v2.6.0")
    print("="*70)
    print("✅ TP1/TP2/TP3 individuels + Heure d’entrée")
    print("✅ Support action CLOSE")
    print("✅ Webhook robuste JSON + text/plain")
    print("✅ Pages HTML restaurées (equity, journal, heatmap, strategie, backtest)")
    print("✅ Bouton Reset")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
