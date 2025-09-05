# main.py
import os
from typing import Optional, Union, Dict, Any
import json

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

# === LLM (OpenAI) ===
LLM_ENABLED = os.getenv("LLM_ENABLED", "1") not in ("0", "false", "False", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

try:
    if LLM_ENABLED and OPENAI_API_KEY:
        from openai import OpenAI
        _openai_client = OpenAI()
    else:
        _openai_client = None
except Exception:
    _openai_client = None
    LLM_ENABLED = False

# =========================
# ENV (Webhook & Telegram)
# =========================
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT               = int(os.getenv("PORT", "8000"))

# Seuil d'affichage facultatif (si tu veux filtrer les messages envoyés au Telegram)
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0"))

# =========================
# APP
# =========================
app = FastAPI(title="AI Trader PRO - Webhook", version="3.0.0")

# =========================
# MODELS
# =========================
Number = Optional[Union[float, int, str]]

class TVPayload(BaseModel):
    # Évènements possibles
    type: Optional[str] = None
    tag:  Optional[str] = None

    # Contexte trade
    symbol: str
    tf: str
    time: int
    side: Optional[str] = None

    # Prix & niveaux
    entry: Number = None        # prix touché / prix d'entrée selon l’évènement
    tp: Number = None           # niveau cible envoyé lors des TPx_HIT
    sl: Number = None
    tp1: Number = None
    tp2: Number = None
    tp3: Number = None
    r1: Number = None
    s1: Number = None

    # Admin
    trade_id: Optional[str] = None
    secret: Optional[str] = None
    term_reason: Optional[str] = None  # pour TRADE_TERMINATED

    # Champs LLM (si jamais fournis par ailleurs)
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
            # Ne bloque pas le webhook si Telegram échoue
            pass

# =========================
# LLM: prompt & appel
# =========================
def _build_llm_prompt(p: TVPayload) -> str:
    # On construit un prompt simple, robuste, en français
    body = {
        "symbol": p.symbol,
        "tf": p.tf,
        "direction_raw": (p.side or "").upper(),
        "entry": p.entry,
        "levels": {"sl": p.sl, "tp1": p.tp1, "tp2": p.tp2, "tp3": p.tp3},
        "sr": {"R1": p.r1, "S1": p.s1},
    }
    return (
        "Tu es un moteur de décision de trading.\n"
        "Retourne UNIQUEMENT un JSON valide avec les clés:\n"
        '  {"decision": "BUY|SELL|IGNORE", "confidence": 0..1, "reason": "français"}\n\n'
        f"Contexte JSON:\n{json.dumps(body, ensure_ascii=False)}\n\n"
        "Règles:\n"
        "- BUY si direction_raw == LONG et le contexte (SR, niveaux, cohérence) est favorable.\n"
        "- SELL si direction_raw == SHORT et le contexte est favorable.\n"
        "- Sinon IGNORE (doute, données incomplètes, incohérence, proximité SR défavorable, etc.).\n"
        "- Sois concis dans 'reason'. Réponse = JSON UNIQUEMENT."
    )

def _safe_json_parse(txt: str) -> Dict[str, Any]:
    try:
        return json.loads(txt)
    except Exception:
        # tente d'extraire un bloc JSON
        start = txt.find("{")
        end = txt.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(txt[start:end+1])
            except Exception:
                pass
    return {}

async def call_llm_for_entry(p: TVPayload) -> Dict[str, Any]:
    """Appelle le LLM pour un évènement ENTRY. Retourne {decision, confidence, reason} (défauts si indisponible)."""
    if not (LLM_ENABLED and _openai_client):
        return {"decision": None, "confidence": None, "reason": None, "llm_used": False}

    prompt = _build_llm_prompt(p)
    try:
        r = _openai_client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": "Tu es un moteur de décision qui NE renvoie que du JSON valide."},
                {"role": "user", "content": prompt},
            ],
            max_output_tokens=200,
        )
        # Essaye de lire le texte de sortie
        txt = getattr(r, "output_text", None)
        if not txt:
            # fallback anciens champs possibles
            try:
                txt = r.output[0].content[0].text  # type: ignore[attr-defined]
            except Exception:
                txt = str(r)

        data = _safe_json_parse((txt or "").strip())
        decision = str(data.get("decision", "")).upper() if isinstance(data.get("decision"), str) else None
        confidence = float(data.get("confidence", 0.0)) if isinstance(data.get("confidence"), (int, float, str)) else None
        reason = str(data.get("reason")) if data.get("reason") is not None else None

        # Normalisations & garde-fous
        if decision not in ("BUY", "SELL", "IGNORE"):
            decision = "IGNORE"
        if confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except Exception:
                confidence = None

        return {"decision": decision, "confidence": confidence, "reason": reason, "llm_used": True, "raw": txt}
    except Exception as e:
        return {"decision": None, "confidence": None, "reason": None, "llm_used": False, "error": str(e)}

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
        ("LLM_ENABLED", str(bool(LLM_ENABLED and _openai_client))),
        ("LLM_MODEL", LLM_MODEL if (LLM_ENABLED and _openai_client) else "-"),
        ("OPENAI_API_KEY", _mask(OPENAI_API_KEY)),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
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
      <a class="btn" href="/openai-health">/openai-health</a>
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
        "LLM_ENABLED": bool(LLM_ENABLED and _openai_client),
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
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
        return {"ok": False, "enabled": False, "why": "LLM off or API key missing"}
    try:
        r = _openai_client.responses.create(
            model=LLM_MODEL,
            input=[{"role": "user", "content": "ping"}],
            max_output_tokens=5,
        )
        txt = getattr(r, "output_text", None) or str(r)
        return {"ok": True, "model": LLM_MODEL, "sample": txt[:120]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

# =========================
# ROUTE — WEBHOOK
# =========================
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 1) Secret
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) Type d’évènement
    t = (payload.type or payload.tag or "").upper()

    # 3) Si ENTRY => on appelle le LLM (sauf si déjà fourni)
    llm_out: Dict[str, Any] = {"decision": payload.decision, "confidence": payload.confidence, "reason": payload.reason}
    if t == "ENTRY" and (payload.decision is None or payload.confidence is None or payload.reason is None):
        llm_out = await call_llm_for_entry(payload)

    # 4) Compose & envoie Telegram
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    if t == "ENTRY":
        # Ligne LLM
        llm_lines = ""
        dec = (llm_out.get("decision") or "—")
        conf_val = llm_out.get("confidence", None)
        conf = "—" if conf_val is None else f"{float(conf_val):.2f}"
        rsn = llm_out.get("reason") or "-"

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

        # envoi — si tu veux filtrer par confiance, décommente ci-dessous:
        # if (llm_out.get("decision") or "").upper() != "IGNORE" and (conf_val or 0) >= CONFIDENCE_MIN:
        #     await send_telegram(msg)
        # else:
        #     pass
        await send_telegram(msg)

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)

        # Prix touché (TradingView -> entry), fallback 'close' si jamais
        hit_price = payload.entry if payload.entry is not None else payload.dict().get("close")

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
            f"Prix touché: <b>{_fmt_num(hit_price)}</b> • Cible: <b>{_fmt_num(target_price)}</b>"
        )
        await send_telegram(msg)

    elif t == "TRADE_TERMINATED":
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

    # 5) Réponse API
    return JSONResponse(
        {
            "ok": True,
            "event": t,
            "received": payload.dict(),
            "llm": {
                "enabled": bool(LLM_ENABLED and _openai_client),
                "decision": llm_out.get("decision"),
                "confidence": llm_out.get("confidence"),
                "reason": llm_out.get("reason"),
            },
            "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )
