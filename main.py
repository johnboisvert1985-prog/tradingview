# main.py
import os
import json
from typing import Optional, Union, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

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
app = FastAPI(title="AI Trader PRO - Webhook", version="2.0.0")

# =========================
# MODELS
# =========================
Number = Optional[Union[float, int, str]]

class TVPayload(BaseModel):
    """
    Modèle aligné avec l’indicateur Pine:
    {
      "type": "ENTRY" | "TP1_HIT" | "TP2_HIT" | "TP3_HIT" | "SL_HIT" | "TRADE_TERMINATED",
      "symbol": "BTCUSDT",
      "tf": "15",
      "time": 1717777777,
      "side": "LONG" | "SHORT",         # seulement pour ENTRY
      "entry": 67000.12,                # prix d'insert (ENTRY) ou dernier prix (events)
      "sl": 64000.0,
      "tp1": 68000.0,
      "tp2": 69000.0,
      "tp3": 70000.0,
      "r1": 70500.0,
      "s1": 66500.0,
      "secret": "....",
      "trade_id": "abc-123"
    }
    """
    type: str
    symbol: str
    tf: str
    time: int
    side: Optional[str] = None
    entry: Number = None
    sl: Number = None
    tp1: Number = None
    tp2: Number = None
    tp3: Number = None
    r1: Number = None
    s1: Number = None
    secret: Optional[str] = None
    trade_id: Optional[str] = None

    class Config:
        extra = "allow"  # tolère des champs en plus au cas où

# =========================
# HELPERS
# =========================
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
            # on ne casse pas le webhook si Telegram échoue
            pass

# =========================
# ROUTES — STATUS
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

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
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 1) Sécurité: secret
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) Log simple
    print("[tv-webhook] payload:", payload.dict())

    # 3) Compose message Telegram
    #   - ENTRY => message complet avec niveaux
    #   - TP*/SL_HIT => évènement
    #   - TRADE_TERMINATED => message de clôture
    t = payload.type.upper()

    # Petit header visuel
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    if t == "ENTRY":
        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{(payload.side or '—').upper()}</b> | Entry: <b>{_fmt_num(payload.entry)}</b>\n"
            f"🎯 SL: <b>{_fmt_num(payload.sl)}</b> | "
            f"TP1: <b>{_fmt_num(payload.tp1)}</b> | "
            f"TP2: <b>{_fmt_num(payload.tp2)}</b> | "
            f"TP3: <b>{_fmt_num(payload.tp3)}</b>\n"
            f"R1: <b>{_fmt_num(payload.r1)}</b>  •  S1: <b>{_fmt_num(payload.s1)}</b>"
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
            f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Prix: <b>{_fmt_num(payload.entry)}</b>"
        )
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
        # message demandé: "TRADE TERMINER VEUILLEZ FERMER ..."
        msg = (
            f"⏹ <b>TRADE TERMINÉ — VEUILLEZ FERMER</b>\n"
            f"Instrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{trade_id_txt}"
        )
        await send_telegram(msg)

    else:
        # Type inconnu -> on ne bloque pas, on log seulement
        print("[tv-webhook] type non géré:", t)

    # 4) Réponse API
    return JSONResponse(
        {
            "ok": True,
            "received": payload.dict(),
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )
