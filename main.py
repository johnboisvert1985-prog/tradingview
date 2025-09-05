# main.py
import os
from typing import Optional, Union
import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

# =========================
# ENV
# =========================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # ex: nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "8000"))

# =========================
# APP
# =========================
app = FastAPI(title="AI Trader PRO - Webhook", version="2.2.0")

# =========================
# MODELS
# =========================
Number = Optional[Union[float, int, str]]

class TVPayload(BaseModel):
    """
    Modèle aligné avec l’indicateur Pine patché.
    Champs possibles envoyés par le Pine:

    type: "ENTRY" | "TP1_HIT" | "TP2_HIT" | "TP3_HIT" | "SL_HIT" | "TRADE_TERMINATED"
    (compat) tag:  même valeur que type, pour compatibilité si le Pine envoie 'tag'

    symbol: "BTCUSDT"
    tf: "15"
    time: 1717777777
    side: "LONG" | "SHORT"   (ENTRY/terminé)
    entry: prix touché / prix d’entrée selon l’évènement
    tp: valeur cible (quand TPx_HIT, le niveau exact TPx)
    sl, tp1, tp2, tp3, r1, s1: niveaux
    trade_id: identifiant
    secret: doit matcher WEBHOOK_SECRET
    term_reason: "REVERSAL" | "TP3_HIT" | "SL_HIT" | ...  (pour TRADE_TERMINATED)

    (optionnel si vous réactivez le LLM côté backend):
    decision: "BUY" | "SELL" | "IGNORE"
    confidence: float 0..1
    reason: str (explication)
    """
    type: Optional[str] = None
    tag:  Optional[str] = None
    symbol: str
    tf: str
    time: int
    side: Optional[str] = None

    entry: Number = None
    tp: Number = None

    sl: Number = None
    tp1: Number = None
    tp2: Number = None
    tp3: Number = None
    r1: Number = None
    s1: Number = None

    trade_id: Optional[str] = None
    secret: Optional[str] = None
    term_reason: Optional[str] = None

    # Champs LLM facultatifs (si fournis on les affiche)
    decision: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None

    class Config:
        extra = "allow"

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
            # On ne casse pas le webhook si Telegram échoue
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
    # 1) Secret
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) Log
    print("[tv-webhook] payload:", payload.dict())

    # 3) Type d’évènement (accepte 'type' OU 'tag')
    t = (payload.type or payload.tag or "").upper()

    # 4) Format Telegram
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    if t == "ENTRY":
        # Ligne LLM si fournie
        llm_lines = ""
        if payload.decision or payload.confidence is not None or payload.reason:
            dec = (payload.decision or "—").upper()
            conf = "—" if payload.confidence is None else f"{float(payload.confidence):.2f}"
            rsn = payload.reason or "-"
            llm_lines = f"\n🤖 LLM: <b>{dec}</b>  | Confiance: <b>{conf}</b>\n📝 Raison: {rsn}"

        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{(payload.side or '—').upper()}</b> | Entry: <b>{_fmt_num(payload.entry)}</b>\n"
            f"🎯 SL: <b>{_fmt_num(payload.sl)}</b> | "
            f"TP1: <b>{_fmt_num(payload.tp1)}</b> | "
            f"TP2: <b>{_fmt_num(payload.tp2)}</b> | "
            f"TP3: <b>{_fmt_num(payload.tp3)}</b>\n"
            f"R1: <b>{_fmt_num(payload.r1)}</b>  •  S1: <b>{_fmt_num(payload.s1)}</b>"
            f"{llm_lines}"
        )
        await send_telegram(msg)

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)

        # Prix touché (le Pine l’envoie dans 'entry'; fallback 'close' si jamais)
        hit_price = payload.entry
        if hit_price is None:
            hit_price = payload.dict().get("close")

        # Cible exacte (tp/sl)
        target_price = payload.tp
        if target_price is None:
            if t == "TP1_HIT":
                target_price = payload.tp1
            elif t == "TP2_HIT":
                target_price = payload.tp2
            elif t == "TP3_HIT":
                target_price = payload.tp3
            elif t == "SL_HIT":
                target_price = payload.sl

        msg = (
            f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Prix touché: <b>{_fmt_num(hit_price)}</b> • "
            f"Cible: <b>{_fmt_num(target_price)}</b>"
        )
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
        # Raison lisible
        reason = (payload.term_reason or "").upper()
        if reason == "TP3_HIT":
            title = "TRADE TERMINÉ — TP3 ATTEINT"
        elif reason in ("REVERSAL", "INVALIDATED"):
            title = "TRADE INVALIDÉ — VEUILLEZ FERMER!"
        elif reason == "SL_HIT":
            title = "TRADE TERMINÉ — SL ATTEINT"
        else:
            title = "TRADE TERMINÉ — VEUILLEZ FERMER"

        msg = (
            f"⏹ <b>{title}</b>\n"
            f"Instrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{trade_id_txt}"
        )
        await send_telegram(msg)

    else:
        print("[tv-webhook] type non géré:", t)

    # 5) Réponse
    return JSONResponse(
        {
            "ok": True,
            "event": t,
            "received": payload.dict(),
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )
