from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Literal
from openai import OpenAI
import os, json

# ------------------ Config ------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # optional; if set, incoming payload must include matching "secret"

if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment (e.g., via .env or cloud env vars).")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

SYSTEM = """Tu es un filtre de signaux de trading.
Retourne UNIQUEMENT ce JSON compact, une seule ligne:
{"decision":"BUY|SELL|IGNORE","reason":"...", "confidence":0.00-1.00}

Règles:
- Évalue le contexte fourni (pas de prédiction ni d'accès marché).
- BUY si confluence haussière claire + niveaux cohérents.
- SELL si confluence baissière claire.
- IGNORE si doute, signal mitigé, ou données insuffisantes.
- Confiance ∈ [0,1]; reste sobre (0.55–0.85 standard).
- 15 mots max dans "reason". Pas d'emoji, pas de guillemets superflus.
"""

def build_user_text(payload: dict) -> str:
    tag = payload.get("tag")
    sym = payload.get("symbol")
    tf  = payload.get("tf")
    tms = payload.get("time")
    px  = payload.get("close")
    direction = payload.get("direction")
    feats = payload.get("features", {})
    levels = payload.get("levels", {})

    sr = feats.get("sr", {})
    parts = [
        f"tag={tag}",
        f"symbol={sym}",
        f"tf={tf}",
        f"time={tms}",
        f"close={px}",
        f"direction={direction}",
        f"trend={feats.get('trend')}",
        f"rejcount={feats.get('rejcount')}",
        f"atr={feats.get('volatility_atr')}",
        f"R1={sr.get('R1')}",
        f"S1={sr.get('S1')}",
        f"vecStreak={feats.get('vectorStreak')}",
        f"mtf={feats.get('mtfSignal')}",
        f"levels={levels}",
    ]
    return " | ".join(map(str, parts))

class Verdict(BaseModel):
    decision: Literal["BUY", "SELL", "IGNORE"]
    reason: str
    confidence: float

@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL}

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # Optional shared secret check
    if WEBHOOK_SECRET:
        if payload.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(401, "Invalid secret")

    user_text = build_user_text(payload)

    try:
        resp = client.responses.create(
            model=MODEL,
            instructions=SYSTEM,
            input=[{"role":"user","content":[{"type":"input_text","text":user_text}]}],
            temperature=0.2,
            max_output_tokens=120,
        )
        raw = resp.output_text.strip()
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")

    # Normalize to JSON
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1]

    try:
        data = json.loads(raw)
        verdict = Verdict(**data)
    except Exception:
        verdict = Verdict(decision="IGNORE", reason="format invalid", confidence=0.0)

    # Place your integrations here (exchange, Telegram, etc.)
    return {"ok": True, "verdict": verdict.model_dump(), "received_tag": payload.get("tag")}
