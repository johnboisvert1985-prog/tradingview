# main.py - AI Trader Pro v3.0 - Version COMPLÈTE ET CORRIGÉE
# Python 3.8+

import os
import sqlite3
import logging
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

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    RATE_LIMIT_ENABLED = True
except ImportError:
    RATE_LIMIT_ENABLED = False

class Settings:
    DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
    DB_PATH = os.path.join(DB_DIR, "data.db")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
    if not WEBHOOK_SECRET:
        raise ValueError("❌ WEBHOOK_SECRET obligatoire")
    
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
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
logger.info("🚀 AI Trader Pro v3.0 ULTIMATE")

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
    db_execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, symbol TEXT NOT NULL,
        tf TEXT, tf_label TEXT, time INTEGER NOT NULL, side TEXT, entry REAL, sl REAL,
        tp1 REAL, tp2 REAL, tp3 REAL, confidence INTEGER, leverage TEXT, note TEXT,
        price REAL, trade_id TEXT, created_at INTEGER DEFAULT (strftime('%s', 'now')))""")
    
    db_execute("""CREATE TABLE IF NOT EXISTS trade_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL, note TEXT,
        emotion TEXT, tags TEXT, created_at INTEGER DEFAULT (strftime('%s', 'now')))""")
    
    db_execute("""CREATE TABLE IF NOT EXISTS circuit_breaker (
        id INTEGER PRIMARY KEY AUTOINCREMENT, triggered_at INTEGER NOT NULL,
        reason TEXT, cooldown_until INTEGER, active INTEGER DEFAULT 1)""")

init_database()

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
        
        rows.append({
            "trade_id": e["trade_id"], "symbol": e["symbol"], "tf": e.get("tf"),
            "tf_label": e.get("tf_label") or tf_to_label(e.get("tf")),
            "side": e.get("side"), "entry": e.get("entry"), "tp1": e.get("tp1"), "sl": e.get("sl"),
            "tp1_hit": tp1_hit, "sl_hit": sl_hit,
            "row_state": "sl" if sl_hit else ("tp" if tp1_hit else "normal"),
            "t_entry": item["t_entry"], "confidence": e.get("confidence", 50)
        })
    return rows

async def fetch_fear_greed_index() -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.alternative.me/fng/?limit=1")
            data = response.json()
            
            if data and "data" in data and len(data["data"]) > 0:
                fng = data["data"][0]
                value = int(fng["value"])
                
                if value <= 25: sentiment, emoji, color, rec = "Extreme Fear", "😱", "#ef4444", "Opportunité d'achat"
                elif value <= 45: sentiment, emoji, color, rec = "Fear", "😰", "#f97316", "Bon moment pour accumuler"
                elif value <= 55: sentiment, emoji, color, rec = "Neutral", "😐", "#64748b", "Marché équilibré"
                elif value <= 75: sentiment, emoji, color, rec = "Greed", "😊", "#10b981", "Soyez vigilant"
                else: sentiment, emoji, color, rec = "Extreme Greed", "🤑", "#22c55e", "Prenez des profits"
                
                return {"value": value, "sentiment": sentiment, "emoji": emoji, "color": color, "recommendation": rec}
    except Exception as e:
        logger.error(f"Fear & Greed error: {e}")
    
    return {"value": 50, "sentiment": "Unknown", "emoji": "❓", "color": "#64748b", "recommendation": "Données non disponibles"}

def detect_bullrun_phase(rows: List[dict]) -> Dict[str, Any]:
    """Détecte la phase du bull run - VERSION CORRIGÉE"""
    
    default_response = {
        "phase": 0, "phase_name": "Accumulation", "emoji": "🐻", "color": "#64748b",
        "description": "Pas assez de données", "confidence": 0,
        "details": {
            "btc": {"winrate": 0, "avg_return": 0, "trades": 0},
            "eth": {"winrate": 0, "avg_return": 0, "trades": 0},
            "large_cap": {"winrate": 0, "avg_return": 0, "trades": 0},
            "small_alts": {"winrate": 0, "avg_return": 0, "trades": 0}
        }
    }
    
    if len(rows) < 10:
        return default_response
    
    try:
        def calc_perf(trades):
            if not trades: return 0, 0
            wins = sum(1 for t in trades if t.get("row_state") == "tp")
            wr = (wins / len(trades)) * 100 if trades else 0
            returns = []
            for r in trades:
                if r.get("entry") and r.get("side"):
                    try:
                        entry = float(r["entry"])
                        exit_p = None
                        if r.get("sl_hit") and r.get("sl"):
                            exit_p = float(r["sl"])
                        elif r.get("tp1_hit") and r.get("tp1"):
                            exit_p = float(r["tp1"])
                        
                        if exit_p:
                            pl = ((exit_p - entry) / entry) * 100
                            if r.get("side") == "SHORT": pl = -pl
                            returns.append(pl)
                    except: pass
            return wr, (sum(returns) / len(returns) if returns else 0)
        
        btc = [r for r in rows if r.get("symbol") and "BTC" in r["symbol"].upper() and r.get("row_state") in ("tp", "sl")]
        eth = [r for r in rows if r.get("symbol") and "ETH" in r["symbol"].upper() and "BTC" not in r["symbol"].upper() and r.get("row_state") in ("tp", "sl")]
        lc_syms = ["SOL", "BNB", "ADA", "AVAX", "DOT", "MATIC", "LINK"]
        lc = [r for r in rows if r.get("symbol") and any(s in r["symbol"].upper() for s in lc_syms) and r.get("row_state") in ("tp", "sl")]
        alt = [r for r in rows if r.get("symbol") and r.get("row_state") in ("tp", "sl") and "BTC" not in r["symbol"].upper() and "ETH" not in r["symbol"].upper() and not any(s in r["symbol"].upper() for s in lc_syms)]
        
        btc_wr, btc_avg = calc_perf(btc)
        eth_wr, eth_avg = calc_perf(eth)
        lc_wr, lc_avg = calc_perf(lc)
        alt_wr, alt_avg = calc_perf(alt)
        
        btc_sc = (btc_wr * 0.6 + (btc_avg + 10) * 4) if btc else 0
        eth_sc = (eth_wr * 0.6 + (eth_avg + 10) * 4) if eth else 0
        lc_sc = (lc_wr * 0.6 + (lc_avg + 10) * 4) if lc else 0
        alt_sc = (alt_wr * 0.6 + (alt_avg + 10) * 4) if alt else 0
        
        details = {
            "btc": {"winrate": round(btc_wr, 1), "avg_return": round(btc_avg, 2), "trades": len(btc)},
            "eth": {"winrate": round(eth_wr, 1), "avg_return": round(eth_avg, 2), "trades": len(eth)},
            "large_cap": {"winrate": round(lc_wr, 1), "avg_return": round(lc_avg, 2), "trades": len(lc)},
            "small_alts": {"winrate": round(alt_wr, 1), "avg_return": round(alt_avg, 2), "trades": len(alt)}
        }
        
        if btc_sc > eth_sc and btc_sc > alt_sc and btc_wr > 55:
            return {"phase": 1, "phase_name": "Bitcoin Season", "emoji": "₿", "color": "#f7931a", "description": "BTC domine", "confidence": min(100, int(btc_sc - max(eth_sc, alt_sc))), "details": details}
        elif (eth_sc > btc_sc or lc_sc > btc_sc) and eth_wr > 50:
            return {"phase": 2, "phase_name": "ETH & Large-Cap", "emoji": "💎", "color": "#627eea", "description": "ETH surperforme", "confidence": min(100, int(max(eth_sc, lc_sc) - btc_sc)), "details": details}
        elif alt_sc > btc_sc and alt_sc > eth_sc and alt_wr > 55:
            return {"phase": 3, "phase_name": "Altcoin Season", "emoji": "🚀", "color": "#10b981", "description": "Alts explosent", "confidence": min(100, int(alt_sc - max(btc_sc, eth_sc))), "details": details}
        
        return {"phase": 0, "phase_name": "Accumulation", "emoji": "🐻", "color": "#64748b", "description": "Phase neutre", "confidence": 30, "details": details}
    
    except Exception as e:
        logger.error(f"Bull Run Phase error: {e}")
        return default_response

def calculate_altseason_metrics(rows: List[dict]) -> Dict[str, Any]:
    """Calcule si c'est l'altseason"""
    btc = [r for r in rows if r.get("symbol") and "BTC" in r["symbol"].upper() and r.get("row_state") in ("tp", "sl")]
    alt = [r for r in rows if r.get("symbol") and "BTC" not in r["symbol"].upper() and r.get("row_state") in ("tp", "sl")]
    
    if len(btc) < 3 or len(alt) < 5:
        return {"is_altseason": False, "confidence": 0, "btc_wr": 0, "alt_wr": 0, "message": "Pas assez de données"}
    
    btc_wins = sum(1 for t in btc if t.get("row_state") == "tp")
    alt_wins = sum(1 for t in alt if t.get("row_state") == "tp")
    
    btc_wr = (btc_wins / len(btc)) * 100
    alt_wr = (alt_wins / len(alt)) * 100
    
    is_alt = alt_wr > btc_wr and alt_wr > 55
    conf = min(100, int(abs(alt_wr - btc_wr)))
    
    return {
        "is_altseason": is_alt,
        "confidence": conf,
        "btc_wr": round(btc_wr, 1),
        "alt_wr": round(alt_wr, 1),
        "message": "🚀 ALTSEASON Active !" if is_alt else "₿ BTC Season" if btc_wr > alt_wr else "🔄 Phase Neutre"
    }

def run_backtest(rows: List[dict], filters: Dict[str, Any]) -> Dict[str, Any]:
    """Backtester avec matching flexible"""
    filtered = rows
    
    if filters.get("side"):
        filtered = [r for r in filtered if r.get("side") == filters["side"]]
    
    if filters.get("symbol"):
        sym = filters["symbol"].upper().strip()
        filtered = [r for r in filtered if r.get("symbol") and sym in r["symbol"].upper()]
    
    if filters.get("tf"):
        tf = filters["tf"].lower().strip()
        filtered = [r for r in filtered if r.get("tf_label", "").lower() == tf]
    
    closed = [r for r in filtered if r.get("row_state") in ("tp", "sl")]
    
    if not closed:
        return {"trades": 0, "winrate": 0, "total_return": 0, "avg_win": 0, "avg_loss": 0}
    
    wins = sum(1 for r in closed if r.get("row_state") == "tp")
    wr = (wins / len(closed)) * 100
    
    returns = []
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                entry = float(r["entry"])
                exit_p = float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None)
                if exit_p:
                    pl = ((exit_p - entry) / entry) * 100
                    if r["side"] == "SHORT": pl = -pl
                    returns.append(pl)
            except: pass
    
    if not returns:
        return {"trades": len(closed), "winrate": round(wr, 1), "wins": wins, "losses": len(closed)-wins, "total_return": 0, "avg_win": 0, "avg_loss": 0}
    
    win_rets = [r for r in returns if r > 0]
    loss_rets = [r for r in returns if r < 0]
    
    return {
        "trades": len(closed),
        "winrate": round(wr, 1),
        "wins": wins,
        "losses": len(closed) - wins,
        "total_return": round(sum(returns), 2),
        "avg_win": round(sum(win_rets) / len(win_rets), 2) if win_rets else 0,
        "avg_loss": round(abs(sum(loss_rets) / len(loss_rets)), 2) if loss_rets else 0,
        "best_trade": round(max(returns), 2) if returns else 0,
        "worst_trade": round(min(returns), 2) if returns else 0,
        "filters": filters
    }

def check_circuit_breaker() -> Dict[str, Any]:
    active = db_query("SELECT * FROM circuit_breaker WHERE active=1 AND cooldown_until > ? ORDER BY triggered_at DESC LIMIT 1", (int(time.time()),))
    if active:
        b = active[0]
        return {"active": True, "reason": b["reason"], "hours_remaining": round((b["cooldown_until"] - int(time.time())) / 3600, 1)}
    return {"active": False}

def trigger_circuit_breaker(reason: str):
    cooldown = int(time.time()) + (24 * 3600)
    db_execute("INSERT INTO circuit_breaker (triggered_at, reason, cooldown_until, active) VALUES (?, ?, ?, 1)", (int(time.time()), reason, cooldown))

def save_event(payload: WebhookPayload) -> str:
    trade_id = payload.trade_id or f"{payload.symbol}_{payload.tf}_{payload.time or now_ms()}"
    db_execute("""INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, confidence, leverage, note, price, trade_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        payload.type, payload.symbol, str(payload.tf) if payload.tf else None,
        payload.tf_label or tf_to_label(payload.tf), int(payload.time or now_ms()),
        payload.side, payload.entry, payload.sl, payload.tp1, payload.tp2, payload.tp3,
        payload.confidence, payload.leverage, payload.note, payload.price, trade_id))
    return trade_id

