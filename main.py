# ============================================================================
# SYSTÈME DE NOTIFICATIONS TELEGRAM COMPLET
# PLACEMENT CORRECT dans votre main.py
# ============================================================================

"""
⚠️ IMPORTANT - ORDRE D'INSERTION:

1. Les classes WebhookPayload et JournalNote doivent être définies EN PREMIER
2. ENSUITE les fonctions de base (get_db, init_database, etc.)
3. ENSUITE les nouvelles fonctions de notification (ci-dessous)
4. ENSUITE l'application FastAPI et les endpoints

PLACEMENT CORRECT:
- Ligne ~50: Classes (WebhookPayload, JournalNote)
- Ligne ~150: Fonctions DB (get_db, init_database, build_trade_rows, etc.)
- Ligne ~380: 👉 PLACER ICI les nouvelles fonctions ci-dessous
- Ligne ~600: app = FastAPI() et endpoints
"""

# ============================================================================
# ÉTAPE 1: REMPLACER la fonction send_telegram existante 
# Chercher "async def send_telegram" et REMPLACER par:
# ============================================================================

async def send_telegram(text: str, parse_mode: str = "HTML"):
    """Envoie un message Telegram avec gestion d'erreur détaillée"""
    if not settings.TELEGRAM_ENABLED:
        logger.warning("⚠️ Telegram désactivé - Variables non configurées")
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
                logger.info("✅ Message Telegram envoyé")
                return True
            else:
                logger.error(f"❌ Telegram API error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Erreur send_telegram: {e}")
        return False


# ============================================================================
# ÉTAPE 2: AJOUTER ces 4 fonctions JUSTE APRÈS send_telegram()
# (IMPORTANT: Après WebhookPayload est défini, avant app = FastAPI())
# ============================================================================

async def notify_new_trade(payload: WebhookPayload):
    """Notification pour un nouveau trade"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    # Emoji basé sur la confiance
    if payload.confidence and payload.confidence >= 80:
        conf_emoji = "🔥"
    elif payload.confidence and payload.confidence >= 60:
        conf_emoji = "✅"
    else:
        conf_emoji = "⚠️"
    
    # Risk/Reward
    rr = "N/A"
    if payload.entry and payload.sl and payload.tp1:
        try:
            risk = abs(float(payload.entry) - float(payload.sl))
            reward = abs(float(payload.tp1) - float(payload.entry))
            rr = f"{reward/risk:.2f}" if risk > 0 else "N/A"
        except:
            pass
    
    message = f"""
🚀 <b>NOUVEAU TRADE</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{payload.side}</b>

💰 Entry: <code>{payload.entry}</code>
🎯 TP1: <code>{payload.tp1}</code>
{f'🎯 TP2: <code>{payload.tp2}</code>' if payload.tp2 else ''}
{f'🎯 TP3: <code>{payload.tp3}</code>' if payload.tp3 else ''}
🛑 SL: <code>{payload.sl}</code>

{conf_emoji} Confiance: {payload.confidence}%
⚖️ R/R: {rr}
{f'🔗 Leverage: {payload.leverage}' if payload.leverage else ''}
{f'📝 {payload.note}' if payload.note else ''}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def notify_tp_hit(payload: WebhookPayload, entry_data: dict):
    """Notification quand un TP est touché"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    tp_level = "TP1" if payload.type == "TP1_HIT" else ("TP2" if payload.type == "TP2_HIT" else "TP3")
    
    # Calculer le profit
    profit_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            
            # Inverser si SHORT
            if entry_data.get("side") == "SHORT":
                pct = -pct
            
            profit_pct = f"{pct:+.2f}%"
        except:
            pass
    
    emoji = "🎯" if tp_level == "TP1" else ("🎯🎯" if tp_level == "TP2" else "🎯🎯🎯")
    
    message = f"""
{emoji} <b>{tp_level} TOUCHÉ!</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
✅ Exit: <code>{payload.price}</code>

