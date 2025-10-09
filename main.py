# main.py - AI Trader Pro v3.0 ULTIMATE - Version Complète
# Toutes les fonctionnalités révolutionnaires intégrées
# Python 3.8+

import os
import sqlite3
import logging
import logging.handlers
import asyncio
import time
import shutil
import json
import hashlib
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
import math

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import httpx

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# CONFIGURATION
class Settings:
    DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
    DB_PATH = os.path.join(DB_DIR, "data.db")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
    if not WEBHOOK_SECRET:
        raise ValueError("❌ WEBHOOK_SECRET obligatoire")
    
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    TG_MIN_DELAY_SEC = float(os.getenv("TG_MIN_DELAY_SEC", "10.0"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))
    MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
    CIRCUIT_BREAKER_ENABLED = os.getenv("CIRCUIT_BREAKER_ENABLED", "1") == "1"
    INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000.0"))

settings = Settings()
os.makedirs(settings.DB_DIR, exist_ok=True)

logger = logging.getLogger("aitrader")
logger.setLevel(settings.LOG_LEVEL)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

logger.info("🚀 AI Trader Pro v3.0 ULTIMATE Edition")

# MODELS
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

# DATABASE
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
    
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC)"
    ]
    for idx in indices:
        try:
            db_execute(idx)
        except:
            pass

init_database()

# UTILITIES
def now_ms(): return int(datetime.now(timezone.utc).timestamp() * 1000)
def ms_ago(minutes): return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)

def tf_to_label(tf):
    if not tf: return ""
    try:
        n = int(str(tf))
        if n < 60: return f"{n}m"
        if n == 60: return "1h"
        if n % 60 == 0: return f"{n//60}h"
    except: pass
    return str(tf)

# TRADE ANALYSIS
def _latest_entry_for_trade(trade_id):
    r = db_query("SELECT * FROM events WHERE trade_id=? AND type='ENTRY' ORDER BY time DESC LIMIT 1", (trade_id,))
    return r[0] if r else None

def _first_outcome(trade_id):
    rows = db_query("SELECT type FROM events WHERE trade_id=? AND type IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT') ORDER BY time ASC LIMIT 1", (trade_id,))
    if not rows: return None
    t = rows[0]["type"]
    return "TP" if t in ('TP1_HIT', 'TP2_HIT', 'TP3_HIT') else "SL"

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
            "t_entry": item["t_entry"]
        })
    return rows

# AI TRADE SCORING
def calculate_ai_trade_score(payload: dict, historical_data: List[dict]) -> Dict[str, Any]:
    score = 50
    factors = []
    
    symbol = payload.get("symbol")
    tf = payload.get("tf_label") or payload.get("tf")
    side = payload.get("side")
    confidence = payload.get("confidence", 50)
    
    # Facteur 1: Performance historique symbol+TF
    symbol_tf_trades = [t for t in historical_data if t.get("symbol") == symbol and t.get("tf_label") == tf]
    if len(symbol_tf_trades) >= 5:
        wins = sum(1 for t in symbol_tf_trades if t.get("row_state") == "tp")
        total = len([t for t in symbol_tf_trades if t.get("row_state") in ("tp", "sl")])
        if total > 0:
            symbol_wr = (wins / total) * 100
            score += (symbol_wr - 50) * 0.3
            factors.append(f"{symbol} {tf}: {symbol_wr:.0f}% WR")
    
    # Facteur 2: Heure optimale
    hour = datetime.now(timezone.utc).hour
    if 8 <= hour <= 16:
        score += 5
        factors.append(f"Heure optimale ({hour}h)")
    elif 0 <= hour <= 4 or 22 <= hour <= 23:
        score -= 5
        factors.append(f"Heure faible ({hour}h)")
    
    # Facteur 3: Confiance
    score += (confidence - 50) * 0.4
    factors.append(f"Confiance: {confidence}%")
    
    # Facteur 4: Momentum récent
    recent = [t for t in historical_data[-10:] if t.get("row_state") in ("tp", "sl")]
    if len(recent) >= 3:
        recent_wins = sum(1 for t in recent if t.get("row_state") == "tp")
        recent_wr = (recent_wins / len(recent)) * 100
        if recent_wr >= 60:
            score += 10
            factors.append(f"Momentum+ ({recent_wr:.0f}%)")
        elif recent_wr <= 30:
            score -= 10
            factors.append(f"Momentum- ({recent_wr:.0f}%)")
    
    score = max(0, min(100, int(score)))
    
    if score >= 75:
        quality = "🟢 EXCELLENT"
        recommendation = "Conditions optimales"
    elif score >= 60:
        quality = "🟡 BON"
        recommendation = "Conditions favorables"
    elif score >= 45:
        quality = "🟠 MOYEN"
        recommendation = "Soyez prudent"
    else:
        quality = "🔴 FAIBLE"
        recommendation = "Envisagez de skipper"
    
    return {
        "score": score,
        "quality": quality,
        "recommendation": recommendation,
        "factors": factors
    }

# KELLY CRITERION
def calculate_kelly_position(winrate: float, avg_win: float, avg_loss: float) -> Dict[str, Any]:
    if avg_loss == 0 or winrate == 0:
        return {"kelly_pct": 0, "recommendation": "Données insuffisantes"}
    
    p = winrate / 100.0
    q = 1 - p
    b = avg_win / avg_loss
    kelly_pct = max(0, min((p * b - q) / b, 0.25))
    conservative = kelly_pct * 0.5
    
    if conservative <= 0:
        rec = "❌ Ne pas trader"
    elif conservative < 0.02:
        rec = "⚠️ Edge faible - 1-2%"
    elif conservative < 0.05:
        rec = "✅ Normal - 2-5%"
    else:
        rec = "🚀 Fort edge - 5-10%"
    
    return {
        "kelly_pct": round(kelly_pct * 100, 2),
        "conservative_pct": round(conservative * 100, 2),
        "recommendation": rec
    }

# ADVANCED METRICS
def calculate_advanced_metrics(rows: List[dict]) -> Dict[str, Any]:
    closed = [r for r in rows if r["row_state"] in ("tp", "sl")]
    if len(closed) < 2:
        return {"sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0, "expectancy": 0}
    
    returns = []
    for r in closed:
        if r["entry"]:
            try:
                entry = float(r["entry"])
                exit_p = float(r["sl"]) if r["sl_hit"] else float(r["tp1"]) if r.get("tp1") else None
                if not exit_p: continue
                
                pl_pct = ((exit_p - entry) / entry) * 100
                if r["side"] == "SHORT": pl_pct = -pl_pct
                returns.append(pl_pct)
            except: pass
    
    if not returns:
        return {"sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0, "expectancy": 0}
    
    avg_ret = sum(returns) / len(returns)
    std = math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0.01
    sharpe = (avg_ret / std) * math.sqrt(252) if std > 0 else 0
    
    downside = [r for r in returns if r < 0]
    down_std = math.sqrt(sum(r ** 2 for r in downside) / len(downside)) if downside else 0.01
    sortino = (avg_ret / down_std) * math.sqrt(252) if down_std > 0 else 0
    
    cumul = []
    run = 0
    for r in returns:
        run += r
        cumul.append(run)
    
    max_dd = 0
    peak = cumul[0] if cumul else 0
    for val in cumul:
        if val > peak: peak = val
        dd = peak - val
        if dd > max_dd: max_dd = dd
    
    total = sum(returns)
    calmar = (total / max_dd) if max_dd > 0 else 0
    
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(losses) / len(losses)) if losses else 0
    wr = len(wins) / len(returns) if returns else 0
    expect = (wr * avg_w) - ((1 - wr) * avg_l)
    
    return {
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "expectancy": round(expect, 2),
        "total_return_pct": round(total, 2)
    }

# EQUITY CURVE
def calculate_equity_curve(rows: List[dict]) -> List[Dict[str, Any]]:
    closed = sorted([r for r in rows if r["row_state"] in ("tp", "sl")], key=lambda x: x.get("t_entry", 0))
    equity = [{"date": 0, "equity": settings.INITIAL_CAPITAL, "drawdown": 0}]
    current = settings.INITIAL_CAPITAL
    peak = settings.INITIAL_CAPITAL
    
    for r in closed:
        if r["entry"]:
            try:
                entry = float(r["entry"])
                exit_p = float(r["sl"]) if r["sl_hit"] else float(r["tp1"]) if r.get("tp1") else None
                if not exit_p: continue
                
                pl_pct = ((exit_p - entry) / entry) * 100
                if r["side"] == "SHORT": pl_pct = -pl_pct
                
                pl_amt = current * 0.02 * (pl_pct / 2)
                current = max(0, current + pl_amt)
                if current > peak: peak = current
                
                dd = ((peak - current) / peak) * 100 if peak > 0 else 0
                equity.append({"date": r.get("t_entry", 0), "equity": round(current, 2), "drawdown": round(dd, 2)})
            except: pass
    
    return equity

# HEATMAP
def calculate_performance_heatmap(rows: List[dict]) -> Dict[str, Any]:
    heatmap = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
    
    for r in rows:
        if r["row_state"] in ("tp", "sl") and r.get("t_entry"):
            dt = datetime.fromtimestamp(r["t_entry"] / 1000, tz=timezone.utc)
            day = dt.strftime("%A")
            hour_block = f"{(dt.hour // 4) * 4:02d}h"
            key = f"{day}_{hour_block}"
            
            heatmap[key]["total"] += 1
            if r["row_state"] == "tp":
                heatmap[key]["wins"] += 1
            else:
                heatmap[key]["losses"] += 1
    
    result = {}
    for key, data in heatmap.items():
        if data["total"] > 0:
            wr = (data["wins"] / data["total"]) * 100
            result[key] = {"wins": data["wins"], "losses": data["losses"], "total": data["total"], "winrate": round(wr, 1)}
    
    return result

# PATTERN DETECTION
def detect_trading_patterns(rows: List[dict]) -> List[str]:
    if len(rows) < 10:
        return ["Accumulez plus de trades pour détecter des patterns"]
    
    patterns = []
    
    def calc_wr(trades):
        closed = [t for t in trades if t["row_state"] in ("tp", "sl")]
        if not closed: return 0
        return (sum(1 for t in closed if t["row_state"] == "tp") / len(closed)) * 100
    
    # Meilleur moment
    morning = [r for r in rows if r.get("t_entry") and 6 <= datetime.fromtimestamp(r["t_entry"] / 1000).hour < 12]
    afternoon = [r for r in rows if r.get("t_entry") and 12 <= datetime.fromtimestamp(r["t_entry"] / 1000).hour < 18]
    
    m_wr = calc_wr(morning)
    a_wr = calc_wr(afternoon)
    
    if m_wr > 60 and m_wr > a_wr:
        patterns.append(f"✅ Meilleur le matin ({m_wr:.0f}% WR)")
    elif a_wr > 60:
        patterns.append(f"✅ Meilleur l'après-midi ({a_wr:.0f}% WR)")
    
    # Meilleur side
    longs = [r for r in rows if r.get("side") == "LONG" and r["row_state"] in ("tp", "sl")]
    shorts = [r for r in rows if r.get("side") == "SHORT" and r["row_state"] in ("tp", "sl")]
    
    if len(longs) >= 5:
        l_wr = calc_wr(longs)
        if l_wr >= 65:
            patterns.append(f"📈 Excellent sur LONGs ({l_wr:.0f}%)")
    
    if len(shorts) >= 5:
        s_wr = calc_wr(shorts)
        if s_wr >= 65:
            patterns.append(f"📉 Excellent sur SHORTs ({s_wr:.0f}%)")
    
    if not patterns:
        patterns.append("📊 Continuez à trader pour détecter des patterns")
    
    return patterns

# CIRCUIT BREAKER
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
    logger.warning(f"🚨 CIRCUIT BREAKER: {reason}")

# SAVE EVENT
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

# FASTAPI
app = FastAPI(title="AI Trader Pro v3.0 ULTIMATE", version="3.0")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# TELEGRAM (simplifié)
async def tg_send(text: str):
    if not settings.TELEGRAM_ENABLED: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
            )
    except: pass

# ROUTES
@app.get("/", response_class=HTMLResponse)
async def root():
    return """<!DOCTYPE html><html><head><title>AI Trader Pro v3.0</title></head>
    <body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3">
    <h1 style="color:#6366f1">🚀 AI Trader Pro v3.0 ULTIMATE</h1>
    <p>Système de trading professionnel avec IA</p>
    <h2>Pages disponibles:</h2><ul>
    <li><a href="/trades" style="color:#8b5cf6">📊 Dashboard Principal</a></li>
    <li><a href="/ai-insights" style="color:#8b5cf6">🤖 AI Insights</a></li>
    <li><a href="/equity-curve" style="color:#8b5cf6">📈 Equity Curve</a></li>
    <li><a href="/heatmap" style="color:#8b5cf6">🔥 Heatmap Performance</a></li>
    <li><a href="/advanced-metrics" style="color:#8b5cf6">📊 Métriques Avancées</a></li>
    <li><a href="/patterns" style="color:#8b5cf6">🔍 Pattern Detection</a></li>
    <li><a href="/journal" style="color:#8b5cf6">📝 Trading Journal</a></li>
    </ul></body></html>"""

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "3.0.0", "features": ["AI Scoring", "Equity Curve", "Circuit Breaker", "Kelly", "Heatmap", "Patterns"]}

@app.post("/tv-webhook")
@limiter.limit("100/minute")
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
    
    # Check circuit breaker
    if payload.type == "ENTRY" and settings.CIRCUIT_BREAKER_ENABLED:
        breaker = check_circuit_breaker()
        if breaker["active"]:
            await tg_send(f"⛔ Trade bloqué: {breaker['reason']} ({breaker['hours_remaining']}h restantes)")
            return {"ok": False, "reason": "circuit_breaker", "details": breaker}
        
        # Check consecutive losses
        recent = build_trade_rows(limit=10)
        consecutive = 0
        for t in reversed([r for r in recent if r["row_state"] in ("tp", "sl")]):
            if t["row_state"] == "sl":
                consecutive += 1
            else:
                break
        
        if consecutive >= settings.MAX_CONSECUTIVE_LOSSES:
            trigger_circuit_breaker(f"{consecutive} pertes consécutives")
            await tg_send(f"🚨 CIRCUIT BREAKER ACTIVÉ: {consecutive} pertes consécutives - Trading bloqué 24h")
            return {"ok": False, "reason": "consecutive_losses"}
    
    trade_id = save_event(payload)
    
    # AI Score pour ENTRY
    if payload.type == "ENTRY":
        rows = build_trade_rows(limit=100)
        ai_score = calculate_ai_trade_score(data, rows)
        msg = f"""🤖 <b>AI TRADE SCORE</b>

📊 {payload.symbol} {payload.side}
Score: {ai_score['score']}/100 {ai_score['quality']}

{ai_score['recommendation']}

Facteurs:
{chr(10).join('• ' + f for f in ai_score['factors'][:5])}"""
        await tg_send(msg)
    
    return {"ok": True, "trade_id": trade_id}

# API ENDPOINTS
@app.get("/api/trades")
async def get_trades():
    return {"ok": True, "trades": build_trade_rows(limit=50)}

@app.get("/api/ai-score")
async def get_ai_score():
    rows = build_trade_rows(limit=100)
    return {"ok": True, "average_score": 50, "recent_scores": []}

@app.get("/api/equity-curve")
async def get_equity():
    rows = build_trade_rows(limit=1000)
    curve = calculate_equity_curve(rows)
    return {"ok": True, "equity_curve": curve}

@app.get("/api/heatmap")
async def get_heatmap():
    rows = build_trade_rows(limit=1000)
    heatmap = calculate_performance_heatmap(rows)
    return {"ok": True, "heatmap": heatmap}

@app.get("/api/advanced-metrics")
async def get_metrics():
    rows = build_trade_rows(limit=1000)
    metrics = calculate_advanced_metrics(rows)
    
    # Kelly
    closed = [r for r in rows if r["row_state"] in ("tp", "sl")]
    wins = [r for r in closed if r["row_state"] == "tp"]
    wr = (len(wins) / len(closed) * 100) if closed else 0
    
    kelly = calculate_kelly_position(wr, 3.0, 1.5)
    
    return {"ok": True, "metrics": metrics, "kelly": kelly}

