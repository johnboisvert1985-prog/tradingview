# main.py
import os
import re
import json
import time
import math
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = FastAPI(title="TradingView Webhook Bridge")

# =========================
# ------- Storage ---------
# =========================
class Trade(BaseModel):
    id: int
    side: str = Field(default="-")            # BUY / SELL
    symbol: str = Field(default="-")          # e.g. BTCUSDT.P
    direction: str = Field(default="-")       # LONG / SHORT
    timeframe: str = Field(default="-")       # e.g. 15
    entry: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl: Optional[float] = None
    entry_time: str = Field(default="-")      # human text
    alert_name: str = Field(default="-")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

TRADES: List[Trade] = []
NEXT_ID = 1

# Simple lock (single worker)
def next_id() -> int:
    global NEXT_ID
    nid = NEXT_ID
    NEXT_ID += 1
    return nid

def to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    try:
        # ignore date-like strings
        if re.search(r"\d{2}:\d{2}", s) or re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", s):
            return None
        return float(s)
    except:
        return None

def safe_text(x: Any, dash: str = "-") -> str:
    if x is None:
        return dash
    s = str(x).strip()
    return s if s else dash

# =========================
# ---- Market Context -----
# =========================
MARKET = {
    "fear_greed": 50,      # 0..100
    "btc_d": 50.0,         # %
    "mc_trillion": 3.5,    # T
    "btc_price": 70000.0,
    "last_update": None,
}

async def refresh_market_loop():
    while True:
        try:
            # Fear & Greed (alternative.me)
            async with httpx.AsyncClient(timeout=8) as client:
                try:
                    r = await client.get("https://api.alternative.me/fng/?limit=1&format=json")
                    if r.status_code == 200:
                        j = r.json()
                        v = int(j["data"][0]["value"])
                        MARKET["fear_greed"] = v
                        log.info(f"✅ Fear & Greed: {v}")
                except Exception:
                    pass

                # BTC Dom + MC + price: coinstats.app (lightweight public)
                try:
                    r = await client.get("https://api.coinstats.app/public/v1/global")
                    if r.status_code == 200:
                        j = r.json()
                        MARKET["mc_trillion"] = round(j.get("marketCap", 0) / 1e12, 2)
                        MARKET["btc_d"] = float(j.get("btcDominance", 0.0))
                        log.info(f"✅ Global: MC ${MARKET['mc_trillion']}T, BTC.D {MARKET['btc_d']}%")
                except Exception:
                    pass

                try:
                    r = await client.get("https://api.coinstats.app/public/v1/coins/bitcoin?currency=USD")
                    if r.status_code == 200:
                        j = r.json()
                        MARKET["btc_price"] = float(j["coin"]["price"])
                        log.info(f"✅ Prix: BTC ${int(MARKET['btc_price']):,}".replace(",", " "))
                except Exception:
                    pass

            MARKET["last_update"] = datetime.utcnow().isoformat()
        except Exception as e:
            log.warning(f"⚠️ market loop: {e}")
        await asyncio.sleep(60)

def score_confiance() -> Dict[str, Any]:
    fg = MARKET["fear_greed"]
    b = MARKET["btc_d"]

    # base from sentiment
    base = 50.0
    # greed (<-> fear)
    if fg <= 25:
        base += 5     # opportunité d'achat
    elif fg <= 45:
        base += 2
    elif fg >= 80:
        base -= 5
    elif fg >= 60:
        base -= 2

    # BTC dominance hurt alts longs
    note = []
    if b >= 57:
        base -= 3
        note.append("⚠️ BTC trop dominant pour altcoins")
    elif b <= 45:
        base += 2
        note.append("✅ BTC.D basse: alts favorisées")

    # clamp
    sc = max(0, min(100, round(base)))
    if sc >= 70:
        label = "ÉLEVÉ"
    elif sc >= 55:
        label = "MOYEN"
    else:
        label = "FAIBLE"

    # default explanation if empty
    if not note:
        if sc >= 55:
            note.append("✅ Sentiment frileux : léger avantage aux longs")
        else:
            note.append("⚠️ Sentiment frileux : avantage modéré")

    return {
        "score": sc,
        "label": label,
        "reasons": note,
        "fg": fg,
        "btc_d": b
    }

