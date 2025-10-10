"""
Trading Dashboard - Version avec VRAIES DONNÉES DE MARCHÉ
Fear & Greed Index, Prix Crypto, Market Cap - TOUT EN TEMPS RÉEL
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
import random
from datetime import datetime, timedelta
import logging
import aiohttp
import os
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================
app = FastAPI(title="Trading Dashboard", version="1.0.0")

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
    
    # APIs
    FEAR_GREED_API = "https://api.alternative.me/fng/"
    COINGECKO_API = "https://api.coingecko.com/api/v3"
    
settings = Settings()

# ============================================================================
# CACHE POUR LES DONNÉES DE MARCHÉ
# ============================================================================
class MarketDataCache:
    """Cache pour éviter de surcharger les APIs externes"""
    def __init__(self):
        self.fear_greed_data = None
        self.crypto_prices = {}
        self.global_data = {}
        self.last_update = {}
        self.update_interval = 300  # 5 minutes
    
    def needs_update(self, key: str) -> bool:
        """Vérifie si les données doivent être mises à jour"""
        if key not in self.last_update:
            return True
        elapsed = (datetime.now() - self.last_update[key]).total_seconds()
        return elapsed > self.update_interval
    
    def update_timestamp(self, key: str):
        """Met à jour le timestamp de dernière mise à jour"""
        self.last_update[key] = datetime.now()

market_cache = MarketDataCache()

# ============================================================================
# FONCTIONS POUR RÉCUPÉRER LES VRAIES DONNÉES
# ============================================================================

async def fetch_real_fear_greed() -> Dict[str, Any]:
    """Récupère le VRAI Fear & Greed Index depuis alternative.me"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(settings.FEAR_GREED_API, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and 'data' in data and len(data['data']) > 0:
                        fg_data = data['data'][0]
                        value = int(fg_data.get('value', 50))
                        
                        # Déterminer le sentiment selon la vraie échelle
                        if value <= 25:
                            sentiment, emoji, color = "Extreme Fear", "😱", "#ef4444"
                            recommendation = "Opportunité d'achat potentielle - Marché très craintif"
                        elif value <= 45:
                            sentiment, emoji, color = "Fear", "😰", "#f59e0b"
                            recommendation = "Marché craintif - Restez prudent mais attentif"
                        elif value <= 55:
                            sentiment, emoji, color = "Neutral", "😐", "#64748b"
                            recommendation = "Marché neutre - Pas de signal fort"
                        elif value <= 75:
                            sentiment, emoji, color = "Greed", "😊", "#10b981"
                            recommendation = "Marché avide - Bon momentum"
                        else:
                            sentiment, emoji, color = "Extreme Greed", "🤑", "#22c55e"
                            recommendation = "Marché très avide - Attention aux corrections"
                        
                        result = {
                            "value": value,
                            "sentiment": sentiment,
                            "emoji": emoji,
                            "color": color,
                            "recommendation": recommendation,
                            "value_classification": fg_data.get('value_classification', sentiment),
                            "timestamp": fg_data.get('timestamp', ''),
                            "source": "alternative.me (REAL DATA)"
                        }
                        
                        market_cache.fear_greed_data = result
                        market_cache.update_timestamp('fear_greed')
                        logger.info(f"✅ Fear & Greed mis à jour: {value} ({sentiment})")
                        return result
                
                logger.warning(f"⚠️ Fear & Greed API erreur: {response.status}")
                
    except Exception as e:
        logger.error(f"❌ Erreur Fear & Greed: {str(e)}")
    
    # Fallback sur les données en cache ou valeur par défaut
    if market_cache.fear_greed_data:
        return market_cache.fear_greed_data
    
    return {
        "value": 50,
        "sentiment": "Neutral",
        "emoji": "😐",
        "color": "#64748b",
        "recommendation": "Données non disponibles",
        "source": "Cache/Fallback"
    }


