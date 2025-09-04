import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
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

# Telegram (facultatif)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Seuil minimal d’envoi vers Telegram (si decision != IGNORE)
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.25"))

# Client OpenAI (lit la clé automatiquement depuis l’ENV)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")


# =========================
# Pydantic models
# =========================
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None


class VectorStreak(BaseModel):
    # Utilise des aliases pour supporter {"5": ..., "15": ...} venant de TV
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


# =========================
# Helpers
# =========================
def mask_secret(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"


def fmt_lvl(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.4f}"


def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)


def build_prompt(p: TVPayload) -> str:
    """
    Prompt pour contraindre le modèle à répondre UNIQUEMENT en JSON.
    """
    features_dump = p.features.model_dump(by_alias=True) if p.features else {}
    levels_dump = p.levels.model_dump(by_alias=True) if p.levels else {}

    return f"""
Tu es un moteur de décision de trading. 
Retourne UNIQUEMENT un JSON valide avec les clés exactes:
- decision: "BUY" | "SELL" | "IGNORE"
- confidence: nombre entre 0 et 1 (float)
- reason: courte explication en français

Contexte:
- Symbole: {p.symbol}
- Timeframe: {p.tf}
- Direction (script): {p.direction}
- Entry (prix d'entrée estimé): {p.close}
- Features JSON: {json.dumps(features_dump, ensure_ascii=False)}
- Levels JSON: {json.dumps(levels_dump, ensure_ascii=False)}

Règles:
- BUY si le signal LONG est cohérent avec le contexte multi-TF, la volatilité et les niveaux S/R.
- SELL si le signal SHORT est cohérent avec le contexte.
- IGNORE par défaut si doute, manque d'alignement, incertitude, ou niveaux défavorables.
- Sois strict mais pas extrême: si le contexte est majoritairement aligné, tu peux BUY/SELL.
- Pas de texte avant/après le JSON. Réponse = JSON UNIQUEMENT.

Exemple de format:
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()


async def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Version 100% Chat Completions (SDK openai ~1.43.x compatible).
    IMPORTANT: ne pas utiliser temperature / max_tokens (certains modèles 4o-mini les refusent).
    """
    r = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un moteur de décision et tu renvoies STRICTEMENT un JSON valide. "
                    'Format: {"decision":"BUY|SELL|IGNORE","confidence":0..1,"reason":"..."} '
                    "N'ajoute AUCUN texte avant/après le JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        # PAS de temperature / max_tokens ici
    )
    txt = (r.choices[0].message.content or "").strip()

    # Parsing JSON robuste
    try:
        data = json.loads(txt)
        data.setdefault("decision", "IGNORE")
        data.setdefault("confidence", 0.5)
        data.setdefault("reason", "no-reason")
        return data
    except Exception:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": "invalid-json-from-llm", "raw": txt}


async def send_telegram(text: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Envoie un message Telegram si BOT_TOKEN + CHAT_ID configurés.
    Retourne {"sent": bool, "error": Optional[str]}.
    """
    if not TELEGRAM_BOT_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        return {"sent": False, "error": "telegram_not_configured"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            return {"sent": True, "error": None}
        except httpx.HTTPStatusError as e:
            return {"sent": False, "error": f"{e.response.status_code}: {e.response.text}"}
        except httpx.HTTPError as e:
            return {"sent": False, "error": str(e)}


# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "OPENAI_API_KEY": mask_secret(OPENAI_API_KEY),
        "LLM_MODEL": LLM_MODEL,
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
    }


@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            # PAS de temperature ici non plus
        )
        sample = r.choices[0].message.content or "pong"
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "openai_key_mask": mask_secret(OPENAI_API_KEY)}
        )


@app.get("/tg-health")
async def tg_health(
    secret: Optional[str] = Query(None),
    text: Optional[str] = Query("Test Telegram OK"),
    chat_id: Optional[str] = Query(None),
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    res = await send_telegram(text or "Test Telegram OK", chat_id=chat_id)
    return res


@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Sécurité simple: secret dans le JSON doit matcher l'env
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Appel LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message Telegram — Masquer "Close", afficher "Entry"
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    tg = []
    tg.append(f"🚨 <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>")
    tg.append(f"Direction script: <b>{payload.direction}</b>")
    tg.append(f"🎯 Entry: <b>{payload.close:.4f}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{float(verdict.get('confidence',0)):.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"🎯 SL={fmt_lvl(levels.SL)} | TP1={fmt_lvl(levels.TP1)} | TP2={fmt_lvl(levels.TP2)} | TP3={fmt_lvl(levels.TP3)}")

    # Envoi Telegram selon seuil
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
    """
    Permet de tester la décision du LLM sans passer par TradingView.
    Body JSON = TVPayload partiel/total.
    """
    dummy = TVPayload(**payload)
    prompt = build_prompt(dummy)
    verdict = await call_llm(prompt)
    return verdict
