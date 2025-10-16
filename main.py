# main.py
import os
import json
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# =========================
# App & CORS
# =========================
app = FastAPI(title="TradingView Webhook → Dashboard + Telegram", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Config
# =========================
TP_PCTS = [0.015, 0.025, 0.040]   # +1.5% / +2.5% / +4.0%
SL_PCT  = 0.02                    # 2% (côté opposé)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =========================
# État en mémoire (demo)
# =========================
class TradingState:
    def __init__(self):
        self.trades: List[Dict[str, Any]] = []
        self.next_id: int = 1
        # caches marché (dans ton infra tu peux les alimenter par des tâches cron)
        self.fear_greed: Optional[int] = 28
        self.btc_dominance: Optional[float] = 57.1
        self.global_mc: Optional[float] = 3.87  # $T
        self.btc_price: Optional[float] = 110_900

    def reset(self):
        self.trades.clear()
        self.next_id = 1

STATE = TradingState()

# =========================
# Utilitaires
# =========================
def _to_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", "")
        return float(s)
    except Exception:
        return None

def _clean_text(s: Optional[str]) -> str:
    return (s or "").strip()

def _norm_symbol(s: Optional[str]) -> str:
    return _clean_text(s).upper()

def _side_from(payload: Dict[str, Any]) -> Optional[str]:
    # Priorité: side > direction > action
    raw = (_clean_text(payload.get("side"))
           or _clean_text(payload.get("direction"))
           or _clean_text(payload.get("action")))
    raw = raw.upper()
    if raw in ("BUY", "LONG"):
        return "BUY"
    if raw in ("SELL", "SHORT"):
        return "SELL"
    return None

def _tf_from(payload: Dict[str, Any]) -> str:
    return _clean_text(payload.get("tf") or payload.get("interval") or payload.get("timeframe") or "-")

def _entry_from(payload: Dict[str, Any]) -> Optional[float]:
    return _to_float(payload.get("entry"))

def _parse_body_text(text: str) -> Dict[str, Any]:
    """
    Analyse un body 'text/plain' souple:
    - essaie d’extraire un JSON interne
    - sinon, lit des paires clef:valeur
    - sinon, essaie de lire des lignes style "key=value" ou "key: value"
    """
    # JSON brut ?
    try:
        cand = text.strip()
        if cand.startswith("{") and cand.endswith("}"):
            return json.loads(cand)
    except Exception:
        pass

    # JSON embarqué au milieu de texte
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass

    # k:v ou k=v (extrait numériques/texte bruts)
    kv: Dict[str, Any] = {}
    keys = ["type", "side", "direction", "action", "symbol", "ticker", "tf", "interval",
            "entry", "tp1", "tp2", "tp3", "sl", "entry_time", "created_at", "alert_name", "message"]
    for k in keys:
        # Exemple: side: BUY  | side=BUY
        r = re.search(rf"(?i)\b{k}\b\s*[:=]\s*([^\n\r,]+)", text)
        if r:
            kv[k] = r.group(1).strip()
    return kv

def compute_targets(side: str, entry: float):
    if entry is None or side not in ("BUY","SELL"):
        return None
    if side == "BUY":
        tp1 = entry * (1 + TP_PCTS[0])
        tp2 = entry * (1 + TP_PCTS[1])
        tp3 = entry * (1 + TP_PCTS[2])
        sl  = entry * (1 - SL_PCT)
    else:  # SELL/SHORT
        tp1 = entry * (1 - TP_PCTS[0])
        tp2 = entry * (1 - TP_PCTS[1])
        tp3 = entry * (1 - TP_PCTS[2])
        sl  = entry * (1 + SL_PCT)
    return round(tp1, 10), round(tp2, 10), round(tp3, 10), round(sl, 10)

def _confidence(side: str,
                fg: Optional[int],
                btc_d: Optional[float],
                btc_price: Optional[float]) -> (int, str, List[str]):
    """
    Score vivant simple (tu pourras brancher un modèle plus évolué ici).
    """
    score = 50
    reasons = []

    if fg is not None:
        if fg <= 20:
            score += 15
            reasons.append("✅ Fear extrême = zone d'achat idéale")
        elif fg <= 35:
            score += 5
            reasons.append("✅ Sentiment frileux : léger avantage aux longs")
        else:
            reasons.append("⚠️ Sentiment neutre")

    if btc_d is not None:
        if btc_d >= 57.5:
            reasons.append("⚠️ BTC trop dominant pour altcoins")
            if side in ("BUY","LONG"):
                score -= 5
        elif btc_d <= 52.0 and side in ("BUY","LONG"):
            score += 5
            reasons.append("✅ Dominance BTC basse : plus d'oxygène pour les alts")

    # Lisse et borne
    score = max(10, min(90, score))
    label = "FAIBLE" if score < 50 else ("MOYEN" if score < 65 else "ÉLEVÉ")
    return score, label, reasons

async def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = httpx.post(url, json=data, timeout=10)
        if r.status_code == 429:
            # Gestion simple du rate limit
            try:
                ra = r.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                ra = 5
            # On renvoie sans retry sync pour ne pas bloquer; à toi de mettre une queue si besoin.
            print(f"ERROR Telegram 429: retry_after={ra}")
        r.raise_for_status()
        print("INFO:main:✅ Telegram envoyé")
    except Exception as e:
        print(f"ERROR:main:❌ Telegram: {e}")

def fmt_money(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.6f}".rstrip("0").rstrip(".")

# =========================
# Webhook TradingView
# =========================
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    ctype = request.headers.get("content-type","").lower()
    raw = await request.body()

    # 1) Parse tolérant (JSON ou text)
    try:
        if "application/json" in ctype:
            payload = await request.json()
        else:
            payload = _parse_body_text(raw.decode("utf-8", errors="ignore"))
    except Exception:
        payload = _parse_body_text(raw.decode("utf-8", errors="ignore"))

    if not isinstance(payload, dict):
        print("WARNING:main:⚠️ Webhook: JSON invalide")
        raise HTTPException(400, "JSON invalide")

    # 2) Champs de base
    typ  = _clean_text(payload.get("type") or "entry").lower()   # défaut = entry
    side = _side_from(payload)
    sym  = _norm_symbol(payload.get("symbol") or payload.get("ticker"))
    tf   = _tf_from(payload)
    ent  = _entry_from(payload)

    alert_name = _clean_text(payload.get("alert_name"))
    message_tv = _clean_text(payload.get("message"))

    # Heures
    entry_time = _clean_text(payload.get("entry_time") or payload.get("created_at"))
    if not entry_time:
        entry_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # 3) garde-fous
    if not side:
        print("WARNING:main:⚠️ Side manquant")
        raise HTTPException(400, "Side manquant")
    if not sym:
        print("WARNING:main:⚠️ Symbol manquant")
        raise HTTPException(400, "Symbol manquant")

    # 4) Targets: si TP/SL fournis par TV, garde, sinon calcule
    tp1 = _to_float(payload.get("tp1"))
    tp2 = _to_float(payload.get("tp2"))
    tp3 = _to_float(payload.get("tp3"))
    sl  = _to_float(payload.get("sl"))
    if ent is not None and (tp1 is None or tp2 is None or tp3 is None or sl is None):
        tgt = compute_targets(side, ent)
        if tgt:
            tp1, tp2, tp3, sl = tgt

    # 5) Score de confiance vivant (basé sur caches)
    fg = STATE.fear_greed
    btc_d = STATE.btc_dominance
    score, label, raisons = _confidence(side, fg, btc_d, STATE.btc_price)

    # 6) Enregistrer trade en mémoire
    trade = {
        "id": STATE.next_id,
        "symbol": sym,
        "side": side,
        "direction": "LONG" if side == "BUY" else "SHORT",
        "timeframe": tf or "-",
        "entry": ent,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl,
        "entry_time": entry_time,
        "alert_name": alert_name,
        "raw_message": message_tv,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }
    STATE.trades.append(trade)
    STATE.next_id += 1
    print(f"INFO:main:✅ Trade #{trade['id']}: {sym} {side} @ {ent if ent is not None else '-'}")

    # 7) Message Telegram clair (avec symbole + prix, pas confondre avec l'heure)
    header = f"🎯 NOUVEAU TRADE — {sym}"
    body_lines = [
        "",
        f"📊 {side}",
        f"📈 Direction: {trade['direction']} | {trade['timeframe']}",
        f"🕒 Heure: {entry_time}",
        "",
        f"💰 Entry: ${fmt_money(ent)}",
        "",
        "🎯 Take Profits:",
        f"  TP1: ${fmt_money(tp1)}",
        f"  TP2: ${fmt_money(tp2)}",
        f"  TP3: ${fmt_money(tp3)}",
        "",
        f"🛑 Stop Loss: ${fmt_money(sl)}",
        "",
        f"📊 CONFIANCE: {score}% ({label})",
        "",
        "Pourquoi ce score ?",
    ]
    for r in raisons:
        body_lines.append(f"  • {r}")
    body_lines.append("")
    body_lines.append(f"💡 Marché: F&G {fg if fg is not None else '-'} | BTC.D {btc_d if btc_d is not None else '-'}%")
    if alert_name:
        body_lines.append(f"\n🏷️ Alerte: {alert_name}")

    await send_telegram(f"{header}\n" + "\n".join(body_lines))

    return {"ok": True, "id": trade["id"]}

# =========================
# API Marché (mock simples)
# =========================
@app.get("/api/fear-greed")
async def api_fg():
    print(f"INFO:main:✅ Fear & Greed: {STATE.fear_greed}")
    return {"fear_greed": STATE.fear_greed}

@app.get("/api/bullrun-phase")
async def api_bullrun():
    print(f"INFO:main:✅ Global: MC ${STATE.global_mc}T, BTC.D {STATE.btc_dominance}%")
    print(f"INFO:main:✅ Prix: BTC ${STATE.btc_price}")
    return {
        "global_mc_trillions": STATE.global_mc,
        "btc_dominance": STATE.btc_dominance,
        "btc_price": STATE.btc_price
    }

# =========================
# API Trades + Reset
# =========================
@app.get("/api/trades")
async def api_trades():
    return {"trades": STATE.trades}

@app.post("/api/reset")
async def api_reset():
    STATE.reset()
    print("INFO:main:♻️ TradingState reset")
    return {"ok": True}

# =========================
# HTML helpers
# =========================
NAV = """
<nav style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
  <a href="/">🏠 Accueil</a>
  <a href="/trades">💹 Trades</a>
  <a href="/equity-curve">📈 Equity</a>
  <a href="/journal">📝 Journal</a>
  <a href="/heatmap">🔥 Heatmap</a>
  <a href="/strategie">⚙️ Stratégie</a>
  <a href="/backtest">⏮️ Backtest</a>
  <a href="/patterns">🔎 Patterns</a>
  <a href="/advanced-metrics">📊 Metrics</a>
  <a href="/annonces">🗞️ Annonces</a>
</nav>
"""

RESET_BTN = """
<form method="post" action="/api/reset" onsubmit="setTimeout(()=>location.reload(),300);">
  <button type="submit">♻️ Reset</button>
</form>
"""

HTML_HEAD = """
<!doctype html><html lang="fr"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Dashboard Trading</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #ddd;padding:8px}
  th{background:#f6f6f6;text-align:left}
  code{background:#f3f3f3;padding:2px 4px;border-radius:4px}
</style>
</head><body>
"""

HTML_FOOT = "</body></html>"

# =========================
# Pages
# =========================
@app.get("/", response_class=HTMLResponse)
async def home():
    html = HTML_HEAD + NAV + """
    <h1>Dashboard</h1>
    <p>Bienvenue. Utilisez le menu ci-dessus.</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = []
    for t in STATE.trades:
        rows.append(f"""
        <tr>
          <td>{t['id']}</td>
          <td>{t['symbol']}</td>
          <td>{t['side']}</td>
          <td>{t['direction']}</td>
          <td>{t['timeframe']}</td>
          <td>{t.get('entry_time','-')}</td>
          <td>{fmt_money(t.get('entry'))}</td>
          <td>{fmt_money(t.get('tp1'))}</td>
          <td>{fmt_money(t.get('tp2'))}</td>
          <td>{fmt_money(t.get('tp3'))}</td>
          <td>{fmt_money(t.get('sl'))}</td>
          <td>{t.get('alert_name','')}</td>
        </tr>
        """)
    html = HTML_HEAD + NAV + f"""
      <h1>Trades</h1>
      {RESET_BTN}
      <table>
        <thead>
          <tr>
            <th>#</th><th>Symbole</th><th>Side</th><th>Dir</th><th>TF</th>
            <th>Heure d’entrée</th><th>Entry</th><th>TP1</th><th>TP2</th><th>TP3</th><th>SL</th><th>Alerte</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows) if rows else '<tr><td colspan="12">Aucun trade</td></tr>'}
        </tbody>
      </table>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    html = HTML_HEAD + NAV + """
    <h1>Équity Curve</h1>
    <p>(Place ton graphe ici si tu veux — pour l’instant placeholder)</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/journal", response_class=HTMLResponse)
async def journal():
    html = HTML_HEAD + NAV + """
    <h1>Journal</h1>
    <p>Ton journal de trades (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    html = HTML_HEAD + NAV + """
    <h1>Heatmap</h1>
    <p>Ta heatmap (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    html = HTML_HEAD + NAV + """
    <h1>Stratégie</h1>
    <p>Notes & paramètres (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    html = HTML_HEAD + NAV + """
    <h1>Backtest</h1>
    <p>Résultats de backtest (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    html = HTML_HEAD + NAV + """
    <h1>Patterns</h1>
    <p>Détection de patterns (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    html = HTML_HEAD + NAV + """
    <h1>Advanced Metrics</h1>
    <p>Métriques avancées (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    html = HTML_HEAD + NAV + """
    <h1>Annonces 🇫🇷</h1>
    <p>Flux RSS & actus (placeholder).</p>
    """ + HTML_FOOT
    return HTMLResponse(html)

# =========================
# Health
# =========================
@app.get("/healthz")
async def health():
    return {"ok": True}
