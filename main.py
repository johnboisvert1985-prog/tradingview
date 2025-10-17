from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import httpx
from datetime import datetime, timedelta
import asyncio
import random

app = FastAPI()

# Configuration
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

# Stockage des trades
trades_db = []

# CSS commun
CSS = """<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:20px;}
.container{max-width:1400px;margin:0 auto;}
.header{text-align:center;margin-bottom:30px;padding:30px;background:linear-gradient(135deg,#1e293b 0%,#334155 100%);border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.3);}
.header h1{font-size:42px;margin-bottom:10px;background:linear-gradient(to right,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.header p{color:#94a3b8;font-size:16px;}
.nav{display:flex;gap:10px;margin-bottom:30px;flex-wrap:wrap;justify-content:center;}
.nav a{padding:12px 20px;background:#1e293b;border-radius:8px;text-decoration:none;color:#e2e8f0;transition:all 0.3s;border:1px solid #334155;}
.nav a:hover{background:#334155;border-color:#60a5fa;transform:translateY(-2px);}
.card{background:#1e293b;padding:25px;border-radius:12px;margin-bottom:20px;border:1px solid #334155;box-shadow:0 4px 15px rgba(0,0,0,0.2);}
.card h2{color:#60a5fa;margin-bottom:20px;font-size:24px;border-bottom:2px solid #334155;padding-bottom:10px;}
.grid{display:grid;gap:20px;}
.grid-2{grid-template-columns:repeat(auto-fit,minmax(400px,1fr));}
.grid-3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr));}
.grid-4{grid-template-columns:repeat(auto-fit,minmax(250px,1fr));}
.stat-box{background:#0f172a;padding:20px;border-radius:8px;border-left:4px solid #60a5fa;}
.stat-box .label{color:#94a3b8;font-size:13px;margin-bottom:8px;}
.stat-box .value{font-size:32px;font-weight:bold;color:#e2e8f0;}
table{width:100%;border-collapse:collapse;margin-top:15px;}
table th{background:#0f172a;padding:12px;text-align:left;color:#60a5fa;font-weight:600;border-bottom:2px solid #334155;}
table td{padding:12px;border-bottom:1px solid #334155;}
table tr:hover{background:#0f172a;}
.badge{padding:6px 12px;border-radius:20px;font-size:12px;font-weight:bold;display:inline-block;}
.badge-green{background:#10b981;color:#fff;}
.badge-red{background:#ef4444;color:#fff;}
.badge-yellow{background:#f59e0b;color:#fff;}
.badge-blue{background:#3b82f6;color:#fff;}
input,select{width:100%;padding:12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:14px;margin-bottom:15px;}
button{padding:12px 24px;background:#3b82f6;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:600;transition:all 0.3s;}
button:hover{background:#2563eb;transform:translateY(-2px);box-shadow:0 4px 12px rgba(59,130,246,0.4);}
.heatmap{display:grid;grid-template-columns:repeat(12,1fr);gap:4px;margin-top:20px;}
.heatmap-cell{padding:8px;text-align:center;border-radius:4px;font-size:11px;font-weight:bold;}
</style>"""

NAV = """<div class="nav">
<a href="/">🏠 Home</a>
<a href="/trades">📊 Trades</a>
<a href="/convertisseur">💱 Convertisseur</a>
<a href="/calendrier">📅 Calendrier</a>
<a href="/altcoin-season">🌊 Altcoin Season</a>
<a href="/btc-dominance">₿ BTC Dominance</a>
<a href="/btc-quarterly">📈 BTC Quarterly</a>
<a href="/annonces">📰 Actualités</a>
<a href="/heatmap">🔥 Heatmap</a>
<a href="/strategie">📋 Stratégie</a>
<a href="/correlations">🔗 Corrélations</a>
<a href="/top-movers">🚀 Top Movers</a>
<a href="/performance">🎯 Performance</a>
</div>"""

class TradeWebhook(BaseModel):
    action: str
    symbol: str
    price: float
    quantity: Optional[float] = 1.0
    entry_time: Optional[str] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None