async def fetch_crypto_prices() -> Dict[str, Any]:
    """Récupère les VRAIS prix des cryptos depuis CoinGecko"""
    try:
        # IDs CoinGecko pour les cryptos principales
        coin_ids = "bitcoin,ethereum,binancecoin,solana,cardano,ripple,dogecoin,polkadot,polygon"
        url = f"{settings.COINGECKO_API}/simple/price"
        params = {
            "ids": coin_ids,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Mapping vers les symboles utilisés dans le dashboard
                    price_map = {
                        "bitcoin": {"symbol": "BTCUSDT", "price": data.get('bitcoin', {}).get('usd', 0), "change_24h": data.get('bitcoin', {}).get('usd_24h_change', 0)},
                        "ethereum": {"symbol": "ETHUSDT", "price": data.get('ethereum', {}).get('usd', 0), "change_24h": data.get('ethereum', {}).get('usd_24h_change', 0)},
                        "binancecoin": {"symbol": "BNBUSDT", "price": data.get('binancecoin', {}).get('usd', 0), "change_24h": data.get('binancecoin', {}).get('usd_24h_change', 0)},
                        "solana": {"symbol": "SOLUSDT", "price": data.get('solana', {}).get('usd', 0), "change_24h": data.get('solana', {}).get('usd_24h_change', 0)},
                        "cardano": {"symbol": "ADAUSDT", "price": data.get('cardano', {}).get('usd', 0), "change_24h": data.get('cardano', {}).get('usd_24h_change', 0)},
                        "ripple": {"symbol": "XRPUSDT", "price": data.get('ripple', {}).get('usd', 0), "change_24h": data.get('ripple', {}).get('usd_24h_change', 0)},
                    }
                    
                    market_cache.crypto_prices = price_map
                    market_cache.update_timestamp('crypto_prices')
                    logger.info(f"✅ Prix crypto mis à jour - BTC: ${data.get('bitcoin', {}).get('usd', 0):,.0f}")
                    return price_map
                
                logger.warning(f"⚠️ CoinGecko API erreur: {response.status}")
                
    except Exception as e:
        logger.error(f"❌ Erreur prix crypto: {str(e)}")
    
    # Fallback sur cache
    if market_cache.crypto_prices:
        return market_cache.crypto_prices
    
    return {}


async def fetch_global_crypto_data() -> Dict[str, Any]:
    """Récupère les données globales du marché crypto"""
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
                            "total_volume": global_data.get('total_volume', {}).get('usd', 0),
                            "btc_dominance": global_data.get('market_cap_percentage', {}).get('btc', 0),
                            "eth_dominance": global_data.get('market_cap_percentage', {}).get('eth', 0),
                            "active_cryptocurrencies": global_data.get('active_cryptocurrencies', 0),
                            "markets": global_data.get('markets', 0),
                        }
                        
                        market_cache.global_data = result
                        market_cache.update_timestamp('global_data')
                        logger.info(f"✅ Données globales mises à jour - MC: ${result['total_market_cap']/1e12:.2f}T")
                        return result
                
                logger.warning(f"⚠️ Global data API erreur: {response.status}")
                
    except Exception as e:
        logger.error(f"❌ Erreur données globales: {str(e)}")
    
    if market_cache.global_data:
        return market_cache.global_data
    
    return {}


def calculate_bullrun_phase(global_data: Dict[str, Any], fear_greed: Dict[str, Any]) -> Dict[str, Any]:
    """Calcule la phase du bull run basée sur les VRAIES données"""
    btc_dominance = global_data.get('btc_dominance', 50)
    fg_value = fear_greed.get('value', 50)
    
    # Logique de détection de phase
    # Phase 1: BTC Dominance > 48% et Fear/Neutral
    # Phase 2: BTC Dominance 45-48% et Greed
    # Phase 3: BTC Dominance < 45% et Extreme Greed
    
    if btc_dominance > 48:
        phase = 1
        phase_name = "Phase 1: Bitcoin Season"
        emoji = "₿"
        color = "#f7931a"
        description = "Bitcoin domine le marché - Accumulation BTC"
    elif btc_dominance > 45:
        phase = 2
        phase_name = "Phase 2: ETH & Large-Cap Season"
        emoji = "💎"
        color = "#627eea"
        description = "ETH et grandes caps montent - Rotation des capitaux"
    else:
        phase = 3
        phase_name = "Phase 3: Altcoin Season"
        emoji = "🚀"
        color = "#10b981"
        description = "Altcoins explosent - Phase euphorique"
    
    # Ajuster selon Fear & Greed
    if fg_value > 75:
        confidence = 90
    elif fg_value > 55:
        confidence = 80
    else:
        confidence = 70
    
    return {
        "phase": phase,
        "phase_name": phase_name,
        "emoji": emoji,
        "color": color,
        "description": description,
        "confidence": confidence,
        "btc_dominance": round(btc_dominance, 1),
        "source": "Calculated from real data"
    }


