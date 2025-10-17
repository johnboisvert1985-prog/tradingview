<meta charset="UTF-8"><title>Annonces FR</title>""" + CSS + """</head>
<body>
<div class="container">
<div class="header">
<h1>Annonces Crypto (100% FR)</h1>
<p>Sources: Journal du Coin, Cointelegraph FR, Cryptoast</p>
</div>""" + NAV + """
<div class="card">
<h2>Dernieres Actualites</h2>
""" + news_html + ""
</div>
</div>
</body></html>""")

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD v2.5.8 COMPLETE")
    print("="*70)
    print("✅ RESET corrigé - supprime VRAIMENT tout")
    print("✅ Toutes les pages HTML fonctionnelles")
    print("✅ Journal, Heatmap, Strategie, Backtest OK")
    print("✅ Patterns, Metrics, Annonces OK")
    print("✅ Webhook TradingView corrigé")
    print("✅ TP1/TP2/TP3 différenciés")
    print("="*70)
    print("\n📋 PAGES DISPONIBLES:")
    print("   http://localhost:8000/              - Home")
    print("   http://localhost:8000/trades        - Dashboard principal (avec RESET)")
    print("   http://localhost:8000/equity-curve  - Courbe d'equity")
    print("   http://localhost:8000/journal       - Journal de trading")
    print("   http://localhost:8000/heatmap       - Heatmap performance")
    print("   http://localhost:8000/strategie     - Strategie de trading")
    print("   http://localhost:8000/backtest      - Backtest engine")
    print("   http://localhost:8000/patterns      - Pattern recognition")
    print("   http://localhost:8000/advanced-metrics - Metriques avancees")
    print("   http://localhost:8000/annonces      - Actualites crypto FR")
    print("\n📡 API ENDPOINTS:")
    print("   GET  /api/trades                    - Liste des trades")
    print("   GET  /api/fear-greed                - Fear & Greed Index")
    print("   GET  /api/bullrun-phase             - Phase du bull run")
    print("   GET  /api/stats                     - Statistiques")
    print("   GET  /api/equity-curve              - Courbe d'equity")
    print("   GET  /api/journal                   - Journal entries")
    print("   POST /api/journal                   - Ajouter journal entry")
    print("   GET  /api/heatmap                   - Heatmap data")
    print("   GET  /api/news                      - Actualites crypto")
    print("   GET  /api/backtest                  - Lancer backtest")
    print("   POST /api/reset                     - RESET COMPLET")
    print("   GET  /api/telegram-test             - Tester Telegram")
    print("\n📥 WEBHOOK:")
    print("   POST /tv-webhook                    - TradingView webhook")
    print("\n🔄 BOUTON RESET:")
    print("   Cliquez sur le bouton rouge en haut a droite du dashboard")
    print("   pour supprimer TOUTES les donnees (trades, equity, journal)")
    print("\n💡 EXEMPLE WEBHOOK TRADINGVIEW:")
    print("""
   Nouveau trade (ENTRY):
   {
     "type": "ENTRY",
     "symbol": "BTCUSDT",
     "side": "LONG",
     "entry": 65000,
     "tp1": 66000,
     "tp2": 66500,
     "tp3": 67000,
     "sl": 64000,
     "tf_label": "15m"
   }

   TP atteint:
   {
     "type": "TP1_HIT",
     "symbol": "BTCUSDT",
     "side": "LONG",
     "price": 66000
   }

   Stop Loss:
   {
     "type": "SL_HIT",
     "symbol": "BTCUSDT",
     "side": "LONG",
     "price": 64000
   }

   Fermeture manuelle:
   {
     "type": "CLOSE",
     "symbol": "BTCUSDT",
     "side": "LONG",
     "price": 65500,
     "reason": "Profit partiel"
   }
""")
    print("="*70)
    print("\n🎯 VARIABLES D'ENVIRONNEMENT (optionnelles):")
    print("   TELEGRAM_BOT_TOKEN    - Token de votre bot Telegram")
    print("   TELEGRAM_CHAT_ID      - ID de votre chat Telegram")
    print("   WEBHOOK_SECRET        - Secret pour securiser le webhook")
    print("\n⚡ Demarrage du serveur...")
    print("="*70 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