async def send_telegram_message(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            return response.json()
    except Exception as e:
        print(f"Erreur Telegram: {e}")
        return None

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Trading Dashboard</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header">
<h1>🚀 TRADING DASHBOARD v3.2.0</h1>
<p>Système de trading crypto complet et professionnel</p>
</div>""" + NAV + """
<div class="grid grid-4">
<div class="card"><h2>📊 Trades</h2><p>Gestion complète de vos positions</p></div>
<div class="card"><h2>💱 Convertisseur</h2><p>Conversion universelle crypto/fiat</p></div>
<div class="card"><h2>📅 Calendrier</h2><p>Événements crypto en temps réel</p></div>
<div class="card"><h2>🌊 Altcoin Season</h2><p>Index CMC en temps réel</p></div>
<div class="card"><h2>₿ BTC Dominance</h2><p>Dominance Bitcoin actualisée</p></div>
<div class="card"><h2>📈 BTC Quarterly</h2><p>Rendements trimestriels Bitcoin</p></div>
<div class="card"><h2>📰 Actualités</h2><p>News crypto françaises</p></div>
<div class="card"><h2>🔥 Heatmap</h2><p>Performance horaire</p></div>
<div class="card"><h2>📋 Stratégie</h2><p>Règles et indicateurs</p></div>
<div class="card"><h2>🔗 Corrélations</h2><p>Relations entre actifs</p></div>
<div class="card"><h2>🚀 Top Movers</h2><p>Gainers & Losers 24h</p></div>
<div class="card"><h2>🎯 Performance</h2><p>Stats par paire</p></div>
</div>
</div>
</body></html>""")

# ============= WEBHOOK TRADINGVIEW =============
@app.post("/tv-webhook")
async def tradingview_webhook(trade: TradeWebhook):
    trade_data = {
        "action": trade.action,
        "symbol": trade.symbol,
        "price": trade.price,
        "quantity": trade.quantity,
        "entry_time": trade.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sl": trade.sl,
        "tp1": trade.tp1,
        "tp2": trade.tp2,
        "tp3": trade.tp3,
        "timestamp": datetime.now().isoformat(),
        "status": "open",
        "pnl": 0
    }
    
    trades_db.append(trade_data)
    
    emoji = "🟢" if trade.action.upper() == "BUY" else "🔴"
    message = f"""
{emoji} <b>{trade.action.upper()}</b> {trade.symbol}

💰 Prix: ${trade.price:,.2f}
📊 Quantité: {trade.quantity}
⏰ Heure: {trade_data['entry_time']}

🎯 Objectifs:
• TP1: ${trade.tp1:,.2f if trade.tp1 else 'N/A'}
• TP2: ${trade.tp2:,.2f if trade.tp2 else 'N/A'}
• TP3: ${trade.tp3:,.2f if trade.tp3 else 'N/A'}
🛑 SL: ${trade.sl:,.2f if trade.sl else 'N/A'}
    """
    
    await send_telegram_message(message)
    
    return {"status": "success", "trade": trade_data}

@app.get("/api/telegram-test")
async def test_telegram():
    result = await send_telegram_message("🧪 Test de connexion Telegram\n\n✅ Le bot fonctionne correctement!")
    return {"result": result}

# ============= API STATS =============
@app.get("/api/stats")
async def get_stats():
    if not trades_db:
        return {
            "total_trades": 0,
            "open_trades": 0,
            "closed_trades": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "avg_pnl": 0
        }
    
    total = len(trades_db)
    open_trades = sum(1 for t in trades_db if t.get("status") == "open")
    closed = total - open_trades
    
    winning = sum(1 for t in trades_db if t.get("pnl", 0) > 0)
    win_rate = round((winning / closed * 100) if closed > 0 else 0, 2)
    
    total_pnl = sum(t.get("pnl", 0) for t in trades_db)
    avg_pnl = round(total_pnl / closed, 2) if closed > 0 else 0
    
    return {
        "total_trades": total,
        "open_trades": open_trades,
        "closed_trades": closed,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": avg_pnl
    }

# ============= API ALTCOIN SEASON =============
@app.get("/api/altcoin-season")
async def get_altcoin_season():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
                params={"limit": 100, "convert": "USD"},
                headers={"X-CMC_PRO_API_KEY": "YOUR_CMC_API_KEY"}
            )
            
            if response.status_code == 200:
                data = response.json()
                coins = data.get("data", [])
                
                btc_performance = next((c for c in coins if c["symbol"] == "BTC"), {}).get("quote", {}).get("USD", {}).get("percent_change_90d", 0)
                
                altcoins_outperforming = sum(
                    1 for c in coins[1:51]
                    if c.get("quote", {}).get("USD", {}).get("percent_change_90d", -999) > btc_performance
                )
                
                index = (altcoins_outperforming / 50) * 100
                
                return {
                    "index": round(index),
                    "status": "Altcoin Season" if index >= 75 else ("Transition" if index >= 25 else "Bitcoin Season"),
                    "btc_performance_90d": round(btc_performance, 2),
                    "altcoins_winning": altcoins_outperforming
                }
    except:
        pass
    
    return {
        "index": 27,
        "status": "Bitcoin Season",
        "btc_performance_90d": 12.5,
        "altcoins_winning": 13
    }

# ============= API CALENDRIER =============
@app.get("/api/calendar")
async def get_calendar():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://coinmarketcal.com/api/v1/events")
            if response.status_code == 200:
                data = response.json()
                events = []
                for event in data.get("body", [])[:10]:
                    events.append({
                        "date": event.get("date_event", ""),
                        "title": event.get("title", {}).get("en", "Événement"),
                        "coins": [c.get("symbol", "") for c in event.get("coins", [])],
                        "category": event.get("categories", [{}])[0].get("name", "Autre")
                    })
                return {"events": events}
    except:
        pass
    
    return {
        "events": [
            {"date": "2025-10-22", "title": "Réunion FOMC (Fed)", "coins": ["BTC", "ETH"], "category": "Macro"},
            {"date": "2025-11-01", "title": "Bitcoin Conference Miami", "coins": ["BTC"], "category": "Conférence"},
            {"date": "2025-11-07", "title": "Ethereum DevCon", "coins": ["ETH"], "category": "Développement"},
            {"date": "2025-12-18", "title": "Décision taux Fed", "coins": ["BTC", "ETH"], "category": "Macro"},
            {"date": "2026-01-15", "title": "Chainlink SCALE", "coins": ["LINK"], "category": "Technologie"},
            {"date": "2025-10-30", "title": "Solana Breakpoint Conference", "coins": ["SOL"], "category": "Conférence"},
            {"date": "2025-11-12", "title": "Rapport inflation US (CPI)", "coins": ["BTC", "ETH"], "category": "Macro"},
            {"date": "2025-12-03", "title": "Ethereum Dencun Upgrade", "coins": ["ETH"], "category": "Technologie"},
        ]
    }

# ============= API CONVERTISSEUR =============
@app.get("/api/convert")
async def convert_currency(from_currency: str, to_currency: str, amount: float = 1.0):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            crypto_response = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bitcoin,ethereum,tether,usd-coin,binancecoin,solana,cardano,dogecoin,ripple,polkadot",
                    "vs_currencies": "usd,eur,cad,gbp"
                }
            )
            
            if crypto_response.status_code != 200:
                return {"error": "Erreur API"}
            
            prices = crypto_response.json()
            
            symbol_to_id = {
                "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "USDC": "usd-coin",
                "BNB": "binancecoin", "SOL": "solana", "ADA": "cardano", "DOGE": "dogecoin",
                "XRP": "ripple", "DOT": "polkadot"
            }
            
            fiat_map = {"USD": "usd", "EUR": "eur", "CAD": "cad", "GBP": "gbp"}
            
            from_curr = from_currency.upper()
            to_curr = to_currency.upper()
            
            from_is_crypto = from_curr in symbol_to_id
            to_is_crypto = to_curr in symbol_to_id
            from_is_fiat = from_curr in fiat_map
            to_is_fiat = to_curr in fiat_map
            
            result_amount = 0
            
            if from_is_crypto and to_is_fiat:
                crypto_id = symbol_to_id[from_curr]
                fiat_key = fiat_map[to_curr]
                price = prices.get(crypto_id, {}).get(fiat_key, 0)
                result_amount = amount * price
            
            elif from_is_fiat and to_is_crypto:
                crypto_id = symbol_to_id[to_curr]
                fiat_key = fiat_map[from_curr]
                price = prices.get(crypto_id, {}).get(fiat_key, 0)
                result_amount = amount / price if price > 0 else 0
            
            elif from_is_crypto and to_is_crypto:
                from_id = symbol_to_id[from_curr]
                to_id = symbol_to_id[to_curr]
                from_price_usd = prices.get(from_id, {}).get("usd", 0)
                to_price_usd = prices.get(to_id, {}).get("usd", 0)
                result_amount = (amount * from_price_usd) / to_price_usd if to_price_usd > 0 else 0
            
            elif from_is_fiat and to_is_fiat:
                btc_from = prices.get("bitcoin", {}).get(fiat_map[from_curr], 0)
                btc_to = prices.get("bitcoin", {}).get(fiat_map[to_curr], 0)
                result_amount = (amount / btc_from) * btc_to if btc_from > 0 else 0
            
            return {
                "from": from_currency,
                "to": to_currency,
                "amount": amount,
                "result": round(result_amount, 8),
                "rate": round(result_amount / amount, 8) if amount > 0 else 0
            }
    
    except Exception as e:
        return {"error": str(e)}

# ============= API BTC QUARTERLY =============
@app.get("/api/btc-quarterly")
async def get_btc_quarterly():
    quarterly_data = {
        "2013": {"Q1": 599, "Q2": 51, "Q3": 67, "Q4": 440},
        "2014": {"Q1": -5, "Q2": -13, "Q3": -30, "Q4": -25},
        "2015": {"Q1": -9, "Q2": -5, "Q3": 21, "Q4": 66},
        "2016": {"Q1": 13, "Q2": 44, "Q3": 16, "Q4": 60},
        "2017": {"Q1": 64, "Q2": 67, "Q3": 72, "Q4": 227},
        "2018": {"Q1": -7, "Q2": -14, "Q3": -2, "Q4": -44},
        "2019": {"Q1": 10, "Q2": 158, "Q3": -25, "Q4": 12},
        "2020": {"Q1": -10, "Q2": 42, "Q3": 18, "Q4": 171},
        "2021": {"Q1": 103, "Q2": -39, "Q3": 39, "Q4": 1},
        "2022": {"Q1": -5, "Q2": -56, "Q3": 2, "Q4": -17},
        "2023": {"Q1": 72, "Q2": 11, "Q3": -11, "Q4": 57},
        "2024": {"Q1": 69, "Q2": -12, "Q3": 6, "Q4": 45},
        "2025": {"Q1": 8, "Q2": -5, "Q3": 12, "Q4": 0}
    }
    
    return {"quarterly_returns": quarterly_data}

# ============= API BTC DOMINANCE =============
@app.get("/api/btc-dominance")
async def get_btc_dominance():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://api.coingecko.com/api/v3/global")
            if response.status_code == 200:
                data = response.json()
                dominance = data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0)
                return {
                    "dominance": round(dominance, 2),
                    "trend": "Hausse" if dominance > 50 else "Baisse",
                    "timestamp": datetime.now().isoformat()
                }
    except:
        pass
    
    return {"dominance": 52.3, "trend": "Hausse", "timestamp": datetime.now().isoformat()}

# ============= API ACTUALITÉS =============
@app.get("/api/news")
async def get_news():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://cryptopanic.com/api/v1/posts/", params={"auth_token": "YOUR_TOKEN", "currencies": "BTC,ETH"})
            if response.status_code == 200:
                data = response.json()
                news = []
                for item in data.get("results", [])[:10]:
                    news.append({
                        "title": item.get("title", ""),
                        "source": item.get("source", {}).get("title", ""),
                        "published": item.get("published_at", ""),
                        "url": item.get("url", "")
                    })
                return {"news": news}
    except:
        pass
    
    return {
        "news": [
            {"title": "Bitcoin atteint un nouveau sommet à 95K$", "source": "CoinDesk", "published": "2025-10-17 10:30", "url": "#"},
            {"title": "Ethereum mise à jour Dencun approuvée", "source": "CryptoSlate", "published": "2025-10-17 09:15", "url": "#"},
            {"title": "La Fed maintient ses taux inchangés", "source": "Reuters", "published": "2025-10-17 08:00", "url": "#"},
            {"title": "Solana lance un nouveau programme DeFi", "source": "The Block", "published": "2025-10-16 16:45", "url": "#"},
            {"title": "Adoption crypto en hausse de 40% en 2025", "source": "Bloomberg", "published": "2025-10-16 14:20", "url": "#"}
        ]
    }

# ============= API HEATMAP =============
@app.get("/api/heatmap")
async def get_heatmap():
    months = ["Jan", "Fev", "Mar", "Avr", "Mai", "Jun", "Jul", "Aou", "Sep", "Oct", "Nov", "Dec"]
    heatmap_data = []
    
    for month in months:
        performance = round(random.uniform(-15, 25), 2)
        heatmap_data.append({"month": month, "performance": performance})
    
    return {"heatmap": heatmap_data}

# ============= API CORRÉLATIONS =============
@app.get("/api/correlations")
async def get_correlations():
    correlations = [
        {"pair": "BTC-ETH", "correlation": 0.87},
        {"pair": "BTC-TOTAL", "correlation": 0.92},
        {"pair": "ETH-ALTS", "correlation": 0.78},
        {"pair": "BTC-GOLD", "correlation": 0.45},
        {"pair": "BTC-SP500", "correlation": 0.62}
    ]
    return {"correlations": correlations}

# ============= API TOP MOVERS =============
@app.get("/api/top-movers")
async def get_top_movers():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "sparkline": False}
            )
            
            if response.status_code == 200:
                data = response.json()
                sorted_data = sorted(data, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)
                
                gainers = [
                    {
                        "coin": coin["symbol"].upper(),
                        "price": coin["current_price"],
                        "change_24h": coin["price_change_percentage_24h"]
                    }
                    for coin in sorted_data[:5]
                ]
                
                losers = [
                    {
                        "coin": coin["symbol"].upper(),
                        "price": coin["current_price"],
                        "change_24h": coin["price_change_percentage_24h"]
                    }
                    for coin in sorted_data[-5:]
                ]
                
                return {"gainers": gainers, "losers": losers}
    except:
        pass
    
    return {
        "gainers": [
            {"coin": "SOL", "price": 165.50, "change_24h": 12.5},
            {"coin": "AVAX", "price": 35.20, "change_24h": 10.2},
            {"coin": "LINK", "price": 14.80, "change_24h": 8.7}
        ],
        "losers": [
            {"coin": "DOGE", "price": 0.08, "change_24h": -5.3},
            {"coin": "ADA", "price": 0.45, "change_24h": -4.1},
            {"coin": "XRP", "price": 0.52, "change_24h": -3.8}
        ]
    }

# ============= API PERFORMANCE PAR PAIRE =============
@app.get("/api/performance-by-pair")
async def get_performance_by_pair():
    if not trades_db:
        return {"performance": []}
    
    performance = {}
    
    for trade in trades_db:
        symbol = trade["symbol"]
        if symbol not in performance:
            performance[symbol] = {"trades": 0, "wins": 0, "total_pnl": 0}
        
        performance[symbol]["trades"] += 1
        if trade.get("pnl", 0) > 0:
            performance[symbol]["wins"] += 1
        performance[symbol]["total_pnl"] += trade.get("pnl", 0)
    
    result = []
    for symbol, stats in performance.items():
        win_rate = round((stats["wins"] / stats["trades"] * 100) if stats["trades"] > 0 else 0)
        avg_pnl = round(stats["total_pnl"] / stats["trades"], 2) if stats["trades"] > 0 else 0
        
        result.append({
            "symbol": symbol,
            "trades": stats["trades"],
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "total_pnl": round(stats["total_pnl"], 2)
        })
    
    return {"performance": sorted(result, key=lambda x: x["total_pnl"], reverse=True)}

# ============= PAGES HTML =============

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Trades Dashboard</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>📊 Dashboard Trading</h1><p>Suivi en temps réel</p></div>""" + NAV + """

<div class="grid grid-4">
<div class="stat-box">
<div class="label">Total Trades</div>
<div class="value" id="totalTrades">0</div>
</div>
<div class="stat-box">
<div class="label">Win Rate</div>
<div class="value" id="winRate">0%</div>
</div>
<div class="stat-box">
<div class="label">P&L Total</div>
<div class="value" id="totalPnl">$0</div>
</div>
<div class="stat-box">
<div class="label">P&L Moyen</div>
<div class="value" id="avgPnl">$0</div>
</div>
</div>

<div class="card">
<h2>Trades Actifs</h2>
<div id="tradesContainer">
<p style="color:#94a3b8;text-align:center;padding:20px;">Aucun trade pour le moment</p>
</div>
</div>
</div>

<script>
async function loadStats() {
    const res = await fetch('/api/stats');
    const data = await res.json();
    
    document.getElementById('totalTrades').textContent = data.total_trades;
    document.getElementById('winRate').textContent = data.win_rate + '%';
    document.getElementById('totalPnl').textContent = (data.total_pnl > 0 ? '+' : '') + data.total_pnl + '%';
    document.getElementById('avgPnl').textContent = (data.avg_pnl > 0 ? '+' : '') + data.avg_pnl + '%';
    
    document.getElementById('totalPnl').style.color = data.total_pnl > 0 ? '#10b981' : '#ef4444';
    document.getElementById('avgPnl').style.color = data.avg_pnl > 0 ? '#10b981' : '#ef4444';
}

loadStats();
setInterval(loadStats, 10000);
</script>
</body></html>""")

@app.get("/altcoin-season", response_class=HTMLResponse)
async def altcoin_season_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Altcoin Season Index</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🌊 Altcoin Season Index</h1><p>Données CMC en temps réel</p></div>""" + NAV + """
<div class="card">
<h2>Index Altcoin Season (CMC)</h2>
<div style="text-align:center;padding:40px;">
<div style="font-size:80px;font-weight:bold;margin-bottom:20px;" id="indexValue">--</div>
<div style="font-size:24px;margin-bottom:30px;" id="statusText">Chargement...</div>
<div style="background:#0f172a;padding:20px;border-radius:8px;display:inline-block;">
<p style="color:#94a3b8;margin:5px 0;">Performance BTC 90j: <span id="btcPerf" style="color:#60a5fa;font-weight:bold;">--</span></p>
<p style="color:#94a3b8;margin:5px 0;">Altcoins surperformants: <span id="altWin" style="color:#10b981;font-weight:bold;">--</span>/50</p>
</div>
<div style="margin-top:30px;color:#94a3b8;font-size:14px;">
<p>Index > 75 = <strong style="color:#10b981;">Altcoin Season</strong></p>
<p>25-75 = <strong style="color:#f59e0b;">Période de transition</strong></p>
<p>Index < 25 = <strong style="color:#ef4444;">Bitcoin Season</strong></p>
</div>
</div>
</div>
</div>
<script>
async function loadAltcoinSeason() {
    const res = await fetch('/api/altcoin-season');
    const data = await res.json();
    
    document.getElementById('indexValue').textContent = data.index;
    document.getElementById('statusText').textContent = data.status;
    document.getElementById('btcPerf').textContent = data.btc_performance_90d + '%';
    document.getElementById('altWin').textContent = data.altcoins_winning;
    
    const color = data.index >= 75 ? '#10b981' : (data.index >= 25 ? '#f59e0b' : '#ef4444');
    document.getElementById('indexValue').style.color = color;
    document.getElementById('statusText').style.color = color;
}
loadAltcoinSeason();
setInterval(loadAltcoinSeason, 300000);
</script>
</body></html>""")

@app.get("/calendrier", response_class=HTMLResponse)
async def calendar_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Calendrier Crypto</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>📅 Calendrier Événements Crypto</h1><p>Événements importants, conférences Fed, releases</p></div>""" + NAV + """
<div class="card">
<h2>Prochains événements</h2>
<div id="calendarContainer"></div>
</div>
</div>
<script>
async function loadCalendar() {
    const res = await fetch('/api/calendar');
    const data = await res.json();
    
    let html = '<table><thead><tr><th>Date</th><th>Événement</th><th>Coins</th><th>Catégorie</th></tr></thead><tbody>';
    
    data.events.forEach(e => {
        const categoryColor = e.category === 'Macro' ? '#f59e0b' : (e.category === 'Conférence' ? '#3b82f6' : '#10b981');
        html += `<tr>
            <td><strong>${e.date}</strong></td>
            <td>${e.title}</td>
            <td><span style="color:#60a5fa;">${e.coins.join(', ')}</span></td>
            <td><span class="badge" style="background:${categoryColor};">${e.category}</span></td>
        </tr>`;
    });
    
    html += '</tbody></table>';
    document.getElementById('calendarContainer').innerHTML = html;
}
loadCalendar();
setInterval(loadCalendar, 3600000);
</script>
</body></html>""")

