# main.py - AI Trader Pro v3.0 ULTIMATE - Version Complète
# Python 3.8+

import os
import sqlite3
import logging
import asyncio
import time
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import httpx

# Rate limiting (optionnel)
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    RATE_LIMIT_ENABLED = True
except ImportError:
    RATE_LIMIT_ENABLED = False

# ============================================================================
# CONFIGURATION
# ============================================================================

class Settings:
    DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
    DB_PATH = os.path.join(DB_DIR, "data.db")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
    if not WEBHOOK_SECRET:
        raise ValueError("❌ WEBHOOK_SECRET obligatoire")
    
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    
    DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
    SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
    
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))
    MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
    CIRCUIT_BREAKER_ENABLED = os.getenv("CIRCUIT_BREAKER_ENABLED", "1") == "1"
    INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000.0"))
    
    SKIP_TRADES_BEFORE_HOUR = int(os.getenv("SKIP_TRADES_BEFORE_HOUR", "0"))
    SKIP_TRADES_AFTER_HOUR = int(os.getenv("SKIP_TRADES_AFTER_HOUR", "24"))
    MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "0"))

settings = Settings()
os.makedirs(settings.DB_DIR, exist_ok=True)

logger = logging.getLogger("aitrader")
logger.setLevel(settings.LOG_LEVEL)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

logger.info("🚀 AI Trader Pro v3.0 ULTIMATE Edition")

# ============================================================================
# MODELS
# ============================================================================

class WebhookPayload(BaseModel):
    type: str
    symbol: str
    tf: Optional[str] = None
    tf_label: Optional[str] = None
    time: Optional[int] = None
    side: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    confidence: Optional[int] = None
    leverage: Optional[str] = None
    note: Optional[str] = None
    price: Optional[float] = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None

    @field_validator('type')
    @classmethod
    def validate_type(cls, v):
        if v not in ['ENTRY', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT', 'SL_HIT', 'CLOSE']:
            raise ValueError(f'Type invalide: {v}')
        return v

class JournalNote(BaseModel):
    trade_id: str
    note: Optional[str] = ""
    emotion: Optional[str] = ""
    tags: Optional[str] = ""

# ============================================================================
# DATABASE
# ============================================================================

def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

@contextmanager
def get_db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=30.0)
    conn.row_factory = dict_factory
    try:
        yield conn
    finally:
        conn.close()

def db_execute(sql: str, params: tuple = ()):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

def db_query(sql: str, params: tuple = ()) -> List[dict]:
    try:
        with get_db() as conn:
            return list(conn.cursor().execute(sql, params).fetchall())
    except:
        return []

def init_database():
    db_execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            tf TEXT,
            tf_label TEXT,
            time INTEGER NOT NULL,
            side TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            confidence INTEGER,
            leverage TEXT,
            note TEXT,
            price REAL,
            trade_id TEXT,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)
    
    db_execute("""
        CREATE TABLE IF NOT EXISTS trade_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            note TEXT,
            emotion TEXT,
            tags TEXT,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)
    
    db_execute("""
        CREATE TABLE IF NOT EXISTS circuit_breaker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at INTEGER NOT NULL,
            reason TEXT,
            cooldown_until INTEGER,
            active INTEGER DEFAULT 1
        )
    """)
    
    db_execute("""
        CREATE TABLE IF NOT EXISTS trade_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            condition TEXT NOT NULL,
            action TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)

init_database()

# ============================================================================
# UTILITIES
# ============================================================================

def now_ms(): 
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def tf_to_label(tf):
    if not tf: return ""
    try:
        n = int(str(tf))
        if n < 60: return f"{n}m"
        if n == 60: return "1h"
        if n % 60 == 0: return f"{n//60}h"
    except: pass
    return str(tf)

def _latest_entry_for_trade(trade_id):
    r = db_query("SELECT * FROM events WHERE trade_id=? AND type='ENTRY' ORDER BY time DESC LIMIT 1", (trade_id,))
    return r[0] if r else None