💵 Profit: <b>{profit_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def notify_sl_hit(payload: WebhookPayload, entry_data: dict):
    """Notification quand le SL est touché"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    # Calculer la perte
    loss_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            
            # Inverser si SHORT
            if entry_data.get("side") == "SHORT":
                pct = -pct
            
            loss_pct = f"{pct:+.2f}%"
        except:
            pass
    
    message = f"""
🛑 <b>STOP LOSS TOUCHÉ</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
❌ Exit: <code>{payload.price}</code>

💸 Perte: <b>{loss_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def send_daily_summary():
    """Envoie un résumé quotidien des trades"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    rows = build_trade_rows(100)
    
    # Trades des dernières 24h
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)
    recent = [r for r in rows if r.get("t_entry", 0) > cutoff]
    
    if not recent:
        return  # Pas de trades aujourd'hui
    
    closed = [r for r in recent if r.get("row_state") in ("tp", "sl")]
    wins = sum(1 for r in closed if r.get("row_state") == "tp")
    losses = sum(1 for r in closed if r.get("row_state") == "sl")
    
    wr = (wins / len(closed) * 100) if closed else 0
    
    # Calculer le P&L du jour
    daily_pnl = 0
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en = float(r["entry"])
                ex = float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None)
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r.get("side") == "SHORT":
                        pl = -pl
                    daily_pnl += pl
            except:
                pass
    
    message = f"""
📊 <b>RÉSUMÉ QUOTIDIEN</b>

🔢 Total trades: {len(recent)}
✅ Wins: {wins}
❌ Losses: {losses}
📈 Win Rate: {wr:.1f}%

💰 P&L du jour: <b>{daily_pnl:+.2f}%</b>

{'🎉 Excellente journée!' if daily_pnl > 5 else '✅ Bonne journée' if daily_pnl > 0 else '📉 Journée difficile'}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)remplacer dans votre main.py
# ============================================================================

"""
ÉTAPE 1: REMPLACER la fonction send_telegram existante (ligne ~380)
"""

async def send_telegram(text: str, parse_mode: str = "HTML"):
    """Envoie un message Telegram avec gestion d'erreur détaillée"""
    if not settings.TELEGRAM_ENABLED:
        logger.warning("⚠️ Telegram désactivé - Variables non configurées")
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
                logger.info("✅ Message Telegram envoyé")
                return True
            else:
                logger.error(f"❌ Telegram API error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Erreur send_telegram: {e}")
        return False


# ============================================================================
# NOUVELLES FONCTIONS POUR LES NOTIFICATIONS
# ============================================================================

async def notify_new_trade(payload: WebhookPayload):
    """Notification pour un nouveau trade"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    # Emoji basé sur la confiance
    if payload.confidence and payload.confidence >= 80:
        conf_emoji = "🔥"
    elif payload.confidence and payload.confidence >= 60:
        conf_emoji = "✅"
    else:
        conf_emoji = "⚠️"
    
    # Risk/Reward
    rr = "N/A"
    if payload.entry and payload.sl and payload.tp1:
        try:
            risk = abs(float(payload.entry) - float(payload.sl))
            reward = abs(float(payload.tp1) - float(payload.entry))
            rr = f"{reward/risk:.2f}" if risk > 0 else "N/A"
        except:
            pass
    
    message = f"""
🚀 <b>NOUVEAU TRADE</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{payload.side}</b>

💰 Entry: <code>{payload.entry}</code>
🎯 TP1: <code>{payload.tp1}</code>
{f'🎯 TP2: <code>{payload.tp2}</code>' if payload.tp2 else ''}
{f'🎯 TP3: <code>{payload.tp3}</code>' if payload.tp3 else ''}
🛑 SL: <code>{payload.sl}</code>

{conf_emoji} Confiance: {payload.confidence}%
⚖️ R/R: {rr}
{f'🔗 Leverage: {payload.leverage}' if payload.leverage else ''}
{f'📝 {payload.note}' if payload.note else ''}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def notify_tp_hit(payload: WebhookPayload, entry_data: dict):
    """Notification quand un TP est touché"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    tp_level = "TP1" if payload.type == "TP1_HIT" else ("TP2" if payload.type == "TP2_HIT" else "TP3")
    
    # Calculer le profit
    profit_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            
            # Inverser si SHORT
            if entry_data.get("side") == "SHORT":
                pct = -pct
            
            profit_pct = f"{pct:+.2f}%"
        except:
            pass
    
    emoji = "🎯" if tp_level == "TP1" else ("🎯🎯" if tp_level == "TP2" else "🎯🎯🎯")
    
    message = f"""
{emoji} <b>{tp_level} TOUCHÉ!</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
✅ Exit: <code>{payload.price}</code>

💵 Profit: <b>{profit_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def notify_sl_hit(payload: WebhookPayload, entry_data: dict):
    """Notification quand le SL est touché"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    # Calculer la perte
    loss_pct = "N/A"
    if entry_data and entry_data.get("entry") and payload.price:
        try:
            entry_price = float(entry_data["entry"])
            exit_price = float(payload.price)
            pct = ((exit_price - entry_price) / entry_price) * 100
            
            # Inverser si SHORT
            if entry_data.get("side") == "SHORT":
                pct = -pct
            
            loss_pct = f"{pct:+.2f}%"
        except:
            pass
    
    message = f"""
🛑 <b>STOP LOSS TOUCHÉ</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
❌ Exit: <code>{payload.price}</code>

💸 Perte: <b>{loss_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


async def send_daily_summary():
    """Envoie un résumé quotidien des trades"""
    if not settings.TELEGRAM_ENABLED:
        return
    
    rows = build_trade_rows(100)
    
    # Trades des dernières 24h
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)
    recent = [r for r in rows if r.get("t_entry", 0) > cutoff]
    
    if not recent:
        return  # Pas de trades aujourd'hui
    
    closed = [r for r in recent if r.get("row_state") in ("tp", "sl")]
    wins = sum(1 for r in closed if r.get("row_state") == "tp")
    losses = sum(1 for r in closed if r.get("row_state") == "sl")
    
    wr = (wins / len(closed) * 100) if closed else 0
    
    # Calculer le P&L du jour
    daily_pnl = 0
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en = float(r["entry"])
                ex = float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None)
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r.get("side") == "SHORT":
                        pl = -pl
                    daily_pnl += pl
            except:
                pass
    
    message = f"""
📊 <b>RÉSUMÉ QUOTIDIEN</b>

🔢 Total trades: {len(recent)}
✅ Wins: {wins}
❌ Losses: {losses}
📈 Win Rate: {wr:.1f}%

💰 P&L du jour: <b>{daily_pnl:+.2f}%</b>

{'🎉 Excellente journée!' if daily_pnl > 5 else '✅ Bonne journée' if daily_pnl > 0 else '📉 Journée difficile'}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    """.strip()
    
    await send_telegram(message)


# ============================================================================
# ÉTAPE 2: MODIFIER LE WEBHOOK pour ajouter les notifications
# Chercher @app.post("/tv-webhook") et REMPLACER par:
# ============================================================================

@app.post("/tv-webhook")
@rate_limit("100/minute")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400)
    
    if data.get("secret") != settings.WEBHOOK_SECRET:
        raise HTTPException(403)
    
    try:
        payload = WebhookPayload(**data)
    except Exception as e:
        raise HTTPException(422, str(e))
    
    # ENTRY - Nouveau trade
    if payload.type == "ENTRY":
        # Circuit breaker check
        if settings.CIRCUIT_BREAKER_ENABLED:
            breaker = check_circuit_breaker()
            if breaker["active"]:
                await send_telegram(f"⛔ <b>TRADE BLOQUÉ</b>\n\nRaison: {breaker['reason']}\nCooldown restant: {breaker['hours_remaining']:.1f}h")
                return {"ok": False, "reason": "circuit_breaker"}
            
            recent = build_trade_rows(10)
            cons = 0
            for t in reversed([r for r in recent if r.get("row_state") in ("tp", "sl")]):
                if t.get("row_state") == "sl":
                    cons += 1
                else:
                    break
            
            if cons >= settings.MAX_CONSECUTIVE_LOSSES:
                trigger_circuit_breaker(f"{cons} pertes consécutives")
                await send_telegram(f"🚨 <b>CIRCUIT BREAKER ACTIVÉ!</b>\n\n{cons} pertes consécutives détectées\nCooldown: 24h\n\n⏸️ Trading suspendu temporairement")
                return {"ok": False, "reason": "consecutive_losses"}
        
        # Sauvegarder le trade
        trade_id = save_event(payload)
        
        # 🔔 NOTIFICATION NOUVEAU TRADE
        await notify_new_trade(payload)
        
        return {"ok": True, "trade_id": trade_id}
    
    # TP HIT - Take Profit touché
    elif payload.type in ["TP1_HIT", "TP2_HIT", "TP3_HIT"]:
        trade_id = save_event(payload)
        
        # Récupérer les données d'entry
        entry = _latest_entry_for_trade(payload.trade_id)
        
        # 🔔 NOTIFICATION TP
        await notify_tp_hit(payload, entry)
        
        return {"ok": True, "trade_id": trade_id}
    
    # SL HIT - Stop Loss touché
    elif payload.type == "SL_HIT":
        trade_id = save_event(payload)
        
        # Récupérer les données d'entry
        entry = _latest_entry_for_trade(payload.trade_id)
        
        # 🔔 NOTIFICATION SL
        await notify_sl_hit(payload, entry)
        
        return {"ok": True, "trade_id": trade_id}
    
    # Autres types (CLOSE, etc.)
    else:
        trade_id = save_event(payload)
        return {"ok": True, "trade_id": trade_id}


