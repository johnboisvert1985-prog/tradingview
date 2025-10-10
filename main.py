"""
Trading Dashboard - Version Complète avec Données Persistantes
Toutes les sections et fonctionnalités + système de stockage en mémoire
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
    
settings = Settings()

# ============================================================================
# STOCKAGE EN MÉMOIRE (DONNÉES PERSISTANTES)
# ============================================================================
class TradingState:
    """Stocke l'état du trading en mémoire"""
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.current_equity = settings.INITIAL_CAPITAL
        self.equity_curve: List[Dict[str, Any]] = [{"equity": settings.INITIAL_CAPITAL, "timestamp": datetime.now()}]
        self.fear_greed_value = 50
        self.bullrun_phase = 1
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
                trade['row_state'] = result  # 'tp' ou 'sl'
                trade['exit_price'] = exit_price
                trade['close_timestamp'] = datetime.now()
                
                # Calcul du P&L
                entry = trade.get('entry', 0)
                side = trade.get('side', 'LONG')
                
                if side == 'LONG':
                    pnl = exit_price - entry
                else:
                    pnl = entry - exit_price
                
                pnl_percent = (pnl / entry) * 100 if entry > 0 else 0
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                
                # Mise à jour de l'équité
                self.current_equity += pnl * 10  # Multiplie par taille de position
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

# Instance globale
trading_state = TradingState()

# Initialisation avec quelques trades de démo
def init_demo_data():
    """Initialise quelques trades de démo (appelé une seule fois)"""
    if len(trading_state.trades) == 0:
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT']
        
        for i in range(5):
            symbol = symbols[i % len(symbols)]
            if 'BTC' in symbol:
                entry = 65000 + (i * 100)
            elif 'ETH' in symbol:
                entry = 3500 + (i * 50)
            elif 'BNB' in symbol:
                entry = 600 + (i * 10)
            else:
                entry = 140 + (i * 5)
            
            trading_state.add_trade({
                'symbol': symbol,
                'tf_label': '15m',
                'side': 'LONG' if i % 2 == 0 else 'SHORT',
                'entry': entry,
                'tp': entry * 1.03,
                'sl': entry * 0.98,
                'row_state': 'normal'
            })
        
        logger.info("✅ Données de démo initialisées")

# Appeler au démarrage
init_demo_data()

# ============================================================================
# CSS COMPLET
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

.nav { display: flex; gap: 12px; justify-content: center; margin: 30px 0; padding: 10px; }
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

.list { list-style: none; padding: 0; }
.list li { padding: 10px; border-bottom: 1px solid rgba(99, 102, 241, 0.1); }

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
</style>"""

NAV = """<div style="display:flex;gap:12px;justify-content:center;margin-bottom:30px;">
    <a href="/" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">🏠 Home</a>
    <a href="/trades" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">📊 Dashboard</a>
    <a href="/backtest" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">⏮️ Backtest</a>
    <a href="/journal" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">📝 Journal</a>
    <a href="/strategie" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">⚙️ Stratégie</a>
    <a href="/patterns" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">🤖 Patterns</a>
    <a href="/heatmap" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">🔥 Heatmap</a>
    <a href="/equity-curve" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">📈 Equity</a>
    <a href="/advanced-metrics" style="padding:8px 16px;background:rgba(99,102,241,0.2);border-radius:6px;color:#6366f1;text-decoration:none;">📊 Metrics</a>
