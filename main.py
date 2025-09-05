# main.py
import os
import json
from typing import Optional, Union, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

# =========================
# ENV
# =========================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # ex: nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "8000"))

# =========================
# APP
# =========================
app = FastAPI(title="AI Trader PRO - Webhook", version="2.1.0")

# =========================
# HELPERS
# =========================
Number = Optional[Union[float, int, str]]

def _mask(val: Optional[str]) -> str:
    if not val:
        return "missing"
    return (val[:6] + "..." + val[-4:]) if len(val) > 12 else "***"

def _fmt_num(x: Number) -> str:
    if x is None:
        return "-"
    try:
        xf = float(x)
        return f"{xf:.8f}" if xf < 1 else f"{xf:.4f}"
    except Exception:
        return str(x)

async def send_telegram(text: str) -> None:
    """Envoie un message Telegram si BOT + CHAT_ID configurés (non bloquant)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            # ne casse pas le webhook si Telegram échoue
            pass

def normalize_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accepte les 2 schémas :
    - Pine 'nouveau' : {"tag":"ENTRY","symbol","tf","time","close","direction","levels":{SL,TP1,TP2,TP3}, "trade_id","secret"}
    - Schéma 'plat'  : {"type":"ENTRY","symbol","tf","time","side","entry","sl","tp1","tp2","tp3","r1","s1","trade_id","secret"}
    Retourne un dict normalisé :
    {
      type, symbol, tf, time, side, entry, sl, tp1, tp2, tp3, r1, s1, trade_id, secret
    }
    """
    # type / tag
    t = (d.get("type") or d.get("tag") or "").upper()

    # symbol / ticker
    symbol = d.get("symbol") or d.get("ticker") or ""

    # tf
    tf = str(d.get("tf") or d.get("timeframe") or "")

    # time (int)
    _time = d.get("time")
    try:
        time_int = int(float(_time)) if _time is not None else 0
    except Exception:
        time_int = 0

    # side / direction
    side = (d.get("side") or d.get("direction") or "").upper()

    # entry / close
    entry = d.get("entry")
    if entry is None:
        entry = d.get("close")

    # niveaux : à plat OU dans levels.{}
    levels = d.get("levels") or {}
    def pick(*keys):
        for k in keys:
            if k in d and d.get(k) is not None:
                return d.get(k)
        return None

    sl  = pick("sl")
    tp1 = pick("tp1")
    tp2 = pick("tp2")
    tp3 = pick("tp3")
    r1  = pick("r1")
    s1  = pick("s1")

    # fallback depuis levels{}
    if sl  is None: sl  = levels.get("SL")
    if tp1 is None: tp1 = levels.get("TP1")
    if tp2 is None: tp2 = levels.get("TP2")
    if tp3 is None: tp3 = levels.get("TP3")
    if r1  is None: r1  = levels.get("R1")
    if s1  is None: s1  = levels.get("S1")

    trade_id = d.get("trade_id") or d.get("tradeId") or None
    secret   = d.get("secret") or None

    return {
        "type": t,
        "symbol": symbol,
        "tf": tf,
        "time": time_int,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "r1": r1,
        "s1": s1,
        "trade_id": trade_id,
        "secret": secret,
        "raw": d,  # pour debug/retour
    }

# =========================
# ROUTES — STATUS
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

# Évite le 404 favicon dans les logs
@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)

@app.get("/", response_class=HTMLResponse)
def home():
    env_rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("PORT", str(PORT)),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'><code>{v}</code></td></tr>"
        for k, v in env_rows
    )
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>AI Trader PRO — Status</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.5;margin:20px;color:#111}}
    .card{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:14px 0}}
    .btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111;margin-right:8px}}
    table{{border-collapse:collapse;width:100%;font-size:14px}}
    code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
  </style>
</head>
<body>
  <h1>AI Trader PRO — Status</h1>
  <div class="card">
    <b>Environnement</b>
    <table>{rows_html}</table>
    <div style="margin-top:10px">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
    </div>
  </div>
  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
  </div>
</body>
</html>
"""

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "PORT": PORT,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: ça fonctionne.")
    return {"ok": True, "info": "Message Telegram envoyé (si BOT + CHAT_ID configurés)."}

# =========================
# ROUTE — WEBHOOK
# =========================
@app.post("/tv-webhook")
async def tv_webhook(req: Request, x_render_signature: Optional[str] = Header(None)):
    # 1) JSON brut
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 2) Normalisation (accepte tag/close/direction/levels ou type/entry/side/…)
    norm = normalize_payload(data)

    # 3) Sécurité: secret
    if WEBHOOK_SECRET:
        if not norm["secret"] or norm["secret"] != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 4) Log
    print("[tv-webhook] normalized:", {k: v for k, v in norm.items() if k != "raw"})

    # 5) Message Telegram
    t = norm["type"]
    header_emoji = "🟩" if norm["side"] == "LONG" else ("🟥" if norm["side"] == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{norm['trade_id']}</code>" if norm["trade_id"] else ""

    if t == "ENTRY":
        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{norm['symbol']}</b> • <b>{norm['tf']}</b>{trade_id_txt}\n"
            f"Direction: <b>{(norm['side'] or '—')}</b> | Entry: <b>{_fmt_num(norm['entry'])}</b>\n"
            f"🎯 SL: <b>{_fmt_num(norm['sl'])}</b> | "
            f"TP1: <b>{_fmt_num(norm['tp1'])}</b> | "
            f"TP2: <b>{_fmt_num(norm['tp2'])}</b> | "
            f"TP3: <b>{_fmt_num(norm['tp3'])}</b>\n"
            f"R1: <b>{_fmt_num(norm['r1'])}</b>  •  S1: <b>{_fmt_num(norm['s1'])}</b>"
        )
        await send_telegram(msg)

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)
        msg = (
            f"{nice} • <b>{norm['symbol']}</b> • <b>{norm['tf']}</b>{trade_id_txt}\n"
            f"Prix: <b>{_fmt_num(norm['entry'])}</b>"
        )
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
        msg = (
            f"⏹ <b>TRADE TERMINÉ — VEUILLEZ FERMER</b>\n"
            f"Instrument: <b>{norm['symbol']}</b> • TF: <b>{norm['tf']}</b>{trade_id_txt}"
        )
        await send_telegram(msg)

    else:
        # Type inconnu -> on ne bloque pas, on log seulement
        print("[tv-webhook] type non géré:", t)

    # 6) Réponse API
    return JSONResponse(
        {
            "ok": True,
            "normalized": {k: v for k, v in norm.items() if k != "raw"},
            "received_raw": norm["raw"],
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )
