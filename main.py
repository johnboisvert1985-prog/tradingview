import os
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse
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

# Ne forward sur Telegram que si confiance >= CONFIDENCE_MIN
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.60"))

# Client OpenAI (lit la clé dans l'env)
client = OpenAI()

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

# ---------------------------
# Pydantic models (Pine payload)
# ---------------------------
class SR(BaseModel):
    R1: Optional[float] = None
    S1: Optional[float] = None

class VectorStreak(BaseModel):
    # Pydantic v2: pas de nom commençant par "_". On utilise des alias "5","15",...
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
    tag: Optional[str] = None           # "ENTRY" | "TP_HIT" (optionnel)
    symbol: str
    tf: str
    time: int
    close: float                        # prix au moment de l’alerte (souvent prix d’entrée)
    direction: str                      # "LONG" | "SHORT"
    features: Optional[Features] = None
    levels: Optional[Levels] = None
    secret: Optional[str] = None
    # champs TP
    tp: Optional[str] = None            # "TP1" | "TP2" | "TP3" (pour TP_HIT)
    trade_id: Optional[str] = None      # id de trade si fourni par le Pine

# ---------------------------
# Helpers
# ---------------------------
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
Retourne UNIQUEMENT un JSON valide avec les clés: decision (BUY|SELL|IGNORE), confidence (0..1), reason (français, concis).

Contexte:
- Symbole: {p.symbol}
- TF: {p.tf}
- Direction signal brut: {p.direction}
- Prix d'entrée estimé: {p.close}
- Features: {p.features.model_dump(by_alias=True) if p.features else {}}
- Levels: {p.levels.model_dump(by_alias=True) if p.levels else {}}

Lignes directrices:
- BUY si LONG avec contexte multi-TF et S/R/volatilité cohérents.
- SELL si SHORT avec contexte cohérent.
- IGNORE par défaut s'il y a doute (volatilité extrême, contre-signal MTF, R1/S1 trop proche, etc.).
- Pas de texte hors JSON.

Format strict (exemple):
{{"decision":"IGNORE","confidence":0.55,"reason":"MTF mitigé, volatilité élevée, S/R proche"}}
""".strip()

async def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Appel via Responses API (modèles o4 / 4.1 / 4o / 4o-mini, etc.)
    - Pas de temperature
    - Utiliser max_completion_tokens (pas max_tokens)
    """
    resp = client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": "Tu es un moteur de décision qui ne renvoie que du JSON valide."},
            {"role": "user", "content": prompt},
        ],
        max_completion_tokens=300,
    )
    txt = (resp.output_text or "").strip()
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
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            # ne bloque pas le webhook si Telegram échoue
            pass

# ---------------------------
# Mini Dashboard HTML (/)
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    # Page HTML minimaliste avec JS pour lancer les checks
    html = f"""
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI Trade Pro — Dashboard</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#0f172a; color:#e2e8f0; margin:0; padding:24px; }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  .sub {{ color:#94a3b8; margin-bottom: 24px; font-size: 14px; }}
  .card {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:16px; margin-bottom:16px; }}
  .row {{ display:flex; gap:12px; flex-wrap:wrap; }}
  label {{ font-size: 13px; color:#cbd5e1; display:block; margin-bottom:6px; }}
  input[type=text] {{ width:100%; padding:10px 12px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0; }}
  button {{ background:#2563eb; color:white; border:none; padding:10px 12px; border-radius:8px; cursor:pointer; }}
  button:disabled {{ opacity:0.6; cursor:not-allowed; }}
  .muted {{ color:#94a3b8; font-size:12px; }}
  pre {{ background:#0b1220; border:1px solid #1f2937; padding:12px; border-radius:10px; overflow:auto; }}
  .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }}
  .ok {{ color:#10b981; }}
  .err {{ color:#f87171; }}
  .tag {{ background:#111827; border:1px solid #1f2937; padding:4px 8px; border-radius:8px; display:inline-block; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>AI Trade Pro — Dashboard</h1>
    <div class="sub">Vérifie en un clic l’état des variables d’environnement, de l’API OpenAI et de l’envoi Telegram.</div>

    <div class="card">
      <div class="row">
        <div style="flex:1; min-width:260px;">
          <label for="secret">WEBHOOK_SECRET</label>
          <input id="secret" type="text" placeholder="Entrez votre secret pour lancer les checks sécurisés">
          <div class="muted">Le secret n’est pas stocké. Il est utilisé seulement pour interroger les endpoints /env-sanity, /openai-health, /tg-health.</div>
        </div>
        <div style="display:flex; align-items:flex-end; gap:8px;">
          <button onclick="runAll()" id="btnRun">Lancer les checks</button>
          <button onclick="sendTgHealth()" id="btnTg">Test Telegram</button>
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h3 style="margin-top:0;">Env sanity</h3>
        <div id="envStatus" class="muted">Résultat ici…</div>
        <pre id="envRaw" style="display:none;"></pre>
      </div>

      <div class="card">
        <h3 style="margin-top:0;">OpenAI health</h3>
        <div id="aiStatus" class="muted">Résultat ici…</div>
        <pre id="aiRaw" style="display:none;"></pre>
      </div>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">Telegram health</h3>
      <div id="tgStatus" class="muted">Clique “Test Telegram” pour envoyer un message dans ton groupe.</div>
      <pre id="tgRaw" style="display:none;"></pre>
    </div>

    <div class="muted">
      <span class="tag">Modèle: {LLM_MODEL}</span>
      <span class="tag">CONFIDENCE_MIN: {CONFIDENCE_MIN}</span>
      <span class="tag">Telegram cfg: {"OK" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "incomplet"}</span>
    </div>
  </div>

<script>
async function fetchJSON(url) {{
  const r = await fetch(url);
  const txt = await r.text();
  try {{ return [r.status, JSON.parse(txt)]; }} catch (e) {{ return [r.status, {{raw: txt}}]; }}
}}

function setBlock(idStatus, idRaw, ok, data) {{
  const elS = document.getElementById(idStatus);
  const elR = document.getElementById(idRaw);
  if (ok) {{
    elS.innerHTML = '<span class="ok">OK</span>';
  }} else {{
    elS.innerHTML = '<span class="err">ERREUR</span>';
  }}
  elR.style.display = 'block';
  elR.textContent = JSON.stringify(data, null, 2);
}}

async function runAll() {{
  const secret = document.getElementById('secret').value.trim();
  if (!secret) {{ alert('Entre ton WEBHOOK_SECRET'); return; }}

  document.getElementById('btnRun').disabled = true;
  try {{
    // env-sanity
    let [st1, js1] = await fetchJSON('/env-sanity?secret=' + encodeURIComponent(secret));
    setBlock('envStatus', 'envRaw', st1 === 200, js1);

    // openai-health
    let [st2, js2] = await fetchJSON('/openai-health?secret=' + encodeURIComponent(secret));
    setBlock('aiStatus', 'aiRaw', st2 === 200 && js2.ok, js2);
  }} catch (e) {{
    console.error(e);
    alert('Erreur durant les checks, vois la console.');
  }} finally {{
    document.getElementById('btnRun').disabled = false;
  }}
}}

async function sendTgHealth() {{
  const secret = document.getElementById('secret').value.trim();
  if (!secret) {{ alert('Entre ton WEBHOOK_SECRET'); return; }}

  document.getElementById('btnTg').disabled = true;
  try {{
    let [st, js] = await fetchJSON('/tg-health?secret=' + encodeURIComponent(secret));
    setBlock('tgStatus', 'tgRaw', st === 200 && js.ok, js);
  }} catch (e) {{
    console.error(e);
    alert('Erreur Telegram, vois la console.');
  }} finally {{
    document.getElementById('btnTg').disabled = false;
  }}
}}
</script>

</body>
</html>
    """
    return HTMLResponse(content=html, status_code=200)

# ---------------------------
# API utilitaires
# ---------------------------
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
    }

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None, description="must match WEBHOOK_SECRET")):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        r = client.responses.create(
            model=LLM_MODEL,
            input="ping",
            max_completion_tokens=5,
        )
        return {"ok": True, "model": LLM_MODEL, "sample": r.output_text}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "openai_key_mask": _mask(OPENAI_API_KEY)}
        )

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: le bot est opérationnel.")
    return {"ok": True}