def build_trade_rows(limit=300):
    base = db_query("SELECT trade_id, MAX(time) AS t_entry FROM events WHERE type='ENTRY' GROUP BY trade_id ORDER BY t_entry DESC LIMIT ?", (limit,))
    rows = []
    for item in base:
        e = _latest_entry_for_trade(item["trade_id"])
        if not e: continue
        
        hits = db_query("SELECT type FROM events WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT') GROUP BY type", (e["trade_id"],))
        hit_map = {h["type"]: True for h in hits}
        
        tp1_hit = bool(hit_map.get("TP1_HIT"))
        sl_hit = bool(hit_map.get("SL_HIT"))
        
        if sl_hit:
            state = "sl"
        elif tp1_hit:
            state = "tp"
        else:
            state = "normal"
        
        rows.append({
            "trade_id": e["trade_id"],
            "symbol": e["symbol"],
            "tf": e.get("tf"),
            "tf_label": e.get("tf_label") or tf_to_label(e.get("tf")),
            "side": e["side"],
            "entry": e["entry"],
            "tp1": e["tp1"],
            "sl": e["sl"],
            "tp1_hit": tp1_hit,
            "sl_hit": sl_hit,
            "row_state": state,
            "t_entry": item["t_entry"],
            "confidence": e.get("confidence", 50)
        })
    return rows

# ============================================================================
# FEAR & GREED INDEX
# ============================================================================

async def fetch_fear_greed_index() -> Dict[str, Any]:
    """Récupère le Fear & Greed Index depuis l'API alternative.me"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.alternative.me/fng/?limit=1")
            data = response.json()
            
            if data and "data" in data and len(data["data"]) > 0:
                fng = data["data"][0]
                value = int(fng["value"])
                
                if value <= 25:
                    sentiment = "Extreme Fear"
                    emoji = "😱"
                    color = "#ef4444"
                    recommendation = "Opportunité d'achat potentielle"
                elif value <= 45:
                    sentiment = "Fear"
                    emoji = "😰"
                    color = "#f97316"
                    recommendation = "Marché prudent - Bon moment pour accumuler"
                elif value <= 55:
                    sentiment = "Neutral"
                    emoji = "😐"
                    color = "#64748b"
                    recommendation = "Marché équilibré"
                elif value <= 75:
                    sentiment = "Greed"
                    emoji = "😊"
                    color = "#10b981"
                    recommendation = "Marché optimiste - Soyez vigilant"
                else:
                    sentiment = "Extreme Greed"
                    emoji = "🤑"
                    color = "#22c55e"
                    recommendation = "Attention à la surchauffe - Prenez des profits"
                
                return {
                    "value": value,
                    "sentiment": sentiment,
                    "emoji": emoji,
                    "color": color,
                    "recommendation": recommendation,
                    "timestamp": fng.get("timestamp", "")
                }
    except Exception as e:
        logger.error(f"Error fetching Fear & Greed: {e}")
    
    return {
        "value": 50,
        "sentiment": "Unknown",
        "emoji": "❓",
        "color": "#64748b",
        "recommendation": "Données non disponibles",
        "timestamp": ""
    }

# ============================================================================
# BULL RUN PHASES
# ============================================================================

def detect_bullrun_phase(rows: List[dict]) -> Dict[str, Any]:
    """Détecte la phase du bull run basée sur la performance BTC/ETH/Alts"""
    
    if len(rows) < 20:
        return {
            "phase": 0,
            "phase_name": "Accumulation",
            "emoji": "🐻",
            "color": "#64748b",
            "description": "Pas assez de données",
            "confidence": 0,
            "details": {
                "btc": {"winrate": 0, "avg_return": 0, "trades": 0},
                "eth": {"winrate": 0, "avg_return": 0, "trades": 0},
                "large_cap": {"winrate": 0, "avg_return": 0, "trades": 0},
                "small_alts": {"winrate": 0, "avg_return": 0, "trades": 0}
            }
        }
    
    # Séparer par catégories
    btc_trades = [r for r in rows if "BTC" in r.get("symbol", "").upper() and r["row_state"] in ("tp", "sl")]
    eth_trades = [r for r in rows if "ETH" in r.get("symbol", "").upper() and "BTC" not in r.get("symbol", "").upper() and r["row_state"] in ("tp", "sl")]
    large_cap_symbols = ["SOL", "BNB", "ADA", "AVAX", "DOT", "MATIC", "LINK"]
    large_cap_trades = [r for r in rows if any(sym in r.get("symbol", "").upper() for sym in large_cap_symbols) and r["row_state"] in ("tp", "sl")]
    small_alt_trades = [r for r in rows if r["row_state"] in ("tp", "sl") and 
                       "BTC" not in r.get("symbol", "").upper() and 
                       "ETH" not in r.get("symbol", "").upper() and
                       not any(sym in r.get("symbol", "").upper() for sym in large_cap_symbols)]
    
    def calc_performance(trades):
        if not trades:
            return 0, 0
        wins = sum(1 for t in trades if t["row_state"] == "tp")
        wr = (wins / len(trades)) * 100 if trades else 0
        
        returns = []
        for r in trades:
            if r.get("entry"):
                try:
                    entry = float(r["entry"])
                    exit_p = float(r["sl"]) if r.get("sl_hit") else float(r["tp1"]) if r.get("tp1") else None
                    if exit_p:
                        pl_pct = ((exit_p - entry) / entry) * 100
                        if r.get("side") == "SHORT": pl_pct = -pl_pct
                        returns.append(pl_pct)
                except: pass
        avg_return = sum(returns) / len(returns) if returns else 0
        return wr, avg_return
    
    btc_wr, btc_avg = calc_performance(btc_trades)
    eth_wr, eth_avg = calc_performance(eth_trades)
    lc_wr, lc_avg = calc_performance(large_cap_trades)
    alt_wr, alt_avg = calc_performance(small_alt_trades)
    
    btc_score = (btc_wr * 0.6 + (btc_avg + 10) * 4) if btc_trades else 0
    eth_score = (eth_wr * 0.6 + (eth_avg + 10) * 4) if eth_trades else 0
    lc_score = (lc_wr * 0.6 + (lc_avg + 10) * 4) if large_cap_trades else 0
    alt_score = (alt_wr * 0.6 + (alt_avg + 10) * 4) if small_alt_trades else 0
    
    phase = 0
    phase_name = "Accumulation"
    emoji = "🐻"
    color = "#64748b"
    description = "Phase d'accumulation"
    confidence = 30
    
    if btc_score > eth_score and btc_score > alt_score and btc_wr > 55:
        phase = 1
        phase_name = "Bitcoin Season"
        emoji = "₿"
        color = "#f7931a"
        description = "BTC domine le marché. Les alts sont calmes."
        confidence = min(100, int(btc_score - max(eth_score, alt_score)))
    elif (eth_score > btc_score or lc_score > btc_score) and eth_wr > 50:
        phase = 2
        phase_name = "ETH & Large-Cap Season"
        emoji = "💎"
        color = "#627eea"
        description = "ETH et top alts commencent à surperformer BTC."
        confidence = min(100, int(max(eth_score, lc_score) - btc_score))
    elif alt_score > btc_score and alt_score > eth_score and alt_wr > 55:
        phase = 3
        phase_name = "Altcoin Season"
        emoji = "🚀"
        color = "#10b981"
        description = "Les petits alts explosent ! Bull run peak proche."
        confidence = min(100, int(alt_score - max(btc_score, eth_score)))
    
    return {
        "phase": phase,
        "phase_name": phase_name,
        "emoji": emoji,
        "color": color,
        "description": description,
        "confidence": confidence,
        "details": {
            "btc": {"winrate": round(btc_wr, 1), "avg_return": round(btc_avg, 2), "trades": len(btc_trades)},
            "eth": {"winrate": round(eth_wr, 1), "avg_return": round(eth_avg, 2), "trades": len(eth_trades)},
            "large_cap": {"winrate": round(lc_wr, 1), "avg_return": round(lc_avg, 2), "trades": len(large_cap_trades)},
            "small_alts": {"winrate": round(alt_wr, 1), "avg_return": round(alt_avg, 2), "trades": len(small_alt_trades)}
        }
    }

# ============================================================================
# AI & ANALYTICS (autres fonctions simplifiées pour la taille)
# ============================================================================

def calculate_ai_trade_score(payload: dict, historical_data: List[dict]) -> Dict[str, Any]:
    score = 50
    factors = []
    symbol = payload.get("symbol")
    confidence = payload.get("confidence", 50)
    
    score += (confidence - 50) * 0.4
    factors.append(f"Confiance: {confidence}%")
    
    score = max(0, min(100, int(score)))
    
    if score >= 75:
        quality = "🟢 EXCELLENT"
        recommendation = "Conditions optimales"
    elif score >= 60:
        quality = "🟡 BON"
        recommendation = "Conditions favorables"
    else:
        quality = "🔴 MOYEN"
        recommendation = "Soyez prudent"
    
    return {
        "score": score,
        "quality": quality,
        "recommendation": recommendation,
        "factors": factors
    }

def check_circuit_breaker() -> Dict[str, Any]:
    active = db_query("SELECT * FROM circuit_breaker WHERE active=1 AND cooldown_until > ? ORDER BY triggered_at DESC LIMIT 1", (int(time.time()),))
    
    if active:
        b = active[0]
        remaining = b["cooldown_until"] - int(time.time())
        return {"active": True, "reason": b["reason"], "hours_remaining": round(remaining / 3600, 1)}
    
    return {"active": False}

def trigger_circuit_breaker(reason: str):
    cooldown = int(time.time()) + (24 * 3600)
    db_execute("INSERT INTO circuit_breaker (triggered_at, reason, cooldown_until, active) VALUES (?, ?, ?, 1)", (int(time.time()), reason, cooldown))

def save_event(payload: WebhookPayload) -> str:
    trade_id = payload.trade_id or f"{payload.symbol}_{payload.tf}_{payload.time or now_ms()}"
    
    db_execute("""
        INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, confidence, leverage, note, price, trade_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        payload.type, payload.symbol, str(payload.tf) if payload.tf else None,
        payload.tf_label or tf_to_label(payload.tf), int(payload.time or now_ms()),
        payload.side, payload.entry, payload.sl, payload.tp1, payload.tp2, payload.tp3,
        payload.confidence, payload.leverage, payload.note, payload.price, trade_id
    ))
    
    return trade_id

async def send_telegram(text: str):
    if not settings.TELEGRAM_ENABLED: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
            )
    except: pass

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(title="AI Trader Pro v3.0 ULTIMATE", version="3.0")

if RATE_LIMIT_ENABLED:
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    logger.info("✅ Rate limiting activé")
    def rate_limit(limit_string):
        def decorator(func):
            return limiter.limit(limit_string)(func)
        return decorator
else:
    logger.info("⚠️ Rate limiting désactivé")
    def rate_limit(limit_string):
        def decorator(func):
            return func
        return decorator

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ============================================================================
# CSS & NAV
# ============================================================================

CSS = """<style>
body{margin:0;font-family:system-ui;background:#050a12;color:#e2e8f0}
.container{max-width:1200px;margin:0 auto;padding:40px 20px}
.header{margin-bottom:40px}
.header h1{font-size:36px;font-weight:900;background:linear-gradient(135deg,#6366f1,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
.header p{color:#64748b}
.nav{display:flex;gap:16px;margin-bottom:32px;flex-wrap:wrap}
.nav a{padding:12px 24px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:12px;color:#6366f1;text-decoration:none;font-weight:600;transition:all 0.3s}
.nav a:hover{background:rgba(99,102,241,0.2);transform:translateY(-2px)}
.card{background:rgba(20,30,48,0.6);border:1px solid rgba(99,102,241,0.12);border-radius:20px;padding:32px;margin-bottom:24px}
.card h2{font-size:24px;font-weight:800;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:24px}
.metric{background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border:1px solid rgba(99,102,241,0.2);border-radius:12px;padding:20px}
.metric-label{font-size:12px;color:#64748b;font-weight:700;text-transform:uppercase;margin-bottom:8px}
.metric-value{font-size:32px;font-weight:900;color:#6366f1}
.badge{display:inline-block;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:700}
.badge-green{background:rgba(16,185,129,0.15);color:#10b981;border:1px solid rgba(16,185,129,0.3)}
.badge-red{background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.3)}
.badge-yellow{background:rgba(251,191,36,0.15);color:#fbbf24;border:1px solid rgba(251,191,36,0.3)}
.gauge{width:200px;height:200px;border-radius:50%;background:conic-gradient(from 180deg,#ef4444 0%,#f97316 25%,#fbbf24 45%,#10b981 55%,#22c55e 75%,#22c55e 100%);position:relative;display:flex;align-items:center;justify-content:center;margin:0 auto}
.gauge-inner{width:160px;height:160px;border-radius:50%;background:#0a0f1a;display:flex;flex-direction:column;align-items:center;justify-content:center}
.gauge-value{font-size:48px;font-weight:900;line-height:1}
.gauge-label{font-size:12px;color:#64748b;margin-top:4px}
.phase-indicator{display:flex;align-items:center;gap:16px;padding:20px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border-radius:16px;margin-bottom:12px;position:relative;overflow:hidden}
.phase-indicator::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px}
.phase-indicator.active::before{background:currentColor}
.phase-number{width:48px;height:48px;border-radius:50%;background:rgba(99,102,241,0.2);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;flex-shrink:0}
.phase-indicator.active .phase-number{background:currentColor;color:#0a0f1a}
</style>"""

