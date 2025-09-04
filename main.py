import os
import time
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from openai import OpenAI
import httpx

# =========================
# ENV & Config
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

# LLM raisonné + JSON strict
LLM_MODEL = os.getenv("LLM_MODEL", "o3-mini")
LLM_REASONING = os.getenv("LLM_REASONING", "medium")  # low | medium | high

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Filtres serveur (qualité > quantité)
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.82"))
MIN_CONFLUENCE = int(os.getenv("MIN_CONFLUENCE", "3"))
NEAR_SR_ATR = float(os.getenv("NEAR_SR_ATR", "0.8"))   # distance mini au S/R adverse en x ATR
RR_MIN = float(os.getenv("RR_MIN", "1.3"))              # TP1/risk >= RR_MIN
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "1800"))   # 30 min par (symbol,tf,direction)

# OpenAI client (lit la clé dans l'env)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

# =========================
# Pydantic models
# =========================
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None

class VectorStreak(BaseModel):
    # Pydantic v2: pas de champs commençant par "_"
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
    trend: Optional[int] = None            # 1 / -1
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
# Utils
# =========================
def _mask(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"

def fmt_lvl(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.4f}"

def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)

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
- BUY si LONG + contexte multi-TF/volatilité/SR OK ; SELL si SHORT + contexte cohérent ; sinon IGNORE.
- Sois strict: évite les faux signaux (IGNORE par défaut si doute).
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).
""".strip()

# Cooldown mem: (symbol, tf, direction) -> last_ts
_COOLDOWN: dict[tuple[str, str, str], float] = {}

def _cooldown_ok(symbol: str, tf: str, direction: str) -> tuple[bool, Optional[str]]:
    k = (symbol, tf, direction)
    now = time.time()
    last = _COOLDOWN.get(k)
    if last and now - last < COOLDOWN_SEC:
        remain = int(COOLDOWN_SEC - (now - last))
        return False, f"cooldown {remain}s"
    _COOLDOWN[k] = now
    return True, None

def _rr_ok(close: float, sl: Optional[float], tp1: Optional[float]) -> tuple[bool, Optional[str]]:
    if sl is None or tp1 is None:
        return False, "levels incomplete (SL/TP1 missing)"
    risk = abs(close - sl)
    reward = abs(tp1 - close)
    if risk <= 0:
        return False, "risk=0"
    rr = reward / risk
    return (rr >= RR_MIN, None if rr >= RR_MIN else f"RR {rr:.2f} < {RR_MIN:.2f}")

def _sr_distance_ok(direction: str, close: float, sr: Optional[SR], atr: Optional[float]) -> tuple[bool, Optional[str]]:
    if atr is None or atr <= 0 or sr is None:
        return True, None
    if direction.upper() == "LONG" and sr.R1 is not None:
        if (sr.R1 - close) <= NEAR_SR_ATR * atr:
            return False, f"near adverse R1 (≤ {NEAR_SR_ATR}×ATR)"
    if direction.upper() == "SHORT" and sr.S1 is not None:
        if (close - sr.S1) <= NEAR_SR_ATR * atr:
            return False, f"near adverse S1 (≤ {NEAR_SR_ATR}×ATR)"
    return True, None

def _confluence(features: Optional[Features], direction: str) -> int:
    if not features:
        return 0
    dir_up = direction.upper() == "LONG"
    score = 0

    # 1) trend
    if features.trend is not None:
        if (dir_up and features.trend > 0) or ((not dir_up) and features.trend < 0):
            score += 1

    # 2) MTF alignment (au moins 2 positifs dans le sens)
    mtf = features.mtfSignal
    if mtf:
        votes = 0
        for v in [mtf.f5, mtf.f15, mtf.f60, mtf.f240, mtf.D]:
            if v is None:
                continue
            if (dir_up and v > 0) or ((not dir_up) and v < 0):
                votes += 1
        if votes >= 2:
            score += 1

    # 3) vector streak court terme (5/15)
    vs = features.vectorStreak
    if vs:
        votes_vs = 0
        for v in [vs.f5, vs.f15]:
            if v is None:
                continue
            if (dir_up and v > 0) or ((not dir_up) and v < 0):
                votes_vs += 1
        if votes_vs >= 1:
            score += 1

    # 4) rejcount
    if features.rejcount is not None and features.rejcount >= 2:
        score += 1

    return score

# =========================
# LLM call (Responses API)
# =========================
async def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Responses API + JSON Schema strict + reasoning.
    Retourne toujours un dict {decision, confidence, reason}.
    """
    schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["BUY", "SELL", "IGNORE"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"}
        },
        "required": ["decision", "confidence", "reason"],
        "additionalProperties": False
    }

    try:
        resp = client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": "Tu es un moteur de décision trading. Réponds uniquement en JSON valide qui matche le schéma."},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": schema, "strict": True},
            },
            reasoning={"effort": LLM_REASONING},
            temperature=0.1,
            max_output_tokens=256,
        )

        data = None
        # SDK récent: output_parsed si strict
        try:
            data = resp.output_parsed  # type: ignore[attr-defined]
        except Exception:
            data = None

        if not data:
            # fallback: tenter de lire le texte
            try:
                # resp.output[0].content[0].text est souvent disponible
                txt = resp.output[0].content[0].text  # type: ignore[index]
                data = json.loads(txt)
            except Exception:
                pass

        if not isinstance(data, dict):
            raise ValueError("no-json")

        decision = str(data.get("decision", "IGNORE")).upper()
        if decision not in ("BUY", "SELL", "IGNORE"):
            decision = "IGNORE"
        confidence = float(data.get("confidence", 0))
        confidence = confidence if 0.0 <= confidence <= 1.0 else 0.0
        reason = data.get("reason", "no-reason")

        return {"decision": decision, "confidence": confidence, "reason": reason}

    except Exception as e:
        return {"decision": "IGNORE", "confidence": 0.0, "reason": f"llm-error: {e.__class__.__name__}"}

