import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
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
    # Mappe les clés numériques vers des champs valides
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
SYSTEM: Tu es un moteur de décision de trading. Tu renvoies STRICTEMENT un JSON valide.
Le JSON doit contenir:
- "decision": "BUY" | "SELL" | "IGNORE"
- "confidence": nombre entre 0 et 1
- "reason": explication courte en français

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Close: {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Règles:
- BUY si LONG + contexte multi-TF/volatilité/SR cohérent ; SELL si SHORT + contexte cohérent ; sinon IGNORE.
- IGNORE par défaut en cas de doute. Préfère la qualité à la quantité.
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).

Exemple de format:
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()

# --- à coller dans main.py, en remplacement intégral des fonctions existantes ---

def parse_response_json_text(resp_obj) -> str:
    """
    Utilisé si jamais le SDK supporte Responses API. Sinon renvoie "".
    """
    try:
        return resp_obj.output_text
    except Exception:
        try:
            return resp_obj.output[0].content[0].text
        except Exception:
            return ""

async def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Essaie Responses API si disponible (SDK récent),
    sinon fallback Chat Completions (SDK plus ancien).
    """
    txt = ""
    try:
        # Tentative Responses API (peut lever AttributeError si non supportée)
        create_resp = getattr(client, "responses").create  # type: ignore[attr-defined]
        r = create_resp(
            model=LLM_MODEL,
            input=prompt,
            temperature=0.2,
            # pas de max_tokens ici pour compatibilité multi-modèles
            response_format={"type": "json_object"},
        )
        txt = parse_response_json_text(r).strip()
    except Exception:
        # Fallback Chat Completions (NE PAS mettre max_tokens avec gpt-4o-mini)
        r = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un moteur de décision et tu renvoies STRICTEMENT un JSON valide. "
                        "Format attendu: {\"decision\":\"BUY|SELL|IGNORE\",\"confidence\":0..1,\"reason\":\"...\"} "
                        "N'ajoute aucun texte en dehors du JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        txt = r.choices[0].message.content.strip()

    # Parsing JSON robuste
    try:
        data = json.loads(txt)
        data.setdefault("decision", "IGNORE")
        data.setdefault("confidence", 0.5)
        data.setdefault("reason", "no-reason")
        return data
    except Exception:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": "invalid-json-from-llm", "raw": txt}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    """
    Teste OpenAI sans supposer la présence de Responses API.
    """
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        if hasattr(client, "responses"):
            r = client.responses.create(model=LLM_MODEL, input="ping")
            sample = parse_response_json_text(r) or "pong"
        else:
            r = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": "ping"}],
                temperature=0
            )
            sample = r.choices[0].message.content or "pong"

        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)}
        )


async def send_telegram(text: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
    """Envoie un message Telegram si BOT_TOKEN + CHAT_ID configurés. Retourne un dict de debug."""
    if not TELEGRAM_BOT_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        return {"sent": False, "error": "telegram_not_configured"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            if r.status_code != 200:
                return {"sent": False, "error": f"{r.status_code}:{r.text}"}
            return {"sent": True, "error": None}
        except httpx.HTTPError as e:
            return {"sent": False, "error": str(e)}

def fmt_lvl(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.4f}"

def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)

def fmt_rr(v: Optional[float]) -> str:
    return "-" if v is None or v != v else f"{v:.2f}"

# ---------------------------
# Routes
# ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/tg-health")
async def tg_health(
    secret: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    text: Optional[str] = Query("Bot OK ✅")
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    res = await send_telegram(text, chat_id=chat_id)
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

    # Prépare message Telegram (on masque "Close" ; Entry = prix au signal)
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    entry = float(payload.close)  # prix au moment du signal

    # RR (optionnel)
    rr1 = rr2 = rr3 = None
    if levels.SL is not None and levels.TP1 is not None:
        SL  = float(levels.SL)
        TP1 = float(levels.TP1)
        TP2 = float(levels.TP2) if levels.TP2 is not None else None
        TP3 = float(levels.TP3) if levels.TP3 is not None else None

        if payload.direction.upper() == "LONG" and entry > SL:
            risk = entry - SL
            rr1 = (TP1 - entry) / risk if TP1 > entry else None
            rr2 = (TP2 - entry) / risk if (TP2 is not None and TP2 > entry) else None
            rr3 = (TP3 - entry) / risk if (TP3 is not None and TP3 > entry) else None
        elif payload.direction.upper() == "SHORT" and SL > entry:
            risk = SL - entry
            rr1 = (entry - TP1) / risk if TP1 < entry else None
            rr2 = (entry - TP2) / risk if (TP2 is not None and TP2 < entry) else None
            rr3 = (entry - TP3) / risk if (TP3 is not None and TP3 < entry) else None

    tg = []
    tg.append(f"🚨 <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>")
    tg.append(f"Direction script: <b>{payload.direction}</b>")
    tg.append(f"📍 Entry (prix au signal): <b>{entry:.4f}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{float(verdict.get('confidence',0)):.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"🎯 SL={fmt_lvl(levels.SL)} | TP1={fmt_lvl(levels.TP1)} | TP2={fmt_lvl(levels.TP2)} | TP3={fmt_lvl(levels.TP3)}")
    tg.append(f"📐 RR: TP1={fmt_rr(rr1)} | TP2={fmt_rr(rr2)} | TP3={fmt_rr(rr3)}")
    # Option: lien TradingView si tu connais l’exchange
    # tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{payload.symbol}"
    # tg.append(f'<a href="{tv_url}">📈 Ouvrir dans TradingView</a>')

    # Envoi Telegram (option seuil de confiance)
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0

    tg_result = {"sent": False, "error": None}
    if verdict.get("decision") != "IGNORE" and conf >= CONFIDENCE_MIN:
        tg_result = await send_telegram("\n".join(tg))

    return JSONResponse(
        {
            "decision": verdict.get("decision", "IGNORE"),
            "confidence": float(verdict.get("confidence", 0)),
            "reason": verdict.get("reason", "no-reason"),
            "telegram": tg_result,
            "received": payload.model_dump(by_alias=True),
        }
    )

def _mask(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.responses.create(
            model=LLM_MODEL,
            input="Dis juste un mot: ping",
            max_output_tokens=5,
        )
        return {"ok": True, "model": LLM_MODEL, "sample": parse_response_json_text(r) or "pong"}
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

