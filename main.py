import os
import json
from time import time
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

# Filtres anti-bruit (tunable via ENV)
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.70"))   # seuil LLM pour envoyer Telegram
MIN_CONFLUENCE = int(os.getenv("MIN_CONFLUENCE", "2"))        # 0..4 requis
NEAR_SR_ATR    = float(os.getenv("NEAR_SR_ATR", "0.50"))      # veto si S/R adverse à <= k*ATR
RR_MIN         = float(os.getenv("RR_MIN", "1.00"))           # TP1/risk >= RR_MIN
COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SEC", "900"))        # anti-spam (15 min)

# Client OpenAI (lit la clé dans l'env)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

# Mémoire en RAM pour anti-spam (par symbole/TF/direction)
last_fire: Dict[str, int] = {}

# ---------------------------
# Pydantic models
# ---------------------------
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
    close: float
    direction: str  # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    secret: Optional[str] = None

# ---------------------------
# Helpers
# ---------------------------
def dir_to_int(direction: str) -> int:
    d = (direction or "").upper()
    return 1 if d == "LONG" else -1 if d == "SHORT" else 0

def rr_ok(levels: Levels, direction: str, close: float, rr_min: float) -> bool:
    if levels is None or levels.SL is None or levels.TP1 is None:
        return False
    risk = abs(close - levels.SL)
    reward = abs(levels.TP1 - close)
    if risk <= 0:
        return False
    return (reward / risk) >= rr_min

def near_adverse_sr(f: Features, direction: str, close: float, atr: Optional[float], k: float) -> bool:
    """Veto si proche d’un S/R défavorable à moins de k*ATR."""
    if not f or not f.sr or atr is None or atr <= 0:
        return False
    if (direction or "").upper() == "LONG" and f.sr.R1 is not None:
        return 0 <= (f.sr.R1 - close) <= k * atr
    if (direction or "").upper() == "SHORT" and f.sr.S1 is not None:
        return 0 <= (close - f.sr.S1) <= k * atr
    return False

def confluence_score(f: Features, direction: str) -> int:
    """Score 0..4 : Trend aligné, ≥2 MTF alignés, VectorStreak court terme aligné, +1 si rejcount≥2."""
    if not f:
        return 0
    s = 0
    d = dir_to_int(direction)

    # Trend
    if f.trend is not None and ((f.trend > 0) == (d > 0)):
        s += 1

    # MTF alignement (compte des TF alignées parmi 15/60/240/D)
    if f.mtfSignal:
        align = 0
        for val in [f.mtfSignal.f15, f.mtfSignal.f60, f.mtfSignal.f240, f.mtfSignal.D]:
            if val is None:
                continue
            if (val > 0) == (d > 0):
                align += 1
        if align >= 2:
            s += 1

    # Vector streak (5m ou 15m)
    if f.vectorStreak:
        vs = f.vectorStreak
        if any([(v is not None and (v > 0) == (d > 0)) for v in [vs.f5, vs.f15]]):
            s += 1

    # Bonus : rejcount significatif
    if f.rejcount is not None and f.rejcount >= 2:
        s += 1

    return s

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

def _mask(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"

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

    # --- Anti-bruit & pré-filtres ---
    key = f"{payload.symbol}:{payload.tf}:{payload.direction}"
    now_ms = payload.time or int(time() * 1000)

    f = payload.features or Features()
    levels = payload.levels or Levels()

    # Cooldown (anti-spam)
    last_ts = last_fire.get(key, 0)
    if COOLDOWN_SEC > 0 and (now_ms - last_ts) < COOLDOWN_SEC * 1000:
        return JSONResponse({
            "decision": "IGNORE", "confidence": 0.0,
            "reason": f"cooldown {COOLDOWN_SEC}s",
            "received": payload.model_dump(by_alias=True)
        })

    # Veto S/R adverse proche
    if near_adverse_sr(f, payload.direction, payload.close, f.volatility_atr, NEAR_SR_ATR):
        return JSONResponse({
            "decision": "IGNORE", "confidence": 0.0,
            "reason": f"near adverse S/R (≤ {NEAR_SR_ATR}×ATR)",
            "received": payload.model_dump(by_alias=True)
        })

    # Veto RR min
    if not rr_ok(levels, payload.direction, payload.close, RR_MIN):
        return JSONResponse({
            "decision": "IGNORE", "confidence": 0.0,
            "reason": f"RR to TP1 < {RR_MIN}",
            "received": payload.model_dump(by_alias=True)
        })

    # Confluence minimale
    score = confluence_score(f, payload.direction)
    if score < MIN_CONFLUENCE:
        return JSONResponse({
            "decision": "IGNORE", "confidence": 0.0,
            "reason": f"low confluence ({score} < {MIN_CONFLUENCE})",
            "received": payload.model_dump(by_alias=True)
        })

    # Appel LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message Telegram (concis)
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
    tg.append(f"Confluence={score}  RR≥{RR_MIN}  Cooldown={COOLDOWN_SEC}s")

    # Envoi Telegram (option seuil de confiance)
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0
    if verdict.get("decision") != "IGNORE" and conf >= CONFIDENCE_MIN:
        await send_telegram("\n".join(tg))
        last_fire[key] = now_ms  # mémorise le dernier envoi

    return JSONResponse(
        {
            "decision": verdict.get("decision", "IGNORE"),
            "confidence": float(verdict.get("confidence", 0)),
            "reason": verdict.get("reason", "no-reason"),
            "received": payload.model_dump(by_alias=True),
            "filters": {
                "confluence": score,
                "rr_min": RR_MIN,
                "near_sr_atr": NEAR_SR_ATR,
                "cooldown_sec": COOLDOWN_SEC,
            }
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
    # Protégé par le même secret que le webhook
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        r = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            temperature=0
        )
        return {
            "ok": True,
            "model": LLM_MODEL,
            "sample": r.choices[0].message.content
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(e),
                "openai_key_mask": _mask(OPENAI_API_KEY)
            }
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
        "MIN_CONFLUENCE": MIN_CONFLUENCE,
        "NEAR_SR_ATR": NEAR_SR_ATR,
        "RR_MIN": RR_MIN,
        "COOLDOWN_SEC": COOLDOWN_SEC,
    }

@app.get("/tg-health")
async def tg_health(
    secret: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None, description="Override du chat_id pour le test"),
    text: Optional[str] = Query("✅ Test Telegram depuis le serveur")
):
    # Protège l'endpoint
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    if not TELEGRAM_BOT_TOKEN:
        return JSONResponse(status_code=500, content={"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"})

    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        return JSONResponse(status_code=400, content={"ok": False, "error": "TELEGRAM_CHAT_ID missing (et pas d'override chat_id)"})

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text, "disable_web_page_preview": True}

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            data = r.json()
            if r.status_code == 200 and data.get("ok"):
                return {"ok": True, "target": target, "telegram": data}
            else:
                return JSONResponse(status_code=502, content={"ok": False, "target": target, "telegram": data})
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"ok": False, "target": target, "error": str(e)})