async def send_telegram(text: str):
    if not settings.TELEGRAM_ENABLED: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except: pass

app = FastAPI(title="AI Trader Pro v3.0", version="3.0")

if RATE_LIMIT_ENABLED:
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    def rate_limit(s): return lambda f: limiter.limit(s)(f)
else:
    def rate_limit(s): return lambda f: f

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

CSS = """<style>
body{margin:0;font-family:system-ui;background:#050a12;color:#e2e8f0}
.container{max-width:1200px;margin:0 auto;padding:40px 20px}
.header{margin-bottom:40px}
.header h1{font-size:36px;font-weight:900;background:linear-gradient(135deg,#6366f1,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
.nav{display:flex;gap:16px;margin-bottom:32px;flex-wrap:wrap}
.nav a{padding:12px 24px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:12px;color:#6366f1;text-decoration:none;font-weight:600;transition:all 0.3s}
.card{background:rgba(20,30,48,0.6);border:1px solid rgba(99,102,241,0.12);border-radius:20px;padding:32px;margin-bottom:24px}
.card h2{font-size:24px;font-weight:800;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:24px}
.metric{background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border:1px solid rgba(99,102,241,0.2);border-radius:12px;padding:20px}
.metric-label{font-size:12px;color:#64748b;font-weight:700;text-transform:uppercase;margin-bottom:8px}
.metric-value{font-size:32px;font-weight:900;color:#6366f1}
.badge{display:inline-block;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:700}
.badge-green{background:rgba(16,185,129,0.15);color:#10b981}
.badge-red{background:rgba(239,68,68,0.15);color:#ef4444}
.badge-yellow{background:rgba(251,191,36,0.15);color:#fbbf24}
.gauge{width:200px;height:200px;border-radius:50%;background:conic-gradient(from 180deg,#ef4444,#f97316 25%,#fbbf24 45%,#10b981 55%,#22c55e);position:relative;display:flex;align-items:center;justify-content:center;margin:0 auto}
.gauge-inner{width:160px;height:160px;border-radius:50%;background:#0a0f1a;display:flex;flex-direction:column;align-items:center;justify-content:center}
.gauge-value{font-size:48px;font-weight:900}
.gauge-label{font-size:12px;color:#64748b;margin-top:4px}
.phase-indicator{display:flex;align-items:center;gap:16px;padding:20px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border-radius:16px;margin-bottom:12px;position:relative}
.phase-indicator::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px}
.phase-indicator.active::before{background:currentColor}
.phase-number{width:48px;height:48px;border-radius:50%;background:rgba(99,102,241,0.2);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900}
.phase-indicator.active .phase-number{background:currentColor;color:#0a0f1a}
</style>"""

NAV = """<div class="nav">
<a href="/trades">📊 Dashboard</a>
<a href="/backtest">⏮️ Backtest</a>
<a href="/strategie">⚙️ Stratégie</a>
<a href="/altseason">🚀 Altseason</a>
</div>"""

@app.get("/")
async def root():
    return HTMLResponse("""<!DOCTYPE html><html><head><title>AI Trader</title></head>
    <body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3">
    <h1 style="color:#6366f1">🚀 AI Trader Pro v3.0</h1>
    <p><a href="/trades" style="color:#8b5cf6">📊 Dashboard</a></p></body></html>""")

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "3.0.0"}

@app.get("/api/fear-greed")
async def get_fear_greed():
    return {"ok": True, "fear_greed": await fetch_fear_greed_index()}

@app.get("/api/bullrun-phase")
async def get_bullrun_phase():
    rows = build_trade_rows(100)
    phase = detect_bullrun_phase(rows)
    return {"ok": True, "bullrun_phase": phase}

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return {"ok": True, "trades": build_trade_rows(limit)}

@app.get("/api/altseason")
async def get_altseason():
    rows = build_trade_rows(100)
    return {"ok": True, "altseason": calculate_altseason_metrics(rows)}

@app.post("/api/backtest")
async def post_backtest(filters: Dict[str, Any]):
    rows = build_trade_rows(1000)
    return {"ok": True, "backtest": run_backtest(rows, filters)}

