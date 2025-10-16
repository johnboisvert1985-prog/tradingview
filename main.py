# main.py
import os
import re
import json
import math
import time
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

APP_NAME = "TradingView Webhook → Dashboard & Telegram"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ────────────────────────────────────────────────────────────────────────────────
# State en mémoire
# ────────────────────────────────────────────────────────────────────────────────

class Trade(BaseModel):
    id: int
    symbol: str
    side: str                # BUY / SELL
    direction: str           # LONG / SHORT
    tf: str                  # "15", "30", ...
    entry: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl: Optional[float] = None
    status: str = "OPEN"     # OPEN / CLOSED / SL_HIT / TP_HIT
    created_at: str = ""
    entry_time: str = ""     # heure précise d'entry (pour ta colonne demandée)
    last_update: str = ""
    notes: Optional[str] = None


class TradingState:
    def __init__(self):
        self.trades: List[Trade] = []
        self.next_id = 1
        self.journal: List[Dict[str, Any]] = []
        self.equity_curve: List[Tuple[str, float]] = []  # (iso, equity)
        self.heatmap: Dict[str, float] = {}  # symbol -> perf
        self.market: Dict[str, Any] = {
            "fg": 28,          # Fear & Greed Index
            "btc_d": 57.2,     # Dominance BTC
            "mc": 3.85,        # market cap trillions
            "btc_price": 110_500
        }

    def reset(self):
        self.trades.clear()
        self.journal.clear()
        self.equity_curve.clear()
        self.heatmap.clear()
        self.next_id = 1
        log("♻️ TradingState reset")

    def add_demo_trades(self):
        demo = [
            ("BTCUSDT", "BUY", "LONG", "15", 65000, None, None, None, None),
            ("ETHUSDT", "SELL", "SHORT", "15", 3500, None, None, None, None),
            ("SOLUSDT", "BUY", "LONG", "15", 140, None, None, None, None),
            ("BTCUSDT", "BUY", "LONG", "30", 63700.0, None, None, None, None),
            ("ETHUSDT", "SELL", "SHORT", "30", 3570.0, None, None, None, None),
            ("BNBUSDT", "BUY", "LONG", "30", 606.0, None, None, None, None),
        ]
        for s, side, direction, tf, entry, tp1, tp2, tp3, sl in demo:
            self.add_trade(
                symbol=s, side=side, direction=direction, tf=tf,
                entry=entry, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
                notes="Démo"
            )
        log(f"✅ Démo initialisée avec {len(demo)} trades")

    def add_trade(
        self, symbol: str, side: str, direction: str, tf: str, entry: float,
        tp1: Optional[float], tp2: Optional[float], tp3: Optional[float], sl: Optional[float],
        notes: Optional[str] = None
    ) -> Trade:
        now = utc_iso()
        t = Trade(
            id=self.next_id,
            symbol=symbol.upper(),
            side=side.upper(),
            direction=direction.upper(),
            tf=str(tf),
            entry=float(entry),
            tp1=float(tp1) if tp1 is not None else None,
            tp2=float(tp2) if tp2 is not None else None,
            tp3=float(tp3) if tp3 is not None else None,
            sl=float(sl) if sl is not None else None,
            status="OPEN",
            created_at=now,
            entry_time=now,           # ✅ pour la colonne « heures d'entry »
            last_update=now,
            notes=notes,
        )
        self.trades.append(t)
        self.next_id += 1
        log(f"✅ Trade #{t.id}: {t.symbol} {t.direction} @ {t.entry}")
        return t


STATE = TradingState()

# ────────────────────────────────────────────────────────────────────────────────
# Utils
# ────────────────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    # Format proche de Render
    print(f"{datetime.now(timezone.utc).isoformat()}Z {level}:main:{msg}")

def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0

# ────────────────────────────────────────────────────────────────────────────────
# Confidence scoring dynamique
# ────────────────────────────────────────────────────────────────────────────────

def compute_confidence(fg: int, btc_d: float) -> Tuple[int, str, List[str]]:
    """
    Score sur 0–100 en fonction de:
      - Fear & Greed (plus bas => opportunité long terme, mais intraday risqué)
      - Dominance BTC (haute dominance pénalise altcoins)
    """
    reasons = []
    score = 50

    # Fear & Greed
    if fg <= 20:
        score += 15
        reasons.append("✅ Fear extrême = zone d'achat idéale")
    elif fg <= 40:
        score += 5
        reasons.append("✅ Sentiment prudent")
    elif fg >= 70:
        score -= 15
        reasons.append("⚠️ Euphorie, risque de correction")
    else:
        reasons.append("ℹ️ Sentiment neutre")

    # Dominance BTC
    if btc_d >= 57.0:
        score -= 10
        reasons.append("⚠️ BTC trop dominant pour altcoins")
    elif btc_d <= 45.0:
        score += 10
        reasons.append("✅ Dominance BTC faible, propice aux alts")

    score = max(0, min(100, score))
    label = "ÉLEVÉ" if score >= 70 else "MOYEN" if score >= 50 else "FAIBLE"
    return score, label, reasons

