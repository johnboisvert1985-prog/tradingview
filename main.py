# main.py
import os, re, json, asyncio, logging, random
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

app = FastAPI(title="TradingView Webhook Dashboard")

# ========= Config =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ========= In-memory state =========
STATE: Dict[str, Any] = {
    "next_id": 1,
    "trades": [],              # list[dict]
    "equity_curve": [],        # list[{t, equity}]
    "journal": [],             # list entries
    "heatmap": {},             # symbol -> score
    "market": {                # Market snapshot pour confiance
        "fear_greed": 28,
        "btc_dominance": 57.2,
        "btc_price": 110_500,
        "marketcap": "3.87T",
    },
    "annonces": [],            # RSS/news items (optionnel)
}

def _now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

# ========= Confidence scoring (vivant) =========
def compute_confidence(market: Dict[str, Any], side: str) -> Dict[str, Any]:
    fg = float(market.get("fear_greed", 50) or 50)
    btc_d = float(market.get("btc_dominance", 50) or 50)

    score = 50
    why: List[str] = []

    # Sentiment global
    if fg <= 20:
        score += 8;  why.append("✅ Peur extrême : opportunités")
    elif fg <= 30:
        score += 4;  why.append("✅ Sentiment frileux : léger avantage")
    elif fg >= 70:
        score -= 6;  why.append("⚠️ Euphorie : prudence")

    # Dominance BTC
    if btc_d >= 57:
        score -= 5;  why.append("⚠️ BTC dominant : altcoins sous pression")
    elif btc_d <= 48:
        score += 4;  why.append("✅ Dominance BTC faible : altcoins favorisés")

    # Alignement côté/condition
    s = (side or "").upper()
    if s == "BUY" and fg <= 30: score += 3
    if s == "SELL" and fg >= 70: score += 3

    score = max(0, min(100, score))
    label = "ÉLEVÉ" if score >= 70 else ("MOYEN" if score >= 55 else "FAIBLE")
    # On garde 3 raisons max, sinon ça spam
    return {"score": score, "label": label, "reasons": why[:3] or ["ℹ️ Modèle neutre"]}

# ========= Telegram =========
async def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ Telegram non configuré (TELEGRAM_BOT_TOKEN/CHAT_ID manquants)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(5):
            r = await client.post(url, json=payload)
            if r.status_code == 429:
                retry = 1
                try: retry = r.json().get("parameters", {}).get("retry_after", 1)
                except Exception: pass
                log.error(f"❌ Telegram: 429 - retry {retry}s (attempt {attempt+1})")
                await asyncio.sleep(retry)
                continue
            if r.status_code >= 400:
                log.error(f"❌ Telegram: {r.status_code} - {r.text}")
            else:
                log.info("✅ Telegram envoyé")
            break

def fmt_money(x: Optional[Any]) -> str:
    try:
        if x is None or x == "" or str(x).strip() == "-" or str(x).strip().lower() == "nan":
            return "-"
        n = float(str(x).replace(",", ""))
        s = f"{n:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return "-"

def build_telegram_message(t: Dict[str, Any], market: Dict[str, Any]) -> str:
    side = (t.get("side") or "").upper()
    direction = "LONG" if side == "BUY" else ("SHORT" if side == "SELL" else "-")
    tf = str(t.get("timeframe") or "-")
    fg = market.get("fear_greed", "-")
    btc_d = market.get("btc_dominance", "-")

    conf = compute_confidence(market, side)
    reasons = conf["reasons"]

    lines = [
        f"<b>🎯 NOUVEAU TRADE — {t.get('symbol','-')}</b>",
        "",
        f"📊 <b>{side or '-'}</b>",
        f"📈 Direction: {direction} | {tf}",
        f"🕒 Heure: {t.get('entry_time','-')}",
        "",
        f"💰 Entry: ${fmt_money(t.get('entry'))}",
        "",
        "🎯 Take Profits:",
        f"  TP1: ${fmt_money(t.get('tp1'))}",
        f"  TP2: ${fmt_money(t.get('tp2'))}",
        f"  TP3: ${fmt_money(t.get('tp3'))}",
        "",
        f"🛑 Stop Loss: ${fmt_money(t.get('sl'))}",
        "",
        f"📊 CONFIANCE: {conf['score']}% ({conf['label']})",
        "",
        "Pourquoi ce score ?",
        *[f"  • {r}" for r in reasons],
        "",
        f"💡 Marché: F&G {fg} | BTC.D {btc_d}%",
    ]
    return "\n".join(lines)