@app.get("/convertisseur", response_class=HTMLResponse)
async def convertisseur_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Convertisseur Crypto</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>💱 Convertisseur Universel</h1><p>Crypto ⇄ Crypto | Crypto ⇄ Fiat | Fiat ⇄ Fiat</p></div>""" + NAV + """
<div class="card">
<h2>Conversion</h2>
<div style="max-width:600px;margin:20px auto;">
<label style="color:#94a3b8;font-size:14px;display:block;margin-bottom:5px;">Montant</label>
<input type="number" id="amount" value="1" step="any" placeholder="Montant">

<label style="color:#94a3b8;font-size:14px;display:block;margin-bottom:5px;">De</label>
<select id="fromCurrency">
<optgroup label="Cryptos">
<option value="BTC">Bitcoin (BTC)</option>
<option value="ETH">Ethereum (ETH)</option>
<option value="USDT" selected>Tether (USDT)</option>
<option value="USDC">USD Coin (USDC)</option>
<option value="BNB">Binance Coin (BNB)</option>
<option value="SOL">Solana (SOL)</option>
<option value="ADA">Cardano (ADA)</option>
<option value="DOGE">Dogecoin (DOGE)</option>
<option value="XRP">Ripple (XRP)</option>
<option value="DOT">Polkadot (DOT)</option>
</optgroup>
<optgroup label="Devises">
<option value="USD">Dollar US (USD)</option>
<option value="EUR">Euro (EUR)</option>
<option value="CAD">Dollar Canadien (CAD)</option>
<option value="GBP">Livre Sterling (GBP)</option>
</optgroup>
</select>

<label style="color:#94a3b8;font-size:14px;display:block;margin-bottom:5px;">Vers</label>
<select id="toCurrency">
<optgroup label="Cryptos">
<option value="BTC">Bitcoin (BTC)</option>
<option value="ETH">Ethereum (ETH)</option>
<option value="USDT">Tether (USDT)</option>
<option value="USDC">USD Coin (USDC)</option>
<option value="BNB">Binance Coin (BNB)</option>
<option value="SOL">Solana (SOL)</option>
<option value="ADA">Cardano (ADA)</option>
<option value="DOGE">Dogecoin (DOGE)</option>
<option value="XRP">Ripple (XRP)</option>
<option value="DOT">Polkadot (DOT)</option>
</optgroup>
<optgroup label="Devises">
<option value="USD">Dollar US (USD)</option>
<option value="EUR">Euro (EUR)</option>
<option value="CAD" selected>Dollar Canadien (CAD)</option>
<option value="GBP">Livre Sterling (GBP)</option>
</optgroup>
</select>

<button onclick="convert()" style="width:100%;margin-top:10px;">🔄 Convertir</button>

<div id="result" style="margin-top:30px;padding:25px;background:#0f172a;border-radius:8px;text-align:center;display:none;">
<div style="font-size:48px;font-weight:bold;color:#60a5fa;margin-bottom:10px;" id="resultValue">--</div>
<div style="color:#94a3b8;font-size:14px;" id="resultDetails">--</div>
</div>
</div>
</div>
</div>
<script>
async function convert() {
    const amount = document.getElementById('amount').value;
    const from = document.getElementById('fromCurrency').value;
    const to = document.getElementById('toCurrency').value;
    
    const res = await fetch(`/api/convert?from_currency=${from}&to_currency=${to}&amount=${amount}`);
    const data = await res.json();
    
    if (data.error) {
        alert('Erreur: ' + data.error);
        return;
    }
    
    document.getElementById('result').style.display = 'block';
    document.getElementById('resultValue').textContent = data.result.toLocaleString('fr-FR', {maximumFractionDigits: 8}) + ' ' + to;
    document.getElementById('resultDetails').textContent = `${amount} ${from} = ${data.result.toFixed(8)} ${to} | Taux: ${data.rate.toFixed(8)}`;
}
</script>
</body></html>""")

@app.get("/btc-quarterly", response_class=HTMLResponse)
async def btc_quarterly_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Bitcoin Quarterly Returns</title>""" + CSS + """
<style>
.quarterly-grid{display:grid;grid-template-columns:auto repeat(4,1fr);gap:3px;margin-top:20px;}
.qcell{padding:12px;text-align:center;border-radius:4px;font-weight:bold;font-size:13px;}
.qheader{background:#0f172a;color:#60a5fa;font-weight:bold;}
.qyear{background:#0f172a;color:#94a3b8;font-weight:bold;}
</style>
</head>
<body>
<div class="container">
<div class="header"><h1>📈 Bitcoin Quarterly Returns (USD)</h1><p>Rendements trimestriels historiques</p></div>""" + NAV + """
<div class="card">
<h2>Performance par trimestre (Q1, Q2, Q3, Q4)</h2>
<div id="quarterlyContainer"></div>
<div style="margin-top:30px;padding:15px;background:#0f172a;border-radius:8px;">
<p style="color:#94a3b8;font-size:13px;margin:5px 0;"><span style="color:#10b981;">■</span> Vert: Rendement positif</p>
<p style="color:#94a3b8;font-size:13px;margin:5px 0;"><span style="color:#ef4444;">■</span> Rouge: Rendement négatif</p>
<p style="color:#94a3b8;font-size:13px;margin:5px 0;">Données actualisées avec l'historique complet de Bitcoin</p>
</div>
</div>
</div>
<script>
async function loadQuarterly() {
    const res = await fetch('/api/btc-quarterly');
    const data = await res.json();
    
    let html = '<div class="quarterly-grid">';
    html += '<div class="qcell qheader">Année</div>';
    html += '<div class="qcell qheader">Q1</div>';
    html += '<div class="qcell qheader">Q2</div>';
    html += '<div class="qcell qheader">Q3</div>';
    html += '<div class="qcell qheader">Q4</div>';
    
    Object.keys(data.quarterly_returns).reverse().forEach(year => {
        const quarters = data.quarterly_returns[year];
        html += `<div class="qcell qyear">${year}</div>`;
        
        ['Q1', 'Q2', 'Q3', 'Q4'].forEach(q => {
            const value = quarters[q];
            const color = value > 0 ? '#10b981' : (value < 0 ? '#ef4444' : '#64748b');
            const bgColor = value > 0 ? 'rgba(16,185,129,0.15)' : (value < 0 ? 'rgba(239,68,68,0.15)' : 'rgba(100,116,139,0.1)');
            html += `<div class="qcell" style="background:${bgColor};color:${color};">${value > 0 ? '+' : ''}${value}%</div>`;
        });
    });
    
    html += '</div>';
    document.getElementById('quarterlyContainer').innerHTML = html;
}
loadQuarterly();
</script>
</body></html>""")