@app.post("/tv-webhook")
@rate_limit("100/minute")
async def webhook(request: Request):
    try: data = await request.json()
    except: raise HTTPException(400, "Invalid JSON")
    
    if data.get("secret") != settings.WEBHOOK_SECRET: raise HTTPException(403)
    
    try: payload = WebhookPayload(**data)
    except Exception as e: raise HTTPException(422, str(e))
    
    if payload.type == "ENTRY" and settings.CIRCUIT_BREAKER_ENABLED:
        breaker = check_circuit_breaker()
        if breaker["active"]:
            await send_telegram(f"⛔ Trade bloqué: {breaker['reason']}")
            return {"ok": False, "reason": "circuit_breaker"}
        
        recent = build_trade_rows(10)
        consecutive = 0
        for t in reversed([r for r in recent if r.get("row_state") in ("tp", "sl")]):
            if t.get("row_state") == "sl": consecutive += 1
            else: break
        
        if consecutive >= settings.MAX_CONSECUTIVE_LOSSES:
            trigger_circuit_breaker(f"{consecutive} pertes")
            await send_telegram(f"🚨 CIRCUIT BREAKER: {consecutive} pertes")
            return {"ok": False, "reason": "consecutive_losses"}
    
    return {"ok": True, "trade_id": save_event(payload)}

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = build_trade_rows(50)
    table = ""
    for r in rows[:20]:
        badge = f'<span class="badge badge-green">TP</span>' if r.get("row_state")=="tp" else (f'<span class="badge badge-red">SL</span>' if r.get("row_state")=="sl" else f'<span class="badge badge-yellow">En cours</span>')
        table += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)">
            <td style="padding:12px">{r.get('symbol', 'N/A')}</td><td style="padding:12px">{r.get('tf_label', 'N/A')}</td>
            <td style="padding:12px">{r.get('side', 'N/A')}</td><td style="padding:12px">{r.get('entry') or 'N/A'}</td>
            <td style="padding:12px">{badge}</td></tr>"""
    
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Dashboard</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>📊 Dashboard</h1></div>{NAV}
    
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(350px,1fr))">
        <div class="card"><h2>😱 Fear & Greed</h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>
        <div class="card"><h2>🚀 Bull Run Phase</h2><div id="br" style="text-align:center;padding:40px">⏳</div></div>
    </div>
    
    <div class="card" id="phases" style="display:none"><h2>📈 Phases</h2>
        <div id="p1" class="phase-indicator" style="color:#f7931a">
            <div class="phase-number">₿</div>
            <div style="flex:1"><div style="font-weight:700">Phase 1: Bitcoin</div>
            <div style="font-size:12px;color:#64748b" id="p1s">--</div></div></div>
        <div id="p2" class="phase-indicator" style="color:#627eea">
            <div class="phase-number">💎</div>
            <div style="flex:1"><div style="font-weight:700">Phase 2: ETH & Large-Cap</div>
            <div style="font-size:12px;color:#64748b" id="p2s">--</div></div></div>
        <div id="p3" class="phase-indicator" style="color:#10b981">
            <div class="phase-number">🚀</div>
            <div style="flex:1"><div style="font-weight:700">Phase 3: Altcoin</div>
            <div style="font-size:12px;color:#64748b" id="p3s">--</div></div></div>
    </div>
    
    <div class="grid">
        <div class="metric"><div class="metric-label">Total</div><div class="metric-value">{len(rows)}</div></div>
        <div class="metric"><div class="metric-label">Actifs</div><div class="metric-value">{sum(1 for r in rows if r.get('row_state')=='normal')}</div></div>
        <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int((sum(1 for r in rows if r.get('row_state')=='tp')/max(1,sum(1 for r in rows if r.get('row_state') in ('tp','sl'))))*100)}%</div></div>
    </div>
    
    <div class="card"><h2>Derniers Trades</h2>
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
    fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
    document.getElementById('fg').innerHTML=`<div class="gauge"><div class="gauge-inner">
    <div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
    <div class="gauge-label">/ 100</div></div></div>
    <div style="text-align:center;margin-top:24px;font-size:24px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
    <p style="color:#64748b;font-size:14px;text-align:center">${{f.recommendation}}</p>`;}}}}).catch(e=>{{console.error(e);document.getElementById('fg').innerHTML='<p style="color:#ef4444">Erreur chargement</p>';}});
    
    fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
    document.getElementById('br').innerHTML=`<div style="font-size:64px;margin-bottom:8px">${{b.emoji}}</div>
    <div style="font-size:24px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>
    <p style="color:#64748b;font-size:14px">${{b.description}}</p>
    <span class="badge" style="background:rgba(99,102,241,0.15);color:#6366f1">Conf: ${{b.confidence}}%</span>`;
    document.getElementById('phases').style.display='block';
    ['p1','p2','p3'].forEach((id,i)=>{{const el=document.getElementById(id);
    if(i+1===b.phase)el.classList.add('active');else el.classList.remove('active');}});
    const det=b.details;
    document.getElementById('p1s').textContent=`WR: ${{det.btc.winrate}}% | ${{det.btc.trades}} trades`;
    document.getElementById('p2s').textContent=`ETH: ${{det.eth.winrate}}% | LC: ${{det.large_cap.winrate}}%`;
    document.getElementById('p3s').textContent=`WR: ${{det.small_alts.winrate}}% | ${{det.small_alts.trades}} trades`;}}}}).catch(e=>{{console.error(e);document.getElementById('br').innerHTML='<p style="color:#ef4444">Erreur chargement</p>';}});
    </script>
    </div></body></html>""")