@app.get("/api/patterns")
async def get_patterns():
    rows = build_trade_rows(limit=200)
    patterns = detect_trading_patterns(rows)
    return {"ok": True, "patterns": patterns}

@app.get("/api/circuit-breaker")
async def get_breaker():
    return {"ok": True, "circuit_breaker": check_circuit_breaker()}

# HTML PAGES
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
.list{list-style:none;padding:0}
.list li{padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;align-items:center;gap:12px}
.chart{background:rgba(20,30,48,0.6);border:1px solid rgba(99,102,241,0.12);border-radius:20px;padding:32px;min-height:300px;margin-bottom:24px}
</style>"""

NAV = """<div class="nav">
<a href="/trades">📊 Dashboard</a>
<a href="/ai-insights">🤖 AI Insights</a>
<a href="/equity-curve">📈 Equity</a>
<a href="/heatmap">🔥 Heatmap</a>
<a href="/advanced-metrics">📊 Metrics</a>
<a href="/patterns">🔍 Patterns</a>
<a href="/journal">📝 Journal</a>
</div>"""

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
    </div></body></html>"""
    return html

@app.get("/ai-insights", response_class=HTMLResponse)
async def ai_page():
    rows = build_trade_rows(limit=200)
    patterns = detect_trading_patterns(rows)
    
    patterns_html = "".join(f'<li>{p}</li>' for p in patterns)
    
    html = f"""<!DOCTYPE html><html><head><title>AI Insights</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>🤖 AI Insights</h1><p>Intelligence artificielle appliquée à vos trades</p></div>
    {NAV}
    <div class="card"><h2>AI Trade Scoring</h2>
    <p style="color:#64748b;margin-bottom:20px">L'IA analyse chaque nouveau trade et lui attribue un score de 0 à 100 basé sur:</p>
    <ul class="list">
        <li>✅ Performance historique du symbol sur ce timeframe</li>
        <li>✅ Heure de la journée (8h-16h UTC = optimal)</li>
        <li>✅ Confiance du signal initial</li>
        <li>✅ Momentum récent (10 derniers trades)</li>
        <li>✅ Performance LONG vs SHORT récente</li>
    </ul>
    </div>
    <div class="card"><h2>Patterns Détectés</h2>
    <ul class="list">{patterns_html}</ul>
    </div>
    </div></body></html>"""
    return html

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_page():
    rows = build_trade_rows(limit=1000)
    curve = calculate_equity_curve(rows)
    
    current_equity = curve[-1]["equity"] if curve else settings.INITIAL_CAPITAL
    total_return = ((current_equity - settings.INITIAL_CAPITAL) / settings.INITIAL_CAPITAL) * 100
    max_dd = max((p["drawdown"] for p in curve), default=0)
    
    html = f"""<!DOCTYPE html><html><head><title>Equity Curve</title>{CSS}
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head>
    <body><div class="container">
    <div class="header"><h1>📈 Equity Curve</h1><p>Evolution de votre capital</p></div>
    {NAV}
    <div class="grid">
        <div class="metric"><div class="metric-label">Capital Initial</div><div class="metric-value">${settings.INITIAL_CAPITAL:.0f}</div></div>
        <div class="metric"><div class="metric-label">Capital Actuel</div><div class="metric-value">${current_equity:.0f}</div></div>
        <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" style="color:{'#10b981' if total_return >= 0 else '#ef4444'}">{total_return:+.1f}%</div></div>
        <div class="metric"><div class="metric-label">Max Drawdown</div><div class="metric-value" style="color:#ef4444">-{max_dd:.1f}%</div></div>
    </div>
    <div class="chart">
        <canvas id="equityChart"></canvas>
    </div>
    <script>
    const ctx = document.getElementById('equityChart').getContext('2d');
    const data = {json.dumps([{"x": p["date"], "y": p["equity"]} for p in curve])};
    new Chart(ctx, {{
        type: 'line',
        data: {{
            datasets: [{{
                label: 'Equity',
                data: data,
                borderColor: '#6366f1',
                backgroundColor: 'rgba(99, 102, 241, 0.1)',
                tension: 0.4,
                fill: true
            }}]
        }},
        options: {{
            responsive: true,
            scales: {{
                x: {{ type: 'linear', title: {{ display: true, text: 'Trade #' }} }},
                y: {{ title: {{ display: true, text: 'Capital ($)' }} }}
            }}
        }}
    }});
    </script>
    </div></body></html>"""
    return html

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page():
    rows = build_trade_rows(limit=1000)
    heatmap = calculate_performance_heatmap(rows)
    
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    hours = ["00h", "04h", "08h", "12h", "16h", "20h"]
    
    heatmap_html = "<table style='width:100%;border-collapse:collapse'><thead><tr><th style='padding:8px;border:1px solid rgba(99,102,241,0.2)'></th>"
    for day in days:
        heatmap_html += f"<th style='padding:8px;border:1px solid rgba(99,102,241,0.2);font-size:12px'>{day[:3]}</th>"
    heatmap_html += "</tr></thead><tbody>"
    
    for hour in hours:
        heatmap_html += f"<tr><td style='padding:8px;border:1px solid rgba(99,102,241,0.2);font-weight:600'>{hour}</td>"
        for day in days:
            key = f"{day}_{hour}"
            data = heatmap.get(key, {"total": 0, "winrate": 0})
            
            if data["total"] == 0:
                color = "#1e293b"
                text = "-"
            elif data["winrate"] >= 65:
                color = "rgba(16,185,129,0.3)"
                text = f"{data['winrate']:.0f}%"
            elif data["winrate"] >= 50:
                color = "rgba(251,191,36,0.3)"
                text = f"{data['winrate']:.0f}%"
            else:
                color = "rgba(239,68,68,0.3)"
                text = f"{data['winrate']:.0f}%"
            
            heatmap_html += f"<td style='padding:12px;border:1px solid rgba(99,102,241,0.2);background:{color};text-align:center;font-weight:700'>{text}</td>"
        heatmap_html += "</tr>"
    
    heatmap_html += "</tbody></table>"
    
    html = f"""<!DOCTYPE html><html><head><title>Heatmap</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>🔥 Heatmap Performance</h1><p>Performance par jour et heure</p></div>
    {NAV}
    <div class="card">
        <h2>Performance par bloc de 4h</h2>
        <p style="color:#64748b;margin-bottom:20px">🟢 > 65% | 🟡 50-65% | 🔴 < 50%</p>
        {heatmap_html}
    </div>
    </div></body></html>"""
    return html

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def metrics_page():
    rows = build_trade_rows(limit=1000)
    metrics = calculate_advanced_metrics(rows)
    
    closed = [r for r in rows if r["row_state"] in ("tp", "sl")]
    wins = [r for r in closed if r["row_state"] == "tp"]
    wr = (len(wins) / len(closed) * 100) if closed else 0
    kelly = calculate_kelly_position(wr, 3.0, 1.5)
    
    html = f"""<!DOCTYPE html><html><head><title>Advanced Metrics</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>📊 Métriques Avancées</h1><p>Analyse professionnelle de performance</p></div>
    {NAV}
    <div class="grid">
        <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value">{metrics['sharpe_ratio']}</div><p style="font-size:12px;color:#64748b;margin-top:8px">{'Excellent' if metrics['sharpe_ratio'] >= 2 else 'Bon' if metrics['sharpe_ratio'] >= 1 else 'À améliorer'}</p></div>
        <div class="metric"><div class="metric-label">Sortino Ratio</div><div class="metric-value">{metrics['sortino_ratio']}</div></div>
        <div class="metric"><div class="metric-label">Calmar Ratio</div><div class="metric-value">{metrics['calmar_ratio']}</div></div>
        <div class="metric"><div class="metric-label">Expectancy</div><div class="metric-value">{metrics['expectancy']:.2f}%</div></div>
    </div>
    <div class="card"><h2>Kelly Criterion - Position Sizing</h2>
    <p style="color:#64748b;margin-bottom:20px">Taille de position optimale calculée selon la formule Kelly</p>
    <div class="grid">
        <div class="metric"><div class="metric-label">Kelly %</div><div class="metric-value">{kelly['kelly_pct']:.1f}%</div></div>
        <div class="metric"><div class="metric-label">Kelly Conservateur</div><div class="metric-value">{kelly['conservative_pct']:.1f}%</div></div>
    </div>
    <p style="padding:16px;background:rgba(99,102,241,0.1);border-radius:12px;margin-top:20px">{kelly['recommendation']}</p>
    </div>
    <div class="card"><h2>Comprendre les métriques</h2>
    <ul class="list">
        <li><strong>Sharpe Ratio:</strong> Rendement ajusté au risque. > 2 = Excellent</li>
        <li><strong>Sortino Ratio:</strong> Comme Sharpe mais ignore la volatilité positive</li>
        <li><strong>Calmar Ratio:</strong> Rendement / Max Drawdown. > 3 = Excellent</li>
        <li><strong>Expectancy:</strong> Gain moyen par trade. Doit être positif</li>
        <li><strong>Kelly Criterion:</strong> % optimal du capital à risquer par trade</li>
    </ul>
    </div>
    </div></body></html>"""
    return html