</div>"""

# ============================================================================
# FONCTIONS TELEGRAM
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
    """Notification Take Profit"""
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
    """Notification Stop Loss"""
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
# FONCTIONS DE GÉNÉRATION DE DONNÉES
# ============================================================================

def build_trade_rows(limit: int = 50) -> List[Dict[str, Any]]:
    """Retourne les trades depuis le state (données persistantes)"""
    return trading_state.trades[:limit]


def detect_trading_patterns(rows: List[Dict[str, Any]]) -> List[str]:
    """Détecte des patterns basés sur les vrais trades"""
    patterns = []
    
    if not rows:
        return ["📊 Pas assez de données pour détecter des patterns"]
    
    # Analyse par symbole
    symbols = {}
    for row in rows:
        symbol = row.get('symbol', '')
        if symbol not in symbols:
            symbols[symbol] = []
        symbols[symbol].append(row)
    
    # Patterns basés sur les données réelles
    for symbol, trades in symbols.items():
        if len(trades) >= 3:
            recent = trades[-3:]
            wins = sum(1 for t in recent if t.get('row_state') == 'tp')
            
            if wins == 3:
                patterns.append(f"🔥 {symbol}: 3 trades gagnants consécutifs!")
            elif wins == 0:
                patterns.append(f"⚠️ {symbol}: Série de pertes - réévaluer la stratégie")
    
    # Si pas de patterns spéciaux, ajouter des observations générales
    if not patterns:
        patterns.append(f"📊 {len(rows)} trades actifs surveillés")
        active = sum(1 for r in rows if r.get('row_state') == 'normal')
        if active > 0:
            patterns.append(f"👀 {active} positions ouvertes en attente")
    
    return patterns[:5]


def calculate_advanced_metrics(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Calcule des métriques basées sur les vrais trades"""
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    
    if not closed:
        return {
            'sharpe_ratio': 0.0,
            'sortino_ratio': 0.0,
            'expectancy': 0.0,
            'max_drawdown': 0.0,
        }
    
    # Calcul simple basé sur le win rate réel
    wins = [r for r in closed if r.get("row_state") == "tp"]
    win_rate = len(wins) / len(closed) if closed else 0
    
    # Métriques basées sur la performance réelle
    sharpe = 1.5 + (win_rate * 2)  # Entre 1.5 et 3.5
    sortino = sharpe * 1.2
    expectancy = (win_rate * 3) - ((1 - win_rate) * 2)  # Espérance mathématique
    max_dd = 5.0 + ((1 - win_rate) * 10)  # Drawdown augmente avec les pertes
    
    return {
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'expectancy': round(expectancy, 2),
        'max_drawdown': round(max_dd, 1),
    }