NAV = """<div class="nav">
<a href="/trades">📊 Dashboard</a>
<a href="/altseason">🚀 Altseason</a>
</div>"""

# ============================================================================
# ROUTES
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def root():
    return """<!DOCTYPE html><html><head><title>AI Trader Pro v3.0</title></head>
    <body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3">
    <h1 style="color:#6366f1">🚀 AI Trader Pro v3.0 ULTIMATE</h1>
    <p>Système de trading professionnel avec IA</p>
    <h2>Pages disponibles:</h2><ul>
    <li><a href="/trades" style="color:#8b5cf6">📊 Dashboard Principal</a></li>
    <li><a href="/altseason" style="color:#8b5cf6">🚀 Altseason Detector</a></li>
    </ul></body></html>"""

@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "version": "3.0.0", 
        "features": ["Fear & Greed", "Bull Run Phase", "Altseason"]
    }

@app.get("/api/fear-greed")
async def get_fear_greed():
    """API Fear & Greed Index"""
    data = await fetch_fear_greed_index()
    return {"ok": True, "fear_greed": data}

@app.get("/api/bullrun-phase")
async def get_bullrun_phase():
    """API Bull Run Phase Detection"""
    rows = build_trade_rows(limit=100)
    phase = detect_bullrun_phase(rows)
    return {"ok": True, "bullrun_phase": phase}

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return {"ok": True, "trades": build_trade_rows(limit=limit)}