# =========================
# ----- Telegram Send -----
# =========================
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None

async def tg_send(text: str, parse_mode: str = "HTML"):
    if not TG_API or not TG_CHAT_ID:
        log.info("ℹ️ Telegram non configuré (TOKEN/CHAT_ID manquants)")
        return
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(TG_API, json=payload)
            if r.status_code == 200:
                log.info("✅ Telegram envoyé")
            else:
                # 429 handling
                try:
                    j = r.json()
                except Exception:
                    j = {}
                ra = 5
                if isinstance(j, dict):
                    ra = int(j.get("parameters", {}).get("retry_after", 5) or 5)
                log.error(f"❌ Telegram: {r.status_code} - {j if j else r.text}")
                if r.status_code == 429:
                    await asyncio.sleep(ra)
                    r2 = await client.post(TG_API, json=payload)
                    if r2.status_code == 200:
                        log.info("✅ Telegram envoyé (retry)")
                    else:
                        log.error(f"❌ Telegram (retry): {r2.status_code} - {r2.text}")
        except Exception as e:
            log.error(f"❌ Telegram exception: {e}")

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"

def build_telegram(tr: Trade) -> str:
    ctx = score_confiance()
    s_score = ctx["score"]; s_label = ctx["label"]
    reasons = "\n  • " + "\n  • ".join(ctx["reasons"])
    fg = ctx["fg"]; b = ctx["btc_d"]
    # direction: si absent, déduire depuis side
    direction = tr.direction
    if direction == "-" or not direction:
        if str(tr.side).upper() == "BUY":
            direction = "LONG"
        elif str(tr.side).upper() == "SELL":
            direction = "SHORT"
        else:
            direction = "-"

    lines = [
        f"🎯 <b>NOUVEAU TRADE — {safe_text(tr.symbol)}</b>",
        "",
        f"📊 <b>{safe_text(tr.side).upper()}</b>",
        f"📈 Direction: {direction} | {safe_text(tr.timeframe)}m" if tr.timeframe not in ("-", "") else f"📈 Direction: {direction} | -",
        "",
    ]
    # Heure + Entry (prix)
    if tr.entry_time and tr.entry_time != "-":
        lines.append(f"🕒 Heure: {tr.entry_time}")
    if tr.entry is not None:
        lines.append(f"💰 Entry: ${fmt_money(tr.entry)}")

    # TP/SL
    show_tps = any(v is not None for v in [tr.tp1, tr.tp2, tr.tp3])
    if show_tps:
        lines.append("")
        lines.append("🎯 Take Profits:")
        lines.append(f"  TP1: ${fmt_money(tr.tp1)}")
        lines.append(f"  TP2: ${fmt_money(tr.tp2)}")
        lines.append(f"  TP3: ${fmt_money(tr.tp3)}")
    if tr.sl is not None:
        if not show_tps:
            lines.append("")
        lines.append(f"🛑 Stop Loss: ${fmt_money(tr.sl)}")

    lines.extend([
        "",
        f"📊 <b>CONFIANCE:</b> {s_score}% ({s_label})",
        "",
        "Pourquoi ce score ?" + reasons,
        "",
        f"💡 Marché: F&G {fg} | BTC.D {fmt_money(b)}%",
    ])
    return "\n".join(lines)

# =========================
# ---- Webhook Parsing ----
# =========================
SIDE_RE = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b", re.I)
SYM_RE  = re.compile(r"\b([A-Z0-9]{3,}USDT(?:\.P)?)\b", re.I)