@app.get("/altseason", response_class=HTMLResponse)
async def altseason_page():
    rows = build_trade_rows(100)
    alt = calculate_altseason_metrics(rows)
    
    coin_stats = {}
    for r in rows:
        if r.get("row_state") in ("tp", "sl") and r.get("symbol"):
            sym = r["symbol"]
            if sym not in coin_stats: coin_stats[sym] = {"wins": 0, "total": 0}
            coin_stats[sym]["total"] += 1
            if r.get("row_state") == "tp": coin_stats[sym]["wins"] += 1
    
    top = sorted([(s, (d["wins"]/d["total"]*100) if d["total"]>0 else 0, d["total"]) for s,d in coin_stats.items() if d["total"]>=3], key=lambda x: x[1], reverse=True)[:10]
    
    top_html = ""
    for sym, wr, tot in top:
        is_btc = "BTC" in sym.upper()
        col = "#f7931a" if is_btc else "#6366f1"
        icon = "₿" if is_btc else "🪙"
        top_html += f"""<div style="display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
            <span style="color:{col}">{icon} {sym}</span>
            <span style="font-weight:700">{wr:.1f}% ({tot} trades)</span></div>"""
    
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Altseason</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>🚀 Altseason Detector</h1></div>{NAV}
    
    <div class="card">
        <h2>📊 Statut Actuel</h2>
        <div style="text-align:center;padding:40px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border-radius:20px;margin-bottom:24px">
            <div style="font-size:48px;margin-bottom:16px">{'🚀' if alt['is_altseason'] else '₿'}</div>
            <div style="font-size:32px;font-weight:900;margin-bottom:8px">{alt['message']}</div>
            <div style="color:#64748b">Confiance: {alt['confidence']}%</div>
        </div>
        <div class="grid">
            <div class="metric"><div class="metric-label">₿ BTC WR</div><div class="metric-value">{alt['btc_wr']}%</div></div>
            <div class="metric"><div class="metric-label">🪙 Alts WR</div><div class="metric-value">{alt['alt_wr']}%</div></div>
        </div>
    </div>
    
    <div class="card"><h2>🏆 Top Performers</h2>{top_html if top_html else '<p style="color:#64748b">Pas assez de données</p>'}</div>
    
    <div class="card">
        <h2>💡 Utilisation</h2>
        <p style="color:#64748b">L'altseason est détectée quand les altcoins ont un meilleur winrate que BTC et surperforment > 55%</p>
    </div>
    </div></body></html>""")

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Backtest</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>⏮️ Backtesting Engine</h1></div>{NAV}
    
    <div class="card">
        <h2>Configuration</h2>
        <form id="form">
            <label>Side</label>
            <select name="side" style="width:100%;padding:12px;background:rgba(20,30,48,0.8);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0;margin-bottom:16px">
                <option value="">Tous</option>
                <option value="LONG">LONG</option>
                <option value="SHORT">SHORT</option>
            </select>
            
            <label>Symbole (ex: XRP)</label>
            <input type="text" name="symbol" placeholder="XRP, BTC, ETH..." style="width:100%;padding:12px;background:rgba(20,30,48,0.8);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0;margin-bottom:16px">
            
            <label>Timeframe (ex: 1h)</label>
            <input type="text" name="tf" placeholder="15m, 1h, 4h..." style="width:100%;padding:12px;background:rgba(20,30,48,0.8);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0;margin-bottom:16px">
            
            <button type="submit" style="width:100%;padding:12px 24px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border:none;border-radius:8px;color:white;font-weight:700;cursor:pointer">🚀 Lancer</button>
        </form>
    </div>
    
    <div id="res" class="card" style="display:none"><h2>Résultats</h2><div id="content"></div></div>
    
    <script>
    document.getElementById('form').addEventListener('submit', async (e) => {{
        e.preventDefault();
        const data = {{}};
        new FormData(e.target).forEach((v, k) => {{ if(v) data[k] = v; }});
        
        document.getElementById('res').style.display = 'block';
        document.getElementById('content').innerHTML = '<p style="color:#64748b">⏳ Calcul...</p>';
        
        const r = await fetch('/api/backtest', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(data)
        }});
        const d = await r.json();
        
        if (d.ok && d.backtest.trades > 0) {{
            const b = d.backtest;
            document.getElementById('content').innerHTML = `
                <div class="grid">
                    <div class="metric"><div class="metric-label">Trades</div><div class="metric-value">${{b.trades}}</div></div>
                    <div class="metric"><div class="metric-label">Wins / Losses</div><div class="metric-value">${{b.wins}} / ${{b.losses}}</div></div>
                    <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">${{b.winrate}}%</div></div>
                    <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" style="color:${{b.total_return>=0?'#10b981':'#ef4444'}}">${{b.total_return>=0?'+':''}}${{b.total_return}}%</div></div>
                    <div class="metric"><div class="metric-label">Avg Win</div><div class="metric-value" style="color:#10b981">+${{b.avg_win}}%</div></div>
                    <div class="metric"><div class="metric-label">Avg Loss</div><div class="metric-value" style="color:#ef4444">-${{b.avg_loss}}%</div></div>
                </div>
                <p style="margin-top:20px;padding:16px;background:rgba(99,102,241,0.1);border-radius:12px">
                    💡 Filtres: ${{JSON.stringify(b.filters)}}
                </p>`;
        }} else {{
            document.getElementById('content').innerHTML = '<p style="color:#ef4444">❌ Aucun trade trouvé avec ces filtres</p>';
        }}
    }});
    </script>
    </div></body></html>""")

