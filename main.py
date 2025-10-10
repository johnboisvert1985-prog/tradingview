# Imports nécessaires
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import asyncio

# IMPORTANT : Créer l'instance FastAPI AVANT les décorateurs
app = FastAPI()

# Vos autres imports et configurations...
# from your_modules import build_trade_rows, detect_trading_patterns, etc.

# ============================================================================
# FONCTIONS DE NOTIFICATION CORRIGÉES
# ============================================================================

async def notify_tp_hit(payload, entry):
    """Notification de Take Profit - Corrigée pour gérer entry=None"""
    # Protection contre entry=None
    entry_data = entry if entry is not None else {}
    
    message = f"""
🎯 <b>TAKE PROFIT HIT!</b> 🎯

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
🎯 TP: <code>{payload.get('tp', 'N/A')}</code>
📊 Symbol: <code>{payload.get('symbol', 'N/A')}</code>
⏰ Timeframe: <code>{payload.get('timeframe', 'N/A')}</code>
📈 Side: <code>{payload.get('side', 'N/A')}</code>

✅ Trade fermé avec succès!
"""
    
    # Votre code pour envoyer le message Telegram
    # await send_telegram_message(message)
    print(f"TP Hit notification: {message}")


async def notify_sl_hit(payload, entry):
    """Notification de Stop Loss - Corrigée pour gérer entry=None"""
    # Protection contre entry=None
    entry_data = entry if entry is not None else {}
    
    message = f"""
🛑 <b>STOP LOSS HIT</b> 🛑

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
🛑 SL: <code>{payload.get('sl', 'N/A')}</code>
📊 Symbol: <code>{payload.get('symbol', 'N/A')}</code>
⏰ Timeframe: <code>{payload.get('timeframe', 'N/A')}</code>
📈 Side: <code>{payload.get('side', 'N/A')}</code>

⚠️ Trade fermé par stop loss
"""
    
    # Votre code pour envoyer le message Telegram
    # await send_telegram_message(message)
    print(f"SL Hit notification: {message}")


# ============================================================================
# WEBHOOK ENDPOINT CORRIGÉ
# ============================================================================

@app.post("/tv-webhook")
async def webhook(request: Request):
    """Webhook TradingView - Corrigé pour gérer les cas null"""
    try:
        payload = await request.json()
        
        # Récupérer l'entry (peut être None)
        entry = payload.get("entry")  # Peut retourner None
        
        # Gestion sécurisée des actions
        action = payload.get("action")
        
        if action == "tp_hit":
            # Passer entry même s'il est None, la fonction gère maintenant
            await notify_tp_hit(payload, entry)
            
        elif action == "sl_hit":
            # Passer entry même s'il est None, la fonction gère maintenant
            await notify_sl_hit(payload, entry)
        
        return {"status": "ok", "message": "Webhook processed"}
        
    except Exception as e:
        print(f"Error in webhook: {str(e)}")
        return {"status": "error", "message": str(e)}, 500


# ============================================================================
# ROUTE /trades CORRIGÉE
# ============================================================================

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    """Page dashboard des trades"""
    try:
        # Vos fonctions pour récupérer les données
        rows = build_trade_rows(50)
        patterns = detect_trading_patterns(rows)
        metrics = calculate_advanced_metrics(rows)
        
        # Calcul du win rate avec protection division par zéro
        closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
        wr = (sum(1 for r in closed if r.get("row_state")=="tp") / len(closed) * 100) if closed else 0
        
        # Construction du tableau
        table = ""
        for r in rows[:20]:
            badge = f'<span class="badge badge-green">TP</span>' if r.get("row_state")=="tp" else (
                f'<span class="badge badge-red">SL</span>' if r.get("row_state")=="sl" else 
                f'<span class="badge badge-yellow">En cours</span>'
            )
            table += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)">
                <td style="padding:12px">{r.get('symbol','N/A')}</td>
                <td style="padding:12px">{r.get('tf_label','N/A')}</td>
                <td style="padding:12px">{r.get('side','N/A')}</td>
                <td style="padding:12px">{r.get('entry') or 'N/A'}</td>
                <td style="padding:12px">{badge}</td>
            </tr>"""
        
        # Patterns HTML
        patterns_html = "".join(f'<li style="padding:8px;font-size:14px">{p}</li>' for p in patterns[:5])
        
        # Equity curve
        curve = calculate_equity_curve(rows)
        curr_equity = curve[-1]["equity"] if curve else settings.INITIAL_CAPITAL
        total_return = ((curr_equity - settings.INITIAL_CAPITAL) / settings.INITIAL_CAPITAL) * 100
        
        # Retourner le HTML (votre template complet)
        return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <title>Dashboard</title>
    {CSS}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Dashboard Principal</h1>
            <p>Vue complète 🔴 <strong>MARCHÉ RÉEL</strong> + 🔔 <strong>Telegram</strong></p>
        </div>
        {NAV}
        
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
            <div class="card">
                <h2>😱 Fear & Greed Index</h2>
                <div id="fg" style="text-align:center;padding:40px">⏳</div>
            </div>
            <div class="card">
                <h2>🚀 Bull Run Phase <span style="color:#10b981;font-size:14px">● LIVE</span></h2>
                <div id="br" style="text-align:center;padding:40px">⏳</div>
            </div>
            <div class="card">
                <h2>🤖 AI Patterns</h2>
                <ul class="list" style="margin:0">
                    {patterns_html if patterns_html else '<li style="padding:8px;color:#64748b">Pas de patterns</li>'}
                </ul>
                <a href="/patterns" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir tous les patterns</a>
            </div>
        </div>
        
        <!-- Reste de votre HTML... -->
        
        <div class="card">
            <h2>📊 Derniers Trades</h2>
            <table style="width:100%;border-collapse:collapse">
                <thead>
                    <tr style="border-bottom:2px solid rgba(99,102,241,0.2)">
                        <th style="padding:12px;text-align:left;color:#64748b">Symbol</th>
                        <th style="padding:12px;text-align:left;color:#64748b">TF</th>
                        <th style="padding:12px;text-align:left;color:#64748b">Side</th>
                        <th style="padding:12px;text-align:left;color:#64748b">Entry</th>
                        <th style="padding:12px;text-align:left;color:#64748b">Status</th>
                    </tr>
                </thead>
                <tbody>{table}</tbody>
            </table>
        </div>
        
        <script>
        // Vos scripts JavaScript...
        </script>
    </div>
</body>
</html>""")
    
    except Exception as e:
        print(f"Error in trades_page: {str(e)}")
        return HTMLResponse(f"<h1>Error: {str(e)}</h1>", status_code=500)


# ============================================================================
# DÉMARRAGE DE L'APPLICATION
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
