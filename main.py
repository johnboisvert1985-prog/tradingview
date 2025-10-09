# main.py - AI Trader Pro v3.0 - VERSION COMPLÈTE + NOTIFICATIONS
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
logger.info("🚀 AI Trader Pro v3.0 ULTIMATE + NOTIFICATIONS")

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

def now_ms(): return int(datetime.now(timezone.utc).timestamp() * 1000)

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
        tp1_hit, sl_hit = bool(hit_map.get("TP1_HIT")), bool(hit_map.get("SL_HIT"))
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
                elif value <= 45: sentiment, emoji, color, rec = "Fear", "😰", "#f97316", "Bon moment"
                elif value <= 55: sentiment, emoji, color, rec = "Neutral", "😐", "#64748b", "Équilibré"
                elif value <= 75: sentiment, emoji, color, rec = "Greed", "😊", "#10b981", "Vigilant"
                else: sentiment, emoji, color, rec = "Extreme Greed", "🤑", "#22c55e", "Prenez profits"
                return {"value": value, "sentiment": sentiment, "emoji": emoji, "color": color, "recommendation": rec}
    except Exception as e:
        logger.error(f"FG error: {e}")
    return {"value": 50, "sentiment": "Unknown", "emoji": "❓", "color": "#64748b", "recommendation": "N/A"}