# =========================
# Telegram helpers
# =========================
async def send_telegram(text: str) -> tuple[bool, Optional[str]]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            if r.status_code == 200:
                return True, None
            return False, f"HTTP {r.status_code}: {r.text}"
        except httpx.HTTPError as e:
            return False, str(e)

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
        "OPENAI_API_KEY": _mask(OPENAI_API_KEY),
        "LLM_MODEL": LLM_MODEL,
        "LLM_REASONING": LLM_REASONING,
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "MIN_CONFLUENCE": MIN_CONFLUENCE,
        "NEAR_SR_ATR": NEAR_SR_ATR,
        "RR_MIN": RR_MIN,
        "COOLDOWN_SEC": COOLDOWN_SEC,
    }

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            temperature=0
        )
        return {"ok": True, "model": "gpt-4o-mini", "sample": r.choices[0].message.content}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)})

@app.get("/tg-health")
async def tg_health(
    secret: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    text: Optional[str] = Query("✅ Test Telegram depuis le serveur")
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat_id:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID",
            "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
            "TELEGRAM_CHAT_ID_set": bool(target_chat_id),
        })

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            ok = r.status_code == 200
            return {"ok": ok, "status_code": r.status_code, "response": r.json() if ok else r.text}
        except httpx.HTTPError as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.post("/verdict-test")
async def verdict_test(payload: Dict[str, Any]):
    dummy = TVPayload(**payload)
    prompt = build_prompt(dummy)
    verdict = await call_llm(prompt)
    return verdict

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 0) Secret TradingView
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    direction = payload.direction.upper()
    features = payload.features or Features()
    levels = payload.levels or Levels()
    sr = features.sr or SR()

    # 1) Cooldown
    ok_cd, err_cd = _cooldown_ok(payload.symbol, payload.tf, direction)
    if not ok_cd:
        return JSONResponse(
            {"decision": "IGNORE", "confidence": 0.0, "reason": err_cd, "received": payload.model_dump(by_alias=True),
             "telegram": {"sent": False, "error": None, "skipped_reason": "cooldown"}}
        )

    # 2) SR distance adverse
    ok_sr, err_sr = _sr_distance_ok(direction, payload.close, sr, features.volatility_atr)
    if not ok_sr:
        return JSONResponse(
            {"decision": "IGNORE", "confidence": 0.0, "reason": err_sr, "received": payload.model_dump(by_alias=True),
             "telegram": {"sent": False, "error": None, "skipped_reason": "near_sr"}}
        )

    # 3) Risk/Reward mini
    ok_rr, err_rr = _rr_ok(payload.close, levels.SL, levels.TP1)
    if not ok_rr:
        return JSONResponse(
            {"decision": "IGNORE", "confidence": 0.0, "reason": err_rr, "received": payload.model_dump(by_alias=True),
             "telegram": {"sent": False, "error": None, "skipped_reason": "low_rr"}}
        )

    # 4) Confluence
    conf_score = _confluence(features, direction)
    if conf_score < MIN_CONFLUENCE:
        return JSONResponse(
            {"decision": "IGNORE", "confidence": 0.0, "reason": f"low confluence ({conf_score} < {MIN_CONFLUENCE})",
             "received": payload.model_dump(by_alias=True),
             "telegram": {"sent": False, "error": None, "skipped_reason": "low_confluence"}}
        )

    # 5) LLM (raisonné)
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # 6) Préparation message Telegram
    vs = features.vectorStreak or VectorStreak()
    mtf = features.mtfSignal or MTFSignal()

    tg = []
    tg.append(f"📈 <b>{payload.symbol}</b>  ⏱ TF <b>{payload.tf}</b>")
    tg.append(f"Signal brut: <b>{direction}</b>  | Close: <b>{payload.close:.4f}</b>")
    tg.append(f"LLM: <b>{verdict.get('decision','?')}</b>  (conf. {float(verdict.get('confidence',0)):.2f})")
    tg.append(f"Raison: {verdict.get('reason','-')}")
    tg.append(f"Trend={features.trend if features.trend is not None else '-'}  "
              f"Rej={features.rejcount if features.rejcount is not None else '-'}  "
              f"ATR={features.volatility_atr if features.volatility_atr is not None else '-'}")
    tg.append(f"R1={fmt_lvl(sr.R1)}  S1={fmt_lvl(sr.S1)}")
    tg.append(f"VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"SL={fmt_lvl(levels.SL)}  TP1={fmt_lvl(levels.TP1)}  TP2={fmt_lvl(levels.TP2)}  TP3={fmt_lvl(levels.TP3)}")

    # 7) Envoi Telegram si decision OK + confidence OK
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0

    telegram_sent = False
    telegram_error = None
    send_skipped_reason = None

    if verdict.get("decision") != "IGNORE" and conf >= CONFIDENCE_MIN:
        telegram_sent, telegram_error = await send_telegram("\n".join(tg))
    else:
        if verdict.get("decision") == "IGNORE":
            send_skipped_reason = "llm_decision_ignore"
        elif conf < CONFIDENCE_MIN:
            send_skipped_reason = f"confidence_below_threshold ({conf:.2f} < {CONFIDENCE_MIN:.2f})"

    return JSONResponse(
        {
            "decision": verdict.get("decision", "IGNORE"),
            "confidence": float(verdict.get("confidence", 0)),
            "reason": verdict.get("reason", "no-reason"),
            "received": payload.model_dump(by_alias=True),
            "server_filters": {
                "confluence": conf_score,
                "min_confluence": MIN_CONFLUENCE,
                "rr_min": RR_MIN,
                "near_sr_atr": NEAR_SR_ATR,
                "cooldown_sec": COOLDOWN_SEC,
            },
            "telegram": {
                "sent": telegram_sent,
                "error": telegram_error,
                "skipped_reason": send_skipped_reason
            }
        }
    )