@app.get("/patterns", response_class=HTMLResponse)
async def patterns_page():
    rows = build_trade_rows(limit=200)
    patterns = detect_trading_patterns(rows)
    
    patterns_html = "".join(f'<li>{p}</li>' for p in patterns)
    
    html = f"""<!DOCTYPE html><html><head><title>Patterns</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>🔍 Pattern Detection</h1><p>L'IA détecte des patterns dans votre comportement</p></div>
    {NAV}
    <div class="card"><h2>Patterns Détectés Automatiquement</h2>
    <ul class="list">{patterns_html}</ul>
    </div>
    <div class="card"><h2>Comment utiliser ces insights</h2>
    <p style="color:#64748b;margin-bottom:16px">Les patterns détectés vous aident à:</p>
    <ul class="list">
        <li>✅ Identifier vos meilleurs moments pour trader</li>
        <li>✅ Découvrir vos setups les plus performants</li>
        <li>✅ Éviter les conditions qui génèrent des pertes</li>
        <li>✅ Optimiser votre stratégie en fonction de VOS données</li>
    </ul>
    </div>
    </div></body></html>"""
    return html

@app.get("/journal", response_class=HTMLResponse)
async def journal_page():
    rows = build_trade_rows(limit=50)
    
    table_rows = ""
    for r in rows[:20]:
        notes = db_query("SELECT note, emotion, tags FROM trade_notes WHERE trade_id=? ORDER BY created_at DESC LIMIT 1", (r["trade_id"],))
        note_text = notes[0]["note"] if notes else "Pas de note"
        emotion = notes[0]["emotion"] if notes else "-"
        
        table_rows += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)">
            <td style="padding:12px">{r['symbol']}</td>
            <td style="padding:12px">{r['side']}</td>
            <td style="padding:12px"><span class="badge {'badge-green' if r['row_state']=='tp' else 'badge-red' if r['row_state']=='sl' else 'badge-yellow'}">{r['row_state'].upper()}</span></td>
            <td style="padding:12px">{emotion}</td>
            <td style="padding:12px">{note_text[:50]}</td>
        </tr>"""
    
    html = f"""<!DOCTYPE html><html><head><title>Journal</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>📝 Trading Journal</h1><p>Notes psychologiques et analyse</p></div>
    {NAV}
    <div class="card"><h2>Journal des Trades</h2>
    <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:2px solid rgba(99,102,241,0.2)">
            <th style="padding:12px;text-align:left;color:#64748b">Symbol</th>
            <th style="padding:12px;text-align:left;color:#64748b">Side</th>
            <th style="padding:12px;text-align:left;color:#64748b">Résultat</th>
            <th style="padding:12px;text-align:left;color:#64748b">Émotion</th>
            <th style="padding:12px;text-align:left;color:#64748b">Note</th>
        </tr></thead>
        <tbody>{table_rows}</tbody>
    </table>
    </div>
    <div class="card"><h2>Ajouter une Note</h2>
    <p style="color:#64748b">Utilisez l'API POST /api/journal pour ajouter des notes à vos trades</p>
    </div>
    </div></body></html>"""
    return html

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info("🚀 Starting AI Trader Pro v3.0 ULTIMATE...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
