# ============================================================================
# MODIFICATIONS POUR TELEGRAM - À APPLIQUER SUR VOTRE FICHIER EXISTANT
# NE SUPPRIMEZ RIEN ! Ajoutez/remplacez SEULEMENT ces parties
# ============================================================================

# ============================================================================
# ÉTAPE 1: REMPLACER la fonction send_telegram existante (ligne ~380)
# Chercher "async def send_telegram" et remplacer par:
# ============================================================================

async def send_telegram(text: str, parse_mode: str = "HTML"):
    """Envoie un message Telegram avec gestion d'erreur"""
    if not settings.TELEGRAM_ENABLED:
        logger.warning("⚠️ Telegram désactivé")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True
                }
            )
            if response.status_code == 200:
                logger.info("✅ Telegram envoyé")
                return True
            else:
                logger.error(f"❌ Telegram: {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"❌ send_telegram: {e}")
        return False


# ============================================================================
# ÉTAPE 2: AJOUTER ces 3 fonctions JUSTE APRÈS send_telegram()
# ============================================================================

async def notify_new_trade(payload: WebhookPayload):
    """Notification nouveau trade"""
    if not settings.TELEGRAM_ENABLED: return
    conf_emoji = "🔥" if payload.confidence and payload.confidence >= 80 else "✅" if payload.confidence and payload.confidence >= 60 else "⚠️"
    rr = "N/A"
    if payload.entry and payload.sl and payload.tp1:
        try:
            risk = abs(float(payload.entry) - float(payload.sl))
            reward = abs(float(payload.tp1) - float(payload.entry))
            rr = f"{reward/risk:.2f}" if risk > 0 else "N/A"
        except: pass
    message = f"""🚀 <b>NOUVEAU TRADE</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{payload.side}</b>

💰 Entry: <code>{payload.entry}</code>
🎯 TP1: <code>{payload.tp1}</code>
{f'🎯 TP2: <code>{payload.tp2}</code>' if payload.tp2 else ''}
{f'🎯 TP3: <code>{payload.tp3}</code>' if payload.tp3 else ''}
🛑 SL: <code>{payload.sl}</code>

{conf_emoji} Confiance: {payload.confidence}%
⚖️ R/R: {rr}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}""".strip()
    await send_telegram(message)


async def notify_tp_hit(payload: WebhookPayload, entry_data: dict):
    """Notification TP touché"""
    if not settings.TELEGRAM_ENABLED: return
    tp_level = "TP1" if payload.type == "TP1_HIT" else ("TP2" if payload.type == "TP2_HIT" else "TP3")
    profit_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            if entry_data.get("side") == "SHORT": pct = -pct
            profit_pct = f"{pct:+.2f}%"
        except: pass
    emoji = "🎯" if tp_level == "TP1" else ("🎯🎯" if tp_level == "TP2" else "🎯🎯🎯")
    message = f"""{emoji} <b>{tp_level} TOUCHÉ!</b>

📊 <b>{payload.symbol}</b>
💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
✅ Exit: <code>{payload.price}</code>

💵 Profit: <b>{profit_pct}</b>""".strip()
    await send_telegram(message)


async def notify_sl_hit(payload: WebhookPayload, entry_data: dict):
    """Notification SL touché"""
    if not settings.TELEGRAM_ENABLED: return
    loss_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            if entry_data.get("side") == "SHORT": pct = -pct
            loss_pct = f"{pct:+.2f}%"
        except: pass
    message = f"""🛑 <b>STOP LOSS</b>

📊 <b>{payload.symbol}</b>
💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
❌ Exit: <code>{payload.price}</code>

💸 Perte: <b>{loss_pct}</b>""".strip()
    await send_telegram(message)


# ============================================================================
# ÉTAPE 3: REMPLACER le webhook existant @app.post("/tv-webhook")
# Chercher "@app.post("/tv-webhook")" et remplacer TOUTE la fonction par:
# ============================================================================

@app.post("/tv-webhook")
@rate_limit("100/minute")
async def webhook(request: Request):
    try: data = await request.json()
    except: raise HTTPException(400)
    if data.get("secret") != settings.WEBHOOK_SECRET: raise HTTPException(403)
    try: payload = WebhookPayload(**data)
    except Exception as e: raise HTTPException(422, str(e))
    
    if payload.type == "ENTRY":
        if settings.CIRCUIT_BREAKER_ENABLED:
            breaker = check_circuit_breaker()
            if breaker["active"]:
                await send_telegram(f"⛔ <b>BLOQUÉ</b>\n{breaker['reason']}\n{breaker['hours_remaining']:.1f}h")
                return {"ok": False, "reason": "circuit_breaker"}
            recent = build_trade_rows(10)
            cons = 0
            for t in reversed([r for r in recent if r.get("row_state") in ("tp", "sl")]):
                if t.get("row_state") == "sl": cons += 1
                else: break
            if cons >= settings.MAX_CONSECUTIVE_LOSSES:
                trigger_circuit_breaker(f"{cons} pertes")
                await send_telegram(f"🚨 <b>BREAKER!</b>\n{cons} pertes\n24h cooldown")
                return {"ok": False, "reason": "consecutive_losses"}
        trade_id = save_event(payload)
        await notify_new_trade(payload)  # ← NOUVELLE LIGNE
        return {"ok": True, "trade_id": trade_id}
    
    elif payload.type in ["TP1_HIT", "TP2_HIT", "TP3_HIT"]:
        trade_id = save_event(payload)
        entry = _latest_entry_for_trade(payload.trade_id)  # ← NOUVELLE LIGNE
        await notify_tp_hit(payload, entry)  # ← NOUVELLE LIGNE
        return {"ok": True, "trade_id": trade_id}
    
    elif payload.type == "SL_HIT":
        trade_id = save_event(payload)
        entry = _latest_entry_for_trade(payload.trade_id)  # ← NOUVELLE LIGNE
        await notify_sl_hit(payload, entry)  # ← NOUVELLE LIGNE
        return {"ok": True, "trade_id": trade_id}
    
    else:
        trade_id = save_event(payload)
        return {"ok": True, "trade_id": trade_id}


# ============================================================================
# ÉTAPE 4: AJOUTER cet endpoint de test (OPTIONNEL mais recommandé)
# Ajouter après les autres @app.get() endpoints
# ============================================================================

@app.get("/test-telegram")
async def test_telegram():
    """Test les notifications Telegram"""
    if not settings.TELEGRAM_ENABLED:
        return {
            "ok": False,
            "error": "Telegram non configuré",
            "bot_token": "❌" if not settings.TELEGRAM_BOT_TOKEN else "✅",
            "chat_id": "❌" if not settings.TELEGRAM_CHAT_ID else "✅"
        }
    
    test_msg = f"""🧪 <b>TEST NOTIFICATION</b>

✅ Bot Telegram OK!
⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}

🚀 AI Trader Pro v3.0""".strip()
    
    success = await send_telegram(test_msg)
    return {
        "ok": success,
        "telegram_enabled": settings.TELEGRAM_ENABLED,
        "message": "Messages envoyés!" if success else "Échec"
    }


# ============================================================================
# ÉTAPE 5: MODIFIER la page d'accueil pour ajouter le lien de test
# Chercher @app.get("/") et remplacer le return HTMLResponse par:
# ============================================================================

@app.get("/")
async def root():
    return HTMLResponse("""<!DOCTYPE html><html><head><title>AI Trader</title></head>
    <body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3">
    <h1 style="color:#6366f1">🚀 AI Trader Pro v3.0</h1>
    <p><a href="/trades" style="color:#8b5cf6">📊 Dashboard</a> | 
    <a href="/test-telegram" style="color:#10b981">🧪 Test Telegram</a></p>
    </body></html>""")


# ============================================================================
# C'EST TOUT ! 🎉
# ============================================================================

"""
RÉSUMÉ DES MODIFICATIONS:
✅ Fonction send_telegram améliorée
✅ 3 nouvelles fonctions de notification
✅ Webhook modifié pour envoyer les notifications
✅ Endpoint /test-telegram pour tester
✅ Page d'accueil avec lien de test

TOUTES VOS PAGES HTML SONT CONSERVÉES:
- /trades (dashboard complet)
- /equity-curve
- /heatmap
- /advanced-metrics
- /patterns
- /journal
- /backtest
- /strategie
- /altseason

TESTER:
1. Redémarrer l'app
2. Visiter https://votre-app.com/test-telegram
3. Vous devriez recevoir un message Telegram!
4. Ensuite, chaque trade enverra des notifications
"""
