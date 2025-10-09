# ============================================================================
# NOUVELLES FONCTIONS POUR ANALYSER LE VRAI MARCHÉ CRYPTO
# À ajouter dans votre main.py
# ============================================================================

import httpx
from typing import Dict, Any, List
from datetime import datetime, timedelta

# ============================================================================
# 1. FONCTION POUR RÉCUPÉRER LES DONNÉES RÉELLES DU MARCHÉ
# ============================================================================

async def fetch_real_market_data() -> Dict[str, Any]:
    """
    Récupère les données réelles du marché crypto via CoinGecko API (gratuite)
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Récupérer les données de plusieurs cryptos importantes
            # CoinGecko API gratuite - pas de clé nécessaire
            coins = "bitcoin,ethereum,binancecoin,solana,cardano,avalanche-2,polkadot,polygon,chainlink,dogecoin"
            
            # Données actuelles + historique 30 jours
            url = f"https://api.coingecko.com/api/v3/coins/markets"
            params = {
                "vs_currency": "usd",
                "ids": coins,
                "order": "market_cap_desc",
                "per_page": 20,
                "sparkline": False,
                "price_change_percentage": "24h,7d,30d"
            }
            
            response = await client.get(url, params=params)
            data = response.json()
            
            if not data:
                return None
            
            # Récupérer la dominance BTC
            global_url = "https://api.coingecko.com/api/v3/global"
            global_response = await client.get(global_url)
            global_data = global_response.json()
            
            btc_dominance = global_data.get("data", {}).get("market_cap_percentage", {}).get("btc", 50)
            total_market_cap = global_data.get("data", {}).get("total_market_cap", {}).get("usd", 0)
            
            return {
                "coins": data,
                "btc_dominance": btc_dominance,
                "total_market_cap": total_market_cap,
                "timestamp": datetime.now().isoformat()
            }
            
    except Exception as e:
        logger.error(f"Erreur fetch market data: {e}")
        return None


# ============================================================================
# 2. NOUVELLE FONCTION POUR DÉTECTER LA VRAIE PHASE DU BULL RUN
# ============================================================================

async def detect_real_bullrun_phase() -> Dict[str, Any]:
    """
    Détecte la vraie phase du bull run basée sur les données réelles du marché
    """
    default = {
        "phase": 0,
        "phase_name": "Accumulation",
        "emoji": "🐻",
        "color": "#64748b",
        "description": "Marché en consolidation",
        "confidence": 0,
        "details": {
            "btc": {"performance_30d": 0, "dominance": 0},
            "eth": {"performance_30d": 0},
            "large_cap": {"avg_performance_30d": 0},
            "alts": {"avg_performance_30d": 0}
        },
        "market_cap": 0,
        "btc_price": 0
    }
    
    # Récupérer les données réelles
    market_data = await fetch_real_market_data()
    if not market_data:
        return default
    
    try:
        coins = market_data["coins"]
        btc_dominance = market_data["btc_dominance"]
        total_mc = market_data["total_market_cap"]
        
        # Séparer les cryptos par catégorie
        btc = next((c for c in coins if c["id"] == "bitcoin"), None)
        eth = next((c for c in coins if c["id"] == "ethereum"), None)
        
        large_caps = ["binancecoin", "solana", "cardano", "avalanche-2", "polkadot", "polygon", "chainlink"]
        lc_coins = [c for c in coins if c["id"] in large_caps]
        
        alts = [c for c in coins if c["id"] not in ["bitcoin", "ethereum"] and c["id"] not in large_caps]
        
        if not btc or not eth:
            return default
        
        # Calculer les performances sur 30 jours
        btc_30d = btc.get("price_change_percentage_30d_in_currency", 0) or 0
        eth_30d = eth.get("price_change_percentage_30d_in_currency", 0) or 0
        
        # Performance moyenne large caps
        lc_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in lc_coins) / len(lc_coins) if lc_coins else 0
        
        # Performance moyenne alts
        alts_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in alts) / len(alts) if alts else 0
        
        # Calculer le score de chaque phase
        # Phase 1: Bitcoin Season (BTC domine ET performe bien)
        btc_score = 0
        if btc_dominance > 55 and btc_30d > 10:
            btc_score = btc_30d * (btc_dominance / 50)
        
        # Phase 2: ETH & Large Cap (ETH et large caps surperforment BTC)
        eth_lc_score = 0
        if (eth_30d > btc_30d or lc_30d > btc_30d) and eth_30d > 5:
            eth_lc_score = max(eth_30d, lc_30d)
        
        # Phase 3: Altcoin Season (Alts surperforment tout, dominance BTC baisse)
        alt_score = 0
        if alts_30d > btc_30d and alts_30d > eth_30d and btc_dominance < 55:
            alt_score = alts_30d * 1.5
        
        # Phase 4: Bull Run complet (TOUT monte ensemble)
        full_bull = btc_30d > 15 and eth_30d > 15 and lc_30d > 15 and alts_30d > 15
        
        details = {
            "btc": {
                "performance_30d": round(btc_30d, 1),
                "dominance": round(btc_dominance, 1),
                "price": btc.get("current_price", 0)
            },
            "eth": {
                "performance_30d": round(eth_30d, 1),
                "price": eth.get("current_price", 0)
            },
            "large_cap": {
                "avg_performance_30d": round(lc_30d, 1),
                "count": len(lc_coins)
            },
            "alts": {
                "avg_performance_30d": round(alts_30d, 1),
                "count": len(alts)
            }
        }
        
        # Déterminer la phase
        if full_bull:
            return {
                "phase": 4,
                "phase_name": "MEGA BULL RUN 🔥",
                "emoji": "🚀🔥",
                "color": "#ff0080",
                "description": "Tout explose ! Bull run maximal",
                "confidence": min(100, int((btc_30d + eth_30d + lc_30d + alts_30d) / 2)),
                "details": details,
                "market_cap": int(total_mc),
                "btc_price": btc.get("current_price", 0)
            }
        
        elif alt_score > max(btc_score, eth_lc_score) and alt_score > 0:
            return {
                "phase": 3,
                "phase_name": "Altcoin Season",
                "emoji": "🚀",
                "color": "#10b981",
                "description": "Les altcoins explosent",
                "confidence": min(100, int(alt_score)),
                "details": details,
                "market_cap": int(total_mc),
                "btc_price": btc.get("current_price", 0)
            }
        
        elif eth_lc_score > btc_score and eth_lc_score > 0:
            return {
                "phase": 2,
                "phase_name": "ETH & Large-Cap",
                "emoji": "💎",
                "color": "#627eea",
                "description": "ETH et large caps dominent",
                "confidence": min(100, int(eth_lc_score)),
                "details": details,
                "market_cap": int(total_mc),
                "btc_price": btc.get("current_price", 0)
            }
        
        elif btc_score > 0:
            return {
                "phase": 1,
                "phase_name": "Bitcoin Season",
                "emoji": "₿",
                "color": "#f7931a",
                "description": "BTC domine le marché",
                "confidence": min(100, int(btc_score)),
                "details": details,
                "market_cap": int(total_mc),
                "btc_price": btc.get("current_price", 0)
            }
        
        else:
            # Phase 0: Accumulation / Consolidation
            return {
                "phase": 0,
                "phase_name": "Accumulation",
                "emoji": "🐻",
                "color": "#64748b",
                "description": "Marché en consolidation",
                "confidence": 30,
                "details": details,
                "market_cap": int(total_mc),
                "btc_price": btc.get("current_price", 0)
            }
        
    except Exception as e:
        logger.error(f"Erreur detect bullrun: {e}")
        return default


# ============================================================================
# 3. FONCTION POUR ANALYSER L'ALTCOIN SEASON (VRAIE DONNÉE)
# ============================================================================

async def calculate_real_altseason_metrics() -> Dict[str, Any]:
    """
    Calcule les vrais métriques d'altseason basé sur les données réelles
    """
    market_data = await fetch_real_market_data()
    if not market_data:
        return {
            "is_altseason": False,
            "confidence": 0,
            "btc_performance": 0,
            "alt_performance": 0,
            "message": "Données indisponibles"
        }
    
    try:
        coins = market_data["coins"]
        btc_dominance = market_data["btc_dominance"]
        
        btc = next((c for c in coins if c["id"] == "bitcoin"), None)
        alts = [c for c in coins if c["id"] != "bitcoin"]
        
        if not btc or not alts:
            return {
                "is_altseason": False,
                "confidence": 0,
                "btc_performance": 0,
                "alt_performance": 0,
                "message": "Données insuffisantes"
            }
        
        # Performance BTC vs Alts sur 30 jours
        btc_30d = btc.get("price_change_percentage_30d_in_currency", 0) or 0
        
        # Calculer combien d'alts surperforment BTC
        alts_beating_btc = sum(1 for c in alts if (c.get("price_change_percentage_30d_in_currency", 0) or 0) > btc_30d)
        alt_performance = (alts_beating_btc / len(alts)) * 100 if alts else 0
        
        # Performance moyenne des alts
        avg_alt_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in alts) / len(alts) if alts else 0
        
        # C'est altseason si:
        # 1. Plus de 75% des alts surperforment BTC
        # 2. Dominance BTC < 55%
        # 3. Performance moyenne alts > BTC
        is_altseason = (alt_performance > 75 and btc_dominance < 55) or (avg_alt_30d > btc_30d and avg_alt_30d > 20)
        
        confidence = min(100, int(alt_performance)) if is_altseason else int(alt_performance / 2)
        
        # Trouver les top performers
        top_alts = sorted(alts, key=lambda x: x.get("price_change_percentage_30d_in_currency", 0) or 0, reverse=True)[:5]
        
        return {
            "is_altseason": is_altseason,
            "confidence": confidence,
            "btc_performance": round(btc_30d, 1),
            "alt_performance": round(avg_alt_30d, 1),
            "alts_beating_btc_pct": round(alt_performance, 1),
            "btc_dominance": round(btc_dominance, 1),
            "message": "🚀 ALTSEASON ACTIVE!" if is_altseason else "₿ Bitcoin domine" if btc_30d > avg_alt_30d else "🔄 Phase neutre",
            "top_performers": [
                {
                    "symbol": c["symbol"].upper(),
                    "name": c["name"],
                    "performance_30d": round(c.get("price_change_percentage_30d_in_currency", 0) or 0, 1),
                    "price": c.get("current_price", 0)
                }
                for c in top_alts
            ]
        }
        
    except Exception as e:
        logger.error(f"Erreur altseason metrics: {e}")
        return {
            "is_altseason": False,
            "confidence": 0,
            "btc_performance": 0,
            "alt_performance": 0,
            "message": "Erreur de calcul"
        }


# ============================================================================
# 4. MODIFIER LES ENDPOINTS API EXISTANTS
# ============================================================================

# REMPLACER l'endpoint /api/bullrun-phase existant par:
@app.get("/api/bullrun-phase")
async def get_bullrun_phase():
    """Retourne la VRAIE phase du bull run basée sur les données de marché"""
    return {"ok": True, "bullrun_phase": await detect_real_bullrun_phase()}


# REMPLACER l'endpoint /api/altseason existant par:
@app.get("/api/altseason")
async def get_altseason():
    """Retourne les VRAIES métriques d'altseason"""
    return {"ok": True, "altseason": await calculate_real_altseason_metrics()}


