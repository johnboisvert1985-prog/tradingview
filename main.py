# main.py
from __future__ import annotations

import os
import html
from string import Template
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

# =========================
# Config / Environment
# =========================
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
LLM_ENABLED        = os.getenv("LLM_ENABLED", "0") in ("1", "true", "True")
LLM_MODEL          = os.getenv("LLM_MODEL", "gpt-4o-mini")
FORCE_LLM          = os.getenv("FORCE_LLM", "0") in ("1", "true", "True")
CONFIDENCE_MIN     = float(os.getenv("CONFIDENCE_MIN", "0.0"))
PORT               = int(os.getenv("PORT", "8000"))
RISK_ACCOUNT_BAL   = float(os.getenv("RISK_ACCOUNT_BAL", "1000"))
RISK_PCT           = float(os.getenv("RISK_PCT", "1.0"))

# (Optionnel) Client LLM si disponible
_openai_client = None
_llm_reason_down = "LLM disabled or client not initialized"

# =========================
# HTTP client (Telegram)
# =========================
# On tente httpx (async). Si indisponible, fallback standard lib.
try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

async def send_telegram(text: str) -> None:
    """Envoie un message Telegram si BOT et CHAT_ID sont configurés."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if httpx:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(url, json=payload)
            except Exception:
                pass
    else:
        # Fallback sync via stdlib, exécuté en thread pour ne pas bloquer l’event loop
        import json, urllib.request, asyncio
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        except Exception:
            pass

# =========================
# HTML Template (string.Template)
# =========================
INDEX_HTML = Template("""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>AI Trader PRO — Status</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji"; margin: 24px; }
  h1 { margin: 0 0 16px; font-size: 24px; }
  .card { border: 1px solid rgba(120,120,120,.25); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 6px 8px; border-bottom: 1px solid rgba(120,120,120,.15); vertical-align: top; }
  td.key { width: 260px; color: #888; }
  .btn { display:inline-block; padding:8px 12px; margin-right:8px; border:1px solid rgba(120,120,120,.25); border-radius:8px; text-decoration:none; }
  code { background:#f9fafb;padding:2px 4px;border-radius:6px }
</style>
</head>
<body>
  <h1>AI Trader PRO — Status</h1>

  <div class="card">
    <b>Environnement</b>
    <table>$rows_html</table>
    <div style="margin-top:10px">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
      <a class="btn" href="/openai-health">/openai-health</a>
      <a class="btn" href="/trades">/trades</a>
    </div>
  </div>

  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
  </div>
</body>
</html>
""")

# =========================
# App
# =========================
app = FastAPI(title="AI Trader PRO")

def _rows() -> list[tuple[str, str]]:
    return [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("LLM_ENABLED", str(bool(LLM_ENABLED))),
        ("LLM_CLIENT_READY", str(_openai_client is not None)),
        ("LLM_DOWN_REASON", _llm_reason_down),
        ("LLM_MODEL", LLM_MODEL if (LLM_ENABLED and _openai_client) else ""),
        ("FORCE_LLM", str(bool(FORCE_LLM))),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
        ("PORT", str(PORT)),
        ("RISK_ACCOUNT_BAL", str(RISK_ACCOUNT_BAL)),
        ("RISK_PCT", str(RISK_PCT)),
    ]

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    rows_html = "\n".join(
        f"<tr><td class='key'>{html.escape(k)}</td><td><code>{html.escape(v)}</code></td></tr>"
        for k, v in _rows()
    )
    return HTMLResponse(INDEX_HTML.substitute(rows_html=rows_html))

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "LLM_ENABLED": bool(LLM_ENABLED),
        "LLM_CLIENT_READY": bool(_openai_client is not None),
        "LLM_DOWN_REASON": _llm_reason_down,
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "FORCE_LLM": bool(FORCE_LLM),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
        "RISK_ACCOUNT_BAL": RISK_ACCOUNT_BAL,
        "RISK_PCT": RISK_PCT,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: ça fonctionne.")
    return {"ok": True, "info": "Message Telegram envoyé (si BOT + CHAT_ID configurés)."}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample[:120]}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

@app.get("/trades")
def trades(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    # TODO: renvoyer vos trades persistés
    return JSONResponse({"trades": []})

# =========================
#  Webhook TradingView
# =========================
class TVPayload(BaseModel):
    # Exemple minimal compatible avec ton Pine (type, symbol, tf, time, side, entry/sl/tp…)
    type: str = Field(..., description="ENTRY, CLOSE, TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, AOE_PREMIUM, AOE_DISCOUNT, etc.")
    symbol: Optional[str] = None
    tf: Optional[str] = None
    time: Optional[int] = None
    side: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    r1: Optional[float] = None
    s1: Optional[float] = None
    lev_reco: Optional[float] = None
    qty_reco: Optional[float] = None
    notional: Optional[float] = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None  # support secret dans le body

    @field_validator("type", mode="before")
    @classmethod
    def _type_upper(cls, v: str):
        return v.upper() if isinstance(v, str) else v

def _check_secret(header_secret: Optional[str], body_secret: Optional[str]):
    """Vérifie X-Webhook-Secret ou champ body.secret."""
    if not WEBHOOK_SECRET:
        return  # pas de protection
    sent = header_secret or body_secret
    if sent != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

def _emoji(evt_type: str) -> str:
    return {
        "ENTRY": "🚀",
        "CLOSE": "🔚",
        "TP1_HIT": "✅",
        "TP2_HIT": "✅✅",
        "TP3_HIT": "🏁",
        "SL_HIT": "⛔",
        "AOE_PREMIUM": "🟥",
        "AOE_DISCOUNT": "🟩",
    }.get(evt_type.upper(), "ℹ️")

def _fmt_float(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.6f}".rstrip("0").rstrip(".")

def _format_tv_message(p: TVPayload) -> str:
    em = _emoji(p.type)
    lines = [
        f"{em} <b>{html.escape(p.type)}</b>",
        f"• Symbol: <code>{html.escape(p.symbol or '-')}</code>",
    ]
    if p.side:
        lines.append(f"• Side: <b>{html.escape(p.side)}</b>")
    if p.tf:
        lines.append(f"• TF: <code>{html.escape(p.tf)}</code>")
    if p.entry is not None:
        lines.append(f"• Entry: <code>{_fmt_float(p.entry)}</code>")
    if p.sl is not None:
        lines.append(f"• SL: <code>{_fmt_float(p.sl)}</code>")
    if p.tp is not None:
        lines.append(f"• TP: <code>{_fmt_float(p.tp)}</code>")
    if p.tp1 is not None or p.tp2 is not None or p.tp3 is not None:
        lines.append(f"• TP1/2/3: <code>{_fmt_float(p.tp1)} / {_fmt_float(p.tp2)} / {_fmt_float(p.tp3)}</code>")
    if p.r1 is not None or p.s1 is not None:
        lines.append(f"• R1/S1: <code>{_fmt_float(p.r1)} / {_fmt_float(p.s1)}</code>")
    if p.lev_reco is not None or p.qty_reco is not None or p.notional is not None:
        lines.append(f"• Lev/Qty/Notional: <code>{_fmt_float(p.lev_reco)} / {_fmt_float(p.qty_reco)} / {_fmt_float(p.notional)}</code>")
    if p.trade_id:
        lines.append(f"• Trade ID: <code>{html.escape(p.trade_id)}</code>")
    return "\n".join(lines)

@app.post("/tv-webhook")
async def tv_webhook(
    request: Request,
    x_webhook_secret: Optional[str] = Header(None, convert_underscores=False),
):
    """
    Reçoit les webhooks TradingView.
    - Secret accepté via header `X-Webhook-Secret` OU champ JSON `secret`.
    - Payload validé via Pydantic, champs facultatifs tolérés.
    - Relais Telegram formaté.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Valide secret (header ou body)
    body_secret = data.get("secret") if isinstance(data, dict) else None
    _check_secret(x_webhook_secret, body_secret)

    # Valide schéma
    try:
        payload = TVPayload(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Bad payload: {e}")

    # (Optionnel) Filtrage côté serveur (ex: seuil de confiance)
    # if payload.type == "ENTRY" and CONFIDENCE_MIN > 0:
    #     ...

    # Compose & envoie le message Telegram
    msg = _format_tv_message(payload)
    await send_telegram(msg)

    # Réponse
    return {"ok": True, "received": payload.model_dump()}

# =========================
# Run local
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
