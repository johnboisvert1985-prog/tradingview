# main.py
import os
import json
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse

# ============ ENV ============
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

PORT = int(os.getenv("PORT", "8000"))

# ============ APP ============
app = FastAPI(title="AI Trader PRO - Webhook+LLM", version="3.0.0")

# Petit store mémoire par trade_id
TRADES: Dict[str, Dict[str, Any]] = {}

# ============ HELPERS ============
def _mask(val: Optional[str]) -> str:
    if not val:
        return "missing"
    return (val[:6] + "..." + val[-4:]) if len(val) > 12 else "***"

def _fmt_num(x) -> str:
    if x is None:
        return "-"
    try:
        xf = float(x)
        return f"{xf:.8f}" if xf < 1 else f"{xf:.4f}"
    except Exception:
        return str(x)

async def send_telegram(text: str) -> None:
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

def build_prompt(p: Dict[str, Any]) -> str:
    """
    Construit le prompt LLM à partir du JSON TradingView (ENTRY).
    On passe symbol, tf, side, entry, sl/tp, S/R si dispo.
    """
    symbol = p.get("symbol")
    tf = p.get("tf")
    side = p.get("side")
    entry = p.get("entry")
    sl, tp1, tp2, tp3 = p.get("sl"), p.get("tp1"), p.get("tp2"), p.get("tp3")
    r1, s1 = p.get("r1"), p.get("s1")

    return f"""
Tu es un moteur de décision de trading. Réponds UNIQUEMENT avec un JSON valide:
{{"decision":"BUY|SELL|IGNORE","confidence":0..1,"reason":"..."}} en français.

Contexte:
- Symbole: {symbol}
- TF: {tf}
- Direction signal brut: {side}
- Entry: {entry}
- SL: {sl} | TP1: {tp1} | TP2: {tp2} | TP3: {tp3}
- R1: {r1} | S1: {s1}

Règles:
- Si side=LONG et le contexte paraît cohérent (niveaux, R/S, ratio, etc.) => decision=BUY.
- Si side=SHORT cohérent => decision=SELL.
- Sinon => decision=IGNORE.
- Sois concis et strict. Réponse = JSON pur.
""".strip()

async def call_llm_for_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Appel OpenAI pour obtenir decision/confidence/reason.
    Si pas de clé, on renvoie un stub (IGNORE).
    """
    if not OPENAI_API_KEY:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": "LLM off (no OPENAI_API_KEY)"}
    try:
        # Appel minimal Responses API (SDK simple via HTTPX)
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": LLM_MODEL,
            "input": [
                {"role": "system", "content": "Tu es un moteur de décision qui ne renvoie que du JSON valide."},
                {"role": "user", "content": build_prompt(payload)},
            ],
            "max_output_tokens": 200,
        }
        async with httpx.AsyncClient(timeout=20.0) as http:
            r = await http.post("https://api.openai.com/v1/responses", headers=headers, json=body)
            r.raise_for_status()
            data = r.json()

        # Extraction robuste du texte
        txt = None
        if isinstance(data, dict):
            # tentatives d'extraction
            txt = (
                data.get("output_text")
                or (data.get("output", [{}])[0].get("content", [{}])[0].get("text") if data.get("output") else None)
                or json.dumps(data)  # fallback
            )
        if not txt:
            txt = json.dumps(data)

        txt = txt.strip()
        try:
            res = json.loads(txt)
        except Exception:
            # si pas JSON pur, essaie de récupérer la partie JSON
            start = txt.find("{")
            end = txt.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    res = json.loads(txt[start:end+1])
                except Exception:
                    res = {"decision": "IGNORE", "confidence": 0.0, "reason": "invalid-json-from-llm", "raw": txt}
            else:
                res = {"decision": "IGNORE", "confidence": 0.0, "reason": "invalid-json-from-llm", "raw": txt}

        # normalisation
        if "decision" not in res: res["decision"] = "IGNORE"
        if "confidence" not in res: res["confidence"] = 0.0
        if "reason" not in res: res["reason"] = "no-reason"
        return res
    except Exception as e:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": f"llm-error: {e.__class__.__name__}"}

# ============ ROUTES: STATUS ============
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home():
    env_rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("OPENAI_API_KEY_set", str(bool(OPENAI_API_KEY))),
        ("LLM_MODEL", LLM_MODEL),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
        ("PORT", str(PORT)),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'><code>{v}</code></td></tr>"
        for k, v in env_rows
    )
    return HTMLResponse(f"""
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
""")

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "OPENAI_API_KEY_set": bool(OPENAI_API_KEY),
        "LLM_MODEL": LLM_MODEL,
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
async def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not OPENAI_API_KEY:
        return JSONResponse(status_code=400, content={"ok": False, "error": "OPENAI_API_KEY missing"})
    test_payload = {"symbol":"TESTUSDT","tf":"15","side":"LONG","entry":1.23}
    res = await call_llm_for_entry(test_payload)
    return {"ok": True, "model": LLM_MODEL, "sample": res}

# ============ ROUTE: WEBHOOK ============
@app.post("/tv-webhook")
async def tv_webhook(req: Request, x_render_signature: Optional[str] = Header(None)):
    """
    Accepte un JSON venant de Pine. Compatible avec:
      - { "type": "ENTRY" ... }  (recommandé)
      - { "tag":  "ENTRY" ... }  (legacy)
    """
    data = await req.json()

    # Sécurité secret
    if WEBHOOK_SECRET:
        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Normalisation type
    t = (data.get("type") or data.get("tag") or "").upper()
    if not t:
        raise HTTPException(status_code=422, detail="Missing type/tag")

    trade_id = str(data.get("trade_id") or "")
    symbol = data.get("symbol")
    tf = data.get("tf")
    side = (data.get("side") or "").upper()
    entry = data.get("entry")
    sl, tp1, tp2, tp3 = data.get("sl"), data.get("tp1"), data.get("tp2"), data.get("tp3")
    r1, s1 = data.get("r1"), data.get("s1")

    # Log basique
    print("[tv-webhook]", t, symbol, tf, "id:", trade_id)

    # ====== ROUTAGE ======
    if t == "ENTRY":
        # Enregistre/écrase l'état du trade
        if trade_id:
            TRADES[trade_id] = {
                "symbol": symbol, "tf": tf, "side": side,
                "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "r1": r1, "s1": s1, "last_event": "ENTRY"
            }

        # LLM verdict (optionnel)
        verdict = await call_llm_for_entry(data)  # decision, confidence, reason
        decision = verdict.get("decision", "IGNORE")
        try:
            confidence = float(verdict.get("confidence", 0))
        except Exception:
            confidence = 0.0
        reason = verdict.get("reason", "-")

        # Telegram: affiche Décision + Confiance + Raison
        header_emoji = "🟩" if side == "LONG" else ("🟥" if side == "SHORT" else "▫️")
        trade_id_txt = f" • ID: <code>{trade_id}</code>" if trade_id else ""
        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{symbol}</b> • <b>{tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{side or '—'}</b> | Entry: <b>{_fmt_num(entry)}</b>\n"
            f"🤖 LLM: <b>{decision}</b>  | Confiance: <b>{confidence:.2f}</b>\n"
            f"📝 Raison: {reason}\n"
            f"🎯 SL: <b>{_fmt_num(sl)}</b> | TP1: <b>{_fmt_num(tp1)}</b> | TP2: <b>{_fmt_num(tp2)}</b> | TP3: <b>{_fmt_num(tp3)}</b>\n"
            f"R1: <b>{_fmt_num(r1)}</b>  •  S1: <b>{_fmt_num(s1)}</b>"
        )
        # Envoi Telegram (tu peux filtrer par CONFIDENCE_MIN si tu veux)
        if confidence >= CONFIDENCE_MIN or not OPENAI_API_KEY:
            await send_telegram(msg)

        return JSONResponse({"ok": True, "decision": decision, "confidence": confidence, "reason": reason})

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        if trade_id and trade_id in TRADES:
            TRADES[trade_id]["last_event"] = t
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)
        msg = (
            f"{nice} • <b>{symbol}</b> • <b>{tf}</b>{(' • ID: <code>'+trade_id+'</code>') if trade_id else ''}\n"
            f"Prix: <b>{_fmt_num(entry)}</b>"
        )
        await send_telegram(msg)
        return {"ok": True}

    elif t == "TRADE_TERMINATED":
        # essaie d'expliquer pourquoi
        cause = (data.get("cause") or "").upper()  # si Pine l'envoie
        last = TRADES.get(trade_id, {}).get("last_event")

        text_reason = None
        if cause in ("TP3_HIT", "SL_HIT"):
            last = cause  # force la cause fournie
        if last == "TP3_HIT":
            text_reason = "TP3 ATTEINT ✅"
        elif last == "SL_HIT":
            text_reason = "SL TOUCHÉ ❌"
        else:
            text_reason = "REVERSAL DÉTECTÉ — TRADE INVALIDÉ, VEUILLEZ FERMER !"

        msg = (
            "⏹ <b>TRADE TERMINÉ — VEUILLEZ FERMER</b>\n"
            f"Instrument: <b>{symbol}</b> • TF: <b>{tf}</b>{(' • ID: <code>'+trade_id+'</code>') if trade_id else ''}\n"
            f"Motif: <b>{text_reason}</b>"
        )
        await send_telegram(msg)

        # Nettoyage
        if trade_id in TRADES:
            del TRADES[trade_id]

        return {"ok": True, "reason": text_reason}

    else:
        # Inconnu: on log et OK
        print("[tv-webhook] type/tag inconnu:", t)
        return {"ok": True, "info": f"ignored type {t}"}