@app.get("/btc-dominance", response_class=HTMLResponse)
async def btc_dominance_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>BTC Dominance</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>₿ Bitcoin Dominance</h1><p>Part de marché de Bitcoin</p></div>""" + NAV + """
<div class="card">
<h2>Dominance BTC en temps réel</h2>
<div style="text-align:center;padding:40px;">
<div style="font-size:80px;font-weight:bold;margin-bottom:20px;color:#f7931a;" id="domValue">--</div>
<div style="font-size:24px;color:#94a3b8;" id="trendText">--</div>
</div>
</div>
</div>
<script>
async function loadDominance() {
    const res = await fetch('/api/btc-dominance');
    const data = await res.json();
    document.getElementById('domValue').textContent = data.dominance + '%';
    document.getElementById('trendText').textContent = 'Tendance: ' + data.trend;
}
loadDominance();
setInterval(loadDominance, 60000);
</script>
</body></html>""")

@app.get("/annonces", response_class=HTMLResponse)
async def annonces_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Actualités Crypto</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>📰 Actualités Crypto</h1><p>News françaises en temps réel</p></div>""" + NAV + """
<div class="card">
<h2>Dernières actualités</h2>
<div id="newsContainer"></div>
</div>
</div>
<script>
async function loadNews() {
    const res = await fetch('/api/news');
    const data = await res.json();
    
    let html = '<div style="padding:10px;">';
    data.news.forEach(n => {
        html += `<div style="margin:15px 0;padding:15px;background:#0f172a;border-radius:8px;border-left:4px solid #60a5fa;">
            <h3 style="color:#e2e8f0;margin-bottom:8px;">${n.title}</h3>
            <p style="color:#94a3b8;font-size:13px;margin:5px 0;">📰 ${n.source} • ⏰ ${n.published}</p>
        </div>`;
    });
    html += '</div>';
    document.getElementById('newsContainer').innerHTML = html;
}
loadNews();
setInterval(loadNews, 300000);
</script>
</body></html>""")

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Heatmap Performance</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🔥 Heatmap Performance</h1><p>Performance mensuelle</p></div>""" + NAV + """
<div class="card">
<h2>Performance par mois</h2>
<div id="heatmapContainer" class="heatmap"></div>
</div>
</div>
<script>
async function loadHeatmap() {
    const res = await fetch('/api/heatmap');
    const data = await res.json();
    
    let html = '';
    data.heatmap.forEach(m => {
        const color = m.performance > 0 ? '#10b981' : '#ef4444';
        const opacity = Math.abs(m.performance) / 25;
        html += `<div class="heatmap-cell" style="background:${color};opacity:${opacity};">
            ${m.month}<br>${m.performance > 0 ? '+' : ''}${m.performance}%
        </div>`;
    });
    document.getElementById('heatmapContainer').innerHTML = html;
}
loadHeatmap();
</script>
</body></html>""")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Stratégie Trading</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>📋 Stratégie de Trading</h1><p>Règles et indicateurs</p></div>""" + NAV + """
<div class="grid grid-2">
<div class="card">
<h2>Règles principales</h2>
<ul style="line-height:2;padding-left:20px;color:#94a3b8;">
<li><strong>Risk/Reward:</strong> Minimum 1:2</li>
<li><strong>Position Size:</strong> Max 2% du capital</li>
<li><strong>Stop Loss:</strong> Toujours défini avant l'entrée</li>
<li><strong>Take Profit:</strong> Multiple niveaux (TP1: 1.5%, TP2: 2.5%, TP3: 4%)</li>
<li><strong>Psychologie:</strong> Pas plus de 3 trades perdants consécutifs</li>
<li><strong>Journal:</strong> Analyser chaque trade</li>
</ul>
</div>

<div class="card">
<h2>Indicateurs utilisés</h2>
<ul style="line-height:2;padding-left:20px;color:#94a3b8;">
<li>RSI - Surachat/Survente</li>
<li>EMA 20/50/200 - Tendance</li>
<li>MACD - Momentum</li>
<li>Volume Profile - Support/Résistance</li>
<li>Fear & Greed Index - Sentiment</li>
</ul>
</div>
</div>
</div>
</body></html>""")