# ============================================================================
# ÉTAPE 3: AJOUTER UN ENDPOINT POUR TESTER LES NOTIFICATIONS
# ============================================================================

@app.get("/test-telegram")
async def test_telegram():
    """Endpoint pour tester les notifications Telegram"""
    if not settings.TELEGRAM_ENABLED:
        return {
            "ok": False,
            "error": "Telegram non configuré",
            "telegram_bot_token": "❌ Non défini" if not settings.TELEGRAM_BOT_TOKEN else "✅ Défini",
            "telegram_chat_id": "❌ Non défini" if not settings.TELEGRAM_CHAT_ID else "✅ Défini"
        }
    
    # Envoyer un message de test
    test_msg = f"""
🧪 <b>TEST NOTIFICATION</b>

✅ Bot Telegram fonctionnel!
⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

🚀 AI Trader Pro v3.0
    """.strip()
    
    success = await send_telegram(test_msg)
    
    if success:
        # Envoyer aussi un résumé
        rows = build_trade_rows(50)
        closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
        wins = sum(1 for r in closed if r.get("row_state") == "tp")
        wr = (wins / len(closed) * 100) if closed else 0
        
        summary_msg = f"""
📊 <b>STATUT ACTUEL</b>

Total trades: {len(rows)}
Trades fermés: {len(closed)}
Win Rate: {wr:.1f}%

🔔 Notifications activées
✅ Vous recevrez désormais:
  • Nouveaux trades (ENTRY)
  • TP touchés
  • SL touchés
  • Circuit breaker
        """.strip()
        
        await send_telegram(summary_msg)
    
    return {
        "ok": success,
        "telegram_enabled": settings.TELEGRAM_ENABLED,
        "message": "Message de test envoyé" if success else "Échec de l'envoi"
    }