def parse_text_payload(txt: str) -> Dict[str, Any]:
    # Format style "key=value; key2=value2" ou texte brut avec indices
    d: Dict[str, Any] = {}
    # key=value pairs
    for part in re.split(r"[;\n]+", txt):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip().lower()] = v.strip()
    # fallback: essayer de repêcher infos via regex dans tout le blob
    blob = txt

    # side
    if not d.get("side"):
        m = SIDE_RE.search(blob)
        if m:
            val = m.group(1).upper()
            if val == "LONG":  val = "BUY"
            if val == "SHORT": val = "SELL"
            d["side"] = val

    # symbol
    if not d.get("symbol"):
        m = SYM_RE.search(blob)
        if m:
            d["symbol"] = m.group(1).upper()

    # timeframe (tf)
    if not d.get("tf"):
        m = re.search(r"\b(1|3|5|15|30|45|60|120|240|D|W|M)\b", blob)
        if m:
            d["tf"] = m.group(1)

    # entry/entry_time heuristics
    if not d.get("entry") and not d.get("entry_time"):
        # prix (décimal)
        m = re.search(r"(\d+\.\d{2,})", blob)
        if m:
            d["entry"] = m.group(1)
        # time
        m2 = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}(:\d{2})?)", blob)
        if m2:
            d["entry_time"] = m2.group(1)

    # alert_name
    if not d.get("alert_name") and "alert_name:" in blob.lower():
        m = re.search(r"alert_name\s*:\s*(.+)", blob, re.I)
        if m:
            d["alert_name"] = m.group(1).strip()

    return d

def normalize_direction(side_val: str) -> str:
    s = (side_val or "").upper()
    if s == "BUY":
        return "LONG"
    if s == "SELL":
        return "SHORT"
    return "-"

def build_trade_from_dict(d: Dict[str, Any]) -> Trade:
    # entry vs entry_time disambiguation
    raw_entry = d.get("entry", None)
    entry_time = safe_text(d.get("entry_time"), "-")
    entry_val = to_float(raw_entry)

    # si entry ressemble à une heure => bascule vers entry_time
    if entry_val is None and raw_entry:
        if re.search(r"\d{2}:\d{2}", str(raw_entry)):
            if entry_time == "-":
                entry_time = str(raw_entry)

    side = safe_text(d.get("side")).upper()
    if side == "LONG":
        side = "BUY"
    if side == "SHORT":
        side = "SELL"

    symbol = safe_text(d.get("symbol")).upper()
    if symbol == "-" and d.get("alert_name"):
        m = SYM_RE.search(d["alert_name"])
        if m:
            symbol = m.group(1).upper()

    timeframe = safe_text(d.get("tf"), "-")

    direction = safe_text(d.get("direction"), "-")
    if direction == "-" and side in ("BUY", "SELL"):
        direction = "LONG" if side == "BUY" else "SHORT"

    tr = Trade(
        id=next_id(),
        side=side if side else "-",
        symbol=symbol,
        direction=direction,
        timeframe=timeframe,
        entry=entry_val,
        tp1=to_float(d.get("tp1")),
        tp2=to_float(d.get("tp2")),
        tp3=to_float(d.get("tp3")),
        sl=to_float(d.get("sl")),
        entry_time=entry_time,
        alert_name=safe_text(d.get("alert_name"), "-"),
    )
    return tr

