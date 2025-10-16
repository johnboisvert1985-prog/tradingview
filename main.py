# main.py
import os
import re
import json
import time
import math
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# App & state
# -----------------------------------------------------------------------------
app = FastAPI(title="TradingView Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# In-memory state
TRADES: List[Dict[str, Any]] = []

MARKET = {
    "fear_greed": 28,        # défaut pour démarrer
    "btc_dominance": 57.1,   # défaut
    "btc_price": 110_000.0,  # défaut approx
}

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class Confidence(BaseModel):
    score: int
    label: str
    reasons: List[str] = Field(default_factory=list)


class Trade(BaseModel):
    symbol: str
    side: str                # BUY/SELL
    direction: str           # LONG/SHORT
    tf: Optional[str] = None
    entry: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl: Optional[float] = None
    confidence: Optional[Confidence] = None
    created_at: str
    entry_time: Optional[str] = None
    alert_name: Optional[str] = None


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        # Certains arrivent comme str avec “$” etc.
        s = str(x).strip().replace("$", "").replace(",", "")
        if s == "" or s == "-":
            return None
        return float(s)
    except Exception:
        return None


def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "$-"
    # formattage lisible
    if abs(x) >= 1:
        return "$" + f"{x:,.3f}".replace(",", " ").replace(" ", ",").replace(",", " ")
    # petites valeurs (altcoins)
    return "$" + f"{x:.6f}"


def side_to_direction(side: str) -> str:
    return "LONG" if (side or "").upper() == "BUY" else "SHORT"


def label_from_score(score: int) -> str:
    if score >= 70:
        return "ÉLEVÉ"
    if score >= 55:
        return "MOYEN"
    return "FAIBLE"


def build_confidence(side: str) -> Confidence:
    """
    Score 'vivant' en fonction de F&G, dominance BTC, et sens (long/short).
    Simple, stable et explicable.
    """
    reasons: List[str] = []
    base = 50

    # Fear & Greed
    fng = MARKET.get("fear_greed", 50)
    if fng <= 25:
        base += 10
        reasons.append("✅ Sentiment de peur : opportunités d'achat accrues")
    elif fng >= 75:
        base -= 10
        reasons.append("⚠️ Euphorie du marché : risques de retournement")

    # BTC Dominance
    btcd = MARKET.get("btc_dominance", 50.0)
    if btcd >= 57.0:
        reasons.append("⚠️ BTC très dominant : altcoins sous-performent")
        # pénalise surtout les trades sur alt si LONG
        if side.upper() == "BUY":
            base -= 5
    else:
        reasons.append("✅ Dominance BTC modérée : terrain favorable aux alts")

    # Normalisation + pincement
    score = max(30, min(85, base))
    label = label_from_score(score)
    return Confidence(score=score, label=label, reasons=reasons)


async def send_telegram(msg: str) -> None:
    """Envoi Telegram avec gestion du 429 Too Many Requests."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        attempts = 0
        while attempts < 3:
            attempts += 1
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                return
            if r.status_code == 429:
                data = r.json()
                retry_after = data.get("parameters", {}).get("retry_after", 1)
                await asyncio.sleep(retry_after)
                continue
            # Autres erreurs : on sort
            return


def render_confidence_md(c: Confidence) -> str:
    lines = [f"📊 *CONFIANCE*: {c.score}% ({c.label})", "", "Pourquoi ce score ?"]
    for r in c.reasons:
        lines.append(f"  • {r}")
    return "\n".join(lines)


def render_trade_md(tr: Trade) -> str:
    # Titre avec symbole
    head = f"🎯 *NOUVEAU TRADE — {tr.symbol}*"
    # Direction et TF
    tf_part = tr.tf if tr.tf else "-"
    dir_line = f"📈 *Direction*: {tr.direction} | {tf_part}"

    # Prix d'entrée + TPs + SL
    entry_line = f"💰 *Entry*: {fmt_money(tr.entry)}"
    tp_block = "\n".join([
        "🎯 *Take Profits*:",
        f"  TP1: {fmt_money(tr.tp1)}",
        f"  TP2: {fmt_money(tr.tp2)}",
        f"  TP3: {fmt_money(tr.tp3)}",
    ])
    sl_line = f"🛑 *Stop Loss*: {fmt_money(tr.sl)}"

    # Confiance
    conf = tr.confidence or build_confidence(tr.side)
    conf_block = render_confidence_md(conf)

    # Marché
    mkt = f"💡 *Marché*: F&G {MARKET.get('fear_greed','-')} | BTC.D {MARKET.get('btc_dominance','-')}%"

    parts = [
        head,
        "",
        f"📊 *{tr.side}*",
        dir_line,
        "",
        entry_line,
        "",
        tp_block,
        "",
        sl_line,
        "",
        conf_block,
        "",
        mkt,
    ]
    return "\n".join(parts)


# -----------------------------------------------------------------------------
# Webhook parsing
# -----------------------------------------------------------------------------
KV_RE = re.compile(r"^\s*([^:=\s]+)\s*[:=]\s*(.+?)\s*$")

def parse_text_payload(txt: str) -> Dict[str, Any]:
    """Parsage large du texte TradingView (clé=valeur, lignes, etc.)."""
    out: Dict[str, Any] = {}
    if not txt:
        return out

    # Si c'est du JSON dans du texte…
    s = txt.strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            j = json.loads(s)
            if isinstance(j, dict):
                return j
        except Exception:
            pass

    # Sinon, on parcourt ligne par ligne
    lines = [x for x in re.split(r"[\r\n]+", s) if x.strip() != ""]
    for line in lines:
        m = KV_RE.match(line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            out[k] = v

    return out


def normalize_webhook(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise différentes clés possibles venant de TradingView.
    Attend : side, symbol, tf, entry, tp1,tp2,tp3, sl, entry_time/created_at, alert_name.
    """
    # alias fréquents
    aliases = {
        "side": ["side", "action", "order", "type"],
        "symbol": ["symbol", "ticker", "symbole"],
        "tf": ["tf", "timeframe", "interval"],
        "entry": ["entry", "entree", "price", "prix", "entry_price"],
        "tp1": ["tp1", "take_profit_1", "tp_1"],
        "tp2": ["tp2", "take_profit_2", "tp_2"],
        "tp3": ["tp3", "take_profit_3", "tp_3"],
        "sl": ["sl", "stop", "stop_loss", "stoploss"],
        "entry_time": ["entry_time", "heure", "time", "timestamp"],
        "created_at": ["created_at", "created", "alert_time"],
        "alert_name": ["alert_name", "name", "titre", "title"],
        "direction": ["direction"],  # parfois fourni
    }

    def pick(keys: List[str]) -> Optional[str]:
        for k in keys:
            if k in data and str(data[k]).strip() != "":
                return str(data[k]).strip()
        return None

    side = (pick(aliases["side"]) or "").upper()
    if side in ("BUY", "LONG"):
        side = "BUY"
    elif side in ("SELL", "SHORT"):
        side = "SELL"

    symbol = pick(aliases["symbol"]) or "-"
    tf = pick(aliases["tf"])
    direction = pick(aliases["direction"]) or side_to_direction(side)

    entry = safe_float(pick(aliases["entry"]))
    tp1 = safe_float(pick(aliases["tp1"]))
    tp2 = safe_float(pick(aliases["tp2"]))
    tp3 = safe_float(pick(aliases["tp3"]))
    sl = safe_float(pick(aliases["sl"]))

    entry_time = pick(aliases["entry_time"])
    created_at = pick(aliases["created_at"]) or now_iso()
    alert_name = pick(aliases["alert_name"])

    return {
        "side": side,
        "symbol": symbol,
        "tf": tf,
        "direction": direction,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl,
        "entry_time": entry_time,
        "created_at": created_at,
        "alert_name": alert_name,
    }


# -----------------------------------------------------------------------------
# Webhook endpoint
# -----------------------------------------------------------------------------
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    ctype = request.headers.get("content-type", "").lower()

    try:
        if "application/json" in ctype:
            body = await request.json()
        else:
            raw = await request.body()
            txt = raw.decode("utf-8", errors="ignore")
            body = parse_text_payload(txt)
    except Exception:
        body = {}

    data = normalize_webhook(body)

    if not data.get("side"):
        return PlainTextResponse("Side manquant", status_code=400)

    # Build trade + confiance
    conf = build_confidence(data["side"])
    trade = Trade(
        symbol=data["symbol"],
        side=data["side"],
        direction=data["direction"],
        tf=data.get("tf"),
        entry=data.get("entry"),
        tp1=data.get("tp1"),
        tp2=data.get("tp2"),
        tp3=data.get("tp3"),
        sl=data.get("sl"),
        confidence=conf,
        created_at=data["created_at"],
        entry_time=data.get("entry_time") or data["created_at"],
        alert_name=data.get("alert_name"),
    ).model_dump()

    # Stocke + notifie Telegram immédiatement
    TRADES.append(trade)
    msg = render_trade_md(Trade(**trade))
    asyncio.create_task(send_telegram(msg))

    return JSONResponse({"ok": True})


# -----------------------------------------------------------------------------
# APIs
# -----------------------------------------------------------------------------
@app.get("/api/trades")
async def api_trades():
    return JSONResponse(TRADES)


@app.post("/api/reset")
async def api_reset():
    TRADES.clear()
    return JSONResponse({"ok": True, "msg": "TradingState reset"})


@app.get("/api/fear-greed")
async def api_fng():
    return JSONResponse({"value": MARKET["fear_greed"]})


@app.get("/api/bullrun-phase")
async def api_bullrun():
    return JSONResponse({
        "global_mc": None,  # optionnel
        "btc_dominance": MARKET["btc_dominance"],
        "btc_price": MARKET["btc_price"],
    })


# (optionnel) setters manuels pour dev
@app.post("/api/market/set")
async def api_market_set(payload: Dict[str, Any]):
    if "fear_greed" in payload:
        MARKET["fear_greed"] = int(payload["fear_greed"])
    if "btc_dominance" in payload:
        MARKET["btc_dominance"] = float(payload["btc_dominance"])
    if "btc_price" in payload:
        MARKET["btc_price"] = float(payload["btc_price"])
    return JSONResponse({"ok": True, "market": MARKET})


# -----------------------------------------------------------------------------
# UI (Dashboard + Trades)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    html = """
    <!doctype html>
    <html lang="fr">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Dashboard Trading</title>
      <style>
        :root{
          --bg:#0f172a;
          --card:#111827;
          --muted:#9ca3af;
          --text:#e5e7eb;
          --accent:#22d3ee;
          --green:#22c55e;
          --red:#ef4444;
          --yellow:#f59e0b;
          --border:#1f2937;
        }
        *{box-sizing:border-box}
        body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif; background:linear-gradient(180deg,#0b1224 0%, #0f172a 100%); color:var(--text); margin:0; padding:24px;}
        a{color:var(--accent); text-decoration:none}
        .container{max-width:1200px; margin:0 auto}
        .topbar{display:flex; align-items:center; justify-content:space-between; margin-bottom:24px}
        .title{font-size:24px; font-weight:700; letter-spacing:.3px}
        .grid{display:grid; gap:16px; grid-template-columns:repeat(12,1fr)}
        .card{background:rgba(17,24,39,.8); border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.25); backdrop-filter: blur(6px);}
        .card h3{margin:.25rem 0 1rem; font-size:14px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.08em}
        .kpi{font-size:28px; font-weight:700}
        .muted{color:var(--muted)}
        .btn{display:inline-block; padding:10px 14px; border-radius:10px; border:1px solid var(--border); background:#0b1224; transition:.2s all; font-weight:600}
        .btn:hover{transform:translateY(-1px); border-color:#273449}
        .row{display:flex; gap:8px; align-items:center}
        .pill{padding:4px 8px; border-radius:999px; font-size:12px; border:1px solid var(--border); background:#0c1428}
        .col-3{grid-column:span 3}
        .col-4{grid-column:span 4}
        .col-8{grid-column:span 8}
        .col-12{grid-column:span 12}
        @media(max-width:1024px){.col-3,.col-4,.col-8{grid-column:span 6}}
        @media(max-width:640px){.col-3,.col-4,.col-8{grid-column:span 12}}
        .list{display:grid; gap:12px}
        .trade{display:grid; grid-template-columns: 120px 1fr 80px 80px 120px; gap:8px; align-items:center; padding:10px; border-radius:12px; border:1px solid var(--border); background:rgba(2,6,23,.45)}
        .badge{padding:4px 8px; border-radius:8px; font-weight:700; font-size:12px}
        .badge.buy{color:#bbf7d0; background:rgba(34,197,94,.12); border:1px solid rgba(34,197,94,.3)}
        .badge.sell{color:#fecaca; background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.3)}
        .mono{font-variant-numeric:tabular-nums}
        canvas{width:100%; height:140px; display:block}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="topbar">
          <div class="title">📊 Dashboard Trading</div>
          <div class="row">
            <a class="btn" href="/trades">Voir les trades</a>
            <a class="btn" href="/journal">Journal</a>
            <a class="btn" href="/equity-curve">Équity</a>
          </div>
        </div>

        <div class="grid">
          <div class="card col-3">
            <h3>Fear & Greed</h3>
            <div class="kpi"><span id="kpi-fng">—</span></div>
            <div class="muted">Indice sentiment marché</div>
          </div>
          <div class="card col-3">
            <h3>BTC Dominance</h3>
            <div class="kpi"><span id="kpi-btcd">—</span></div>
            <div class="muted">Poids BTC sur marché</div>
          </div>
          <div class="card col-3">
            <h3>BTC Price</h3>
            <div class="kpi mono"><span id="kpi-btc">—</span></div>
            <div class="muted">Prix spot (approx.)</div>
          </div>
          <div class="card col-3">
            <h3>Actions rapides</h3>
            <div class="row" style="margin-bottom:8px">
              <a class="btn" href="/api/reset">♻️ Reset</a>
              <a class="btn" href="/annonces">🗞️ News</a>
            </div>
            <div class="muted">Maintenance & navigation</div>
          </div>

          <div class="card col-8">
            <h3>Derniers trades</h3>
            <div id="recent" class="list"></div>
          </div>

          <div class="card col-4">
            <h3>Équity (mini)</h3>
            <canvas id="eq"></canvas>
            <div class="muted" style="margin-top:6px">Aperçu indicatif</div>
          </div>
        </div>
      </div>

      <script>
        async function getJSON(u){ const r = await fetch(u); return r.json(); }

        function fmtNum(x){
          if(x==null||x==='') return '—';
          const n=Number(x);
          if(Number.isNaN(n)) return '—';
          if(Math.abs(n)>=1000) return n.toLocaleString(undefined,{maximumFractionDigits:0});
          if(Math.abs(n)>=1) return n.toFixed(2);
          if(Math.abs(n)>=0.01) return n.toFixed(4);
          return n.toFixed(6);
        }
        function fmtMoney(x){ const v=fmtNum(x); return v==='—'?'—':('$'+v); }

        async function loadMarket(){
          try{
            const f = await getJSON('/api/fear-greed'); 
            document.getElementById('kpi-fng').textContent = f?.value ?? '—';
          }catch(e){}
          try{
            const b = await getJSON('/api/bullrun-phase');
            document.getElementById('kpi-btcd').textContent = (b?.btc_dominance!=null? b.btc_dominance.toFixed(1)+'%':'—');
            document.getElementById('kpi-btc').textContent = (b?.btc_price!=null? Math.round(b.btc_price).toLocaleString():'—');
          }catch(e){}
        }

        function pill(side){
          return '<span class="badge '+(side==='BUY'?'buy':'sell')+'">'+side+'</span>';
        }

        function renderRecent(trades){
          const root = document.getElementById('recent'); root.innerHTML='';
          trades.slice(-5).reverse().forEach(t=>{
            const el=document.createElement('div'); el.className='trade';
            el.innerHTML = `
              <div class="mono">${t.entry_time || t.created_at || '-'}</div>
              <div><strong>${t.symbol||'-'}</strong> <span class="pill">${t.tf||'-'}</span></div>
              <div>${pill(t.side||'-')}</div>
              <div class="mono">${t.direction||'-'}</div>
              <div class="mono">${fmtMoney(t.entry)}</div>
            `;
            root.appendChild(el);
          });
        }

        function miniEq(trades){
          const c = document.getElementById('eq'); const ctx = c.getContext('2d');
          const w = c.width = c.clientWidth; const h = c.height = 140;
          ctx.clearRect(0,0,w,h);
          const pts=[]; let eq=0;
          trades.forEach(t=>{ eq += (t.side==='BUY'?+1:-1); pts.push(eq); });
          if(pts.length===0){ ctx.strokeStyle='#334155'; ctx.strokeRect(0,0,w,h); return; }
          const min=Math.min(...pts), max=Math.max(...pts);
          const rng = (max-min)||1;
          ctx.beginPath(); ctx.strokeStyle = '#22d3ee'; ctx.lineWidth=2;
          pts.forEach((v,i)=>{
            const x = (i/(pts.length-1))*w;
            const y = h - ((v-min)/rng)*h;
            if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
          });
          ctx.stroke();
        }

        async function loadTrades(){
          const t = await getJSON('/api/trades');
          renderRecent(t);
          miniEq(t);
        }

        async function boot(){
          await loadMarket();
          await loadTrades();
          setInterval(loadMarket, 30000);
          setInterval(loadTrades, 10000);
        }
        boot();
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    html = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trades</title>
  <style>
    :root{
      --bg:#0f172a; --card:#111827; --border:#1f2937; --text:#e5e7eb; --muted:#94a3b8;
      --green:#22c55e; --red:#ef4444; --accent:#22d3ee;
    }
    *{box-sizing:border-box}
    body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif; background:linear-gradient(180deg,#0b1224 0%, #0f172a 100%); color:var(--text); margin:0; padding:24px;}
    .wrap{max-width:1200px; margin:0 auto}
    h1{margin:0 0 16px 0}
    .muted{color:var(--muted)}
    .card{background:rgba(17,24,39,.8); border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.25); backdrop-filter: blur(6px);}
    table{border-collapse:collapse; width:100%; overflow:hidden; border-radius:12px; border:1px solid var(--border)}
    thead th{background:#0b1224; color:#a5b4fc; font-weight:700; font-size:12px; letter-spacing:.06em; text-transform:uppercase; padding:12px; text-align:left}
    tbody td{border-top:1px solid var(--border); padding:10px; font-variant-numeric:tabular-nums}
    .badge{padding:4px 8px; border-radius:8px; font-weight:700; font-size:12px; border:1px solid transparent}
    .badge.buy{color:#bbf7d0; background:rgba(34,197,94,.12); border-color:rgba(34,197,94,.3)}
    .badge.sell{color:#fecaca; background:rgba(239,68,68,.12); border-color:rgba(239,68,68,.3)}
    .pill{padding:4px 8px; border-radius:999px; font-size:12px; border:1px solid var(--border); color:#cbd5e1}
    .mono{font-variant-numeric:tabular-nums}
    .toolbar{display:flex; gap:8px; align-items:center; margin:12px 0 16px}
    .btn{display:inline-block; padding:8px 12px; border-radius:10px; border:1px solid var(--border); background:#0b1224; color:var(--text); text-decoration:none; font-weight:600}
    .btn:hover{transform:translateY(-1px)}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>📈 Trades</h1>
    <div class="muted">Actualisation auto toutes les 10s</div>
    <div class="toolbar">
      <a class="btn" href="/">← Retour</a>
      <a class="btn" href="/api/reset">♻️ Reset</a>
    </div>
    <div class="card">
      <table id="t">
        <thead>
          <tr>
            <th>Heure</th>
            <th>Symbole</th>
            <th>Side</th>
            <th>TF</th>
            <th>Entry</th>
            <th>TP1</th>
            <th>TP2</th>
            <th>TP3</th>
            <th>SL</th>
            <th>Confiance</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <script>
    function formatNumber(x) {
      if (x === null || x === undefined || x === '') return '-';
      const n = Number(x);
      if (Number.isNaN(n)) return '-';
      if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
      if (Math.abs(n) >= 1) return n.toFixed(3);
      if (Math.abs(n) >= 0.01) return n.toFixed(4);
      return n.toFixed(6);
    }
    function money(x){ const v = formatNumber(x); return v==='-'?'-':('$'+v); }
    function badge(side){
      side = (side||'').toUpperCase();
      if(side==='BUY') return '<span class="badge buy">BUY</span>';
      if(side==='SELL') return '<span class="badge sell">SELL</span>';
      return '<span class="pill">-</span>';
    }

    async function load(){ 
      const r = await fetch('/api/trades'); 
      const js = await r.json();
      const tbody = document.querySelector('#t tbody');
      tbody.innerHTML = '';
      js.slice().reverse().forEach(tr => {
        const c = tr.confidence ? (tr.confidence.score + '% (' + tr.confidence.label + ')') : '-';
        const trEl = document.createElement('tr');
        trEl.innerHTML = `
          <td class="mono">${tr.entry_time || tr.created_at || '-'}</td>
          <td><strong>${tr.symbol || '-'}</strong></td>
          <td>${badge(tr.side)}</td>
          <td><span class="pill">${tr.tf || '-'}</span></td>
          <td class="mono">${money(tr.entry)}</td>
          <td class="mono">${money(tr.tp1)}</td>
          <td class="mono">${money(tr.tp2)}</td>
          <td class="mono">${money(tr.tp3)}</td>
          <td class="mono">${money(tr.sl)}</td>
          <td>${c}</td>
        `;
        tbody.appendChild(trEl);
      });
    }
    load(); setInterval(load, 10000);
  </script>
</body>
</html>"""
    return HTMLResponse(html)


# -----------------------------------------------------------------------------
# Petites pages (pour éviter 404 et garder l’UX)
# -----------------------------------------------------------------------------
@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8"><title>Équity</title></head>
    <body style="background:#0f172a;color:#e5e7eb;font-family:Inter;padding:24px">
      <h1>📈 Équity (aperçu)</h1>
      <p>Simple placeholder (à brancher sur vos données historiques si besoin).</p>
      <p><a href="/" style="color:#22d3ee">← Retour</a></p>
    </body></html>
    """)


@app.get("/journal", response_class=HTMLResponse)
async def journal():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8"><title>Journal</title></head>
    <body style="background:#0f172a;color:#e5e7eb;font-family:Inter;padding:24px">
      <h1>📝 Journal</h1>
      <p>Placeholder. Vous pouvez consigner vos notes ici ultérieurement.</p>
      <p><a href="/" style="color:#22d3ee">← Retour</a></p>
    </body></html>
    """)


@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8"><title>News</title></head>
    <body style="background:#0f172a;color:#e5e7eb;font-family:Inter;padding:24px">
      <h1>🗞️ News</h1>
      <p>Flux d'annonces (placeholder). Intégrez vos RSS ici.</p>
      <p><a href="/" style="color:#22d3ee">← Retour</a></p>
    </body></html>
    """)


# -----------------------------------------------------------------------------
# Run hint (Render/uvicorn utilise: "uvicorn main:app")
# -----------------------------------------------------------------------------
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
