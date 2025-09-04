import os
import json
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic import ConfigDict
from openai import OpenAI
import httpx

# =========================
# ENV / CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Seuil d'envoi (entrées uniquement)
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.70"))  # ex: 0.70 = 70%

# Durée de conservation des trades approuvés (pour valider TP1/2/3)
APPROVED_TTL_HOURS = float(os.getenv("APPROVED_TTL_HOURS", "96"))  # 4 jours

# Tags TP attendus depuis TradingView
TP_TAGS = {"TP1_HIT", "TP2_HIT", "TP3_HIT"}

# OpenAI client
client = OpenAI()  # lit OPENAI_API_KEY depuis l'env

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

# =========================
# Pydantic models (Payload)
# =========================
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None

class VectorStreak(BaseModel):
    # Noms sans underscore en tête; aliases pour clés numériques
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
    tag: Optional[str] = None               # "ENTRY" | "TP1_HIT" | ...
    trade_id: Optional[str] = None          # identifiant stable entre entrée & TP
    symbol: str
    tf: str
    time: int
    close: float
    direction: str                          # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    secret: Optional[str] = None

# =========================
# Helpers
# =========================
def _mask(s: Optional[str]) -> str:
    if not s:
        return "missing"
    return (s[:7] + "..." + s[-4:]) if len(s) > 12 else "***"

def fmt_lvl(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.4f}"

def fmt_int(x: Optional[int]) -> str:
    return "-" if x is None else str(x)

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
            # on ne bloque pas le webhook si Telegram échoue
            pass

def build_prompt(p: TVPayload) -> str:
    return f"""
Tu es un moteur de décision de trading.
Retourne UNIQUEMENT un JSON valide avec les clés: decision (BUY|SELL|IGNORE), confidence (0..1), reason (français).

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Close (entrée): {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Règles:
- BUY si LONG + contexte multi-TF/volatilité/sr OK ; SELL si SHORT + contexte cohérent ; sinon IGNORE.
- Sois strict mais pas ultra-conservateur: si la confluence est raisonnable, approuve avec une confiance appropriée.
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).

Exemple de format:
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()

async def call_llm(prompt: str) -> Dict[str, Any]:
    # chat.completions (gpt-4o-mini supporte max_tokens et temperature)
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

def trade_id_from_payload(p: TVPayload) -> str:
    if p.trade_id:
        return str(p.trade_id).strip()
    # fallback quand on n'a pas trade_id (moins fiable)
    return f"{p.symbol}:{p.tf}:{p.direction}:{p.time}"

# =========================
# Mémoire des trades approuvés
# =========================
class ApprovedStore:
    """
    Mémoire en RAM (simple) : trade_id -> {'exp': epoch, 'direction': 'LONG|SHORT',
                                           'symbol': str, 'tf': str, 'entry': float}
    Pour du multi-instance, utiliser Redis (clé=trade_id, TTL).
    """
    def __init__(self):
        self._d: Dict[str, Dict[str, Any]] = {}

    def put(self, trade_id: str, direction: str, symbol: str, tf: str, entry: Optional[float]):
        if not trade_id:
            return
        self._d[trade_id] = {
            "exp": time.time() + APPROVED_TTL_HOURS * 3600.0,
            "direction": (direction or "").upper(),
            "symbol": symbol,
            "tf": tf,
            "entry": entry,
        }

    def get(self, trade_id: str) -> Optional[Dict[str, Any]]:
        if not trade_id:
            return None
        item = self._d.get(trade_id)
        if not item:
            return None
        if item["exp"] < time.time():
            del self._d[trade_id]
            return None
        return item

    def has(self, trade_id: str) -> bool:
        return self.get(trade_id) is not None

approved_store = ApprovedStore()

# =========================
# ROUTES
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
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "APPROVED_TTL_HOURS": APPROVED_TTL_HOURS,
    }

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            temperature=0
        )
        return {"ok": True, "model": LLM_MODEL, "sample": r.choices[0].message.content}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)})

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram OK (tg-health)")
    return {"ok": True}

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # 0) Sécurité simple avec secret
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    tag = (payload.tag or "ENTRY").upper().strip()
    tid = trade_id_from_payload(payload)

    # 1) TP1/TP2/TP3 HIT -> UNIQUEMENT si trade approuvé
    if tag in TP_TAGS:
        info = approved_store.get(tid)
        if not info:
            return {"ok": False, "skipped": "tp_for_unapproved_trade", "trade_id": tid}

        # On prend direction/symbol/tf mémorisés si absents dans le TP
        direction = (payload.direction or info.get("direction") or "").upper()
        symbol = payload.symbol or info.get("symbol") or "?"
        tf = payload.tf or info.get("tf") or "?"
        entry = payload.close if payload.close is not None else info.get("entry")

        # Style LONG / SHORT (émoji carrés vert/rouge)
        square = "🟩" if direction == "LONG" else "🟥"

        tp_name = {"TP1_HIT": "TP1 Réussit !", "TP2_HIT": "TP2 Réussit !", "TP3_HIT": "TP3 Réussit !"}.get(tag, "TP Réussit !")
        price_str = f"{payload.close:.4f}" if payload.close is not None else "-"

        # ===== Message Telegram TP (PERSONNALISABLE) =====
        lines = []
        lines.append(f"{square} <b>{symbol}</b> • <b>{tf}</b> — <b>{tp_name}</b>")
        if entry is not None:
            lines.append(f"🎯 Entrée: <b>{entry:.4f}</b>   🔔 TP prix: <b>{price_str}</b>")
        else:
            lines.append(f"🔔 TP prix: <b>{price_str}</b>")
        lines.append(f"Direction: <b>{direction}</b>")
        lines.append(f"ID: <code>{tid}</code>")
        # ================================================

        await send_telegram("\n".join(lines))
        return {"ok": True, "forwarded": tag, "trade_id": tid}

    # 2) Signal d’ENTRÉE -> passage par le LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message d’entrée
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    # style LONG/SHORT
    square = "🟩" if (payload.direction or "").upper() == "LONG" else "🟥"

    # ===== Message Telegram ENTRÉE (PERSONNALISABLE) =====
    tg = []
    tg.append(f"{square} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>")
    tg.append(f"Direction script: <b>{payload.direction}</b>")
    tg.append(f"🎯 Entrée: <b>{payload.close:.4f}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{float(verdict.get('confidence',0)):.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"🎯 SL={fmt_lvl(levels.SL)} | TP1={fmt_lvl(levels.TP1)} | TP2={fmt_lvl(levels.TP2)} | TP3={fmt_lvl(levels.TP3)}")
    tg.append(f"ID: <code>{tid}</code>")
    # =====================================================

    # filtre d’envoi par confiance & décision
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0

    if verdict.get("decision") in {"BUY", "SELL"} and conf >= CONFIDENCE_MIN:
        # on mémorise le trade approuvé pour autoriser TP1/2/3
        approved_store.put(
            trade_id=tid,
            direction=(payload.direction or "").upper(),
            symbol=payload.symbol,
            tf=payload.tf,
            entry=payload.close,
        )
        await send_telegram("\n".join(tg))

    return JSONResponse(
        {
            "decision": verdict.get("decision", "IGNORE"),
            "confidence": float(verdict.get("confidence", 0)),
            "reason": verdict.get("reason", "no-reason"),
            "received": payload.model_dump(by_alias=True),
            "trade_id": tid
        }
    )