# AJOUTER un nouvel endpoint pour les données brutes du marché:
@app.get("/api/market-data")
async def get_market_data():
    """Retourne les données brutes du marché"""
    return {"ok": True, "market": await fetch_real_market_data()}


# ============================================================================
# 5. METTRE À JOUR LA PAGE ALTSEASON POUR AFFICHER LES VRAIES DONNÉES
# ============================================================================

@app.get("/altseason", response_class=HTMLResponse)
async def altseason_page_updated():
    """Page altseason avec vraies données de marché"""
    alt = await calculate_real_altseason_metrics()
    
    # Top performers HTML
    top_html = ""
    if "top_performers" in alt:
        for coin in alt["top_performers"]:
            color = "#10b981" if coin["performance_30d"] > 0 else "#ef4444"
            top_html += f"""
            <div style="display:flex;justify-content:space-between;align-items:center;padding:16px;border-bottom:1px solid rgba(99,102,241,0.1)">
                <div>
                    <div style="font-weight:700;color:#e2e8f0">{coin['symbol']}</div>
                    <div style="font-size:12px;color:#64748b">{coin['name']}</div>
                </div>
                <div style="text-align:right">
                    <div style="font-weight:700;color:{color};font-size:18px">{coin['performance_30d']:+.1f}%</div>
                    <div style="font-size:12px;color:#64748b">${coin['price']:,.2f}</div>
                </div>
            </div>
            """
    
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Altseason</title>{CSS}</head><body>
    <div class="container">
        <div class="header">
            <h1>🚀 Altseason Detector</h1>
            <p style="color:#64748b">Données de marché en temps réel</p>
        </div>
        {NAV}
        
        <div class="card">
            <h2>📊 Statut Altseason (Données Réelles)</h2>
            <div style="text-align:center;padding:40px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border-radius:20px;margin-bottom:24px">
                <div style="font-size:48px;margin-bottom:16px">{'🚀' if alt['is_altseason'] else '₿'}</div>
                <div style="font-size:32px;font-weight:900;margin-bottom:8px">{alt['message']}</div>
                <div style="color:#64748b;margin-top:8px">Confiance: {alt['confidence']}%</div>
                <div style="margin-top:16px;font-size:14px;color:#64748b">
                    {alt['alts_beating_btc_pct']:.0f}% des alts surperforment BTC
                </div>
            </div>
            
            <div class="grid">
                <div class="metric">
                    <div class="metric-label">₿ BTC 30D</div>
                    <div class="metric-value" style="color:{'#10b981' if alt['btc_performance']>=0 else '#ef4444'}">{alt['btc_performance']:+.1f}%</div>
                </div>
                <div class="metric">
                    <div class="metric-label">🪙 Alts Moyenne</div>
                    <div class="metric-value" style="color:{'#10b981' if alt['alt_performance']>=0 else '#ef4444'}">{alt['alt_performance']:+.1f}%</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Dominance BTC</div>
                    <div class="metric-value">{alt['btc_dominance']:.1f}%</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>🏆 Top Performers (30 jours)</h2>
            {top_html if top_html else '<p style="color:#64748b;text-align:center;padding:20px">Chargement...</p>'}
        </div>
        
        <div style="margin-top:24px;padding:16px;background:rgba(99,102,241,0.1);border-radius:12px;font-size:14px;color:#64748b">
            💡 <strong>Source:</strong> Données en temps réel via CoinGecko API
        </div>
    </div>
    </body></html>""")


# ============================================================================
# NOTES D'UTILISATION:
# ============================================================================
"""
1. Ces fonctions utilisent CoinGecko API (gratuite, pas de clé nécessaire)
2. Limites: ~10-50 requêtes/minute selon l'endpoint
3. Pour production: ajouter un cache Redis/Memcached (TTL 5-10 min)
4. Alternative: CoinMarketCap API (nécessite clé gratuite)

INSTALLATION:
pip install httpx

REMPLACER dans votre main.py:
- La fonction detect_bullrun_phase() existante
- La fonction calculate_altseason_metrics() existante  
- Les endpoints /api/bullrun-phase et /api/altseason
- La page /altseason HTML

Les données seront maintenant TOUJOURS à jour avec le vrai marché crypto!
"""