# ========= Parsing Webhook =========
ALIASES = {
    "symbol": {"ticker", "asset", "pair", "sym"},
    "side": {"action", "direction", "type"},
    "timeframe": {"tf", "interval"},
    "entry_time": {"entrytime","time","heure"},
    "alert_name": {"alert","name","message_name","messagename","messanem"},
}
NUMBER_KEYS = {"entry","tp1","tp2","tp3","sl"}

SYMBOL_RE = re.compile(r"\b([A-Z0-9]{2,}[A-Z]{3,})(?:\.P|USDT\.P|USD\.P|USDC\.P|\.P)?\b")
PRICE_RE  = re.compile(r"(@|entry|entrée|price|prix)\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
TP_RE     = re.compile(r"TP\s*1?\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
TP2_RE    = re.compile(r"TP\s*2\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
TP3_RE    = re.compile(r"TP\s*3\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
SL_RE     = re.compile(r"\bSL|stop\s*loss\b\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)

def norm_key(k:str)->str:
    k = (k or "").strip().lower()
    for canonical, alts in ALIASES.items():
        if k == canonical or k in alts:
            return canonical
    return k

def parse_number(v: Any) -> Optional[float]:
    if v is None: return None
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def parse_text_body(body: str) -> Dict[str, Any]:
    """Parse format texte libre type:
       side=BUY; symbol=FLUXUSDT.P; tf=15; entry=0.0123; tp1=...; sl=...
       ou lignes 'key: value'"""
    data: Dict[str, Any] = {}
    parts = re.split(r"[;\n]+", body)
    for p in parts:
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+?)\s*$", p)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        k = norm_key(k)
        v = v.strip()
        data[k] = v
    # si un seul champ message contient du json/kv, reparse
    msg = data.get("message")
    if isinstance(msg, str) and ("=" in msg or ":" in msg):
        data.update(parse_text_body(msg))
    return data

def coerce_types(d: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(d)
    for k in NUMBER_KEYS:
        if k in out:
            out[k] = parse_number(out[k])
    if "timeframe" in out and isinstance(out["timeframe"], str):
        out["timeframe"] = out["timeframe"].strip().upper()
    if "side" in out and isinstance(out["side"], str):
        s = out["side"].strip().upper()
        if s in ("BUY","LONG"): out["side"] = "BUY"
        elif s in ("SELL","SHORT"): out["side"] = "SELL"
    if not out.get("entry_time"):
        out["entry_time"] = _now_iso()
    return out

def try_fill_from_text(d: Dict[str, Any]) -> Dict[str, Any]:
    """Complète symbol/entry/tp/sl depuis alert_name/message si manquants."""
    txt_parts = []
    for k in ("alert_name","message","name","text","raw"):
        v = d.get(k)
        if isinstance(v, str) and v:
            txt_parts.append(v)
    blob = " | ".join(txt_parts)

    if not d.get("symbol"):
        m = SYMBOL_RE.search(blob)
        if m:
            d["symbol"] = m.group(1) + (".P" if ".P" in blob else "")
    if d.get("entry") is None:
        for rx in (PRICE_RE,):
            m = rx.search(blob)
            if m:
                d["entry"] = parse_number(m.group(2)); break
    if d.get("tp1") is None:
        m = TP_RE.search(blob)
        if m: d["tp1"] = parse_number(m.group(1))
    if d.get("tp2") is None:
        m = TP2_RE.search(blob)
        if m: d["tp2"] = parse_number(m.group(1))
    if d.get("tp3") is None:
        m = TP3_RE.search(blob)
        if m: d["tp3"] = parse_number(m.group(1))
    if d.get("sl") is None:
        m = SL_RE.search(blob)
        if m: d["sl"] = parse_number(m.group(1))
    return d

async def parse_webhook(request: Request) -> Dict[str, Any]:
    ctype = request.headers.get("content-type","").lower()
    body_bytes = await request.body()
    log.info(f"📥 Webhook content-type: {ctype}")
    try:
        raw_text = body_bytes.decode("utf-8", errors="ignore")
    except Exception:
        raw_text = ""

    # JSON direct
    if "application/json" in ctype:
        try:
            j = await request.json()
            log.info(f"📥 Webhook payload (keys): {list(j.keys())}")
            data = {norm_key(k): v for k, v in j.items()}
            # Si j["message"] contient un JSON stringifié
            msg = data.get("message")
            if isinstance(msg, str) and msg.strip().startswith("{") and msg.strip().endswith("}"):
                try:
                    inner = json.loads(msg)
                    data.update({norm_key(k): v for k, v in inner.items()})
                except Exception:
                    pass
            data = coerce_types(try_fill_from_text(data))
            return data
        except Exception:
            log.warning("⚠️ JSON invalide -> tentative texte")

    # Form
    if "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        data = {norm_key(k): (form.get(k) if form.get(k) is not None else "") for k in form.keys()}
        log.info(f"📥 Webhook form (keys): {list(data.keys())}")
        # JSON dans "message"
        msg = data.get("message")
        if isinstance(msg, str) and msg.strip().startswith("{") and msg.strip().endswith("}"):
            try:
                inner = json.loads(msg)
                data.update({norm_key(k): v for k, v in inner.items()})
            except Exception:
                pass
        else:
            # kv dans "message"
            if isinstance(msg, str) and ("=" in msg or ":" in msg):
                data.update(parse_text_body(msg))
        data = coerce_types(try_fill_from_text(data))
        return data

    # Texte: JSON pur
    if raw_text.strip().startswith("{") and raw_text.strip().endswith("}"):
        try:
            j = json.loads(raw_text)
            log.info(f"📥 Webhook payload (keys via text->json): {list(j.keys())}")
            data = {norm_key(k): v for k, v in j.items()}
            data = coerce_types(try_fill_from_text(data))
            return data
        except Exception:
            pass

    # Texte kv
    if raw_text:
        data = parse_text_body(raw_text)
        log.info(f"📥 Webhook payload (keys via text): {list(data.keys())}")
        data = coerce_types(try_fill_from_text(data))
        return data

    return {}

# ========= Background market refresher (léger, mockable) =========
async def market_refresher():
    while True:
        try:
            # Ici tu peux brancher de vraies API si tu veux.
            # On fait varier un peu pour rendre le score "vivant".
            fg = STATE["market"].get("fear_greed", 28)
            fg = max(0, min(100, fg + random.choice([-1,0,0,1])))
            btc_d = STATE["market"].get("btc_dominance", 57.1)
            btc_d = max(30, min(70, btc_d + random.choice([-0.1,0,0,0.1])))
            price = STATE["market"].get("btc_price", 110_500)
            price = max(9000, price + random.choice([-50,0,25,40,-30,10]))
            STATE["market"].update({"fear_greed": fg, "btc_dominance": round(btc_d,1), "btc_price": price})
        except Exception as e:
            log.warning(f"Market refresher error: {e}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def on_start():
    asyncio.create_task(market_refresher())

# ========= API =========
@app.get("/api/trades")
async def api_trades():
    return {"trades": STATE["trades"]}

@app.post("/api/reset")
async def api_reset():
    STATE["trades"].clear()
    STATE["next_id"] = 1
    log.info("♻️ TradingState reset")
    return {"ok": True}

@app.get("/api/fear-greed")
async def api_fg():
    fg = STATE["market"].get("fear_greed", 28)
    log.info(f"✅ Fear & Greed: {fg}")
    return {"fear_greed": fg}

@app.get("/api/bullrun-phase")
async def api_phase():
    mc = STATE["market"].get("marketcap", "3.87T")
    btc_d = STATE["market"].get("btc_dominance", 57.1)
    price = STATE["market"].get("btc_price", 110_500)
    log.info(f"✅ Global: MC ${mc}, BTC.D {btc_d}%")
    log.info(f"✅ Prix: BTC ${price:,}".replace(",", " "))
    return {"marketcap":"$"+mc, "btc_dominance": btc_d, "btc_price": price}

@app.get("/api/equity-curve")
async def api_equity_curve():
    # Placeholder simple: equity = base + nb_trades * delta
    if not STATE["equity_curve"]:
        base = 10_000
        eq = base
        for i, t in enumerate(STATE["trades"] or []):
            eq += (1 if t["side"]=="BUY" else -1) * 5
            STATE["equity_curve"].append({"t": i+1, "equity": eq})
    return {"equity": STATE["equity_curve"]}

@app.get("/api/journal")
async def api_journal():
    return {"journal": STATE["journal"]}

@app.get("/api/heatmap")
async def api_heatmap():
    # Placeholder: score aléatoire
    if not STATE["heatmap"]:
        for t in STATE["trades"]:
            STATE["heatmap"][t["symbol"]] = random.randint(-5, 5)
    return {"heatmap": STATE["heatmap"]}

# ========= Webhook =========
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    data = await parse_webhook(request)

    side = (data.get("side") or "").upper()
    symbol = (data.get("symbol") or "").upper()
    if not symbol:
        log.warning("⚠️ Webhook: Symbol manquant")
        return PlainTextResponse("symbol missing", status_code=400)
    if side not in ("BUY","SELL"):
        log.warning("⚠️ Action inconnue: ''")
        return PlainTextResponse("unknown action", status_code=400)

    trade = {
        "id": STATE["next_id"],
        "symbol": symbol,
        "side": side,
        "direction": "LONG" if side=="BUY" else "SHORT",
        "timeframe": data.get("timeframe") or data.get("tf") or "-",
        "entry_time": data.get("entry_time") or _now_iso(),
        "entry": parse_number(data.get("entry")),
        "tp1": parse_number(data.get("tp1")),
        "tp2": parse_number(data.get("tp2")),
        "tp3": parse_number(data.get("tp3")),
        "sl":  parse_number(data.get("sl")),
        "alert_name": data.get("alert_name") or data.get("name") or "",
    }

    STATE["trades"].append(trade)
    STATE["next_id"] += 1
    log.info(f"✅ Trade #{trade['id']}: {trade['symbol']} {trade['side']} @ {trade['entry']}")

    # Journal minimal auto
    STATE["journal"].append({
        "time": _now_iso(),
        "text": f"{trade['symbol']} {trade['side']} {fmt_money(trade['entry'])} {trade['timeframe']}"
    })

    # Equity simple: +5/-5
    eq_last = STATE["equity_curve"][-1]["equity"] if STATE["equity_curve"] else 10_000
    eq_new = eq_last + (5 if side=="BUY" else -5)
    STATE["equity_curve"].append({"t": len(STATE["equity_curve"])+1, "equity": eq_new})

    try:
        msg = build_telegram_message(trade, STATE["market"])
        asyncio.create_task(send_telegram(msg))
    except Exception as e:
        log.error(f"Telegram build/send error: {e}")

    return JSONResponse({"ok": True, "id": trade["id"]})

# ========= HTML (sans f-strings !) =========
HTML_HEAD = """
<!doctype html><html lang="fr"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Dashboard Trading</title>
<style>
  :root{--bg:#0b0f14;--card:#121822;--muted:#98a2b3;--txt:#e6edf3;--border:#1f2937;--green:#22c55e;--red:#ef4444}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,Segoe UI,Roboto}
  nav{display:flex;gap:8px;flex-wrap:wrap;padding:12px 16px;background:#0e141b;border-bottom:1px solid var(--border)}
  nav a{padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:#0f1720;color:#cbd5e1;text-decoration:none}
  main{max-width:1100px;margin:20px auto;padding:0 16px}
  h1{margin:0 0 14px 0}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
  .table{overflow:auto;border-radius:10px;border:1px solid var(--border)}
  table{width:100%;border-collapse:collapse;min-width:900px}
  th,td{padding:10px 12px;border-bottom:1px solid var(--border);white-space:nowrap}
  th{background:#101723;color:#cbd5e1;text-align:left;position:sticky;top:0}
  tr:hover td{background:#0e1620}
  .mono{font-family:ui-monospace,Consolas,Menlo,Monaco,monospace}
  .right{text-align:right}
  .pill{padding:2px 8px;border-radius:999px;border:1px solid var(--border)}
  .buy{color:var(--green)} .sell{color:var(--red)}
  .toolbar{display:flex;justify-content:space-between;align-items:center;margin:0 0 10px 0;gap:8px;flex-wrap:wrap}
  .btn{padding:8px 12px;border-radius:10px;border:1px solid var(--border);background:#0f1720;color:#cbd5e1;cursor:pointer}
  .muted{color:var(--muted)}
  input,select{background:#0f1720;border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:8px 10px}
</style>
</head><body>
"""

HTML_FOOT = "</body></html>"

NAV = """
<nav>
  <a href="/">Accueil</a>
  <a href="/trades">Trades</a>
  <a href="/equity-curve">Equity</a>
  <a href="/journal">Journal</a>
  <a href="/heatmap">Heatmap</a>
  <a href="/strategie">Stratégie</a>
  <a href="/backtest">Backtest</a>
  <a href="/annonces">Annonces</a>
  <a href="/advanced-metrics">Advanced</a>
  <a href="/patterns">Patterns</a>
</nav>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <div class="card"><h1>🚀 Dashboard Trading</h1>
        <p class="muted">Webhook TradingView ➜ stockage ➜ Telegram ➜ visualisation.</p>
        <p>Utilise le menu ci-dessus. La page <b>Trades</b> liste toutes les entrées avec TP/SL et heure d’entrée.</p>
      </div>
    </main>
    """ + HTML_FOOT)

@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <div class="toolbar">
        <h1>💹 Trades</h1>
        <form method="post" action="/api/reset" onsubmit="setTimeout(()=>location.reload(),400);">
          <button class="btn" type="submit">♻️ Reset</button>
        </form>
      </div>
      <div class="card table">
        <table id="t">
          <thead>
            <tr>
              <th>#</th><th>Symbole</th><th>Side</th><th>Dir</th><th>TF</th>
              <th>Heure entrée</th>
              <th class="right">Entry</th>
              <th class="right">TP1</th>
              <th class="right">TP2</th>
              <th class="right">TP3</th>
              <th class="right">SL</th>
              <th>Alerte</th>
            </tr>
          </thead>
          <tbody id="body"><tr><td colspan="12" class="muted">Chargement…</td></tr></tbody>
        </table>
      </div>
    </main>
    <script>
      function money(x){ if(x===null||x===undefined||x==='') return '-'; const n=Number(x); if(!isFinite(n)) return '-'; let s=n.toFixed(6).replace(/0+$/,'').replace(/\.$/,''); return s || '0'; }
      function text(x){ return (x===null||x===undefined||x==='')?'-':x; }
      function cls(side){ return (String(side).toUpperCase()==='BUY')?'buy':'sell'; }
      async function load(){
        const r = await fetch('/api/trades',{cache:'no-store'}); const j = await r.json();
        const rows = (j.trades||[]).slice().sort((a,b)=>b.id-a.id).map(t=>`
          <tr>
            <td class="mono">${t.id}</td>
            <td class="mono">${text(t.symbol)}</td>
            <td class="pill ${cls(t.side)}">${text(t.side)}</td>
            <td class="muted">${text(t.direction)}</td>
            <td class="muted">${text(t.timeframe)}</td>
            <td class="muted mono">${text(t.entry_time)}</td>
            <td class="right mono">${money(t.entry)}</td>
            <td class="right mono">${money(t.tp1)}</td>
            <td class="right mono">${money(t.tp2)}</td>
            <td class="right mono">${money(t.tp3)}</td>
            <td class="right mono">${money(t.sl)}</td>
            <td class="muted">${text(t.alert_name)}</td>
          </tr>`).join('');
        document.getElementById('body').innerHTML = rows || '<tr><td colspan="12" class="muted">Aucun trade</td></tr>';
      }
      load(); setInterval(load, 5000);
    </script>
    """ + HTML_FOOT)

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>📈 Equity</h1>
      <p class="muted">Courbe d’equity (placeholder). API: <code>/api/equity-curve</code></p>
    </div></main>""" + HTML_FOOT)

@app.get("/journal", response_class=HTMLResponse)
async def journal():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>📝 Journal</h1>
      <p class="muted">Journal (placeholder). API: <code>/api/journal</code></p>
    </div></main>""" + HTML_FOOT)

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>🔥 Heatmap</h1>
      <p class="muted">Heatmap (placeholder). API: <code>/api/heatmap</code></p>
    </div></main>""" + HTML_FOOT)

@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>⚙️ Stratégie</h1>
      <p class="muted">Ta description de stratégie ici.</p>
    </div></main>""" + HTML_FOOT)

@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>⏮️ Backtest</h1>
      <p class="muted">Section backtest (placeholder).</p>
    </div></main>""" + HTML_FOOT)

@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>🗞️ Annonces</h1>
      <p class="muted">Flux d’annonces (placeholder). Tu peux brancher du RSS comme avant.</p>
    </div></main>""" + HTML_FOOT)

@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>📊 Advanced Metrics</h1>
      <p class="muted">Espace pour ratios, winrate, etc.</p>
    </div></main>""" + HTML_FOOT)

@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main><div class="card"><h1>🧩 Patterns</h1>
      <p class="muted">Section patterns (placeholder).</p>
    </div></main>""" + HTML_FOOT)

# ========= Demo seed =========
def seed_demo():
    if STATE["trades"]:
        return
    demo = [
        {"symbol":"BTCUSDT","side":"BUY","timeframe":"15","entry":65000,"tp1":65650,"tp2":66300,"tp3":67600,"sl":63700},
        {"symbol":"ETHUSDT","side":"SELL","timeframe":"15","entry":3500,"tp1":3465,"tp2":3430,"tp3":3360,"sl":3570},
        {"symbol":"SOLUSDT","side":"BUY","timeframe":"15","entry":140,"tp1":141.4,"tp2":142.8,"tp3":145.6,"sl":136.7},
    ]
    for d in demo:
        STATE["trades"].append({
            "id": STATE["next_id"], "symbol": d["symbol"], "side": d["side"],
            "direction": "LONG" if d["side"]=="BUY" else "SHORT",
            "timeframe": d["timeframe"], "entry_time": _now_iso(),
            "entry": d["entry"], "tp1": d["tp1"], "tp2": d["tp2"], "tp3": d["tp3"], "sl": d["sl"],
            "alert_name": "DEMO"
        })
        log.info(f"✅ Trade #{STATE['next_id']}: {d['symbol']} {d['side']} @ {d['entry']}")
        STATE["next_id"] += 1
    log.info(f"✅ Démo initialisée avec {len(demo)} trades")

seed_demo()

# ========= Run (local) =========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
