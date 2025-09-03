import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic import ConfigDict
from openai import OpenAI
import httpx

# ---------------------------
# ENV / Config
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram (facultatif; si vides -> pas d'envoi)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Option: ne forward sur Telegram que si confiance >= CONFIDENCE_MIN
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0"))

# Client OpenAI (lit la clé dans l'env)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

# ---------------------------
# Pydantic models
# ---------------------------
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None

class VectorStreak(BaseModel):
    # Pydantic v2: pas de nom commençant par "_"
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
    close: float
    direction: str  # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    secret: Optional[str] = None

# ---------------------------
# Helpers
# ---------------------------
def build_prompt(p: TVPayload) -> str:
    return f"""
Tu es un moteur de décision de trading. 
Retourne UNIQUEMENT un JSON valide avec les clés: decision (BUY|SELL|IGNORE), confidence (0..1), reason (français).

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Close: {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Règles:
- BUY si LONG + contexte multi-TF/volatilité/sr OK ; SELL si SHORT + contexte cohérent ; sinon IGNORE.
- Sois strict: évite les faux signaux (IGNORE par défaut si doute).
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).

Exemple de format:
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()

async def call_llm(prompt: str) -> Dict[str, Any]:
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "Tu es un moteur de décision qui ne renvoie que du JSON valide."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=200,
    )
    txt = resp.choices[0].message.content.strip()
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
    return "-" if x is None else f"{x:.4f}"

def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)

# ---------------------------
# Routes
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Sécurité simple: secret dans le JSON doit matcher l'env
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # (Exemples de hard-rules en amont si tu veux)
    # f = payload.features or Features()
    # levels = payload.levels or Levels()
    # if not levels.SL or not levels.TP1:
    #     return JSONResponse({"decision":"IGNORE","confidence":0.0,"reason":"levels incomplets","received":payload.model_dump(by_alias=True)})

    # Appel LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message Telegram (concis)
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    tg = []
    tg.append(f"📈 <b>{payload.symbol}</b>  ⏱ TF <b>{payload.tf}</b>")
    tg.append(f"Signal: <b>{payload.direction}</b>  | Close: <b>{payload.close:.4f}</b>")
    tg.append(f"LLM: <b>{verdict.get('decision','?')}</b>  (conf. {float(verdict.get('confidence',0)):.2f})")
    tg.append(f"Raison: {verdict.get('reason','-')}")
    tg.append(f"Trend={f.trend if f.trend is not None else '-'}  Rej={f.rejcount if f.rejcount is not None else '-'}  ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"R1={fmt_lvl(sr.R1)}  S1={fmt_lvl(sr.S1)}")
    tg.append(f"VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"SL={fmt_lvl(levels.SL)}  TP1={fmt_lvl(levels.TP1)}  TP2={fmt_lvl(levels.TP2)}  TP3={fmt_lvl(levels.TP3)}")

    # Envoi Telegram (option seuil de confiance)
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
        }
    )

@app.post("/verdict-test")
async def verdict_test(payload: Dict[str, Any]):
    dummy = TVPayload(**payload)
    prompt = build_prompt(dummy)
    verdict = await call_llm(prompt)
    return verdict