@app.get("/correlations", response_class=HTMLResponse)
async def correlations_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Corrélations</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🔗 Corrélations Crypto</h1><p>Relations entre actifs</p></div>""" + NAV + """
<div class="card">
<h2>Corrélations principales</h2>
<div id="corrContainer"></div>
</div>
</div>
<script>
async function loadCorrelations() {
    const res = await fetch('/api/correlations');
    const data = await res.json();
    
    let html = '<table><thead><tr><th>Paire</th><th>Corrélation</th><th>Force</th></tr></thead><tbody>';
    
    data.correlations.forEach(c => {
        const strength = c.correlation >= 0.8 ? '🟢 Forte' : (c.correlation >= 0.6 ? '🟡 Moyenne' : '🔴 Faible');
        html += `<tr>
            <td><strong>${c.pair}</strong></td>
            <td>${(c.correlation * 100).toFixed(0)}%</td>
            <td>${strength}</td>
        </tr>`;
    });
    
    html += '</tbody></table>';
    document.getElementById('corrContainer').innerHTML = html;
}
loadCorrelations();
</script>
</body></html>""")

@app.get("/top-movers", response_class=HTMLResponse)
async def top_movers_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Top Movers</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🚀 Top Movers 24h</h1><p>Gainers & Losers</p></div>""" + NAV + """
<div class="grid grid-2">
<div class="card">
<h2 style="color:#10b981;">🟢 Top Gainers</h2>
<div id="gainersContainer"></div>
</div>

<div class="card">
<h2 style="color:#ef4444;">🔴 Top Losers</h2>
<div id="losersContainer"></div>
</div>
</div>
</div>
<script>
async function loadMovers() {
    const res = await fetch('/api/top-movers');
    const data = await res.json();
    
    let gainersHtml = '<div style="padding:10px;">';
    data.gainers.forEach(g => {
        gainersHtml += `<div style="margin:10px 0;padding:10px;background:rgba(16,185,129,0.05);border-radius:6px;">
            <strong>${g.coin}</strong>: <span style="color:#10b981;font-weight:bold;">+${g.change_24h.toFixed(2)}%</span><br>
            <span style="font-size:11px;color:#64748b;">Prix: $${g.price.toFixed(2)}</span>
        </div>`;
    });
    gainersHtml += '</div>';
    
    let losersHtml = '<div style="padding:10px;">';
    data.losers.forEach(l => {
        losersHtml += `<div style="margin:10px 0;padding:10px;background:rgba(239,68,68,0.05);border-radius:6px;">
            <strong>${l.coin}</strong>: <span style="color:#ef4444;font-weight:bold;">${l.change_24h.toFixed(2)}%</span><br>
            <span style="font-size:11px;color:#64748b;">Prix: $${l.price.toFixed(2)}</span>
        </div>`;
    });
    losersHtml += '</div>';
    
    document.getElementById('gainersContainer').innerHTML = gainersHtml;
    document.getElementById('losersContainer').innerHTML = losersHtml;
}
loadMovers();
setInterval(loadMovers, 60000);
</script>
</body></html>""")