# =========================
# --------- API ----------
# =========================
@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh_market_loop())
    # Seed demo (visuel)
    global TRADES, NEXT_ID
    TRADES.clear(); NEXT_ID = 1
    demo = [
        {"side":"BUY","symbol":"BTCUSDT","entry":65000,"tf":"15","entry_time":"-"},
        {"side":"SELL","symbol":"ETHUSDT","entry":3500,"tf":"15"},
        {"side":"BUY","symbol":"SOLUSDT","entry":140,"tf":"15"},
    ]
    for d in demo:
        TRADES.append(build_trade_from_dict(d))
    log.info(f"✅ Démo initialisée avec {len(TRADES)} trades")

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <div class="hero">
        <h1>🚀 Dashboard Trading</h1>
        <p class="muted">Bridge TradingView → Telegram + Web UI</p>
        <div class="grid">
          <a class="card link" href="/trades">📈 Trades</a>
          <a class="card link" href="/equity-curve">📉 Equity</a>
          <a class="card link" href="/journal">📝 Journal</a>
          <a class="card link" href="/heatmap">🔥 Heatmap</a>
          <a class="card link" href="/strategie">⚙️ Stratégie</a>
          <a class="card link" href="/backtest">⏮️ Backtest</a>
        </div>
      </div>
    </main>
    """ + HTML_FOOT)

@app.get("/api/trades")
async def api_trades():
    return {"trades": [t.dict() for t in TRADES]}

@app.post("/api/reset")
async def api_reset():
    global TRADES, NEXT_ID
    TRADES.clear(); NEXT_ID = 1
    log.info("♻️ TradingState reset")
    return {"ok": True}

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

# --- Equity ---
@app.get("/api/equity-curve")
async def api_equity():
    # Equity synthétique depuis entries
    eq = 10000.0
    pts = []
    for t in TRADES:
        # petit PnL aléatoire déterministe sur id
        pnl = (1 if (t.side.upper()=="BUY") else -1) * (0.0015 * (t.id % 7))
        eq *= (1 + pnl)
        pts.append({"id": t.id, "symbol": t.symbol, "equity": round(eq,2)})
    return {"equity": pts}

@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <h1>📉 Equity Curve</h1>
      <div class="card">
        <canvas id="c" height="140"></canvas>
      </div>
    </main>
    <script>
      async function load(){
        const r = await fetch('/api/equity-curve',{cache:'no-store'}); const j = await r.json();
        const labels = j.equity.map(x=>x.id);
        const data = j.equity.map(x=>x.equity);
        const ctx = document.getElementById('c').getContext('2d');
        new Chart(ctx,{type:'line',data:{labels:labels,datasets:[{label:'Equity',data:data,fill:false}]},options:{responsive:true,plugins:{legend:{display:false}}}});
      }
      load();
    </script>
    """ + HTML_FOOT_CHART)

# --- Journal ---
@app.get("/api/journal")
async def api_journal():
    return {"notes":[{"when":t.created_at,"text":f"{t.side} {t.symbol} @ {fmt_money(t.entry)}"} for t in TRADES]}

@app.get("/journal", response_class=HTMLResponse)
async def journal_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <h1>📝 Journal</h1>
      <div id="list" class="stack card"></div>
    </main>
    <script>
      async function load(){
        const r = await fetch('/api/journal',{cache:'no-store'}); const j = await r.json();
        const html = (j.notes||[]).reverse().map(n=>`<div class="row"><div class="muted">${n.when}</div><div>${n.text}</div></div>`).join('');
        document.getElementById('list').innerHTML = html || '<div class="muted">Vide</div>';
      }
      load();
    </script>
    """ + HTML_FOOT)

# --- Heatmap ---
@app.get("/api/heatmap")
async def api_heatmap():
    # synthétique
    rows = []
    syms = list({t.symbol for t in TRADES})
    for s in syms:
        v = (sum((1 if t.side=="BUY" else -1) for t in TRADES if t.symbol==s))/max(1,len([t for t in TRADES if t.symbol==s]))
        rows.append({"symbol": s, "score": round(50+v*25,1)})
    return {"rows": rows}

@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <h1>🔥 Heatmap</h1>
      <div id="grid" class="grid"></div>
    </main>
    <script>
      async function load(){
        const r = await fetch('/api/heatmap',{cache:'no-store'}); const j = await r.json();
        const html = (j.rows||[]).map(x=>`<div class="card box"><div class="muted">${x.symbol}</div><div class="big">${x.score}</div></div>`).join('');
        document.getElementById('grid').innerHTML = html || '<div class="muted">Vide</div>';
      }
      load();
    </script>
    """ + HTML_FOOT)

