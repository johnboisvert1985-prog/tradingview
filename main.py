import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic import ConfigDict
from openai import OpenAI
import httpx

# ----------------------------------
# ENV / Config
# ----------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram (si vides -> pas d'envoi)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Forward vers Telegram seulement si confidence >= CONFIDENCE_MIN
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0"))

# OpenAI client (lit la clé depuis l'env)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="2.0.0")

# CORS pour autoriser le dashboard à appeler les endpoints
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------
# Pydantic models
# ----------------------------------
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
    tag: Optional[str] = None          # "ENTRY", "TP1_HIT", etc. (si tu l’utilises)
    symbol: str
    tf: str
    time: int
    close: float                       # prix de la bougie (sert d’Entry si tu veux)
    direction: str                     # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    secret: Optional[str] = None
    trade_id: Optional[str] = None     # optionnel si tu l’utilises côté TV

# ----------------------------------
# Helpers
# ----------------------------------
def build_prompt(p: TVPayload) -> str:
    return f"""
Tu es un moteur de décision de trading.
Retourne UNIQUEMENT un JSON valide avec les clés: decision (BUY|SELL|IGNORE), confidence (0..1), reason (français).

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Entry (close de la bougie): {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Règles:
- BUY si LONG + contexte multi-TF/volatilité/SR cohérent.
- SELL si SHORT + contexte cohérent.
- Sinon IGNORE.
- Sois strict: IGNORE par défaut en cas de doute.
- Réponse = JSON UNIQUEMENT (pas de texte avant/après).

Exemple de format:
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()

async def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Utilise l'API Chat Completions (compatible large majorité des SDK).
    IMPORTANT: pas de 'temperature' ni 'max_tokens' pour éviter les erreurs "unsupported_parameter".
    """
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "Tu es un moteur de décision qui ne renvoie que du JSON valide."},
            {"role": "user", "content": prompt},
        ],
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

# ----------------------------------
# Routes — dashboard simple (GET "/")
# ----------------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AI Trade Pro — Dashboard</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
  h1 { margin-bottom: 8px; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin: 12px 0; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  input[type=text] { padding: 8px; width: 360px; }
  button { padding: 8px 12px; cursor: pointer; }
  .ok { color: #0a7c2f; font-weight: 600; }
  .err { color: #b00020; font-weight: 600; }
  pre { background:#f7f7f7; padding:12px; border-radius:6px; overflow:auto; display:none; max-height:260px;}
  small { color:#555; }
</style>
</head>
<body>
  <h1>AI Trade Pro — Dashboard</h1>
  <div class="card">
    <div class="row">
      <label for="secret"><b>WEBHOOK_SECRET</b> :</label>
      <input id="secret" type="text" placeholder="colle ton secret exact ici"/>
      <button id="btnRun" onclick="runAll()">Lancer les checks</button>
      <button id="btnTg" onclick="sendTgHealth()">Tester Telegram</button>
    </div>
    <small>Ce dashboard appelle /env-sanity, /openai-health et /tg-health avec le secret saisi.</small>
  </div>

  <div class="card">
    <h3>Env sanity</h3>
    <div>Statut : <span id="envStatus">—</span></div>
    <pre id="envRaw"></pre>
  </div>

  <div class="card">
    <h3>OpenAI health</h3>
    <div>Statut : <span id="aiStatus">—</span></div>
    <pre id="aiRaw"></pre>
  </div>

  <div class="card">
    <h3>Telegram health</h3>
    <div>Statut : <span id="tgStatus">—</span></div>
    <pre id="tgRaw"></pre>
  </div>

<script>
async function fetchJSON(url) {
  const r = await fetch(url);
  const txt = await r.text();
  try { return [r.status, JSON.parse(txt)]; } catch (e) { return [r.status, {raw: txt}]; }
}
function setBlock(idStatus, idRaw, ok, data) {
  const elS = document.getElementById(idStatus);
  const elR = document.getElementById(idRaw);
  elS.innerHTML = ok ? '<span class="ok">OK</span>' : '<span class="err">ERREUR</span>';
  elR.style.display = 'block';
  elR.textContent = JSON.stringify(data, null, 2);
}
async function runAll() {
  const secret = document.getElementById('secret').value.trim();
  if (!secret) { alert('Entre ton WEBHOOK_SECRET'); return; }
  document.getElementById('btnRun').disabled = true;
  try {
    const base = new URL('.', window.location.href);
    const envUrl = new URL('env-sanity?secret=' + encodeURIComponent(secret), base);
    const aiUrl  = new URL('openai-health?secret=' + encodeURIComponent(secret), base);
    let [st1, js1] = await fetchJSON(envUrl.href);
    setBlock('envStatus', 'envRaw', st1 === 200, js1);
    let [st2, js2] = await fetchJSON(aiUrl.href);
    setBlock('aiStatus', 'aiRaw', st2 === 200 && js2.ok === true, js2);
  } finally {
    document.getElementById('btnRun').disabled = false;
  }
}
async function sendTgHealth() {
  const secret = document.getElementById('secret').value.trim();
  if (!secret) { alert('Entre ton WEBHOOK_SECRET'); return; }
  document.getElementById('btnTg').disabled = true;
  try {
    const base = new URL('.', window.location.href);
    const tgUrl = new URL('tg-health?secret=' + encodeURIComponent(secret), base);
    let [st, js] = await fetchJSON(tgUrl.href);
    setBlock('tgStatus', 'tgRaw', st === 200 && js.ok === true, js);
  } finally {
    document.getElementById('btnTg').disabled = false;
  }
}
</script>
</body>
</html>
    """

# ----------------------------------
# Routes — API JSON
# ----------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Sécurité simple: secret dans le JSON doit matcher l'env
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Appel LLM
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Prépare message Telegram (sans "Close" affiché, on parle d'Entry)
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    tg = []
    tg.append(f"🚨 <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>")
    tg.append(f"Direction script: <b>{payload.direction}</b> | Entry: <b>{payload.close:.4f}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{float(verdict.get('confidence',0)):.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"R1={fmt_lvl(sr.R1)}  S1={fmt_lvl(sr.S1)}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"🎯 SL={fmt_lvl(levels.SL)} | TP1={fmt_lvl(levels.TP1)} | TP2={fmt_lvl(levels.TP2)} | TP3={fmt_lvl(levels.TP3)}")

    # Envoi Telegram si décision ≠ IGNORE et confiance >= seuil
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

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    # Protégé par le même secret que le webhook
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {
            "ok": True,
            "model": LLM_MODEL,
            "sample": r.choices[0].message.content
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)}
        )

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    # Petit état des lieux, sans divulguer les secrets
    return {
        "OPENAI_API_KEY": _mask(OPENAI_API_KEY),
        "LLM_MODEL": LLM_MODEL,
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
    }

@app.get("/tg-health")
async def tg_health(secret: str = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        await send_telegram("✅ Test Telegram OK (tg-health)")
        return {"ok": True, "sent": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
