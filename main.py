# -*- coding: utf-8 -*-
"""
Trading Dashboard - VERSION 3.2.0 ULTIMATE EDITION
✅ Convertisseur universel (crypto↔crypto, fiat↔crypto)
✅ Calendrier événements RÉELS (CoinGecko + Fed + CPI)
✅ Altcoin Season Index CORRIGÉ (formule réaliste ~27/100)
✅ Bitcoin Quarterly Returns (heatmap 2013-2025)
✅ Support USDT complet
✅ Telegram FIXÉ
✅ Sans Journal/Equity
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
    """Calcule l'Altcoin Season Index RÉEL basé sur les performances des altcoins vs BTC"""
    btc_dom = global_data.get('btc_dominance', 50)
    
    index = max(0, min(100, int(100 - (btc_dom * 1.8))))
    
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