# ---------------------------
# Webhook principal
# ---------------------------
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Sécurité simple: secret dans le JSON doit matcher l'env
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Normalise structures pour formatage du message
    f = payload.features or Features()
    levels = payload.levels or Levels()
    sr = f.sr or SR()
    vs = f.vectorStreak or VectorStreak()
    mtf = f.mtfSignal or MTFSignal()

    # 1) Gestion TP_HIT (notification directe)
    if (payload.tag or "").upper() == "TP_HIT":
        # Carré rouge/vert unicode selon direction (Telegram ne supporte pas fond coloré)
        square = "🟥" if payload.direction.upper() == "SHORT" else "🟩"
        tp_label = payload.tp or "TP?"
        trade_id = payload.trade_id or "-"
        msg = [
            f"{square} <b>{payload.symbol}</b> • <b>{payload.tf}</b>",
            f"<b>{tp_label} Réussi</b> ({payload.direction.upper()})",
            f"Trade ID: <code>{trade_id}</code>",
        ]
        await send_telegram("\n".join(msg))
        return {"ok": True, "note": "tp_hit_sent"}

    # 2) Gestion ENTRY -> appel LLM, filtrage par confiance, envoi Telegram
    prompt = build_prompt(payload)
    verdict = await call_llm(prompt)

    # Confidence parsing
    try:
        conf = float(verdict.get("confidence", 0))
    except Exception:
        conf = 0.0

    # Prépare message Telegram (sans “Close” en double, on l’appelle “Entrée”)
    tg: list[str] = []
    tg.append(f"🚨 <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>")
    tg.append(f"Direction script: <b>{payload.direction}</b> | Entrée: <b>{payload.close:.4f}</b>")
    tg.append(f"🤖 LLM: <b>{verdict.get('decision','?')}</b>  | Confiance: <b>{conf:.2f}</b>")
    tg.append(f"📝 Raison: {verdict.get('reason','-')}")
    tg.append("—")
    tg.append(f"⚙️ Trend={f.trend if f.trend is not None else '-'} | Rej={f.rejcount if f.rejcount is not None else '-'} | ATR={f.volatility_atr if f.volatility_atr is not None else '-'}")
    tg.append(f"📊 VS 5/15/60/240/D = {fmt_int(vs.f5)}/{fmt_int(vs.f15)}/{fmt_int(vs.f60)}/{fmt_int(vs.f240)}/{fmt_int(vs.D)}")
    tg.append(f"🧭 MTF 5/15/60/240/D = {fmt_int(mtf.f5)}/{fmt_int(mtf.f15)}/{fmt_int(mtf.f60)}/{fmt_int(mtf.f240)}/{fmt_int(mtf.D)}")
    tg.append(f"🎯 SL={fmt_lvl(levels.SL)} | TP1={fmt_lvl(levels.TP1)} | TP2={fmt_lvl(levels.TP2)} | TP3={fmt_lvl(levels.TP3)}")
    # tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{payload.symbol}"
    # tg.append(f'<a href="{tv_url}">📈 Ouvrir dans TradingView</a>')

    # Envoi Telegram si non ignoré et confiance suffisante
    decision = (verdict.get("decision") or "IGNORE").upper()
    if decision != "IGNORE" and conf >= CONFIDENCE_MIN:
        await send_telegram("\n".join(tg))

    return JSONResponse(
        {
            "decision": decision,
            "confidence": conf,
            "reason": verdict.get("reason", "no-reason"),
            "received": payload.model_dump(by_alias=True),
            "forwarded_to_telegram": bool(decision != "IGNORE" and conf >= CONFIDENCE_MIN and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    )

# ---------------------------
# Endpoint test verdict (dev)
# ---------------------------
@app.post("/verdict-test")
async def verdict_test(payload: Dict[str, Any]):
    dummy = TVPayload(**payload)
    prompt = build_prompt(dummy)
    verdict = await call_llm(prompt)
    return verdict