@app.get("/strategie", response_class=HTMLResponse)
async def strategie_page():
    breaker = check_circuit_breaker()
    
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Stratégie</title>{CSS}</head>
    <body><div class="container">
    <div class="header"><h1>⚙️ Stratégie & Protection</h1></div>{NAV}
    
    <div class="card">
        <h2>🛡️ Circuit Breaker</h2>
        {'<div style="padding:20px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:12px;margin-top:16px"><h3 style="color:#ef4444;margin:0 0 8px 0">🚨 ACTIF</h3><p style="margin:0">Raison: '+breaker['reason']+'</p><p style="margin:8px 0 0 0;color:#64748b">Temps restant: '+str(breaker['hours_remaining'])+'h</p></div>' if breaker['active'] else '<p style="padding:16px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:12px;color:#10b981">✅ Trading autorisé</p>'}
        
        <div style="margin-top:24px">
            <h3>Paramètres de Protection</h3>
            <ul style="list-style:none;padding:0">
                <li style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                    <strong>Pertes consécutives max:</strong> {settings.MAX_CONSECUTIVE_LOSSES}
                </li>
                <li style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                    <strong>Circuit Breaker:</strong> {'✅ Activé' if settings.CIRCUIT_BREAKER_ENABLED else '❌ Désactivé'}
                </li>
                <li style="padding:12px">
                    <strong>Capital initial:</strong> ${settings.INITIAL_CAPITAL}
                </li>
            </ul>
        </div>
    </div>
    
    <div class="card">
        <h2>📋 Règles de Trading</h2>
        <ul style="list-style:none;padding:0">
            <li style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                ✅ Position sizing: 2% max du capital par trade
            </li>
            <li style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                ✅ Stop après {settings.MAX_CONSECUTIVE_LOSSES} pertes consécutives
            </li>
            <li style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                ✅ Cooldown 24h si circuit breaker activé
            </li>
            <li style="padding:12px">
                ✅ Telegram notifications activées: {'✅ Oui' if settings.TELEGRAM_ENABLED else '❌ Non'}
            </li>
        </ul>
    </div>
    </div></body></html>""")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info("🚀 Starting...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