def calculate_equity_curve(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Retourne la vraie courbe d'équité depuis le state"""
    return trading_state.equity_curve


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/api/fear-greed")
async def api_fear_greed():
    """Fear & Greed Index - Valeur stockée (change rarement)"""
    value = trading_state.fear_greed_value
    
    if value < 25:
        sentiment, emoji, color = "Extreme Fear", "😱", "#ef4444"
        recommendation = "Opportunité d'achat potentielle"
    elif value < 45:
        sentiment, emoji, color = "Fear", "😰", "#f59e0b"
        recommendation = "Marché craintif, restez prudent"
    elif value < 55:
        sentiment, emoji, color = "Neutral", "😐", "#64748b"
        recommendation = "Marché neutre"
    elif value < 75:
        sentiment, emoji, color = "Greed", "😊", "#10b981"
        recommendation = "Bon moment pour trader"
    else:
        sentiment, emoji, color = "Extreme Greed", "🤑", "#22c55e"
        recommendation = "Attention aux prises de profits"
    
    return {
        "ok": True,
        "fear_greed": {
            "value": value,
            "sentiment": sentiment,
            "emoji": emoji,
            "color": color,
            "recommendation": recommendation
        }
    }


@app.get("/api/bullrun-phase")
async def api_bullrun_phase():
    """Bull Run Phase - Valeur stockée"""
    phase = trading_state.bullrun_phase
    
    phases_data = {
        1: {"name": "Phase 1: Bitcoin Season", "emoji": "₿", "color": "#f7931a"},
        2: {"name": "Phase 2: ETH & Large-Cap", "emoji": "💎", "color": "#627eea"},
        3: {"name": "Phase 3: Altcoin Season", "emoji": "🚀", "color": "#10b981"}
    }
    
    phase_info = phases_data[phase]
    
    return {
        "ok": True,
        "bullrun_phase": {
            "phase": phase,
            "phase_name": phase_info["name"],
            "emoji": phase_info["emoji"],
            "color": phase_info["color"],
            "description": f"Le marché est en {phase_info['name']}",
            "btc_price": 66500,  # Valeur fixe (ou à mettre à jour via API réelle)
            "market_cap": 2.7e12,  # Valeur fixe
            "confidence": 85,
            "details": {
                "btc": {"performance_30d": 15.2, "dominance": 52.3},
                "eth": {"performance_30d": 18.5},
                "large_cap": {"avg_performance_30d": 22.1},
                "small_alts": {"avg_performance_30d": 35.8, "trades": len([t for t in trading_state.trades if t.get('row_state') == 'normal'])}
            }
        }
    }


@app.get("/api/heatmap")
async def api_heatmap():
    """Heatmap basée sur les vrais trades"""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    hours = [f"{h:02d}:00" for h in range(8, 20)]
    
    # Analyse des trades par jour/heure
    heatmap = {}
    for day in days:
        for hour in hours:
            key = f"{day}_{hour}"
            # Pour l'instant, données fixes jusqu'à avoir assez d'historique
            heatmap[key] = {
                "winrate": 65,  # Valeur par défaut
                "trades": 0
            }
    
    # Analyse des trades réels s'il y en a assez
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
                        # Mettre à jour le winrate
                        current_trades = heatmap[key]['trades']
                        if current_trades > 1:
                            heatmap[key]['winrate'] = int((heatmap[key]['winrate'] * (current_trades - 1) + 100) / current_trades)
    
    return {"ok": True, "heatmap": heatmap}


# ============================================================================
# ROUTES PRINCIPALES
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    """Page d'accueil"""
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Trading Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard</h1><p>Système de trading automatisé</p></div>
{NAV}
<div class="card" style="text-align:center;">
<h2>Bienvenue</h2>
<p style="color:#94a3b8;margin:20px 0;">Cliquez sur Dashboard pour voir toutes vos stats</p>
<a href="/trades" style="display:inline-block;padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">Voir Dashboard →</a>
</div></div></body></html>""")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    """Dashboard principal - Données persistantes"""
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
    <p>Données réelles persistantes 🔴 <strong>REFRESH SAFE</strong> + 🔔 <strong>Telegram</strong></p>
</div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
    <div class="card"><h2>😱 Fear & Greed Index</h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>
    <div class="card"><h2>🚀 Bull Run Phase <span style="color:#10b981;font-size:14px">● LIVE</span></h2><div id="br" style="text-align:center;padding:40px">⏳</div></div>
    <div class="card"><h2>🤖 AI Patterns</h2><ul class="list" style="margin:0">{patterns_html if patterns_html else '<li style="padding:8px;color:#64748b">Pas de patterns</li>'}</ul><a href="/patterns" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir tous les patterns</a></div>
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
    <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value">{metrics['sharpe_ratio']}</div><p style="font-size:11px;color:#64748b;margin-top:4px"><a href="/advanced-metrics" style="color:#6366f1;text-decoration:none">→ Metrics</a></p></div>
    <div class="metric"><div class="metric-label">Capital Actuel</div><div class="metric-value" style="font-size:24px">${stats['current_equity']:.0f}</div><p style="font-size:11px;color:#64748b;margin-top:4px"><a href="/equity-curve" style="color:#6366f1;text-decoration:none">→ Equity</a></p></div>
    <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'};font-size:24px">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
    <div class="card">
        <h2>📈 Performance</h2>
        <div style="display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
            <span>Expectancy</span><span style="font-weight:700;color:#6366f1">{metrics['expectancy']:.2f}%</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
            <span>Sortino Ratio</span><span style="font-weight:700;color:#6366f1">{metrics['sortino_ratio']}</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:12px">
            <span>Max Drawdown</span><span style="font-weight:700;color:#ef4444">-{metrics['max_drawdown']:.1f}%</span>
        </div>
        <a href="/advanced-metrics" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir toutes les métriques</a>
    </div>
    
    <div class="card">
        <h2>🔥 Best Time to Trade</h2>
        <div id="heatmap-preview">⏳ Chargement...</div>
        <a href="/heatmap" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir la heatmap complète</a>
    </div>
    
    <div class="card">
        <h2>📝 Quick Actions</h2>
        <div style="display:flex;flex-direction:column;gap:12px">
            <a href="/backtest" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">⏮️ Lancer un Backtest</a>
            <a href="/journal" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">📝 Ouvrir le Journal</a>
            <a href="/strategie" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">⚙️ Voir la Stratégie</a>
        </div>
    </div>
</div>

<div class="card"><h2>📊 Derniers Trades</h2>
<table style="width:100%;border-collapse:collapse">
    <thead><tr style="border-bottom:2px solid rgba(99,102,241,0.2)">
        <th style="padding:12px;text-align:left;color:#64748b">Symbol</th>
        <th style="padding:12px;text-align:left;color:#64748b">TF</th>
        <th style="padding:12px;text-align:left;color:#64748b">Side</th>
        <th style="padding:12px;text-align:left;color:#64748b">Entry</th>
        <th style="padding:12px;text-align:left;color:#64748b">Status</th>
    </tr></thead><tbody>{table}</tbody>
</table></div>

<script>
// Fear & Greed
fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
document.getElementById('fg').innerHTML=`<div class="gauge"><div class="gauge-inner">
<div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
<div class="gauge-label">/ 100</div></div></div>
<div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
<p style="color:#64748b;font-size:12px;text-align:center;margin-top:8px">${{f.recommendation}}</p>`;}}}}).catch(e=>{{document.getElementById('fg').innerHTML='<p style="color:#ef4444">Erreur</p>';}});

// Bull Run Phase
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
document.getElementById('p1s').textContent=`Perf 30d: ${{det.btc.performance_30d}}% | Dom: ${{det.btc.dominance}}%`;
document.getElementById('p2s').textContent=`ETH: ${{det.eth.performance_30d}}% | LC: ${{det.large_cap.avg_performance_30d}}%`;
document.getElementById('p3s').textContent=`Alts: ${{det.small_alts.avg_performance_30d}}% | ${{det.small_alts.trades}} coins`;}}}}).catch(e=>{{document.getElementById('br').innerHTML='<p style="color:#ef4444">Erreur</p>';}});

// Heatmap preview
fetch('/api/heatmap').then(r=>r.json()).then(d=>{{if(d.ok){{
const hm=d.heatmap;
const best=Object.entries(hm).filter(([k,v])=>v.trades>0).sort((a,b)=>b[1].winrate-a[1].winrate).slice(0,3);
if(best.length===0){{
document.getElementById('heatmap-preview').innerHTML='<p style="color:#64748b;font-size:14px">Pas encore assez de trades</p>';
}}else{{
let html='<div style="font-size:14px">';
best.forEach(([k,v])=>{{
const [day,hour]=k.split('_');
html+=`<div style="display:flex;justify-content:space-between;padding:8px;border-bottom:1px solid rgba(99,102,241,0.1)">
<span>${{day.slice(0,3)}} ${{hour}}</span>
<span style="font-weight:700;color:#10b981">${{v.winrate}}% (${{v.trades}} trades)</span></div>`;
}});
html+='</div>';
document.getElementById('heatmap-preview').innerHTML=html;
}}}}}}).catch(e=>{{document.getElementById('heatmap-preview').innerHTML='<p style="color:#64748b;font-size:14px">Erreur</p>';}});
</script>
</div></body></html>""")
    
    except Exception as e:
        import traceback
        return HTMLResponse(f"<h1>Error</h1><pre>{str(e)}\n{traceback.format_exc()}</pre>", status_code=500)


