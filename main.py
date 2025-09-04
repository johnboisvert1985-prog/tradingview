import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from pydantic import ConfigDict
from openai import OpenAI
import httpx

# =========================
# ENV / Config
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram (si vides -> pas d'envoi)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Option: envoyer sur Telegram seulement si confiance >= ce seuil
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0"))

# Client OpenAI
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.3.0")

# =========================
# Pydantic models
# =========================
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None

class VectorStreak(BaseModel):
    f5:   Optional[int] = Field(None, alias="5")
    f15:  Optional[int] = Field(None, alias="15")
    f60:  Optional[int] = Field(None, alias="60")
    f240: Optional[int] = Field(None, alias="240")
    D:    Optional[int] = None
    model_config = ConfigDict(populate_by_name=True)

class MTFSignal(BaseModel):
    f5:   Optional[int] = Field(None, alias="5")
    f15:  Optional[int] = Field(None, alias="15")
    f60:  Optional[int] = Field(None, alias="60")
    f240: Optional[int] = Field(None, alias="240")
    D:    Optional[int] = None
    model_config = ConfigDict(populate_by_name=True)

class Features(BaseModel):
    trend: Optional[int] = None
    rejcount: Optional[int] = None
    volatility_atr: Optional[float] = None
    sr: Optional[SR] = None
    vectorStreak: Optional[VectorStreak] = None
    mtfSignal: Optional[MTFSignal] = None

class Levels(BaseModel):
    SL: Optional[float] = None
    TP1: Optional[float] = None
    TP2: Optional[float] = None
    TP3: Optional[float] = None

class TVPayload(BaseModel):
    tag: Optional[str] = None
    symbol: str
    tf: str
    time: int
    close: float               # = Entry (on masque "Close" dans le message)
    direction: str             # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None

# =========================
# Helpers
# =========================
def build_prompt(p: TVPayload) -> str:
    return f"""
Tu es un moteur de décision de trading.
Retourne UNIQUEMENT un JSON valide avec les clés: decision (BUY|SELL|IGNORE), confidence (0..1), reason (français).

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Entry (close): {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Règles:
- BUY si LONG + contexte multi-TF/volatilité/SR cohérent ; SELL si SHORT + contexte cohérent ; sinon IGNORE.
- Sois strict mais pas excessif: si les niveaux sont manquants, tu peux décider IGNORE.
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).
""".strip()

def _extract_text_from_responses(r) -> str:
    # Nouvel SDK: r.output_text (pratique). Sinon, retours bruts.
    txt = getattr(r, "output_text", None)
    if txt:
        return txt
    # Fallbacks
    try:
        return r.output[0].content[0].text
    except Exception:
        pass
    try:
        # dernier recours: str(r)
        return str(r)
    except Exception:
        return ""

async def call_llm(prompt: str) -> Dict[str, Any]:
    # IMPORTANT: avec les modèles "o" (ex: gpt-4o-mini), on utilise Responses API
    # et "max_output_tokens" (pas "max_tokens"), et on évite "temperature" si non supporté.
    r = client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": "Tu es un moteur de décision qui ne renvoie que du JSON valide."},
            {"role": "user", "content": prompt},
        ],
        max_output_tokens=200,
    )
    txt = _extract_text_from_responses(r).strip()
    try:
        data = json.loads(txt)
        if "decision" not in data:
            data["decision"] = "IGNORE"
        if "confidence" not in data:
            data["confidence"] = 0.5
        if "reason" not in data:
            data["reason"] = "no-reason"
        return data
    except Exception:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": "invalid-json-from-llm", "raw": txt}

async def send_telegram(text: str) -> None:
    """Envoie un message Telegram si BOT_TOKEN + CHAT_ID configurés."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            # ne bloque pas le webhook si Telegram échoue
            pass

def fmt_lvl(x: Optional[float]) -> str:
    if x is None:
        return "-"
    # précision plus fine pour petits prix
    try:
        return f"{x:.8f}" if x < 1 else f"{x:.4f}"
    except Exception:
        return str(x)

def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)

def _mask(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"

# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def dashboard():
    # Petit dashboard HTML
    env_rows = [
        ("OPENAI_API_KEY", _mask(OPENAI_API_KEY)),
        ("LLM_MODEL", LLM_MODEL),
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'><code>{v}</code></td></tr>"
        for k,v in env_rows
    )
    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>AI Trade Pro — Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.4;margin:20px;color:#111}}
h1{{margin:0 0 10px}}
.card{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:14px 0}}
.btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111;margin-right:8px}}
small{{color:#6b7280}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
</style>
</head>
<body>
  <h1>AI Trade Pro — Status</h1>
  <div class="card">
    <b>Environnement</b>
    <table>{rows_html}</table>
    <div style="margin-top:10px">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/openai-health">/openai-health</a>
      <a class="btn" href="/tg-health">/tg-health</a>
    </div>
    <small>Utilisez les endpoints ci-dessus (avec ?secret=... si nécessaire) pour tester.</small>
  </div>
  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
    <div>POST <code>/verdict-test</code> (JSON manuel)</div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=200)

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Sécurité simple: secret du JSON doit matcher l'env
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Appel LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message Telegram (concis)
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    entry = payload.close
    def pct(v: Optional[float]) -> str:
        try:
            if v is None:
                return "-"
            return f"{((v/entry) - 1) * 100:.2f}%"
        except Exception:
            return "-"

    header_emoji = "🟩" if payload.direction.upper() == "LONG" else "🟥"
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    tg = []
    tg.append(f"{header_emoji} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}")
    # On masque "Close", on affiche "Entry"
    tg.append(f"Direction script: <b>{payload.direction}</b> | Entry: <b>{fmt_lvl(entry)}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{float(verdict.get('confidence',0)):.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"R1={fmt_lvl(sr.R1)}  S1={fmt_lvl(sr.S1)}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(
        f"🎯 SL={fmt_lvl(levels.SL)} ({pct(levels.SL)}) | "
        f"TP1={fmt_lvl(levels.TP1)} ({pct(levels.TP1)}) | "
        f"TP2={fmt_lvl(levels.TP2)} ({pct(levels.TP2)}) | "
        f"TP3={fmt_lvl(levels.TP3)} ({pct(levels.TP3)})"
    )

    # Envoi Telegram (seuil confiance)
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0
    if verdict.get("decision") != "IGNORE" and conf >= CONFIDENCE_MIN:
        await send_telegram("\n".join(tg))

    return JSONResponse(
        {
            "decision": verdict.get("decision", "IGNORE"),
            "confidence": float(verdict.get("confidence", 0)),
            "reason": verdict.get("reason", "no-reason"),
            "received": payload.model_dump(by_alias=True),
            "sent_to_telegram": verdict.get("decision") != "IGNORE" and conf >= CONFIDENCE_MIN,
        }
    )

@app.post("/verdict-test")
async def verdict_test(payload: Dict[str, Any]):
    dummy = TVPayload(**payload)
    prompt = build_prompt(dummy)
    verdict = await call_llm(prompt)
    return verdict

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.responses.create(
            model=LLM_MODEL,
            input=[{"role": "user", "content": "ping"}],
            max_output_tokens=5,
        )
        sample = _extract_text_from_responses(r)
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)}
        )

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "OPENAI_API_KEY": _mask(OPENAI_API_KEY),
        "LLM_MODEL": LLM_MODEL,
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: ça fonctionne.")
    return {"ok": True, "info": "Message de test envoyé (si BOT + CHAT_ID configurés)."}