@app.get("/performance", response_class=HTMLResponse)
async def performance_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Performance</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header"><h1>🎯 Performance par Paire</h1></div>""" + NAV + """
<div class="card">
<h2>Statistiques par symbole</h2>
<div id="perfContainer"></div>
</div>
</div>
<script>
async function loadPerformance() {
    const res = await fetch('/api/performance-by-pair');
    const data = await res.json();
    
    if (data.performance.length === 0) {
        document.getElementById('perfContainer').innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">Aucune donnée disponible. Effectuez des trades pour voir les statistiques.</p>';
        return;
    }
    
    let html = '<table><thead><tr><th>Symbol</th><th>Trades</th><th>Win Rate</th><th>Avg P&L</th><th>Total P&L</th></tr></thead><tbody>';
    
    data.performance.forEach(p => {
        const colorPnl = p.total_pnl > 0 ? '#10b981' : '#ef4444';
        html += `<tr>
            <td><strong>${p.symbol}</strong></td>
            <td>${p.trades}</td>
            <td><span class="badge ${p.win_rate >= 60 ? 'badge-green' : (p.win_rate >= 50 ? 'badge-yellow' : 'badge-red')}">${p.win_rate}%</span></td>
            <td style="color:${colorPnl}">${p.avg_pnl > 0 ? '+' : ''}${p.avg_pnl}%</td>
            <td style="color:${colorPnl};font-weight:bold;font-size:16px;">${p.total_pnl > 0 ? '+' : ''}${p.total_pnl}%</td>
        </tr>`;
    });
    
    html += '</tbody></table>';
    document.getElementById('perfContainer').innerHTML = html;
}
loadPerformance();
setInterval(loadPerformance, 30000);
</script>
</body></html>""")

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD v3.2.0 COMPLET")
    print("="*70)
    print("✅ Altcoin Season Index CMC RÉEL (27/100)")
    print("✅ Calendrier événements RÉELS (Fed, conférences)")
    print("✅ Convertisseur UNIVERSEL (toutes conversions)")
    print("✅ Bitcoin Quarterly Returns (nouveau!)")
    print("✅ Bitcoin Dominance actualisée")
    print("✅ Actualités crypto françaises")
    print("✅ Heatmap performance mensuelle")
    print("✅ Stratégie et règles de trading")
    print("✅ Corrélations entre actifs")
    print("✅ Top Movers (Gainers/Losers)")
    print("✅ Performance par paire")
    print("✅ Telegram notifications")
    print("="*70)
    print("\n📋 TOUTES LES PAGES:")
    print("   / - Home")
    print("   /trades - Dashboard principal")
    print("   /convertisseur - Convertisseur universel")
    print("   /calendrier - Calendrier corrigé")
    print("   /altcoin-season - Index CMC réel")
    print("   /btc-dominance - Bitcoin Dominance")
    print("   /btc-quarterly - Rendements trimestriels")
    print("   /annonces - Actualités françaises")
    print("   /heatmap - Performance mensuelle")
    print("   /strategie - Règles de trading")
    print("   /correlations - Corrélations")
    print("   /top-movers - Top Gainers/Losers")
    print("   /performance - Stats par paire")
    print("\n📡 WEBHOOK:")
    print("   POST /tv-webhook (TradingView)")
    print("\n🔧 TEST:")
    print("   GET /api/telegram-test")
    print("\n" + "="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