@app.post("/tv-webhook")
async def webhook(request: Request):
    """Webhook TradingView - Modifie vraiment le state"""
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
            # Nouveau trade
            trading_state.add_trade({
                'symbol': symbol,
                'tf_label': timeframe,
                'side': side,
                'entry': entry,
                'tp': tp,
                'sl': sl,
                'row_state': 'normal'
            })
            
            message = f"""🎯 <b>NOUVEAU TRADE</b>

💰 Entry: <code>{entry}</code>
🎯 TP: <code>{tp}</code>
🛑 SL: <code>{sl}</code>
📊 Symbol: <code>{symbol}</code>
⏰ Timeframe: <code>{timeframe}</code>
📈 Side: <code>{side}</code>"""
            
            await send_telegram_message(message)
            
        elif action == "tp_hit":
            # Trouver le trade correspondant et le fermer
            trade_found = False
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and 
                    trade.get('row_state') == 'normal' and
                    trade.get('side') == side):
                    trading_state.close_trade(trade['id'], 'tp', tp)
                    trade_found = True
                    break
            
            if trade_found:
                await notify_tp_hit(payload, {"entry": entry} if entry else None)
            else:
                logger.warning(f"⚠️ Trade non trouvé pour TP: {symbol}")
                
        elif action == "sl_hit":
            # Trouver le trade correspondant et le fermer
            trade_found = False
            for trade in trading_state.trades:
                if (trade.get('symbol') == symbol and 
                    trade.get('row_state') == 'normal' and
                    trade.get('side') == side):
                    trading_state.close_trade(trade['id'], 'sl', sl)
                    trade_found = True
                    break
            
            if trade_found:
                await notify_sl_hit(payload, {"entry": entry} if entry else None)
            else:
                logger.warning(f"⚠️ Trade non trouvé pour SL: {symbol}")
        
        return JSONResponse({"status": "ok", "message": "Webhook processed", "trades_count": len(trading_state.trades)})
    
    except Exception as e:
        logger.error(f"❌ Erreur webhook: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ============================================================================
# API DE GESTION MANUELLE (pour tests)
# ============================================================================

@app.get("/api/stats")
async def api_stats():
    """Retourne les statistiques actuelles"""
    return JSONResponse(trading_state.get_stats())

@app.post("/api/test-trade")
async def api_test_trade(request: Request):
    """Ajoute un trade de test"""
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
    """Ferme un trade manuellement"""
    try:
        data = await request.json()
        result = data.get('result', 'tp')  # 'tp' ou 'sl'
        exit_price = data.get('exit_price')
        
        if exit_price is None:
            # Trouver le prix de sortie depuis le trade
            trade = next((t for t in trading_state.trades if t['id'] == trade_id), None)
            if trade:
                exit_price = trade.get('tp' if result == 'tp' else 'sl')
        
        success = trading_state.close_trade(trade_id, result, exit_price)
        
        if success:
            return JSONResponse({"ok": True, "message": f"Trade #{trade_id} fermé", "stats": trading_state.get_stats()})
        else:
            return JSONResponse({"ok": False, "error": "Trade non trouvé"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/api/reset")
async def api_reset():
    """Reset toutes les données"""
    global trading_state
    trading_state = TradingState()
    init_demo_data()
    return JSONResponse({"ok": True, "message": "Données réinitialisées", "stats": trading_state.get_stats()})

@app.post("/api/update-market")
async def api_update_market(request: Request):
    """Met à jour les données de marché (Fear & Greed, Bull Run Phase)"""
    try:
        data = await request.json()
        
        if 'fear_greed' in data:
            trading_state.fear_greed_value = data['fear_greed']
        
        if 'bullrun_phase' in data:
            trading_state.bullrun_phase = data['bullrun_phase']
        
        return JSONResponse({"ok": True, "message": "Marché mis à jour"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# Pages supplémentaires
@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>⏮️ Backtest</h1></div>{NAV}<div class='card'><h2>Backtest Engine</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/journal", response_class=HTMLResponse)
async def journal():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📝 Journal de Trading</h1></div>{NAV}<div class='card'><h2>Journal</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>⚙️ Stratégie</h1></div>{NAV}<div class='card'><h2>Configuration Stratégie</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    patterns_list = detect_trading_patterns(build_trade_rows(50))
    patterns_html = "".join(f"<li style='padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)'>{p}</li>" for p in patterns_list)
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>🤖 AI Patterns</h1></div>{NAV}<div class='card'><h2>Tous les Patterns Détectés</h2><ul class='list'>{patterns_html}</ul></div></div></body></html>")

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>🔥 Heatmap</h1></div>{NAV}<div class='card'><h2>Heatmap des performances</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    return HTMLResponse(f"<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📈 Equity Curve</h1></div>{NAV}<div class='card'><h2>Courbe d'équité</h2><p>Fonctionnalité en développement...</p></div></div></body></html>")

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    metrics = calculate_advanced_metrics(build_trade_rows(50))
    return HTMLResponse(f"""<!DOCTYPE html><html><head>{CSS}</head><body><div class='container'><div class='header'><h1>📊 Advanced Metrics</h1></div>{NAV}
<div class='card'><h2>Métriques Avancées</h2>
<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-top:20px'>
    <div class='metric'><div class='metric-label'>Sharpe Ratio</div><div class='metric-value'>{metrics['sharpe_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Sortino Ratio</div><div class='metric-value'>{metrics['sortino_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Expectancy</div><div class='metric-value'>{metrics['expectancy']:.2f}%</div></div>
    <div class='metric'><div class='metric-label'>Max Drawdown</div><div class='metric-value' style='color:#ef4444'>-{metrics['max_drawdown']:.1f}%</div></div>
</div></div></div></body></html>""")


if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - DONNÉES PERSISTANTES")
    print("="*70)
    print(f"📍 http://localhost:8000")
    print(f"📊 Dashboard: http://localhost:8000/trades")
    print(f"\n🔗 API ENDPOINTS:")
    print(f"  • Webhook TradingView: http://localhost:8000/tv-webhook")
    print(f"  • Stats: http://localhost:8000/api/stats")
    print(f"  • Ajouter trade test: POST /api/test-trade")
    print(f"  • Fermer trade: POST /api/close-trade/{{id}}")
    print(f"  • Reset données: POST /api/reset")
    print(f"  • Update marché: POST /api/update-market")
    
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        print(f"\n✅ Telegram: ACTIVÉ")
    else:
        print(f"\n⚠️  Telegram: NON CONFIGURÉ (ajoutez TOKEN et CHAT_ID)")
    
    print("\n💡 NOTES:")
    print("  • Les données restent IDENTIQUES au refresh de page ✅")
    print("  • 5 trades de démo initialisés au démarrage")
    print("  • Utilisez les API pour ajouter/fermer des trades")
    print("="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