# ────────────────────────────────────────────────────────────────────────────────
# Telegram
# ────────────────────────────────────────────────────────────────────────────────

class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{token}/sendMessage" if token else ""
        self._client = httpx.AsyncClient(timeout=15)

    async def send(self, text: str, parse_mode: str = "HTML"):
        if not self.token or not self.chat_id:
            log("ℹ️ Telegram désactivé (TOKEN/CHAT_ID manquant)", "WARNING")
            return

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        # Tentatives avec backoff sur 429
        for attempt in range(5):
            r = await self._client.post(self.api, json=payload)
            if r.status_code == 200:
                log("✅ Telegram envoyé")
                return
            if r.status_code == 429:
                data = r.json()
                retry_after = int(data.get("parameters", {}).get("retry_after", 3))
                log(f"❌ Telegram: 429 - {r.text}", "ERROR")
                await asyncio.sleep(retry_after + 1)
                continue
            log(f"❌ Telegram: {r.status_code} - {r.text}", "ERROR")
            break

TELEGRAM = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x >= 1:
        return f"${x:,.4f}".replace(",", " ")
    return f"${x:.6f}"

def build_tg_entry_message(t: Trade, fg: int, btc_d: float) -> str:
    score, label, reasons = compute_confidence(fg, btc_d)
    reasons_text = "\n  • " + "\n  • ".join(reasons)
    return (
        f"🎯 <b>NOUVEAU TRADE</b> 🟡 — <b>{t.symbol}</b>\n\n"
        f"📊 <b>{t.side}</b>\n"
        f"📈 Direction: <b>{t.direction}</b> | {t.tf}m\n\n"
        f"💰 Entry: <b>{fmt_money(t.entry)}</b>\n\n"
        f"🎯 Take Profits:\n"
        f"  TP1: {fmt_money(t.tp1)}\n"
        f"  TP2: {fmt_money(t.tp2)}\n"
        f"  TP3: {fmt_money(t.tp3)}\n\n"
        f"🛑 Stop Loss: {fmt_money(t.sl)}\n\n"
        f"📊 <b>CONFIANCE:</b> {score}% ({label})\n\n"
        f"Pourquoi ce score ?{reasons_text}\n\n"
        f"💡 Marché: F&G {fg} | BTC.D {btc_d:.1f}%"
    )

# ────────────────────────────────────────────────────────────────────────────────
# Parsing Webhook (JSON & texte)
# ────────────────────────────────────────────────────────────────────────────────

PRICE_RE = r"(?:price|px|entry|entree|prix)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+(?:e-?\d+)?)"
SYMBOL_RE = r"(?:symbol|ticker|symbole)\s*[:=]?\s*([A-Z0-9\._-]+)"
SIDE_RE = r"\b(BUY|SELL|LONG|SHORT)\b"
TF_RE = r"(?:tf|timeframe|interval|tfm)\s*[:=]?\s*([0-9]+)"
TP_RE = r"(?:tp|take\s*profit)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+)"
TP1_RE = r"(?:tp1)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+)"
TP2_RE = r"(?:tp2)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+)"
TP3_RE = r"(?:tp3)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+)"
SL_RE = r"(?:sl|stop\s*loss)\s*[:=]?\s*\$?\s*([0-9]*\.?[0-9]+)"

def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None

def _direction_from_side(side: str) -> str:
    s = side.upper()
    if s == "BUY":
        return "LONG"
    if s == "SELL":
        return "SHORT"
    # si on reçoit LONG/SHORT directement
    return s