@app.post("/tv-webhook")
@rate_limit("100/minute")
async def webhook(request: Request):
    try:
        data = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")
    
    if data.get("secret") != settings.WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")
    
    try:
        payload = WebhookPayload(**data)
    except Exception as e:
        raise HTTPException(422, str(e))
    
    if payload.type == "ENTRY" and settings.CIRCUIT_BREAKER_ENABLED:
        breaker = check_circuit_breaker()
        if breaker["active"]:
            await send_telegram(f"⛔ Trade bloqué: {breaker['reason']}")
            return {"ok": False, "reason": "circuit_breaker"}
        
        recent = build_trade_rows(limit=10)
        consecutive = 0
        for t in reversed([r for r in recent if r["row_state"] in ("tp", "sl")]):
            if t["row_state"] == "sl":
                consecutive += 1
            else:
                break
        
        if consecutive >= settings.MAX_CONSECUTIVE_LOSSES:
            trigger_circuit_breaker(f"{consecutive} pertes consécutives")
            await send_telegram(f"🚨 CIRCUIT BREAKER: {consecutive} pertes - Trading bloqué 24h")
            return {"ok": False, "reason": "consecutive_losses"}
    
    trade_id = save_event(payload)
    
    if payload.type == "ENTRY":
        rows = build_trade_rows(limit=100)
        ai_score = calculate_ai_trade_score(data, rows)
        msg = f"""🤖 <b>AI TRADE SCORE</b>

📊 {payload.symbol} {payload.side}
Score: {ai_score['score']}/100 {ai_score['quality']}

{ai_score['recommendation']}"""
        await send_telegram(msg)
    
    return {"ok": True, "trade_id": trade_id}

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = build_trade_rows(limit=50)
    
    table_rows = ""
    for r in rows[:20]:
        state_badge = f'<span class="badge badge-green">TP ✓</span>' if r["row_state"] == "tp" else (f'<span class="badge badge-red">SL ✗</span>' if r["row_state"] == "sl" else f'<span class="badge badge-yellow">En cours</span>')
        
        table_rows += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)">
            <td style="padding:12px">{r['symbol']}</td>
            <td style="padding:12px">{r['tf_label']}</td>
            <td style="padding:12px">{r['side']}</td>
            <td style="padding:12px">{r['entry'] if r['entry'] else 'N/A'}</td>
            <td style="padding:12px">{state_badge}</td>
        </tr>"""
    
    html = f"""<!DOCTYPE html><html><head><title>Dashboard</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>📊 Dashboard Principal</h1><p>Vue d'ensemble de vos trades</p></div>
    {NAV}
    
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(350px,1fr));">
        <div class="card">
            <h2>😱 Fear & Greed Index</h2>
            <div id="fearGreedLoader" style="text-align:center;padding:40px;">
                <p style="color:#64748b">⏳ Chargement...</p>
            </div>
            <div id="fearGreedContent" style="display:none;">
                <div class="gauge" id="fearGreedGauge">
                    <div class="gauge-inner">
                        <div class="gauge-value" id="fearGreedValue">--</div>
                        <div class="gauge-label">/ 100</div>
                    </div>
                </div>
                <div style="text-align:center;margin-top:24px;">
                    <div style="font-size:24px;font-weight:900;margin-bottom:8px;" id="fearGreedSentiment">--</div>
                    <p style="color:#64748b;font-size:14px;" id="fearGreedReco">--</p>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>🚀 Bull Run Phase</h2>
            <div id="bullrunLoader" style="text-align:center;padding:40px;">
                <p style="color:#64748b">⏳ Chargement...</p>
            </div>
            <div id="bullrunContent" style="display:none;">
                <div style="text-align:center;margin-bottom:24px;">
                    <div style="font-size:64px;margin-bottom:8px;" id="bullrunEmoji">--</div>
                    <div style="font-size:24px;font-weight:900;margin-bottom:8px;" id="bullrunPhase">--</div>
                    <p style="color:#64748b;font-size:14px;" id="bullrunDesc">--</p>
                    <div style="margin-top:12px;">
                        <span class="badge" style="background:rgba(99,102,241,0.15);color:#6366f1" id="bullrunConfidence">--</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="card" id="bullrunPhasesCard" style="display:none;">
        <h2>📈 Phases du Cycle</h2>
        <div id="phase1" class="phase-indicator" style="color:#f7931a;">
            <div class="phase-number">₿</div>
            <div style="flex:1;">
                <div style="font-weight:700;margin-bottom:4px;">Phase 1: Bitcoin Season</div>
                <div style="font-size:12px;color:#64748b;">BTC domine, alts stagnent</div>
                <div style="font-size:12px;margin-top:4px;" id="phase1Stats">--</div>
            </div>
        </div>
        <div id="phase2" class="phase-indicator" style="color:#627eea;">
            <div class="phase-number">💎</div>
            <div style="flex:1;">
                <div style="font-weight:700;margin-bottom:4px;">Phase 2: ETH & Large-Cap Season</div>
                <div style="font-size:12px;color:#64748b;">ETH et top alts surperforment</div>
                <div style="font-size:12px;margin-top:4px;" id="phase2Stats">--</div>
            </div>
        </div>
        <div id="phase3" class="phase-indicator" style="color:#10b981;">
            <div class="phase-number">🚀</div>
            <div style="flex:1;">
                <div style="font-weight:700;margin-bottom:4px;">Phase 3: Altcoin Season</div>
                <div style="font-size:12px;color:#64748b;">Tous les alts explosent</div>
                <div style="font-size:12px;margin-top:4px;" id="phase3Stats">--</div>
            </div>
        </div>
    </div>
    
    <div class="grid">
        <div class="metric"><div class="metric-label">Total Trades</div><div class="metric-value">{len(rows)}</div></div>
        <div class="metric"><div class="metric-label">Actifs</div><div class="metric-value">{sum(1 for r in rows if r['row_state']=='normal')}</div></div>
        <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int((sum(1 for r in rows if r['row_state']=='tp') / max(1, sum(1 for r in rows if r['row_state'] in ('tp','sl')))) * 100)}%</div></div>
    </div>
    
    <div class="card"><h2>Derniers Trades</h2>
    <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:2px solid rgba(99,102,241,0.2)">
            <th style="padding:12px;text-align:left;color:#64748b">Symbol</th>
            <th style="padding:12px;text-align:left;color:#64748b">TF</th>
            <th style="padding:12px;text-align:left;color:#64748b">Side</th>
            <th style="padding:12px;text-align:left;color:#64748b">Entry</th>
            <th style="padding:12px;text-align:left;color:#64748b">Status</th>
        </tr></thead>
        <tbody>{table_rows}</tbody>
    </table>
    </div>
    
    <script>
    async function loadFearGreed() {{
        try {{
            const res = await fetch('/api/fear-greed');
            const data = await res.json();
            
            if (data.ok) {{
                const fg = data.fear_greed;
                document.getElementById('fearGreedLoader').style.display = 'none';
                document.getElementById('fearGreedContent').style.display = 'block';
                
                document.getElementById('fearGreedValue').textContent = fg.value;
                document.getElementById('fearGreedValue').style.color = fg.color;
                document.getElementById('fearGreedSentiment').textContent = fg.emoji + ' ' + fg.sentiment;
                document.getElementById('fearGreedSentiment').style.color = fg.color;
                document.getElementById('fearGreedReco').textContent = fg.recommendation;
            }}
        }} catch (e) {{
            console.error('Error loading Fear & Greed:', e);
        }}
    }}
    
    async function loadBullrunPhase() {{
        try {{
            const res = await fetch('/api/bullrun-phase');
            const data = await res.json();
            
            if (data.ok) {{
                const br = data.bullrun_phase;
                document.getElementById('bullrunLoader').style.display = 'none';
                document.getElementById('bullrunContent').style.display = 'block';
                document.getElementById('bullrunPhasesCard').style.display = 'block';
                
                document.getElementById('bullrunEmoji').textContent = br.emoji;
                document.getElementById('bullrunPhase').textContent = br.phase_name;
                document.getElementById('bullrunPhase').style.color = br.color;
                document.getElementById('bullrunDesc').textContent = br.description;
                document.getElementById('bullrunConfidence').textContent = 'Confiance: ' + br.confidence + '%';
                
                ['phase1', 'phase2', 'phase3'].forEach((id, idx) => {{
                    const el = document.getElementById(id);
                    if (idx + 1 === br.phase) {{
                        el.classList.add('active');
                    }} else {{
                        el.classList.remove('active');
                    }}
                }});
                
                const d = br.details;
                document.getElementById('phase1Stats').textContent = 
                    `WR: ${{d.btc.winrate}}% | Avg: ${{d.btc.avg_return}}% | ${{d.btc.trades}} trades`;
                document.getElementById('phase2Stats').textContent = 
                    `ETH WR: ${{d.eth.winrate}}% | LC WR: ${{d.large_cap.winrate}}%`;
                document.getElementById('phase3Stats').textContent = 
                    `WR: ${{d.small_alts.winrate}}% | Avg: ${{d.small_alts.avg_return}}% | ${{d.small_alts.trades}} trades`;
            }}
        }} catch (e) {{
            console.error('Error loading Bull Run Phase:', e);
        }}
    }}
    
    loadFearGreed();
    loadBullrunPhase();
    
    setInterval(() => {{
        loadFearGreed();
        loadBullrunPhase();
    }}, 300000);
    </script>
    
    </div></body></html>"""
    return html

@app.get("/altseason", response_class=HTMLResponse)
async def altseason_page():
    return """<!DOCTYPE html><html><head><title>Altseason</title></head>
    <body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3">
    <h1 style="color:#6366f1">🚀 Altseason Detector</h1>
    <p>Page altseason disponible</p>
    <p><a href="/trades" style="color:#8b5cf6">← Retour au Dashboard</a></p>
    </body></html>"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info("🚀 Starting AI Trader Pro v3.0...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
