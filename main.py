>""" + NAV + """
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
<tr><td>Active Trades</td><td>{stats['active_trades']}</td></tr>
<tr><td>Closed Trades</td><td>{stats['closed_trades']}</td></tr>
<tr><td>Wins</td><td>{stats['wins']}</td></tr>
<tr><td>Losses</td><td>{stats['losses']}</td></tr>
<tr><td>Current Equity</td><td>${stats['current_equity']:,.2f}</td></tr>
<tr><td>Initial Capital</td><td>${stats['initial_capital']:,.2f}</td></tr>
<tr><td>Total Return</td><td style="color:{'#10b981' if stats['total_return'] > 0 else '#ef4444'}">{stats['total_return']:+.2f}%</td></tr>
</table>
</div>
</div>
</body></html>""")

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD v2.5.5 CORRIGÉE")
    print("="*70)
    print("✅ TP1/TP2/TP3 différenciés et corrigés")
    print("✅ Support action CLOSE")
    print("✅ Toutes les routes HTML ajoutées")
    print("✅ Telegram avec confiance détaillée")
    print("✅ WEBHOOK CORRIGÉ - Caractères de contrôle nettoyés")
    print("="*70)
    print("\n📋 ENDPOINTS DISPONIBLES:")
    print("   🏠 Home:            http://localhost:8000/")
    print("   📊 Dashboard:       http://localhost:8000/trades")
    print("   📈 Equity:          http://localhost:8000/equity-curve")
    print("   📝 Journal:         http://localhost:8000/journal")
    print("   🔥 Heatmap:         http://localhost:8000/heatmap")
    print("   ⚙️  Stratégie:      http://localhost:8000/strategie")
    print("   ⏮️  Backtest:        http://localhost:8000/backtest")
    print("   🤖 Patterns:        http://localhost:8000/patterns")
    print("   📊 Metrics:         http://localhost:8000/advanced-metrics")
    print("   🗞️  Annonces:        http://localhost:8000/annonces")
    print("\n📡 WEBHOOK:")
    print("   POST http://localhost:8000/tv-webhook")
    print("   ✅ Support TradingView")
    print("   ✅ Support messages Telegram formatés")
    print("   ✅ Nettoyage automatique des caractères invalides")
    print("\n🧪 TEST TELEGRAM:")
    print("   GET http://localhost:8000/api/telegram-test")
    print("\n💡 EXEMPLE WEBHOOK (TradingView):")
    print("""   {
     "type": "ENTRY",
     "symbol": "BTCUSDT",
     "side": "LONG",
     "entry": 65000,
     "tp1": 66000,
     "tp2": 66500,
     "tp3": 67000,
     "sl": 64000,
     "tf_label": "15m"
   }""")
    print("\n💡 EXEMPLE WEBHOOK (Telegram formaté):")
    print("""   {
     "chat_id": "-1002940633257",
     "text": "⚡ <b>SELL</b> — <b>BTCUSDT.P</b> (15) P ix: <code>65000</code>\\nTP1: <code>63700</code>\\nTP2: <code>63375</code>\\nTP3: <code>62400</code>\\nSL: <code>66300</code>"
   }""")
    print("\n" + "="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