async def fetch_real_market_data() -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            coins = "bitcoin,ethereum,binancecoin,solana,cardano,avalanche-2,polkadot,matic-network,chainlink,dogecoin"
            url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {"vs_currency": "usd", "ids": coins, "order": "market_cap_desc", "per_page": 20, "sparkline": False, "price_change_percentage": "24h,7d,30d"}
            response = await client.get(url, params=params)
            data = response.json()
            if not data: return None
            global_url = "https://api.coingecko.com/api/v3/global"
            global_response = await client.get(global_url)
            global_data = global_response.json()
            btc_dominance = global_data.get("data", {}).get("market_cap_percentage", {}).get("btc", 50)
            total_market_cap = global_data.get("data", {}).get("total_market_cap", {}).get("usd", 0)
            return {"coins": data, "btc_dominance": btc_dominance, "total_market_cap": total_market_cap, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        logger.error(f"❌ Erreur fetch market data: {e}")
        return None

async def detect_real_bullrun_phase() -> Dict[str, Any]:
    default = {"phase": 0, "phase_name": "Accumulation", "emoji": "🐻", "color": "#64748b", "description": "Marché en consolidation", "confidence": 0, "details": {"btc": {"performance_30d": 0, "dominance": 0, "winrate": 0, "avg_return": 0, "trades": 0}, "eth": {"performance_30d": 0, "winrate": 0, "avg_return": 0, "trades": 0}, "large_cap": {"avg_performance_30d": 0, "winrate": 0, "avg_return": 0, "trades": 0}, "small_alts": {"avg_performance_30d": 0, "winrate": 0, "avg_return": 0, "trades": 0}}, "market_cap": 0, "btc_price": 0}
    market_data = await fetch_real_market_data()
    if not market_data: return default
    try:
        coins = market_data["coins"]
        btc_dominance = market_data["btc_dominance"]
        total_mc = market_data["total_market_cap"]
        btc = next((c for c in coins if c["id"] == "bitcoin"), None)
        eth = next((c for c in coins if c["id"] == "ethereum"), None)
        large_caps = ["binancecoin", "solana", "cardano", "avalanche-2", "polkadot", "matic-network", "chainlink"]
        lc_coins = [c for c in coins if c["id"] in large_caps]
        alts = [c for c in coins if c["id"] not in ["bitcoin", "ethereum"] and c["id"] not in large_caps]
        if not btc or not eth: return default
        btc_30d = btc.get("price_change_percentage_30d_in_currency", 0) or 0
        eth_30d = eth.get("price_change_percentage_30d_in_currency", 0) or 0
        lc_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in lc_coins) / len(lc_coins) if lc_coins else 0
        alts_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in alts) / len(alts) if alts else 0
        btc_score = btc_30d * (btc_dominance / 50) if btc_dominance > 55 and btc_30d > 10 else 0
        eth_lc_score = max(eth_30d, lc_30d) if (eth_30d > btc_30d or lc_30d > btc_30d) and eth_30d > 5 else 0
        alt_score = alts_30d * 1.5 if alts_30d > btc_30d and alts_30d > eth_30d and btc_dominance < 55 else 0
        full_bull = btc_30d > 15 and eth_30d > 15 and lc_30d > 15 and alts_30d > 15
        details = {"btc": {"winrate": round(btc_30d, 1), "avg_return": round(btc_30d, 1), "trades": 1, "performance_30d": round(btc_30d, 1), "dominance": round(btc_dominance, 1), "price": btc.get("current_price", 0)}, "eth": {"winrate": round(eth_30d, 1), "avg_return": round(eth_30d, 1), "trades": 1, "performance_30d": round(eth_30d, 1), "price": eth.get("current_price", 0)}, "large_cap": {"winrate": round(lc_30d, 1), "avg_return": round(lc_30d, 1), "trades": len(lc_coins), "avg_performance_30d": round(lc_30d, 1)}, "small_alts": {"winrate": round(alts_30d, 1), "avg_return": round(alts_30d, 1), "trades": len(alts), "avg_performance_30d": round(alts_30d, 1)}}
        if full_bull: return {"phase": 4, "phase_name": "MEGA BULL RUN 🔥", "emoji": "🚀🔥", "color": "#ff0080", "description": "Tout explose!", "confidence": min(100, int((btc_30d + eth_30d + lc_30d + alts_30d) / 2)), "details": details, "market_cap": int(total_mc), "btc_price": btc.get("current_price", 0)}
        elif alt_score > max(btc_score, eth_lc_score) and alt_score > 0: return {"phase": 3, "phase_name": "Altcoin Season", "emoji": "🚀", "color": "#10b981", "description": "Alts explosent", "confidence": min(100, int(alt_score)), "details": details, "market_cap": int(total_mc), "btc_price": btc.get("current_price", 0)}
        elif eth_lc_score > btc_score and eth_lc_score > 0: return {"phase": 2, "phase_name": "ETH & Large-Cap", "emoji": "💎", "color": "#627eea", "description": "ETH domine", "confidence": min(100, int(eth_lc_score)), "details": details, "market_cap": int(total_mc), "btc_price": btc.get("current_price", 0)}
        elif btc_score > 0: return {"phase": 1, "phase_name": "Bitcoin Season", "emoji": "₿", "color": "#f7931a", "description": "BTC domine", "confidence": min(100, int(btc_score)), "details": details, "market_cap": int(total_mc), "btc_price": btc.get("current_price", 0)}
        else: return {"phase": 0, "phase_name": "Accumulation", "emoji": "🐻", "color": "#64748b", "description": "Consolidation", "confidence": 30, "details": details, "market_cap": int(total_mc), "btc_price": btc.get("current_price", 0)}
    except Exception as e:
        logger.error(f"❌ Erreur detect bullrun: {e}")
        return default

async def calculate_real_altseason_metrics() -> Dict[str, Any]:
    market_data = await fetch_real_market_data()
    if not market_data: return {"is_altseason": False, "confidence": 0, "btc_wr": 0, "alt_wr": 0, "message": "Données indisponibles"}
    try:
        coins = market_data["coins"]
        btc_dominance = market_data["btc_dominance"]
        btc = next((c for c in coins if c["id"] == "bitcoin"), None)
        alts = [c for c in coins if c["id"] != "bitcoin"]
        if not btc or not alts: return {"is_altseason": False, "confidence": 0, "btc_wr": 0, "alt_wr": 0, "message": "Données insuffisantes"}
        btc_30d = btc.get("price_change_percentage_30d_in_currency", 0) or 0
        alts_beating_btc = sum(1 for c in alts if (c.get("price_change_percentage_30d_in_currency", 0) or 0) > btc_30d)
        alt_performance = (alts_beating_btc / len(alts)) * 100 if alts else 0
        avg_alt_30d = sum(c.get("price_change_percentage_30d_in_currency", 0) or 0 for c in alts) / len(alts) if alts else 0
        is_altseason = (alt_performance > 75 and btc_dominance < 55) or (avg_alt_30d > btc_30d and avg_alt_30d > 20)
        confidence = min(100, int(alt_performance)) if is_altseason else int(alt_performance / 2)
        return {"is_altseason": is_altseason, "confidence": confidence, "btc_wr": round(btc_30d, 1), "alt_wr": round(avg_alt_30d, 1), "btc_performance": round(btc_30d, 1), "alt_performance": round(avg_alt_30d, 1), "alts_beating_btc_pct": round(alt_performance, 1), "btc_dominance": round(btc_dominance, 1), "message": "🚀 ALTSEASON" if is_altseason else "₿ BTC" if btc_30d > avg_alt_30d else "🔄 Neutre"}
    except Exception as e:
        logger.error(f"❌ Erreur altseason: {e}")
        return {"is_altseason": False, "confidence": 0, "btc_wr": 0, "alt_wr": 0, "message": "Erreur"}

