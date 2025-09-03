from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Literal
import os, json

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

def get_client():
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY not configured on server")
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)

SYSTEM = """Tu es un filtre de signaux de trading.
Retourne UNIQUEMENT ce JSON compact:
{"decision":"BUY|SELL|IGNORE","reason":"...", "confidence":0.00-1.00}
Règles: BUY si confluence haussière; SELL si baissière; IGNORE sinon; raison ≤15 mots; confiance [0,1].
"""

class Verdict(BaseModel):
    decision: Literal["BUY", "SELL", "IGNORE"]
    reason: str
    confidence: float

@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL}

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
        f"tag={tag}", f"symbol={sym}", f"tf={tf}", f"time={tms}", f"close={px}",
        f"direction={direction}", f"trend={feats.get('trend')}",
        f"rejcount={feats.get('rejcount')}", f"atr={feats.get('volatility_atr')}",
        f"R1={sr.get('R1')}", f"S1={sr.get('S1')}",
        f"vecStreak={feats.get('vectorStreak')}", f"mtf={feats.get('mtfSignal')}",
        f"levels={levels}",
    ]
    return " | ".join(map(str, parts))

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid secret")

    user_text = build_user_text(payload)

    client = get_client()
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

    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1]

    try:
        data = json.loads(raw)
        verdict = Verdict(**data)
    except Exception:
        verdict = Verdict(decision="IGNORE", reason="format invalid", confidence=0.0)

    return {"ok": True, "verdict": verdict.model_dump(), "received_tag": payload.get("tag")}