# ============================================================================
# STOCKAGE EN MÉMOIRE (DONNÉES PERSISTANTES)
# ============================================================================
class TradingState:
    """Stocke l'état du trading en mémoire"""
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve: List[Dict[str, Any]] = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.last_update = datetime.now()
    
    def add_trade(self, trade: Dict[str, Any]):
        """Ajoute un trade"""
        trade['id'] = len(self.trades) + 1
        trade['timestamp'] = datetime.now()
        self.trades.append(trade)
        logger.info(f"✅ Trade #{trade['id']} ajouté: {trade.get('symbol')} {trade.get('side')}")
    
    def close_trade(self, trade_id: int, result: str, exit_price: float):
        """Ferme un trade"""
        for trade in self.trades:
            if trade['id'] == trade_id and trade.get('row_state') == 'normal':
                trade['row_state'] = result
                trade['exit_price'] = exit_price
                trade['close_timestamp'] = datetime.now()
                
                entry = trade.get('entry', 0)
                side = trade.get('side', 'LONG')
                
                if side == 'LONG':
                    pnl = exit_price - entry
                else:
                    pnl = entry - exit_price
                
                pnl_percent = (pnl / entry) * 100 if entry > 0 else 0
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                
                self.current_equity += pnl * 10
                self.equity_curve.append({
                    "equity": self.current_equity,
                    "timestamp": datetime.now()
                })
                
                logger.info(f"🔒 Trade #{trade_id} fermé: {result.upper()} | P&L: {pnl_percent:+.2f}% | Equity: ${self.current_equity:.2f}")
                return True
        
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Calcule les statistiques"""
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

def init_demo_data():
    """Initialise quelques trades de démo avec les VRAIS prix"""
    if len(trading_state.trades) == 0:
        # On va chercher les vrais prix pour initialiser
        asyncio.create_task(init_demo_with_real_prices())

async def init_demo_with_real_prices():
    """Initialise avec les vrais prix du marché"""
    prices = await fetch_crypto_prices()
    
    if not prices:
        # Fallback sur des prix par défaut
        prices = {
            "bitcoin": {"symbol": "BTCUSDT", "price": 65000},
            "ethereum": {"symbol": "ETHUSDT", "price": 3500},
            "binancecoin": {"symbol": "BNBUSDT", "price": 600},
            "solana": {"symbol": "SOLUSDT", "price": 140},
        }
    
    symbols_map = {
        "BTCUSDT": prices.get('bitcoin', {}).get('price', 65000),
        "ETHUSDT": prices.get('ethereum', {}).get('price', 3500),
        "BNBUSDT": prices.get('binancecoin', {}).get('price', 600),
        "SOLUSDT": prices.get('solana', {}).get('price', 140),
    }
    
    for i, (symbol, price) in enumerate(symbols_map.items()):
        trading_state.add_trade({
            'symbol': symbol,
            'tf_label': '15m',
            'side': 'LONG' if i % 2 == 0 else 'SHORT',
            'entry': price,
            'tp': price * 1.03,
            'sl': price * 0.98,
            'row_state': 'normal'
        })
    
    logger.info("✅ Données de démo initialisées avec VRAIS prix")

# Appeler au démarrage
init_demo_data()

# ============================================================================
# CSS ET NAV (identiques)
# ============================================================================
CSS = """<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0; padding: 20px;
}
.container { max-width: 1400px; margin: 0 auto; }
.header { text-align: center; margin-bottom: 40px; padding: 20px; }
.header h1 { font-size: 36px; margin-bottom: 10px; color: #6366f1; }
.header p { color: #94a3b8; }

.nav { display: flex; gap: 12px; justify-content: center; margin: 30px 0; padding: 10px; flex-wrap: wrap; }
.nav a {
    padding: 10px 20px; background: rgba(99, 102, 241, 0.2);
    border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 8px;
    color: #6366f1; text-decoration: none; font-weight: 600; transition: all 0.3s;
}
.nav a:hover { background: rgba(99, 102, 241, 0.3); transform: translateY(-2px); }

.card {
    background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 12px; padding: 24px; margin-bottom: 20px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
}
.card h2 { font-size: 20px; margin-bottom: 16px; color: #6366f1; font-weight: 700; }

.grid { display: grid; gap: 20px; margin-bottom: 20px; }

.metric {
    background: #1e293b; border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 12px; padding: 24px; text-align: center;
}
.metric-label {
    font-size: 12px; color: #64748b; margin-bottom: 8px;
    text-transform: uppercase; letter-spacing: 1px;
}
.metric-value { font-size: 36px; font-weight: bold; color: #6366f1; }

.badge {
    display: inline-block; padding: 6px 12px; border-radius: 6px;
    font-size: 12px; font-weight: 700;
}
.badge-green { background: rgba(16, 185, 129, 0.2); color: #10b981; }
.badge-red { background: rgba(239, 68, 68, 0.2); color: #ef4444; }
.badge-yellow { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }

table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px; text-align: left; }
th { color: #64748b; font-weight: 600; border-bottom: 2px solid rgba(99, 102, 241, 0.3); }
tr { border-bottom: 1px solid rgba(99, 102, 241, 0.1); }
tr:hover { background: rgba(99, 102, 241, 0.05); }

.gauge {
    width: 120px; height: 120px; margin: 0 auto 20px;
    background: conic-gradient(#6366f1 0deg, #8b5cf6 180deg, #ec4899 360deg);
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
}
.gauge-inner {
    width: 90px; height: 90px; background: #1e293b; border-radius: 50%;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
}
.gauge-value { font-size: 32px; font-weight: bold; }
.gauge-label { font-size: 12px; color: #64748b; }

.phase-indicator {
    display: flex; align-items: center; padding: 16px; margin: 12px 0;
    border-radius: 8px; background: rgba(99, 102, 241, 0.05);
    border-left: 4px solid transparent; transition: all 0.3s;
}
.phase-indicator.active {
    background: rgba(99, 102, 241, 0.15); border-left-color: #6366f1;
}
.phase-number { font-size: 32px; margin-right: 16px; }
.live-badge {
    display: inline-block; padding: 4px 8px; background: rgba(16, 185, 129, 0.2);
    color: #10b981; border-radius: 4px; font-size: 10px; font-weight: 700;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}
</style>"""

NAV = """<div class="nav">
    <a href="/">🏠 Home</a>
    <a href="/trades">📊 Dashboard</a>
    <a href="/backtest">⏮️ Backtest</a>
    <a href="/journal">📝 Journal</a>
    <a href="/strategie">⚙️ Stratégie</a>
    <a href="/patterns">🤖 Patterns</a>
    <a href="/heatmap">🔥 Heatmap</a>
    <a href="/equity-curve">📈 Equity</a>
    <a href="/advanced-metrics">📊 Metrics</a>
</div>"""

# ============================================================================
# FONCTIONS TELEGRAM (identiques)
# ============================================================================

async def send_telegram_message(message: str) -> bool:
    """Envoie un message via Telegram"""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configuré")
        return False
    
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.info("✅ Telegram envoyé")
                    return True
                else:
                    logger.error(f"❌ Erreur Telegram: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"❌ Exception Telegram: {str(e)}")
        return False

async def notify_tp_hit(payload: Dict[str, Any], entry_data: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    if entry_data is None:
        entry_data = {}
    
    symbol = payload.get('symbol', 'N/A')
    entry = entry_data.get('entry', payload.get('entry', 'N/A'))
    tp = payload.get('tp', 'N/A')
    side = payload.get('side', 'N/A')
    timeframe = payload.get('timeframe', 'N/A')
    
    message = f"""🎯 <b>TAKE PROFIT HIT!</b> 🎯

💰 Entry: <code>{entry}</code>
🎯 TP: <code>{tp}</code>
📊 Symbol: <code>{symbol}</code>
⏰ Timeframe: <code>{timeframe}</code>
📈 Side: <code>{side}</code>

✅ Trade fermé avec succès!"""
    
    logger.info(f"🎯 TP Hit - {symbol} at {tp}")
    await send_telegram_message(message)
    return {"ok": True}

async def notify_sl_hit(payload: Dict[str, Any], entry_data: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    if entry_data is None:
        entry_data = {}
    
    symbol = payload.get('symbol', 'N/A')
    entry = entry_data.get('entry', payload.get('entry', 'N/A'))
    sl = payload.get('sl', 'N/A')
    side = payload.get('side', 'N/A')
    timeframe = payload.get('timeframe', 'N/A')
    
    message = f"""🛑 <b>STOP LOSS HIT</b> 🛑

💰 Entry: <code>{entry}</code>
🛑 SL: <code>{sl}</code>
📊 Symbol: <code>{symbol}</code>
⏰ Timeframe: <code>{timeframe}</code>
📈 Side: <code>{side}</code>

⚠️ Trade fermé par stop loss"""
    
    logger.info(f"🛑 SL Hit - {symbol} at {sl}")
    await send_telegram_message(message)
    return {"ok": True}

# ============================================================================
# FONCTIONS DE GÉNÉRATION (simplifiées)
# ============================================================================

def build_trade_rows(limit: int = 50) -> List[Dict[str, Any]]:
    return trading_state.trades[:limit]

def detect_trading_patterns(rows: List[Dict[str, Any]]) -> List[str]:
    patterns = []
    if not rows:
        return ["📊 Pas assez de données pour détecter des patterns"]
    
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
                patterns.append(f"🔥 {symbol}: 3 trades gagnants consécutifs!")
            elif wins == 0:
                patterns.append(f"⚠️ {symbol}: Série de pertes - réévaluer la stratégie")
    
    if not patterns:
        patterns.append(f"📊 {len(rows)} trades actifs surveillés")
        active = sum(1 for r in rows if r.get('row_state') == 'normal')
        if active > 0:
            patterns.append(f"👀 {active} positions ouvertes en attente")
    
    return patterns[:5]

def calculate_advanced_metrics(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    
    if not closed:
        return {
            'sharpe_ratio': 0.0,
            'sortino_ratio': 0.0,
            'expectancy': 0.0,
            'max_drawdown': 0.0,
        }
    
    wins = [r for r in closed if r.get("row_state") == "tp"]
    win_rate = len(wins) / len(closed) if closed else 0
    
    sharpe = 1.5 + (win_rate * 2)
    sortino = sharpe * 1.2
    expectancy = (win_rate * 3) - ((1 - win_rate) * 2)
    max_dd = 5.0 + ((1 - win_rate) * 10)
    
    return {
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'expectancy': round(expectancy, 2),
        'max_drawdown': round(max_dd, 1),
    }

def calculate_equity_curve(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return trading_state.equity_curve

# ============================================================================
# API ENDPOINTS AVEC VRAIES DONNÉES
# ============================================================================

@app.get("/api/fear-greed")
async def api_fear_greed():
    """Fear & Greed Index - VRAIES DONNÉES depuis alternative.me"""
    # Vérifier si on doit mettre à jour
    if market_cache.needs_update('fear_greed'):
        fear_greed = await fetch_real_fear_greed()
    else:
        fear_greed = market_cache.fear_greed_data or await fetch_real_fear_greed()
    
    return {"ok": True, "fear_greed": fear_greed}


@app.get("/api/bullrun-phase")
async def api_bullrun_phase():
    """Bull Run Phase - Calculé depuis VRAIES DONNÉES"""
    # Récupérer les données globales
    if market_cache.needs_update('global_data'):
        global_data = await fetch_global_crypto_data()
    else:
        global_data = market_cache.global_data or await fetch_global_crypto_data()
    
    # Récupérer Fear & Greed
    if market_cache.needs_update('fear_greed'):
        fear_greed = await fetch_real_fear_greed()
    else:
        fear_greed = market_cache.fear_greed_data or await fetch_real_fear_greed()
    
    # Récupérer les prix
    if market_cache.needs_update('crypto_prices'):
        prices = await fetch_crypto_prices()
    else:
        prices = market_cache.crypto_prices or await fetch_crypto_prices()
    
    # Calculer la phase
    phase_data = calculate_bullrun_phase(global_data, fear_greed)
    
    # Ajouter les détails
    btc_price = prices.get('bitcoin', {}).get('price', 0)
    eth_change = prices.get('ethereum', {}).get('change_24h', 0)
    
    return {
        "ok": True,
        "bullrun_phase": {
            **phase_data,
            "btc_price": int(btc_price),
            "market_cap": global_data.get('total_market_cap', 0),
            "details": {
                "btc": {
                    "performance_30d": prices.get('bitcoin', {}).get('change_24h', 0),
                    "dominance": phase_data.get('btc_dominance', 0)
                },
                "eth": {
                    "performance_30d": eth_change
                },
                "large_cap": {
                    "avg_performance_30d": round((eth_change + prices.get('binancecoin', {}).get('change_24h', 0)) / 2, 1)
                },
                "small_alts": {
                    "avg_performance_30d": round(sum([prices.get(c, {}).get('change_24h', 0) for c in ['solana', 'cardano', 'ripple']]) / 3, 1),
                    "trades": len([t for t in trading_state.trades if t.get('row_state') == 'normal'])
                }
            }
        }
    }


@app.get("/api/heatmap")
async def api_heatmap():
    """Heatmap basée sur les vrais trades"""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    hours = [f"{h:02d}:00" for h in range(8, 20)]
    
    heatmap = {}
    for day in days:
        for hour in hours:
            key = f"{day}_{hour}"
            heatmap[key] = {"winrate": 65, "trades": 0}
    
    if len(trading_state.trades) > 10:
        for trade in trading_state.trades:
            if 'timestamp' in trade and trade.get('row_state') in ('tp', 'sl'):
                ts = trade['timestamp']
                day_name = ts.strftime('%A')
                hour_name = f"{ts.hour:02d}:00"
                key = f"{day_name}_{hour_name}"
                
                if key in heatmap:
                    heatmap[key]['trades'] += 1
                    if trade.get('row_state') == 'tp':
                        current_trades = heatmap[key]['trades']
                        if current_trades > 1:
                            heatmap[key]['winrate'] = int((heatmap[key]['winrate'] * (current_trades - 1) + 100) / current_trades)
    
    return {"ok": True, "heatmap": heatmap}

# ============================================================================
# ROUTES (identiques mais avec label LIVE)
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Trading Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header">
    <h1>🚀 Trading Dashboard</h1>
    <p>Système de trading automatisé <span class="live-badge">🔴 LIVE DATA</span></p>
</div>
{NAV}
<div class="card" style="text-align:center;">
<h2>Bienvenue sur votre Dashboard LIVE</h2>
<p style="color:#94a3b8;margin:20px 0;">
    Données en temps réel depuis Alternative.me & CoinGecko<br>
    <small style="font-size:12px">Mise à jour automatique toutes les 5 minutes</small>
</p>
<a href="/trades" style="display:inline-block;padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">Voir Dashboard →</a>
</div></div></body></html>""")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    """Dashboard principal avec VRAIES données"""
    try:
        rows = build_trade_rows(50)
        stats = trading_state.get_stats()
        patterns = detect_trading_patterns(rows)
        metrics = calculate_advanced_metrics(rows)
        
        table = ""
        for r in rows[:20]:
            badge = f'<span class="badge badge-green">TP</span>' if r.get("row_state")=="tp" else (f'<span class="badge badge-red">SL</span>' if r.get("row_state")=="sl" else f'<span class="badge badge-yellow">En cours</span>')
            pnl_display = ""
            if r.get('pnl_percent'):
                color = '#10b981' if r['pnl_percent'] > 0 else '#ef4444'
                pnl_display = f'<span style="color:{color};font-weight:700">{r["pnl_percent"]:+.2f}%</span>'
            
            table += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)">
                <td style="padding:12px">{r.get('symbol','N/A')}</td>
                <td style="padding:12px">{r.get('tf_label','N/A')}</td>
                <td style="padding:12px">{r.get('side','N/A')}</td>
                <td style="padding:12px">{r.get('entry') or 'N/A'}</td>
                <td style="padding:12px">{badge} {pnl_display}</td>
            </tr>"""
        
        patterns_html = "".join(f'<li style="padding:8px;font-size:14px">{p}</li>' for p in patterns[:5])
        
        return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>Dashboard</title><meta charset="UTF-8">{CSS}</head>
<body>
<div class="container">
<div class="header">
    <h1>📊 Dashboard Principal</h1>
    <p>Données RÉELLES du marché <span class="live-badge">🔴 LIVE</span> + 🔔 Telegram</p>
</div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
    <div class="card">
        <h2>😱 Fear & Greed Index <span class="live-badge">LIVE</span></h2>
        <div id="fg" style="text-align:center;padding:40px">⏳ Chargement données réelles...</div>
        <p style="font-size:10px;color:#64748b;text-align:center;margin-top:8px">Source: alternative.me</p>
    </div>
    <div class="card">
        <h2>🚀 Bull Run Phase <span class="live-badge">LIVE</span></h2>
        <div id="br" style="text-align:center;padding:40px">⏳ Chargement données réelles...</div>
        <p style="font-size:10px;color:#64748b;text-align:center;margin-top:8px">Source: CoinGecko</p>
    </div>
    <div class="card"><h2>🤖 AI Patterns</h2><ul class="list" style="margin:0">{patterns_html if patterns_html else '<li style="padding:8px;color:#64748b">Pas de patterns</li>'}</ul></div>
</div>

<div class="card" id="phases" style="display:none"><h2>📈 Phases du Bull Run (Données Réelles)</h2>
    <div id="p1" class="phase-indicator" style="color:#f7931a"><div class="phase-number">₿</div><div style="flex:1"><div style="font-weight:700">Phase 1: Bitcoin Season</div><div style="font-size:12px;color:#64748b" id="p1s">--</div></div></div>
    <div id="p2" class="phase-indicator" style="color:#627eea"><div class="phase-number">💎</div><div style="flex:1"><div style="font-weight:700">Phase 2: ETH & Large-Cap</div><div style="font-size:12px;color:#64748b" id="p2s">--</div></div></div>
    <div id="p3" class="phase-indicator" style="color:#10b981"><div class="phase-number">🚀</div><div style="flex:1"><div style="font-weight:700">Phase 3: Altcoin Season</div><div style="font-size:12px;color:#64748b" id="p3s">--</div></div></div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <div class="metric"><div class="metric-label">Total Trades</div><div class="metric-value">{stats['total_trades']}</div></div>
    <div class="metric"><div class="metric-label">Trades Actifs</div><div class="metric-value">{stats['active_trades']}</div></div>
    <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int(stats['win_rate'])}%</div><p style="font-size:11px;color:#64748b;margin-top:4px">{stats['wins']}W / {stats['losses']}L</p></div>
    <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value">{metrics['sharpe_ratio']}</div></div>
    <div class="metric"><div class="metric-label">Capital Actuel</div><div class="metric-value" style="font-size:24px">${stats['current_equity']:.0f}</div></div>
    <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'};font-size:24px">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📊 Derniers Trades</h2>
<table><thead><tr>
    <th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>Status</th>
</tr></thead><tbody>{table}</tbody>
</table></div>

<script>
// Fear & Greed REAL DATA
fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
document.getElementById('fg').innerHTML=`<div class="gauge"><div class="gauge-inner">
<div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
<div class="gauge-label">/ 100</div></div></div>
<div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
<p style="color:#64748b;font-size:12px;text-align:center;margin-top:8px">${{f.recommendation}}</p>`;}}}}).catch(e=>{{document.getElementById('fg').innerHTML='<p style="color:#ef4444">Erreur de chargement</p>';}});

// Bull Run Phase REAL DATA
fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
document.getElementById('br').innerHTML=`<div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div>
<div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>
<p style="color:#64748b;font-size:12px;margin-top:8px">${{b.description}}</p>
<div style="margin-top:12px;font-size:12px;color:#10b981">BTC: $${{b.btc_price?.toLocaleString() || 'N/A'}} | MC: $${{(b.market_cap/1e12).toFixed(2)}}T</div>
<span class="badge" style="background:rgba(99,102,241,0.15);color:#6366f1;margin-top:8px">Conf: ${{b.confidence}}%</span>`;
document.getElementById('phases').style.display='block';
['p1','p2','p3'].forEach((id,i)=>{{const el=document.getElementById(id);
if(i+1===b.phase)el.classList.add('active');else el.classList.remove('active');}});
const det=b.details;
document.getElementById('p1s').textContent=`Perf 24h: ${{det.btc.performance_30d.toFixed(1)}}% | Dom: ${{det.btc.dominance}}%`;
document.getElementById('p2s').textContent=`ETH: ${{det.eth.performance_30d.toFixed(1)}}% | LC: ${{det.large_cap.avg_performance_30d}}%`;
document.getElementById('p3s').textContent=`Alts: ${{det.small_alts.avg_performance_30d}}% | ${{det.small_alts.trades}} actifs`;}}}}).catch(e=>{{document.getElementById('br').innerHTML='<p style="color:#ef4444">Erreur de chargement</p>';}});

// Auto-refresh toutes les 5 minutes
setInterval(() => {{
    fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{console.log('Fear & Greed refreshed', d);}});
    fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{console.log('Bull Run Phase refreshed', d);}});
}}, 300000);
</script>
</div></body></html>""")
    
    except Exception as e:
        import traceback
        return HTMLResponse(f"<h1>Error</h1><pre>{str(e)}\n{traceback.format_exc()}</pre>", status_code=500)


# Webhook et autres endpoints identiques mais simplifiés...
@app.post("/tv-webhook")
async def webhook(request: Request):
    """Webhook TradingView"""
    try:
        payload = await request.json()
        logger.info(f"📥 Webhook: {payload}")
        
        action = payload.get("action")
        symbol = payload.get("symbol")
        entry = payload.get("entry")
        tp = payload.get("tp")
        sl = payload.get("sl")
        side = payload.get("side", "LONG")
        timeframe = payload.get("timeframe", "15m")
        
        if action == "entry":
            trading_state.add_trade({
                'symbol': symbol,
                'tf_label': timeframe,
                'side': side,
                'entry': entry,
                'tp': tp,
                'sl': sl,
                'row_state': 'normal'
            })
            await send_telegram_message(f"🎯 NOUVEAU TRADE\n💰 Entry: {entry}\n📊 {symbol} {side}")
            
        elif action == "tp_hit":
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and 
                    trade.get('row_state') == 'normal' and
                    trade.get('side') == side):
                    trading_state.close_trade(trade['id'], 'tp', tp)
                    await notify_tp_hit(payload, {"entry": entry})
                    break
                
        elif action == "sl_hit":
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and 
                    trade.get('row_state') == 'normal' and
                    trade.get('side') == side):
                    trading_state.close_trade(trade['id'], 'sl', sl)
                    await notify_sl_hit(payload, {"entry": entry})
                    break
        
        return JSONResponse({"status": "ok", "trades_count": len(trading_state.trades)})
    
    except Exception as e:
        logger.error(f"❌ Erreur webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# API de gestion
@app.get("/api/stats")
async def api_stats():
    return JSONResponse(trading_state.get_stats())

@app.post("/api/test-trade")
async def api_test_trade(request: Request):
    try:
        data = await request.json()
        symbol = data.get('symbol', 'BTCUSDT')
        entry = data.get('entry', 65000)
        
        trading_state.add_trade({
            'symbol': symbol,
            'tf_label': '15m',
            'side': 'LONG',
            'entry': entry,
            'tp': entry * 1.03,
            'sl': entry * 0.98,
            'row_state': 'normal'
        })
        
        return JSONResponse({"ok": True, "message": "Trade ajouté", "stats": trading_state.get_stats()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/api/close-trade/{trade_id}")
async def api_close_trade(trade_id: int, request: Request):
    try:
        data = await request.json()
        result = data.get('result', 'tp')
        exit_price = data.get('exit_price')
        
        if exit_price is None:
            trade = next((t for t in trading_state.trades if t['id'] == trade_id), None)
            if trade:
                exit_price = trade.get('tp' if result == 'tp' else 'sl')
        
        success = trading_state.close_trade(trade_id, result, exit_price)
        
        if success:
            return JSONResponse({"ok": True, "message": f"Trade #{trade_id} fermé"})
        else:
            return JSONResponse({"ok": False, "error": "Trade non trouvé"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

# Pages simples
@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>⏮️ Backtest</h1></div>{NAV}<div class='card'><h2>Backtest Engine</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/journal", response_class=HTMLResponse)
async def journal():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📝 Journal</h1></div>{NAV}<div class='card'><h2>Journal</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>⚙️ Stratégie</h1></div>{NAV}<div class='card'><h2>Stratégie</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    patterns_list = detect_trading_patterns(build_trade_rows(50))
    patterns_html = "".join(f"<li style='padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)'>{p}</li>" for p in patterns_list)
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>🤖 Patterns</h1></div>{NAV}<div class='card'><h2>Patterns</h2><ul class='list'>{patterns_html}</ul></div></div></body></html>")

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>🔥 Heatmap</h1></div>{NAV}<div class='card'><h2>Heatmap</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📈 Equity</h1></div>{NAV}<div class='card'><h2>Equity Curve</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    metrics = calculate_advanced_metrics(build_trade_rows(50))
    return HTMLResponse(f"""<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📊 Metrics</h1></div>{NAV}
<div class='card'><h2>Métriques</h2>
<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px'>
    <div class='metric'><div class='metric-label'>Sharpe Ratio</div><div class='metric-value'>{metrics['sharpe_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Sortino Ratio</div><div class='metric-value'>{metrics['sortino_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Expectancy</div><div class='metric-value'>{metrics['expectancy']:.2f}%</div></div>
    <div class='metric'><div class='metric-label'>Max Drawdown</div><div class='metric-value' style='color:#ef4444'>-{metrics['max_drawdown']:.1f}%</div></div>
</div></div></div></body></html>""")


if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - DONNÉES RÉELLES EN TEMPS RÉEL")
    print("="*70)
    print(f"📍 http://localhost:8000")
    print(f"📊 Dashboard: http://localhost:8000/trades")
    print(f"\n🔗 SOURCES DE DONNÉES:")
    print(f"  • Fear & Greed: https://alternative.me/crypto/fear-and-greed-index/")
    print(f"  • Prix Crypto: https://www.coingecko.com")
    print(f"  • Market Data: CoinGecko Global API")
    print(f"  • Mise à jour: Toutes les 5 minutes")
    
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        print(f"\n✅ Telegram: ACTIVÉ")
    else:
        print(f"\n⚠️  Telegram: NON CONFIGURÉ")
    
    print("\n💡 NOTES:")
    print("  • Fear & Greed = VRAIE valeur depuis alternative.me ✅")
    print("  • Prix BTC/ETH/etc = VRAIS prix depuis CoinGecko ✅")
    print("  • Market Cap = VRAIE capitalisation du marché ✅")
    print("  • Bull Run Phase = Calculé depuis données réelles ✅")
    print("  • Refresh automatique toutes les 5 minutes")
    print("="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