# ============================================================================
# ÉTAPE 4: (OPTIONNEL) AJOUTER UNE COMMANDE POUR LE RÉSUMÉ QUOTIDIEN
# À exécuter via un cron job ou scheduler
# ============================================================================

@app.get("/send-daily-summary")
async def trigger_daily_summary():
    """Endpoint pour déclencher manuellement le résumé quotidien"""
    await send_daily_summary()
    return {"ok": True, "message": "Résumé envoyé"}


# ============================================================================
# INSTRUCTIONS D'UTILISATION
# ============================================================================

"""
1. REMPLACER la fonction send_telegram() existante

2. AJOUTER les 4 nouvelles fonctions de notification:
   - notify_new_trade()
   - notify_tp_hit()
   - notify_sl_hit()
   - send_daily_summary()

3. REMPLACER l'endpoint @app.post("/tv-webhook")

4. AJOUTER les endpoints de test:
   - @app.get("/test-telegram")
   - @app.get("/send-daily-summary")

5. VÉRIFIER vos variables d'environnement:
   export TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
   export TELEGRAM_CHAT_ID="123456789"

6. TESTER:
   Visitez: https://votre-app.com/test-telegram
   Vous devriez recevoir 2 messages Telegram!

7. DÉSORMAIS, vous recevrez des notifications pour:
   ✅ Chaque nouveau trade (ENTRY)
   ✅ Chaque TP touché (avec profit calculé)
   ✅ Chaque SL touché (avec perte calculée)
   ✅ Circuit breaker activé
   ✅ Résumé quotidien (optionnel)
"""