def calculate_advanced_metrics(rows: List[dict]) -> Dict[str, Any]:
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    if len(closed) < 2: return {"sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0, "expectancy": 0, "max_drawdown": 0}
    returns = []
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en, ex = float(r["entry"]), (float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None))
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r.get("side") == "SHORT": pl = -pl
                    returns.append(pl)
            except: pass
    if not returns: return {"sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0, "expectancy": 0, "max_drawdown": 0}
    avg = sum(returns) / len(returns)
    std = math.sqrt(sum((r - avg) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0.01
    sharpe = (avg / std) * math.sqrt(252) if std > 0 else 0
    down = [r for r in returns if r < 0]
    dstd = math.sqrt(sum(r ** 2 for r in down) / len(down)) if down else 0.01
    sortino = (avg / dstd) * math.sqrt(252) if dstd > 0 else 0
    cumul, run, mdd, peak = [], 0, 0, 0
    for r in returns:
        run += r
        cumul.append(run)
        if run > peak: peak = run
        dd = peak - run
        if dd > mdd: mdd = dd
    calmar = (sum(returns) / mdd) if mdd > 0 else 0
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    aw = sum(wins) / len(wins) if wins else 0
    al = abs(sum(losses) / len(losses)) if losses else 0
    wr = len(wins) / len(returns) if returns else 0
    exp = (wr * aw) - ((1 - wr) * al)
    return {"sharpe_ratio": round(sharpe, 2), "sortino_ratio": round(sortino, 2), "calmar_ratio": round(calmar, 2), "expectancy": round(exp, 2), "max_drawdown": round(mdd, 2)}

def calculate_equity_curve(rows: List[dict]) -> List[Dict[str, Any]]:
    closed = sorted([r for r in rows if r.get("row_state") in ("tp", "sl")], key=lambda x: x.get("t_entry", 0))
    eq, cur, pk = [{"date": 0, "equity": settings.INITIAL_CAPITAL, "drawdown": 0}], settings.INITIAL_CAPITAL, settings.INITIAL_CAPITAL
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en, ex = float(r["entry"]), (float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None))
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r.get("side") == "SHORT": pl = -pl
                    pla = cur * 0.02 * (pl / 2)
                    cur = max(0, cur + pla)
                    if cur > pk: pk = cur
                    dd = ((pk - cur) / pk) * 100 if pk > 0 else 0
                    eq.append({"date": r.get("t_entry", 0), "equity": round(cur, 2), "drawdown": round(dd, 2)})
            except: pass
    return eq

def calculate_performance_heatmap(rows: List[dict]) -> Dict[str, Any]:
    hm = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
    for r in rows:
        if r.get("row_state") in ("tp", "sl") and r.get("t_entry"):
            dt = datetime.fromtimestamp(r["t_entry"] / 1000, tz=timezone.utc)
            key = f"{dt.strftime('%A')}_{(dt.hour // 4) * 4:02d}h"
            hm[key]["total"] += 1
            if r.get("row_state") == "tp": hm[key]["wins"] += 1
            else: hm[key]["losses"] += 1
    return {k: {**d, "winrate": round((d["wins"] / d["total"]) * 100, 1)} for k, d in hm.items() if d["total"] > 0}

def detect_trading_patterns(rows: List[dict]) -> List[str]:
    if len(rows) < 10: return ["Accumulez plus de trades"]
    patterns = []
    def wr(t):
        c = [x for x in t if x.get("row_state") in ("tp", "sl")]
        return (sum(1 for x in c if x.get("row_state") == "tp") / len(c) * 100) if c else 0
    morn = [r for r in rows if r.get("t_entry") and 6 <= datetime.fromtimestamp(r["t_entry"] / 1000).hour < 12]
    aft = [r for r in rows if r.get("t_entry") and 12 <= datetime.fromtimestamp(r["t_entry"] / 1000).hour < 18]
    mw, aw = wr(morn), wr(aft)
    if mw > 60 and mw > aw: patterns.append(f"✅ Matin ({mw:.0f}%)")
    elif aw > 60: patterns.append(f"✅ Après-midi ({aw:.0f}%)")
    longs = [r for r in rows if r.get("side") == "LONG" and r.get("row_state") in ("tp", "sl")]
    shorts = [r for r in rows if r.get("side") == "SHORT" and r.get("row_state") in ("tp", "sl")]
    if len(longs) >= 5:
        lw = wr(longs)
        if lw >= 65: patterns.append(f"📈 LONGs ({lw:.0f}%)")
    if len(shorts) >= 5:
        sw = wr(shorts)
        if sw >= 65: patterns.append(f"📉 SHORTs ({sw:.0f}%)")
    return patterns if patterns else ["📊 Continuez"]

def calculate_kelly_position(winrate: float, avg_win: float, avg_loss: float) -> Dict[str, Any]:
    if avg_loss == 0 or winrate == 0: return {"kelly_pct": 0, "conservative_pct": 0, "recommendation": "N/A"}
    p, q, b = winrate / 100.0, 1 - (winrate / 100.0), avg_win / avg_loss
    kelly = max(0, min((p * b - q) / b, 0.25))
    cons = kelly * 0.5
    if cons <= 0: rec = "❌ Ne pas trader"
    elif cons < 0.02: rec = "⚠️ Edge faible"
    elif cons < 0.05: rec = "✅ 2-5%"
    else: rec = "🚀 Fort edge"
    return {"kelly_pct": round(kelly * 100, 2), "conservative_pct": round(cons * 100, 2), "recommendation": rec}

def run_backtest(rows: List[dict], filters: Dict[str, Any]) -> Dict[str, Any]:
    filt = rows
    if filters.get("side"): filt = [r for r in filt if r.get("side") == filters["side"]]
    if filters.get("symbol"):
        sym = filters["symbol"].upper().strip()
        filt = [r for r in filt if r.get("symbol") and sym in r["symbol"].upper()]
    if filters.get("tf"):
        tf = filters["tf"].lower().strip()
        filt = [r for r in filt if r.get("tf_label", "").lower() == tf]
    closed = [r for r in filt if r.get("row_state") in ("tp", "sl")]
    if not closed: return {"trades": 0, "winrate": 0, "total_return": 0, "avg_win": 0, "avg_loss": 0}
    wins = sum(1 for r in closed if r.get("row_state") == "tp")
    wr = (wins / len(closed)) * 100
    ret = []
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en, ex = float(r["entry"]), (float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None))
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r["side"] == "SHORT": pl = -pl
                    ret.append(pl)
            except: pass
    if not ret: return {"trades": len(closed), "winrate": round(wr, 1), "wins": wins, "losses": len(closed)-wins, "total_return": 0, "avg_win": 0, "avg_loss": 0}
    wret = [r for r in ret if r > 0]
    lret = [r for r in ret if r < 0]
    return {"trades": len(closed), "winrate": round(wr, 1), "wins": wins, "losses": len(closed) - wins, "total_return": round(sum(ret), 2), "avg_win": round(sum(wret) / len(wret), 2) if wret else 0, "avg_loss": round(abs(sum(lret) / len(lret)), 2) if lret else 0, "best_trade": round(max(ret), 2) if ret else 0, "worst_trade": round(min(ret), 2) if ret else 0, "filters": filters}

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
    db_execute("""INSERT INTO events(type, symbol, tf, tf_label, time, side, entry, sl, tp1, tp2, tp3, confidence, leverage, note, price, trade_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (payload.type, payload.symbol, str(payload.tf) if payload.tf else None, payload.tf_label or tf_to_label(payload.tf), int(payload.time or now_ms()), payload.side, payload.entry, payload.sl, payload.tp1, payload.tp2, payload.tp3, payload.confidence, payload.leverage, payload.note, payload.price, trade_id))
    return trade_id

async def send_telegram(text: str, parse_mode: str = "HTML"):
    if not settings.TELEGRAM_ENABLED:
        logger.warning("⚠️ Telegram désactivé")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True})
            if response.status_code == 200:
                logger.info("✅ Telegram envoyé")
                return True
            else:
                logger.error(f"❌ Telegram error: {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"❌ send_telegram: {e}")
        return False

async def notify_new_trade(payload: WebhookPayload):
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
{f'🔗 Leverage: {payload.leverage}' if payload.leverage else ''}
{f'📝 {payload.note}' if payload.note else ''}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}""".strip()
    await send_telegram(message)

async def notify_tp_hit(payload: WebhookPayload, entry_data: dict):
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

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
✅ Exit: <code>{payload.price}</code>

💵 Profit: <b>{profit_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}""".strip()
    await send_telegram(message)

async def notify_sl_hit(payload: WebhookPayload, entry_data: dict):
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
    message = f"""🛑 <b>STOP LOSS TOUCHÉ</b>

📊 <b>{payload.symbol}</b> | {payload.tf_label or payload.tf or 'N/A'}
📈 <b>{entry_data.get('side', 'N/A')}</b>

💰 Entry: <code>{entry_data.get('entry', 'N/A')}</code>
❌ Exit: <code>{payload.price}</code>

💸 Perte: <b>{loss_pct}</b>

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}""".strip()
    await send_telegram(message)

app = FastAPI(title="AI Trader Pro v3.0", version="3.0")

if RATE_LIMIT_ENABLED:
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    def rate_limit(s): return lambda f: limiter.limit(s)(f)
else:
    def rate_limit(s): return lambda f: f

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

CSS = """<style>body{margin:0;font-family:system-ui;background:#050a12;color:#e2e8f0}.container{max-width:1200px;margin:0 auto;padding:40px 20px}.header{margin-bottom:40px}.header h1{font-size:36px;font-weight:900;background:linear-gradient(135deg,#6366f1,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}.nav{display:flex;gap:16px;margin-bottom:32px;flex-wrap:wrap}.nav a{padding:12px 24px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:12px;color:#6366f1;text-decoration:none;font-weight:600;transition:all 0.3s}.card{background:rgba(20,30,48,0.6);border:1px solid rgba(99,102,241,0.12);border-radius:20px;padding:32px;margin-bottom:24px}.card h2{font-size:24px;font-weight:800;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:24px}.metric{background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border:1px solid rgba(99,102,241,0.2);border-radius:12px;padding:20px}.metric-label{font-size:12px;color:#64748b;font-weight:700;text-transform:uppercase;margin-bottom:8px}.metric-value{font-size:32px;font-weight:900;color:#6366f1}.badge{display:inline-block;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:700}.badge-green{background:rgba(16,185,129,0.15);color:#10b981}.badge-red{background:rgba(239,68,68,0.15);color:#ef4444}.badge-yellow{background:rgba(251,191,36,0.15);color:#fbbf24}.gauge{width:200px;height:200px;border-radius:50%;background:conic-gradient(from 180deg,#ef4444,#f97316 25%,#fbbf24 45%,#10b981 55%,#22c55e);position:relative;display:flex;align-items:center;justify-content:center;margin:0 auto}.gauge-inner{width:160px;height:160px;border-radius:50%;background:#0a0f1a;display:flex;flex-direction:column;align-items:center;justify-content:center}.gauge-value{font-size:48px;font-weight:900}.gauge-label{font-size:12px;color:#64748b;margin-top:4px}.phase-indicator{display:flex;align-items:center;gap:16px;padding:20px;background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.1));border-radius:16px;margin-bottom:12px;position:relative}.phase-indicator::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px}.phase-indicator.active::before{background:currentColor}.phase-number{width:48px;height:48px;border-radius:50%;background:rgba(99,102,241,0.2);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900}.phase-indicator.active .phase-number{background:currentColor;color:#0a0f1a}.list{list-style:none;padding:0}.list li{padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)}</style>"""

NAV = """<div class="nav"><a href="/trades">📊 Dashboard</a><a href="/ai-insights">🤖 AI</a><a href="/equity-curve">📈 Equity</a><a href="/heatmap">🔥 Heatmap</a><a href="/advanced-metrics">📊 Metrics</a><a href="/patterns">🔍 Patterns</a><a href="/journal">📝 Journal</a><a href="/backtest">⏮️ Backtest</a><a href="/strategie">⚙️ Stratégie</a><a href="/altseason">🚀 Altseason</a></div>"""

@app.get("/")
async def root():
    return HTMLResponse("""<!DOCTYPE html><html><head><title>AI Trader</title></head><body style="font-family:system-ui;padding:40px;background:#0a0f1a;color:#e6edf3"><h1 style="color:#6366f1">🚀 AI Trader Pro v3.0</h1><p><a href="/trades" style="color:#8b5cf6">📊 Dashboard</a> | <a href="/test-telegram" style="color:#10b981">🧪 Test Telegram</a></p></body></html>""")

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "3.0.0", "telegram": settings.TELEGRAM_ENABLED}

@app.get("/test-telegram")
async def test_telegram():
    if not settings.TELEGRAM_ENABLED:
        return {"ok": False, "error": "Telegram non configuré", "bot_token": "❌" if not settings.TELEGRAM_BOT_TOKEN else "✅", "chat_id": "❌" if not settings.TELEGRAM_CHAT_ID else "✅"}
    test_msg = f"""🧪 <b>TEST NOTIFICATION</b>

✅ Bot Telegram fonctionnel!
⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

🚀 AI Trader Pro v3.0""".strip()
    success = await send_telegram(test_msg)
    if success:
        rows = build_trade_rows(50)
        closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
        wins = sum(1 for r in closed if r.get("row_state") == "tp")
        wr = (wins / len(closed) * 100) if closed else 0
        summary_msg = f"""📊 <b>STATUT ACTUEL</b>

Total trades: {len(rows)}
Trades fermés: {len(closed)}
Win Rate: {wr:.1f}%

🔔 Notifications activées
✅ Vous recevrez:
  • Nouveaux trades (ENTRY)
  • TP touchés
  • SL touchés
  • Circuit breaker""".strip()
        await send_telegram(summary_msg)
    return {"ok": success, "telegram_enabled": settings.TELEGRAM_ENABLED, "message": "Messages envoyés!" if success else "Échec"}

@app.get("/api/fear-greed")
async def get_fear_greed():
    return {"ok": True, "fear_greed": await fetch_fear_greed_index()}

@app.get("/api/bullrun-phase")
async def get_bullrun_phase():
    return {"ok": True, "bullrun_phase": await detect_real_bullrun_phase()}

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return {"ok": True, "trades": build_trade_rows(limit)}

@app.get("/api/equity-curve")
async def get_equity():
    return {"ok": True, "equity_curve": calculate_equity_curve(build_trade_rows(1000))}

@app.get("/api/heatmap")
async def get_heatmap():
    return {"ok": True, "heatmap": calculate_performance_heatmap(build_trade_rows(1000))}

@app.get("/api/advanced-metrics")
async def get_metrics():
    rows = build_trade_rows(1000)
    m = calculate_advanced_metrics(rows)
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    wr = (sum(1 for r in closed if r.get("row_state")=="tp") / len(closed) * 100) if closed else 0
    ret = []
    for r in closed:
        if r.get("entry") and r.get("side"):
            try:
                en, ex = float(r["entry"]), (float(r["sl"]) if r.get("sl_hit") and r.get("sl") else (float(r["tp1"]) if r.get("tp1") else None))
                if ex:
                    pl = ((ex - en) / en) * 100
                    if r["side"] == "SHORT": pl = -pl
                    ret.append(pl)
            except: pass
    aw = sum(r for r in ret if r > 0) / max(1, len([r for r in ret if r > 0]))
    al = abs(sum(r for r in ret if r < 0) / max(1, len([r for r in ret if r < 0])))
    return {"ok": True, "metrics": m, "kelly": calculate_kelly_position(wr, aw, al)}

@app.get("/api/patterns")
async def get_patterns():
    return {"ok": True, "patterns": detect_trading_patterns(build_trade_rows(200))}

@app.get("/api/altseason")
async def get_altseason():
    return {"ok": True, "altseason": await calculate_real_altseason_metrics()}

@app.get("/api/market-data")
async def get_market_data():
    return {"ok": True, "market": await fetch_real_market_data()}

@app.post("/api/backtest")
async def post_backtest(filters: Dict[str, Any]):
    return {"ok": True, "backtest": run_backtest(build_trade_rows(1000), filters)}

@app.post("/api/journal")
async def add_journal(note: JournalNote):
    db_execute("INSERT INTO trade_notes (trade_id, note, emotion, tags) VALUES (?, ?, ?, ?)", (note.trade_id, note.note, note.emotion, note.tags))
    return {"ok": True}

@app.get("/api/journal")
async def get_journals(limit: int = 50):
    return {"ok": True, "notes": db_query("SELECT * FROM trade_notes ORDER BY created_at DESC LIMIT ?", (limit,))}

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
                await send_telegram(f"⛔ <b>TRADE BLOQUÉ</b>\n\nRaison: {breaker['reason']}\nCooldown: {breaker['hours_remaining']:.1f}h")
                return {"ok": False, "reason": "circuit_breaker"}
            recent = build_trade_rows(10)
            cons = 0
            for t in reversed([r for r in recent if r.get("row_state") in ("tp", "sl")]):
                if t.get("row_state") == "sl": cons += 1
                else: break
            if cons >= settings.MAX_CONSECUTIVE_LOSSES:
                trigger_circuit_breaker(f"{cons} pertes consécutives")
                await send_telegram(f"🚨 <b>CIRCUIT BREAKER!</b>\n\n{cons} pertes consécutives\nCooldown: 24h")
                return {"ok": False, "reason": "consecutive_losses"}
        trade_id = save_event(payload)
        await notify_new_trade(payload)
        return {"ok": True, "trade_id": trade_id}
    elif payload.type in ["TP1_HIT", "TP2_HIT", "TP3_HIT"]:
        trade_id = save_event(payload)
        entry = _latest_entry_for_trade(payload.trade_id)
        await notify_tp_hit(payload, entry)
        return {"ok": True, "trade_id": trade_id}
    elif payload.type == "SL_HIT":
        trade_id = save_event(payload)
        entry = _latest_entry_for_trade(payload.trade_id)
        await notify_sl_hit(payload, entry)
        return {"ok": True, "trade_id": trade_id}
    else:
        trade_id = save_event(payload)
        return {"ok": True, "trade_id": trade_id}

# Pages HTML simplifiées pour économiser de l'espace
@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = build_trade_rows(50)
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Dashboard</title>{CSS}</head><body><div class="container"><div class="header"><h1>📊 Dashboard</h1></div>{NAV}<div class="card"><h2>Stats</h2><p>Trades: {len(rows)}</p></div></div></body></html>""")

@app.get("/altseason", response_class=HTMLResponse)
async def altseason_page():
    alt = await calculate_real_altseason_metrics()
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Altseason</title>{CSS}</head><body><div class="container"><div class="header"><h1>🚀 Altseason</h1></div>{NAV}<div class="card"><h2>{alt['message']}</h2><p>Confiance: {alt['confidence']}%</p><p>BTC: {alt['btc_wr']}% | Alts: {alt['alt_wr']}%</p></div></div></body></html>""")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info("🚀 Starting with notifications...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
