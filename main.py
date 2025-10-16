# main.py
import os
import re
import json
import time
import math
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# ----------------------------
# Config
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
APP_TITLE = "TradingView → Dashboard"

# Sources publiques (si indisponibles, on dégrade proprement)
FEAR_GREED_API = "https://api.alternative.me/fng/?limit=1&format=json"
COINCAP_GLOBAL = "https://api.coincap.io/v2/global"  # market cap, dominance (approximative)
BINANCE_BTCUSDT = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Web UI mini-templates
BASE_CSS = """
<style>
  body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:24px;color:#111}
  h1{font-size:24px;margin:0 0 12px}
  .nav a{margin-right:10px}
  table{border-collapse:collapse;width:100%;margin-top:12px}
  th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;font-size:14px}
  small{color:#666}
  .tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#f2f4f7;font-size:12px;margin-left:8px}
  .ok{color:#0a7}
  .warn{color:#d80}
  .bad{color:#c22}
  .mono{font-family:ui-monospace,Menlo,Consolas,monospace}
  .pill{border:1px solid #ddd;padding:2px 8px;border-radius:12px}
  .muted{color:#6b7280}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
  .card{border:1px solid #eee;border-radius:12px;padding:16px}
  .btn{display:inline-block;padding:8px 12px;border-radius:10px;border:1px solid #ddd;background:#fafafa;text-decoration:none;color:#111}
  .btn:hover{background:#f3f4f6}
</style>
"""

NAV = """
<div class="nav">
  <a href="/trades">📊 Trades</a>
  <a href="/equity-curve">📈 Equity</a>
  <a href="/journal">📝 Journal</a>
  <a href="/heatmap">🔥 Heatmap</a>
  <a href="/strategie">⚙️ Stratégie</a>
  <a href="/backtest">⏮️ Backtest</a>
  <a href="/patterns">📐 Patterns</a>
  <a href="/advanced-metrics">📟 Avancées</a>
  <a class="btn" href="#" onclick="fetch('/api/reset',{method:'POST'}).then(()=>location.reload())">♻️ Reset</a>
</div>
"""

# ----------------------------
# App & Logger
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")
app = FastAPI(title=APP_TITLE)


# ----------------------------
# État en mémoire
# ----------------------------
class Trade(BaseModel):
    id: int
    type: str = "entry"                 # entry / update / exit
    side: str                           # BUY/SELL or LONG/SHORT (normalisé)
    symbol: str
    tf: Optional[str] = None
    entry: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl: Optional[float] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    entry_time: Optional[datetime] = None
    confidence: Optional[int] = None
    confidence_text: Optional[str] = None
    reasons: Optional[List[str]] = None


class MarketState(BaseModel):
    fear_greed: Optional[int] = None
    btc_dominance: Optional[float] = None
    market_cap: Optional[float] = None
    btc_price: Optional[float] = None
    last_update: Optional[datetime] = None


class TradingState(BaseModel):
    trades: List[Trade] = Field(default_factory=list)
    next_id: int = 1
    market: MarketState = Field(default_factory=MarketState)


STATE = TradingState()


# ----------------------------
# Utils
# ----------------------------
def now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 100:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:,.4f}"
    return f"${v:.6f}"


def normalize_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in {"BUY", "LONG"}:
        return "BUY"
    if s in {"SELL", "SHORT"}:
        return "SELL"
    return s


def extract_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        txt = str(x).replace(",", "").strip()
        # match scientific (7.3e-05) or normal
        return float(txt)
    except Exception:
        return None


def percent(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


# ----------------------------
# “IA” : score de confiance vivant (heuristique multi-facteurs)
# ----------------------------
def compute_ai_confidence(trade: Trade, market: MarketState) -> Dict[str, Any]:
    """
    Score 0..100 basé sur :
      - Fear & Greed (contrarian) : F&G bas → achat plus “confiant” / short moins confiant
      - Dominance BTC : forte dom. pénalise altcoins
      - Direction vs conditions (LONG quand F&G bas, SELL quand F&G haut)
      - Distance SL/TP si fournis
      - Prix BTC (volatilité légère via variation glissante — si indispo : neutre)
    Retourne: dict(score=int, label=str, reasons=[...])
    """
    score = 50.0
    reasons = []

    fg = market.fear_greed
    dom = market.btc_dominance
    btc = market.btc_price

    # 1) Fear & Greed
    if fg is not None:
        # Contrarian: peur extrême (<25) favorise LONG, défavorise SHORT
        if fg <= 25:
            if trade.side == "BUY":
                score += 12
                reasons.append("✅ Fear extrême : context propice aux achats")
            else:
                score -= 10
                reasons.append("⚠️ Fear extrême : les shorts sont plus risqués")
        elif fg <= 45:
            if trade.side == "BUY":
                score += 5
                reasons.append("✅ Sentiment frileux : léger avantage aux longs")
            else:
                score -= 3
                reasons.append("⚠️ Sentiment frileux : shorts moins évidents")
        elif fg >= 75:
            if trade.side == "SELL":
                score += 10
                reasons.append("✅ Avidité élevée : context propice aux ventes")
            else:
                score -= 8
                reasons.append("⚠️ Avidité élevée : longs plus fragiles")
        else:
            reasons.append("ℹ️ Sentiment neutre/modéré")

    # 2) Dominance BTC
    if dom is not None:
        if dom >= 57.0:
            score -= 8
            reasons.append("⚠️ BTC dominant : pression sur altcoins")
        elif dom <= 45.0:
            score += 6
            reasons.append("✅ BTC.D faible : souffle pour altcoins")

    # 3) Structure risk-reward si TP/SL fournis
    rr_bonus = 0.0
    if trade.entry and (trade.tp1 or trade.tp2 or trade.tp3) and trade.sl:
        # calcule meilleurs TP vs SL
        best_tp = max([p for p in [trade.tp1, trade.tp2, trade.tp3] if p], default=None)
        if best_tp and trade.sl:
            # selon BUY/SELL, la distance se calcule différemment
            if trade.side == "BUY":
                up = percent(best_tp, trade.entry) or 0
                dn = percent(trade.entry, trade.sl) or 0
            else:
                # en SELL, entry > tp attendu, sl > entry
                up = percent(trade.entry, best_tp) or 0  # gain si TP atteint (positif)
                dn = percent(trade.sl, trade.entry) or 0  # perte si SL touché (positif)
            # ratio simple
            if dn > 0:
                ratio = up / dn
                if ratio >= 2.0:
                    rr_bonus = 10
                    reasons.append(f"✅ R/R favorable (~{ratio:.1f})")
                elif ratio >= 1.2:
                    rr_bonus = 5
                    reasons.append(f"ℹ️ R/R correct (~{ratio:.1f})")
                else:
                    rr_bonus = -5
                    reasons.append(f"⚠️ R/R faible (~{ratio:.1f})")
    score += rr_bonus

    # 4) Volatilité BTC très simplifiée (placeholder) : si prix > 0 et < 10 USD de diff fictive → neutre
    # Ici on ne dispose pas d’un historique court natif ; on peut laisser neutre
    if btc is not None:
        reasons.append(f"ℹ️ BTC: ${btc:,.0f}")

    # Clamp & label
    score = max(0, min(100, int(round(score))))
    if score >= 75:
        label = "ÉLEVÉ"
    elif score >= 55:
        label = "MOYEN"
    else:
        label = "FAIBLE"

    return {
        "score": score,
        "label": label,
        "reasons": reasons or ["ℹ️ Données partielles → estimation prudente"]
    }


# ----------------------------
# Telegram : envoi avec retry & cooldown
# ----------------------------
_last_telegram_ts = 0.0
_min_telegram_interval = 0.35  # 350ms pour lisser les rafales


async def send_telegram(text: str) -> bool:
    global _last_telegram_ts
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ Telegram non configuré")
        return False

    # Cooldown basique
    delay = time.time() - _last_telegram_ts
    if delay < _min_telegram_interval:
        await asyncio.sleep(_min_telegram_interval - delay)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                _last_telegram_ts = time.time()
                log.info("✅ Telegram envoyé")
                return True
            elif r.status_code == 429:
                data = r.json()
                retry_after = int(data.get("parameters", {}).get("retry_after", 2))
                log.error(f"❌ Telegram: 429 - {r.text}")
                await asyncio.sleep(retry_after + 1)
                # second essai
                r2 = await client.post(url, json=payload)
                if r2.status_code == 200:
                    _last_telegram_ts = time.time()
                    log.info("✅ Telegram envoyé (après retry)")
                    return True
                else:
                    log.error(f"❌ Telegram (retry): {r2.status_code} - {r2.text}")
                    return False
            else:
                log.error(f"❌ Telegram: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            log.exception(f"❌ Telegram exception: {e}")
            return False


def render_telegram(trade: Trade, market: MarketState) -> str:
    # bloc titre + symbole
    dir_txt = "LONG" if trade.side == "BUY" else "SHORT"
    lines = [
        f"🎯 <b>NOUVEAU TRADE</b> — <b>{trade.symbol}</b>",
        "",
        f"📊 <b>{'BUY' if trade.side=='BUY' else 'SELL'}</b>",
        f"📈 Direction: <b>{dir_txt}</b>{' | '+trade.tf if trade.tf else ''}",
        "",
        f"💰 Entry: <b>{fmt_money(trade.entry)}</b>",
    ]

    # Take Profits
    tps = [("TP1", trade.tp1), ("TP2", trade.tp2), ("TP3", trade.tp3)]
    have_tp = any(p for _, p in tps)
    if have_tp:
        lines.append("")
        lines.append("🎯 <b>Take Profits</b>:")
        for name, val in tps:
            if val:
                # variation relative en % dans le bon sens
                if trade.side == "BUY":
                    pv = percent(val, trade.entry)
                else:
                    pv = percent(trade.entry, val)
                pv_txt = f" ({pv:+.1f}%)" if pv is not None else ""
                lines.append(f"  • {name}: <b>{fmt_money(val)}</b>{pv_txt}")

    # SL
    if trade.sl:
        lines.append("")
        lines.append(f"🛑 Stop Loss: <b>{fmt_money(trade.sl)}</b>")

    # Confiance IA
    if trade.confidence is not None:
        lines.append("")
        lines.append(f"📊 <b>CONFIANCE</b>: <b>{trade.confidence}%</b> ({trade.confidence_text})")
        lines.append("")
        lines.append("Pourquoi ce score ?")
        for r in (trade.reasons or []):
            lines.append(f"  • {r}")

    # Marché
    fg = market.fear_greed
    dom = market.btc_dominance
    fg_txt = f"{fg}" if fg is not None else "—"
    dom_txt = f"{dom:.1f}%" if dom is not None else "—"
    lines.append("")
    lines.append(f"💡 Marché: F&G {fg_txt} | BTC.D {dom_txt}")

    return "\n".join(lines)


# ----------------------------
# Fetchers marché (async background)
# ----------------------------
async def fetch_fear_greed() -> Optional[int]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(FEAR_GREED_API)
            if r.status_code == 200:
                data = r.json()
                v = int(data["data"][0]["value"])
                return v
    except Exception:
        pass
    return None


async def fetch_global() -> Dict[str, Optional[float]]:
    # essaie CoinCap, sinon None
    out = {"market_cap": None, "btc_dominance": None}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(COINCAP_GLOBAL)
            if r.status_code == 200:
                data = r.json().get("data", {})
                out["market_cap"] = float(data.get("marketCapUsd")) if data.get("marketCapUsd") else None
                # CoinCap dominance n'est pas direct → on laisse None (neutre) si indispo
    except Exception:
        pass
    return out


async def fetch_btc_price() -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(BINANCE_BTCUSDT)
            if r.status_code == 200:
                data = r.json()
                return float(data["price"])
    except Exception:
        pass
    return None


async def refresh_market_loop():
    while True:
        try:
            fg, glb, btc = await asyncio.gather(
                fetch_fear_greed(),
                fetch_global(),
                fetch_btc_price()
            )
            if fg is not None:
                STATE.market.fear_greed = fg
                log.info(f"✅ Fear & Greed: {fg}")
            if glb.get("market_cap") is not None:
                STATE.market.market_cap = glb["market_cap"]
            # BTC dominance indisponible fiable → on garde la valeur existante si user la renseigne ailleurs
            if STATE.market.btc_dominance is None:
                # Option: déduire dom via fallback, sinon laisser None
                pass
            if btc is not None:
                STATE.market.btc_price = btc
                log.info(f"✅ Prix: BTC ${btc:,.0f}")
            STATE.market.last_update = now()
        except Exception as e:
            log.exception(f"Market refresh error: {e}")
        await asyncio.sleep(60)  # rafraîchit chaque minute


@app.on_event("startup")
async def on_startup():
    log.info("Application startup complete.")
    # lance rafraîchissement marché
    asyncio.create_task(refresh_market_loop())


# ----------------------------
# Webhook parsing
# ----------------------------
def parse_plaintext(body: str) -> Dict[str, Any]:
    """
    Essaye d’extraire side/symbol/tf/entry/(tp1..tp3)/sl depuis un texte HTML/Markdown.
    Accepte plusieurs formats connus.
    """
    txt = body.strip()

    # 1) side
    side = None
    m = re.search(r"\b(BUY|SELL|LONG|SHORT)\b", txt, re.IGNORECASE)
    if m:
        side = normalize_side(m.group(1))

    # 2) symbol (entre balises <b>XYZ</b> ou format XXXUSDT.P / XXX/USDT)
    symbol = None
    m = re.search(r"<b>\s*([A-Z0-9\-\._/]+)\s*</b>", txt)
    if m:
        symbol = m.group(1).replace("/", "")
    if not symbol:
        m = re.search(r"\b([A-Z0-9]{2,20})(?:USDT|USDC|USD|BTC|ETH)\.?[A-Z]?/?P?\b", txt)
        if m:
            # recompose suffixe
            symbol = m.group(0).replace("/", "")

    # 3) timeframe
    tf = None
    m = re.search(r"\b(?:TF|Timeframe|interval|tf)\s*[:=]\s*([0-9]+[mhd]?)\b", txt, re.IGNORECASE)
    if m:
        tf = m.group(1)
    else:
        m = re.search(r"\b\((\d+[mhd]?)\)", txt, re.IGNORECASE)
        if m:
            tf = m.group(1)

    # 4) entry
    entry = None
    m = re.search(r"(?:Entry|Prix|price|ix)\s*[:=]\s*(?:<code>)?([0-9]*\.?[0-9]+(?:e-?\d+)?)(?:</code>)?", txt, re.IGNORECASE)
    if m:
        entry = extract_float(m.group(1))

    # 5) TP/SL
    def find_price(label: str) -> Optional[float]:
        p = re.search(rf"{label}\s*[:=]\s*([0-9]*\.?[0-9]+(?:e-?\d+)?)", txt, re.IGNORECASE)
        return extract_float(p.group(1)) if p else None

    tp1 = find_price("TP1") or find_price("TP 1")
    tp2 = find_price("TP2") or find_price("TP 2")
    tp3 = find_price("TP3") or find_price("TP 3")
    sl = find_price("SL") or find_price("Stop Loss")

    # 6) fallback “created_at / entry_time / direction” (certains templates récents)
    if not side:
        m = re.search(r"\bdirection\s*[:=]\s*(LONG|SHORT)\b", txt, re.IGNORECASE)
        if m:
            side = normalize_side(m.group(1))

    # entry_time bonus
    entry_time = None
    m = re.search(r"\b(entry_time|Heure)\s*[:=]\s*([0-9:\- ]{10,})", txt, re.IGNORECASE)
    if m:
        try:
            entry_time = datetime.fromisoformat(m.group(2).strip()).replace(tzinfo=timezone.utc)
        except Exception:
            entry_time = None

    # Résultat
    return {
        "type": "entry" if side and entry else None,
        "side": side,
        "symbol": symbol,
        "tf": tf,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl,
        "entry_time": entry_time
    }


@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
            log.info(f"📥 Webhook payload (keys): {list(payload.keys())}")
        except Exception:
            log.warning("⚠️ Webhook: JSON invalide")
            return PlainTextResponse("Bad JSON", status_code=400)

        wtype = (payload.get("type") or "").strip().lower()
        side = normalize_side(payload.get("side", ""))
        symbol = (payload.get("symbol") or "").strip().upper()
        tf = (payload.get("tf") or payload.get("interval") or "").strip()
        entry = extract_float(payload.get("entry"))
        tp1 = extract_float(payload.get("tp1"))
        tp2 = extract_float(payload.get("tp2"))
        tp3 = extract_float(payload.get("tp3"))
        sl = extract_float(payload.get("sl"))
        entry_time = None
        if payload.get("entry_time"):
            try:
                entry_time = datetime.fromisoformat(str(payload["entry_time"])).replace(tzinfo=timezone.utc)
            except Exception:
                entry_time = None

        if not wtype:
            # assume entry si on a le minimum
            if side and symbol and entry is not None:
                wtype = "entry"
            else:
                log.warning(f"⚠️ Action inconnue: '{wtype}'")
                return PlainTextResponse("Unknown action", status_code=400)

        if wtype == "entry":
            if not symbol:
                log.warning("⚠️ Webhook: Symbol manquant")
                return PlainTextResponse("Symbol required", status_code=400)
            if entry is None:
                log.warning(f"⚠️ Entry incomplet: entry={entry}, tp1={tp1}, sl={sl}")
                return PlainTextResponse("Entry required", status_code=400)

            trade = Trade(
                id=STATE.next_id,
                type="entry",
                side=side or "BUY",
                symbol=symbol,
                tf=tf or None,
                entry=entry,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                sl=sl,
                entry_time=entry_time or now()
            )
            # IA score vivant
            ai = compute_ai_confidence(trade, STATE.market)
            trade.confidence = ai["score"]
            trade.confidence_text = ai["label"]
            trade.reasons = ai["reasons"]

            STATE.trades.append(trade)
            STATE.next_id += 1

            log.info(f"✅ Trade #{trade.id}: {trade.symbol} {trade.side} @ {trade.entry}")
            # Envoi Telegram immédiat
            await send_telegram(render_telegram(trade, STATE.market))
            return JSONResponse({"ok": True, "id": trade.id})

        else:
            log.warning(f"⚠️ Action non gérée: {wtype}")
            return PlainTextResponse("Unsupported type", status_code=400)

    else:
        # text/plain etc. → parseur custom
        raw = await request.body()
        body = raw.decode("utf-8", errors="ignore")
        log.info("📥 Webhook content-type: %s", ctype)
        parsed = parse_plaintext(body)
        log.info("📥 Webhook payload (keys via text): %s", [k for k,v in parsed.items() if v is not None])

        if not parsed.get("type"):
            log.warning("⚠️ Action inconnue: ''")
            return PlainTextResponse("Unknown action (text)", status_code=400)

        side = parsed["side"]
        symbol = parsed["symbol"]
        entry = parsed["entry"]
        if not symbol:
            log.warning("⚠️ Webhook: Symbol manquant")
            return PlainTextResponse("Symbol required", status_code=400)
        if entry is None:
            log.warning(f"⚠️ Entry incomplet: entry={entry}, tp1={parsed.get('tp1')}, sl={parsed.get('sl')}")
            return PlainTextResponse("Entry required", status_code=400)

        trade = Trade(
            id=STATE.next_id,
            type="entry",
            side=side or "BUY",
            symbol=symbol,
            tf=parsed.get("tf"),
            entry=entry,
            tp1=parsed.get("tp1"),
            tp2=parsed.get("tp2"),
            tp3=parsed.get("tp3"),
            sl=parsed.get("sl"),
            entry_time=parsed.get("entry_time") or now()
        )

        ai = compute_ai_confidence(trade, STATE.market)
        trade.confidence = ai["score"]
        trade.confidence_text = ai["label"]
        trade.reasons = ai["reasons"]

        STATE.trades.append(trade)
        STATE.next_id += 1

        log.info(f"✅ Trade #{trade.id}: {trade.symbol} {trade.side} @ {trade.entry}")
        await send_telegram(render_telegram(trade, STATE.market))
        return JSONResponse({"ok": True, "id": trade.id})


# ----------------------------
# API
# ----------------------------
@app.get("/api/trades")
async def api_trades():
    def to_row(t: Trade):
        return {
            "id": t.id,
            "type": t.type,
            "side": t.side,
            "symbol": t.symbol,
            "tf": t.tf,
            "entry": t.entry,
            "tp1": t.tp1,
            "tp2": t.tp2,
            "tp3": t.tp3,
            "sl": t.sl,
            "created_at": t.created_at.isoformat(),
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "confidence": t.confidence,
            "confidence_text": t.confidence_text,
            "reasons": t.reasons,
        }
    return JSONResponse([to_row(t) for t in STATE.trades])


@app.get("/api/journal")
async def api_journal():
    # journal minimal = mêmes trades (peut être enrichi)
    return await api_trades()


@app.get("/api/heatmap")
async def api_heatmap():
    # heatmap fictive par symbole: compte longs/shorts
    stats: Dict[str, Dict[str, int]] = {}
    for t in STATE.trades:
        s = stats.setdefault(t.symbol, {"LONG": 0, "SHORT": 0})
        if t.side == "BUY":
            s["LONG"] += 1
        else:
            s["SHORT"] += 1
    return JSONResponse(stats)


@app.get("/api/equity-curve")
async def api_equity():
    # courbe equity fictive: +1 pour BUY, -1 pour SELL cumulés
    eq = 0
    points = []
    for t in sorted(STATE.trades, key=lambda x: x.created_at):
        eq += (1 if t.side == "BUY" else -1)
        points.append({"t": t.created_at.isoformat(), "eq": eq})
    return JSONResponse(points)


@app.get("/api/fear-greed")
async def api_fg():
    v = STATE.market.fear_greed
    if v is not None:
        log.info(f"✅ Fear & Greed: {v}")
    return JSONResponse({"fear_greed": v})


@app.get("/api/bullrun-phase")
async def api_phase():
    # phase simplifiée basée sur dominance BTC
    dom = STATE.market.btc_dominance
    if dom is None:
        phase = "Neutre"
    elif dom >= 57:
        phase = "BTC Season"
    elif dom <= 45:
        phase = "Alt Season"
    else:
        phase = "Rotation"
    return JSONResponse({"phase": phase, "btc_dominance": dom})


@app.post("/api/reset")
async def api_reset():
    STATE.trades.clear()
    STATE.next_id = 1
    log.info("♻️ TradingState reset")
    return JSONResponse({"ok": True})


# ----------------------------
# Pages HTML
# ----------------------------
def layout(title: str, body_html: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{BASE_CSS}</head>
<body>
  <h1>{title} <span class="tag">live</span></h1>
  {NAV}
  {body_html}
</body></html>"""
    return HTMLResponse(html)


@app.get("/")
async def home():
    body = f"""
    <div class="grid">
      <div class="card">
        <div><b>Fear & Greed</b></div>
        <div style="font-size:28px">{STATE.market.fear_greed if STATE.market.fear_greed is not None else '—'}</div>
        <div class="muted">Dernière maj: {STATE.market.last_update.isoformat() if STATE.market.last_update else '—'}</div>
      </div>
      <div class="card">
        <div><b>BTC Dominance</b></div>
        <div style="font-size:28px">{f"{STATE.market.btc_dominance:.1f}%" if STATE.market.btc_dominance is not None else "—"}</div>
      </div>
      <div class="card">
        <div><b>BTC</b></div>
        <div style="font-size:28px">{f"${STATE.market.btc_price:,.0f}" if STATE.market.btc_price else "—"}</div>
      </div>
    </div>
    <p><a class="btn" href="/trades">Voir les trades</a></p>
    """
    return layout("Dashboard", body)


@app.get("/trades")
async def page_trades():
    rows = []
    for t in sorted(STATE.trades, key=lambda x: x.created_at, reverse=True):
        conf = f"{t.confidence}% <span class='pill'>{t.confidence_text}</span>" if t.confidence is not None else "—"
        reasons = "<br>".join(t.reasons or [])
        rows.append(f"""
        <tr>
          <td class="mono">#{t.id}</td>
          <td>{t.symbol}</td>
          <td>{'BUY/LONG' if t.side=='BUY' else 'SELL/SHORT'}</td>
          <td>{t.tf or '—'}</td>
          <td>{fmt_money(t.entry)}</td>
          <td>{fmt_money(t.tp1)}</td>
          <td>{fmt_money(t.tp2)}</td>
          <td>{fmt_money(t.tp3)}</td>
          <td>{fmt_money(t.sl)}</td>
          <td>{t.entry_time.astimezone(timezone.utc).strftime("%H:%M:%S") if t.entry_time else '—'}</td>
          <td>{conf}<br><small class="muted">{reasons}</small></td>
          <td><small>{t.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}</small></td>
        </tr>
        """)
    table = f"""
    <table>
      <thead>
        <tr>
          <th>#</th><th>Symbole</th><th>Side</th><th>TF</th>
          <th>Entry</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th>
          <th>Heure d’entrée (UTC)</th>
          <th>Confiance IA</th><th>Reçu</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) if rows else '<tr><td colspan="12">Aucun trade encore.</td></tr>'}
      </tbody>
    </table>
    """
    return layout("Trades", table)


@app.get("/equity-curve")
async def page_equity():
    body = """
    <p>Courbe d’equity simplifiée (API: <code>/api/equity-curve</code>).</p>
    <p><small>Intégrer un chart JS côté client si besoin.</small></p>
    """
    return layout("Équity Curve", body)


@app.get("/journal")
async def page_journal():
    body = """
    <p>Journal = liste des trades + commentaires (API: <code>/api/journal</code>).</p>
    """
    return layout("Journal", body)


@app.get("/heatmap")
async def page_heatmap():
    body = """
    <p>Heatmap par symbole (API: <code>/api/heatmap</code>).</p>
    """
    return layout("Heatmap", body)


@app.get("/strategie")
async def page_strategie():
    fg = STATE.market.fear_greed
    dom = STATE.market.btc_dominance
    txt = f"F&G {fg if fg is not None else '—'} | BTC.D {f'{dom:.1f}%' if dom is not None else '—'}"
    body = f"""
    <div class="card">
      <b>Confiance globale (indicative)</b>
      <div class="muted">{txt}</div>
      <p>Le score par trade est calculé par l’IA côté serveur en temps réel.</p>
    </div>
    """
    return layout("Stratégie", body)


@app.get("/backtest")
async def page_backtest():
    body = """
    <p>Zone backtest (placeholder). Branche ton moteur ou tes résultats ici.</p>
    """
    return layout("Backtest", body)


@app.get("/patterns")
async def page_patterns():
    return layout("Patterns", "<p>Détection de patterns (placeholder).</p>")


@app.get("/advanced-metrics")
async def page_adv():
    return layout("Métriques avancées", "<p>Métriques avancées (placeholder).</p>")


# ----------------------------
# Santé
# ----------------------------
@app.get("/health")
async def health():
    return PlainTextResponse("ok")