# --- Stratégie / Backtest (placeholders esthétiques) ---
@app.get("/strategie", response_class=HTMLResponse)
async def strat_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <h1>⚙️ Stratégie</h1>
      <div class="card">
        <p class="muted">Décrivez ici les règles de votre stratégie. (Placeholder)</p>
      </div>
    </main>
    """ + HTML_FOOT)

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    return HTMLResponse(HTML_HEAD + NAV + """
    <main>
      <h1>⏮️ Backtest</h1>
      <div class="card">
        <p class="muted">Courbes & stats de backtest (placeholder).</p>
      </div>
    </main>
    """ + HTML_FOOT)

# =========================
# ----- TV Webhook --------
# =========================
@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    ctype = request.headers.get("content-type","").lower()
    try:
        if "application/json" in ctype:
            payload = await request.json()
            if isinstance(payload, dict):
                d = {k.lower(): v for k,v in payload.items()}
            else:
                # TradingView peut envoyer une string JSON en 'text/plain'
                d = {}
        elif "text/plain" in ctype or "application/x-www-form-urlencoded" in ctype:
            text = (await request.body()).decode("utf-8", errors="ignore")
            log.info("📥 Webhook payload (keys via text): %s", list(parse_text_payload(text).keys()))
            d = parse_text_payload(text)
        else:
            # tenter json puis texte
            try:
                j = await request.json()
                d = {k.lower(): v for k,v in j.items()}
            except:
                text = (await request.body()).decode("utf-8","ignore")
                d = parse_text_payload(text)

        # déductions supplémentaires
        # side
        if not d.get("side"):
            blob = json.dumps(d, ensure_ascii=False)
            m = SIDE_RE.search(blob)
            if m:
                val = m.group(1).upper()
                if val == "LONG":  val = "BUY"
                if val == "SHORT": val = "SELL"
                d["side"] = val

        # symbol via alert_name si manquant
        if not d.get("symbol") and d.get("alert_name"):
            m = SYM_RE.search(str(d["alert_name"]))
            if m:
                d["symbol"] = m.group(1).upper()

        # require minimal keys
        if not d.get("side"):
            log.warning("⚠️ Side manquant")
            return PlainTextResponse("Bad Request", status_code=400)

        tr = build_trade_from_dict(d)
        TRADES.append(tr)

        # Telegram
        txt = build_telegram(tr)
        asyncio.create_task(tg_send(txt))

        return JSONResponse({"ok": True, "id": tr.id})
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return PlainTextResponse("Error", status_code=500)

# =========================
# --------- UI ------------
# =========================
HTML_HEAD = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Trading Dashboard</title>
<style>
:root{
  --bg:#0e1014; --card:#161a22; --text:#e7ecf3; --muted:#8a93a5; --buy:#16c784; --sell:#ea3943; --line:#222838;
}
*{box-sizing:border-box} body{margin:0;font:14px/1.35 system-ui,Segoe UI,Roboto,Helvetica,Arial;color:var(--text);background:linear-gradient(180deg,#0e1014 0%,#0e1014 60%,#0b0d11 100%) fixed;}
main{max-width:1100px;margin:24px auto;padding:0 16px}
.nav{display:flex;gap:10px;flex-wrap:wrap;padding:10px 16px;background:#0b0d11;border-bottom:1px solid var(--line)}
.nav a{color:var(--text);text-decoration:none;padding:8px 10px;border-radius:10px}
.nav a:hover{background:#121620}
.hero{display:flex;flex-direction:column;gap:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.card.link{display:flex;align-items:center;justify-content:center;min-height:90px;font-weight:600}
.table table{width:100%;border-collapse:collapse}
.table th,.table td{padding:10px;border-bottom:1px solid var(--line)}
.table th{font-weight:600;text-align:left;color:#cdd6e5}
.table td.right{text-align:right}
.muted{color:var(--muted)}
.mono{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;}
.big{font-size:24px;font-weight:700}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--line);background:#10141b}
.pill.buy{border-color:rgba(22,199,132,.35);color:#b7f0d5}
.pill.sell{border-color:rgba(234,57,67,.35);color:#f7c0c5}
.toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.btn{background:#0f1320;color:#e7ecf3;border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer}
.stack .row{display:flex;gap:12px;padding:8px;border-bottom:1px solid var(--line)}
.box{display:flex;align-items:center;justify-content:space-between}
</style>
</head>
<body>
<nav class="nav">
  <a href="/">🏠 Accueil</a>
  <a href="/trades">📈 Trades</a>
  <a href="/equity-curve">📉 Equity</a>
  <a href="/journal">📝 Journal</a>
  <a href="/heatmap">🔥 Heatmap</a>
  <a href="/strategie">⚙️ Stratégie</a>
  <a href="/backtest">⏮️ Backtest</a>
</nav>
"""

NAV = ""  # (déjà inclus dans HTML_HEAD)

HTML_FOOT = """
</body></html>
"""

HTML_FOOT_CHART = """
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</body></html>
"""

# =========================
# ------- Run (local) -----
# =========================
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