def _extract_from_plain(t: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    # 1) Symbol
    m = re.search(SYMBOL_RE, t, re.I)
    if m:
        payload["symbol"] = m.group(1).upper()

    # 2) Entry/Price
    m = re.search(PRICE_RE, t, re.I)
    if m:
        payload["entry"] = _coerce_float(m.group(1))

    # 3) Side/Direction
    m = re.search(SIDE_RE, t, re.I)
    if m:
        side = m.group(1).upper()
        if side in ("BUY", "SELL"):
            payload["side"] = side
        elif side in ("LONG", "SHORT"):
            payload["side"] = "BUY" if side == "LONG" else "SELL"
            payload["direction"] = side

    # 4) TF
    m = re.search(TF_RE, t, re.I)
    if m:
        payload["tf"] = m.group(1)

    # 5) TP / TP1 / TP2 / TP3
    m1 = re.search(TP1_RE, t, re.I)
    m2 = re.search(TP2_RE, t, re.I)
    m3 = re.search(TP3_RE, t, re.I)
    if m1:
        payload["tp1"] = _coerce_float(m1.group(1))
    if m2:
        payload["tp2"] = _coerce_float(m2.group(1))
    if m3:
        payload["tp3"] = _coerce_float(m3.group(1))

    # si un seul TP générique
    if not any(k in payload for k in ("tp1", "tp2", "tp3")):
        m = re.search(TP_RE, t, re.I)
        if m:
            payload["tp1"] = _coerce_float(m.group(1))

    # 6) SL
    m = re.search(SL_RE, t, re.I)
    if m:
        payload["sl"] = _coerce_float(m.group(1))

    # 7) Heure éventuelle "Heure: YYYY-mm-dd HH:MM"
    hm = re.search(r"Heure\s*:\s*([0-9:\-\sT]+)", t, re.I)
    if hm:
        try:
            dt = hm.group(1).strip()
            payload["created_at"] = dt
            payload["entry_time"] = dt
        except Exception:
            pass

    # 8) Nettoyage 'side' / 'direction'
    if "side" in payload and "direction" not in payload:
        payload["direction"] = _direction_from_side(payload["side"])

    # 9) Déduction automatique du type si absent
    if "type" not in payload or not str(payload.get("type")).strip():
        t_lower = t.lower()
        if re.search(r"\btp\s*[_ -]?hit\b|\btake\s*[_ -]?profit\s*[_ -]?hit\b", t_lower):
            payload["type"] = "tp_hit"
        elif re.search(r"\bsl\s*[_ -]?hit\b|\bstop\s*[_ -]?loss\s*[_ -]?hit\b", t_lower):
            payload["type"] = "sl_hit"
        elif re.search(r"\bclose(d)?\b", t_lower):
            payload["type"] = "close"
        elif payload.get("symbol") and (payload.get("entry") or payload.get("price")) and payload.get("side"):
            payload["type"] = "entry"

    return payload

# ────────────────────────────────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────────────────────────────────

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ────────────────────────────────────────────────────────────────────────────────
# Pages (HTML simples pour éviter les 404)
# ────────────────────────────────────────────────────────────────────────────────

BASE_STYLE = """
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial,"Noto Sans","Apple Color Emoji","Segoe UI Emoji";margin:40px;line-height:1.5}
code{background:#f5f5f5;padding:2px 6px;border-radius:6px}
.btn{display:inline-block;background:#111;color:#fff;text-decoration:none;padding:8px 12px;border-radius:8px}
table{border-collapse:collapse;width:100%;margin-top:16px}
th,td{border:1px solid #eee;padding:8px;text-align:left}
thead{background:#fafafa}
small{color:#666}
</style>
"""

def page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>{title}</title>{BASE_STYLE}</head>
<body>
<h1>{title}</h1>
{body}
<p><small>Service: {APP_NAME}</small></p>
</body></html>"""
    return HTMLResponse(html)

@app.get("/", response_class=HTMLResponse)
async def home():
    body = """
<p>Bienvenue 👋</p>
<p>
  <a class="btn" href="/trades">📊 Trades</a>
  <a class="btn" href="/equity-curve">📈 Equity</a>
  <a class="btn" href="/journal">📝 Journal</a>
  <a class="btn" href="/heatmap">🔥 Heatmap</a>
  <a class="btn" href="/strategie">⚙️ Stratégie</a>
  <a class="btn" href="/backtest">⏮️ Backtest</a>
  <a class="btn" href="/patterns">📐 Patterns</a>
  <a class="btn" href="/advanced-metrics">📊 Avancé</a>
  <a class="btn" href="/annonces">🗞️ Annonces</a>
</p>
"""
    return page("Dashboard", body)

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    # petit tableau direct + bouton Reset
    rows = ""
    for t in STATE.trades:
        rows += f"<tr><td>#{t.id}</td><td>{t.symbol}</td><td>{t.side}</td><td>{t.direction}</td><td>{t.tf}m</td><td>{fmt_money(t.entry)}</td><td>{t.entry_time}</td><td>{fmt_money(t.tp1)}</td><td>{fmt_money(t.tp2)}</td><td>{fmt_money(t.tp3)}</td><td>{fmt_money(t.sl)}</td><td>{t.status}</td></tr>"
    if not rows:
        rows = '<tr><td colspan="12"><i>Aucun trade</i></td></tr>'
    body = f"""
<p>
  <button class="btn" onclick="doReset()">♻️ Reset</button>
  <a class="btn" href="/api/trades" target="_blank">API</a>
</p>
<table>
  <thead><tr>
    <th>ID</th><th>Symbol</th><th>Side</th><th>Dir</th><th>TF</th><th>Entry</th><th>Entry time</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th><th>Status</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<script>
async function doReset(){{
  const r = await fetch('/api/reset', {{method:'POST'}});
  if(r.ok) location.reload();
  else alert('Reset failed');
}}
</script>
"""
    return page("📊 Trades", body)

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_page():
    body = """
<p>Courbe d'equity simple. Données: <a href="/api/equity-curve" target="_blank">/api/equity-curve</a></p>
"""
    return page("📈 Equity", body)

@app.get("/journal", response_class=HTMLResponse)
async def journal_page():
    body = """
<p>Journal des opérations. Données: <a href="/api/journal" target="_blank">/api/journal</a></p>
"""
    return page("📝 Journal", body)

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page():
    body = """
<p>Heatmap des performances. Données: <a href="/api/heatmap" target="_blank">/api/heatmap</a></p>
"""
    return page("🔥 Heatmap", body)

@app.get("/strategie", response_class=HTMLResponse)
async def strategy_page():
    body = """
<p>Paramètres de stratégie (placeholder).</p>
"""
    return page("⚙️ Stratégie", body)

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    body = """
<p>Résultats de backtest (placeholder).</p>
"""
    return page("⏮️ Backtest", body)

@app.get("/patterns", response_class=HTMLResponse)
async def patterns_page():
    return page("📐 Patterns", "<p>Détection de patterns (placeholder).</p>")

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def adv_page():
    return page("📊 Avancé", "<p>Métriques avancées (placeholder).</p>")

@app.get("/annonces", response_class=HTMLResponse)
async def news_page():
    return page("🗞️ Annonces", "<p>Flux d'actualités FR (placeholder).</p>")

# ────────────────────────────────────────────────────────────────────────────────
# API
# ────────────────────────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def api_trades():
    return [t.dict() for t in STATE.trades]

@app.get("/api/equity-curve")
async def api_equity():
    # Simple: equity = 100 + N * 0.2
    base = 100.0
    eq = []
    for i, t in enumerate(STATE.trades, start=1):
        base *= 1.002
        eq.append({"time": t.created_at, "equity": round(base, 2)})
    if not eq:
        now = utc_iso()
        eq = [{"time": now, "equity": 100.0}]
    return eq

@app.get("/api/journal")
async def api_journal():
    return STATE.journal

@app.get("/api/heatmap")
async def api_heatmap():
    return STATE.heatmap

@app.get("/api/fear-greed")
async def api_fg():
    log(f"✅ Fear & Greed: {STATE.market['fg']}")
    return {"fg": STATE.market["fg"]}

@app.get("/api/bullrun-phase")
async def api_phase():
    log(f"✅ Global: MC ${STATE.market['mc']:.2f}T, BTC.D {STATE.market['btc_d']:.1f}%")
    log(f"✅ Prix: BTC ${STATE.market['btc_price']:,}".replace(",", " "))
    # Phase simple selon dominance & fg
    fg = STATE.market["fg"]
    btc_d = STATE.market["btc_d"]
    if btc_d > 56 and fg < 40:
        phase = "Accu BTC"
    elif fg > 65 and btc_d < 50:
        phase = "Alt season"
    else:
        phase = "Range"
    return {"phase": phase, "fg": fg, "btc_d": btc_d}

@app.post("/api/reset")
async def api_reset():
    STATE.reset()
    return {"ok": True}

# ────────────────────────────────────────────────────────────────────────────────
# Webhook TradingView
# ────────────────────────────────────────────────────────────────────────────────

@app.post("/tv-webhook")
async def tv_webhook(request: Request, raw: bytes = Body(...)):
    ctype = request.headers.get("content-type", "")
    log(f"📥 Webhook content-type: {ctype}")

    text = raw.decode(errors="ignore")
    payload: Dict[str, Any] = {}

    # Tenter JSON d'abord
    try:
        payload = json.loads(text)
        log(f"📥 Webhook payload (keys): {list(payload.keys())}")
    except Exception:
        # fallback texte
        payload = _extract_from_plain(text)
        log(f"📥 Webhook payload (keys via text): {list(payload.keys())}")

    # Normalisation de base
    if "side" in payload and "direction" not in payload:
        payload["direction"] = _direction_from_side(payload["side"])

    # Filet de sécurité: si action (type) manquante, deviner selon champs
    action = (payload.get("type") or payload.get("action") or "").lower()
    action = action.replace(" ", "_").replace("-", "_")
    if not action:
        if payload.get("symbol") and (payload.get("entry") or payload.get("price")) and payload.get("side"):
            action = "entry"
        elif any(k in payload for k in ("tp_hit", "take_profit_hit", "tp1", "tp2", "tp3")):
            action = "tp_hit"
        elif "sl" in payload and str(payload.get("sl")).strip() and "hit" in (payload.get("event", "") or "").lower():
            action = "sl_hit"
        elif str(payload.get("reason", "")).lower().startswith("close") or "close" in (payload.get("event", "") or "").lower():
            action = "close"

    # Router
    if action == "entry":
        # validations minimales
        symbol = (payload.get("symbol") or "").upper()
        side = (payload.get("side") or "").upper()
        tf = str(payload.get("tf") or payload.get("timeframe") or "15")
        entry = _coerce_float(payload.get("entry") or payload.get("price"))

        if not symbol:
            log("⚠️ Webhook: Symbol manquant", "WARNING")
            return PlainTextResponse("symbol missing", status_code=400)
        if not side or side not in ("BUY", "SELL"):
            log("⚠️ Webhook: Side invalide ou manquant", "WARNING")
            return PlainTextResponse("side invalid", status_code=400)
        if entry is None:
            log(f"⚠️ Entry incomplet: entry={entry}, tp1={payload.get('tp1')}, sl={payload.get('sl')}", "WARNING")
            return PlainTextResponse("entry missing", status_code=400)

        # TPs par défaut si absents
        tp1 = _coerce_float(payload.get("tp1"))
        tp2 = _coerce_float(payload.get("tp2"))
        tp3 = _coerce_float(payload.get("tp3"))
        sl = _coerce_float(payload.get("sl"))

        # Si TP/SL absents on calcule des niveaux par défaut (1.5%, 2.5%, 4% ; SL 2%)
        if side == "BUY":
            tp1 = tp1 or entry * 1.015
            tp2 = tp2 or entry * 1.025
            tp3 = tp3 or entry * 1.040
            sl = sl or entry * 0.98
            direction = "LONG"
        else:
            tp1 = tp1 or entry * 0.985
            tp2 = tp2 or entry * 0.975
            tp3 = tp3 or entry * 0.960
            sl = sl or entry * 1.02
            direction = "SHORT"

        # Enregistrement
        t = STATE.add_trade(
            symbol=symbol, side=side, direction=direction, tf=tf,
            entry=entry, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl
        )

        # Telegram immédiat
        fg = STATE.market["fg"]
        btc_d = STATE.market["btc_d"]
        msg = build_tg_entry_message(t, fg, btc_d)
        asyncio.create_task(TELEGRAM.send(msg))

        return JSONResponse({"ok": True, "id": t.id})

    elif action in ("tp_hit", "sl_hit", "close"):
        # Marquer le dernier trade ouvert du symbole
        symbol = (payload.get("symbol") or "").upper()
        if not symbol:
            log("⚠️ Webhook: Symbol manquant (evento)", "WARNING")
            return PlainTextResponse("symbol missing", status_code=400)

        # find last OPEN for symbol
        target = None
        for t in reversed(STATE.trades):
            if t.symbol == symbol and t.status == "OPEN":
                target = t
                break
        if not target:
            return PlainTextResponse("no open trade for symbol", status_code=404)

        target.last_update = utc_iso()
        if action == "tp_hit":
            target.status = "TP_HIT"
        elif action == "sl_hit":
            target.status = "SL_HIT"
        else:
            target.status = "CLOSED"
        return JSONResponse({"ok": True, "id": target.id, "status": target.status})

    else:
        log(f"⚠️ Action inconnue: '{action}'", "WARNING")
        return PlainTextResponse("unknown action", status_code=400)

# ────────────────────────────────────────────────────────────────────────────────
# Lancement (démo locale)
# ────────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    # Juste pour avoir des données si vide
    if not STATE.trades:
        STATE.add_demo_trades()

    # Logs de démarrage similaires à tes extraits
    for t in STATE.trades[:6]:
        log(f"✅ Trade #{t.id}: {t.symbol} {t.direction} @ {t.entry}")
