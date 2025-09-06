# main.py
import os
import json
from typing import Optional, Union, Dict, Any, List

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

# ============== LLM (OpenAI) ==============
LLM_ENABLED = os.getenv("LLM_ENABLED", "1").lower() not in ("0", "false", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

_openai_client = None
_llm_mode = None  # "responses" or "chat"
if LLM_ENABLED and OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        # On essaiera Responses d'abord, puis fallback Chat à l'exécution
        _llm_mode = "auto"
    except Exception as e:
        _openai_client = None
        _llm_mode = None
        LLM_ENABLED = False
        print("[LLM] init error:", e)

# ============== ENV (Webhook & Telegram) ==============
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT               = int(os.getenv("PORT", "8000"))
CONFIDENCE_MIN     = float(os.getenv("CONFIDENCE_MIN", "0.0"))  # seuil de filtrage, mais on affiche la vraie valeur

# ============== APP ==============
app = FastAPI(title="AI Trader PRO - Webhook", version="3.2.0")

# ============== IN-MEMORY STORE ==============
TRADES: List[Dict[str, Any]] = []
MAX_TRADES = int(os.getenv("MAX_TRADES", "2000"))

# ============== MODELS ==============
Number = Optional[Union[float, int, str]]

class TVPayload(BaseModel):
    # On tolère "type" ou "tag"
    type: Optional[str] = None
    tag:  Optional[str] = None
    symbol: str
    tf: str
    time: int
    side: Optional[str] = None
    entry: Number = None
    tp: Number = None
    sl: Number = None
    tp1: Number = None
    tp2: Number = None
    tp3: Number = None
    r1: Number = None
    s1: Number = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None
    # Terminaison + LLM facultatifs
    term_reason: Optional[str] = None
    decision: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None

    class Config:
        extra = "allow"

# ============== HELPERS ==============
def _mask(val: Optional[str]) -> str:
    if not val:
        return "missing"
    return (val[:6] + "..." + val[-4:]) if len(val) > 12 else "***"

def _fmt_num(x: Number) -> str:
    if x is None:
        return "-"
    try:
        xf = float(x)
        return f"{xf:.8f}" if xf < 1 else f"{xf:.6f}" if xf < 10 else f"{xf:.4f}"
    except Exception:
        return str(x)

async def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            print("[Telegram] send error:", e)

# ============== LLM ==============
def _build_llm_prompt(p: TVPayload) -> str:
    body = {
        "symbol": p.symbol,
        "tf": p.tf,
        "direction_raw": (p.side or "").upper(),
        "entry": p.entry,
        "levels": {"sl": p.sl, "tp1": p.tp1, "tp2": p.tp2, "tp3": p.tp3},
        "sr": {"R1": p.r1, "S1": p.s1},
    }
    return (
        "Tu es un moteur de décision de trading.\n"
        "Retourne UNIQUEMENT un JSON strictement valide avec les clés:\n"
        '  {"decision": "BUY|SELL|IGNORE", "confidence": 0..1, "reason": "français"}\n\n'
        f"Contexte JSON:\n{json.dumps(body, ensure_ascii=False)}\n\n"
        "Règles:\n"
        "- BUY si direction_raw == LONG et le contexte est favorable.\n"
        "- SELL si direction_raw == SHORT et le contexte est favorable.\n"
        "- Sinon IGNORE.\n"
        "- Réponse = JSON UNIQUEMENT (pas de texte en dehors du JSON)."
    )

def _safe_json_parse(txt: str) -> Dict[str, Any]:
    try:
        return json.loads(txt)
    except Exception:
        start = txt.find("{")
        end = txt.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(txt[start:end+1])
            except Exception:
                pass
    return {}

def _normalize_llm_result(data: Dict[str, Any]) -> Dict[str, Any]:
    decision = data.get("decision")
    if isinstance(decision, str):
        decision = decision.upper()
    else:
        decision = None

    conf = data.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except Exception:
        conf = None
    if conf is not None:
        conf = max(0.0, min(1.0, conf))

    reason = data.get("reason")
    if reason is not None:
        reason = str(reason)

    if decision not in ("BUY", "SELL", "IGNORE"):
        decision = "IGNORE"

    return {"decision": decision, "confidence": conf, "reason": reason}

async def _llm_via_responses(prompt: str) -> Optional[Dict[str, Any]]:
    # API "responses" (nouvelle); peut ne pas être dispo selon version SDK
    r = _openai_client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": "Tu es un moteur de décision qui NE renvoie que du JSON strict."},
            {"role": "user", "content": prompt},
        ],
        max_output_tokens=200,
        temperature=0.0,
    )
    txt = getattr(r, "output_text", None)
    if not txt:
        # fallback extraction
        try:
            txt = r.output[0].content[0].text  # type: ignore
        except Exception:
            txt = str(r)
    data = _safe_json_parse((txt or "").strip())
    return _normalize_llm_result(data)

async def _llm_via_chat(prompt: str) -> Optional[Dict[str, Any]]:
    # API "chat.completions" (très compatible)
    r = _openai_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "Tu es un moteur de décision qui NE renvoie que du JSON strict."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=200,
        temperature=0.0,
    )
    txt = r.choices[0].message.content if r and r.choices else None
    data = _safe_json_parse((txt or "").strip())
    return _normalize_llm_result(data)

async def call_llm_for_entry(p: TVPayload) -> Dict[str, Any]:
    if not (LLM_ENABLED and _openai_client):
        print("[LLM] disabled or no API key.")
        return {"decision": None, "confidence": None, "reason": None, "llm_used": False}

    prompt = _build_llm_prompt(p)
    try:
        result = None
        mode_used = None
        if _llm_mode in ("auto", "responses"):
            try:
                result = await _llm_via_responses(prompt)
                mode_used = "responses"
            except Exception as e:
                print("[LLM] responses mode failed, fallback to chat:", e)
        if result is None:
            result = await _llm_via_chat(prompt)
            mode_used = "chat"

        if result is None:
            print("[LLM] parsing failed, no JSON.")
            return {"decision": None, "confidence": None, "reason": None, "llm_used": True, "mode": mode_used}

        return {**result, "llm_used": True, "mode": mode_used}
    except Exception as e:
        print("[LLM] call error:", e)
        return {"decision": None, "confidence": None, "reason": None, "llm_used": False, "error": str(e)}

# ============== RECORDING (Dashboard) ==============
def _push_trade(row: Dict[str, Any]) -> None:
    TRADES.append(row)
    if len(TRADES) > MAX_TRADES:
        del TRADES[: len(TRADES) - MAX_TRADES]

def _basic_stats() -> Dict[str, Any]:
    entries = [t for t in TRADES if t.get("event") == "ENTRY"]
    tp_hits = [t for t in TRADES if t.get("event") in ("TP1_HIT", "TP2_HIT", "TP3_HIT")]
    sl_hits = [t for t in TRADES if t.get("event") == "SL_HIT"]
    wins = len(tp_hits)
    losses = len(sl_hits)
    total = wins + losses
    winrate = (wins / total * 100.0) if total > 0 else 0.0
    return {
        "entries": len(entries),
        "tp_hits": len(tp_hits),
        "sl_hits": len(sl_hits),
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "events_total": len(TRADES),
    }

# ============== ROUTES — STATUS ==============
@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home():
    env_rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("LLM_ENABLED", str(bool(LLM_ENABLED and _openai_client))),
        ("LLM_MODEL", LLM_MODEL if (LLM_ENABLED and _openai_client) else "-"),
        ("OPENAI_API_KEY", _mask(OPENAI_API_KEY)),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
        ("PORT", str(PORT)),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{k}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'><code>{v}</code></td></tr>"
        for k, v in env_rows
    )
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>AI Trader PRO — Status</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.5;margin:20px;color:#111}}
    .card{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:14px 0}}
    .btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111;margin-right:8px}}
    table{{border-collapse:collapse;width:100%;font-size:14px}}
    code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
  </style>
</head>
<body>
  <h1>AI Trader PRO — Status</h1>
  <div class="card">
    <b>Environnement</b>
    <table>{rows_html}</table>
    <div style="margin-top:10px">
      <a class="btn" href="/env-sanity">/env-sanity</a>
      <a class="btn" href="/tg-health">/tg-health</a>
      <a class="btn" href="/openai-health">/openai-health</a>
      <a class="btn" href="/trades">/trades</a>
    </div>
  </div>
  <div class="card">
    <b>Webhooks</b>
    <div>POST <code>/tv-webhook</code> (JSON TradingView)</div>
  </div>
</body>
</html>
"""

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "LLM_ENABLED": bool(LLM_ENABLED and _openai_client),
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    await send_telegram("✅ Test Telegram: ça fonctionne.")
    return {"ok": True, "info": "Message Telegram envoyé (si BOT + CHAT_ID configurés)."}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": False, "why": "LLM off or API key missing"}
    try:
        # Essai Responses sinon Chat
        try:
            out = _openai_client.responses.create(
                model=LLM_MODEL,
                input=[{"role": "user", "content": "ping"}],
                max_output_tokens=5,
            )
            sample = getattr(out, "output_text", None) or str(out)
            mode = "responses"
        except Exception:
            out = _openai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            sample = out.choices[0].message.content if out and out.choices else "ok"
            mode = "chat"
        return {"ok": True, "model": LLM_MODEL, "mode": mode, "sample": (sample or "")[:120]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)

# ============== ROUTE — TRADES DASHBOARD ==============
@app.get("/trades")
def trades(format: Optional[str] = Query(None), secret: Optional[str] = Query(None)):
    if format == "json":
        return {"stats": _basic_stats(), "trades": TRADES}

    stats = _basic_stats()
    rows = []
    for t in sorted(TRADES, key=lambda x: x.get("time", 0), reverse=True)[:500]:
        conf = t.get("confidence")
        conf_txt = "-" if conf is None else f"{float(conf)*100:.0f}%"
        rsn = t.get("reason") or "-"
        price_line = ""
        if t["event"] in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
            price_line = f"<div>Prix touché: <b>{_fmt_num(t.get('entry'))}</b> • Cible: <b>{_fmt_num(t.get('target_price'))}</b></div>"
        line = f"""
<tr>
  <td style="padding:6px;border-bottom:1px solid #eee">{t.get('time')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee"><code>{t.get('event')}</code></td>
  <td style="padding:6px;border-bottom:1px solid #eee">{t.get('symbol')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{t.get('tf')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{t.get('side','-')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{_fmt_num(t.get('entry'))}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{t.get('trade_id','-')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">
    <div>SL: <b>{_fmt_num(t.get('sl'))}</b> | TP1: <b>{_fmt_num(t.get('tp1'))}</b> | TP2: <b>{_fmt_num(t.get('tp2'))}</b> | TP3: <b>{_fmt_num(t.get('tp3'))}</b></div>
    <div>R1: <b>{_fmt_num(t.get('r1'))}</b> • S1: <b>{_fmt_num(t.get('s1'))}</b></div>
    {price_line}
    <div>LLM: <b>{t.get('decision','-')}</b> • Confiance: <b>{conf_txt}</b></div>
    <div>Raison: {rsn}</div>
  </td>
</tr>
"""
        rows.append(line)

    clear_btn = ""
    if WEBHOOK_SECRET:
        clear_btn = f'<a class="btn" href="/trades/clear?secret={WEBHOOK_SECRET}">Vider l’historique</a>'

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>Trades — AI Trader PRO</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;line-height:1.5;margin:20px;color:#111}}
.card{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:14px 0}}
.btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111;margin-right:8px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
th,td{{vertical-align:top}}
</style>
</head>
<body>
  <h1>Trades — AI Trader PRO</h1>

  <div class="card">
    <b>Stats</b>
    <div>Entrées: <b>{stats['entries']}</b> • TP hits: <b>{stats['tp_hits']}</b> • SL hits: <b>{stats['sl_hits']}</b></div>
    <div>Wins: <b>{stats['wins']}</b> • Losses: <b>{stats['losses']}</b> • Winrate: <b>{stats['winrate_pct']}%</b></div>
    <div>Événements total: <b>{stats['events_total']}</b></div>
    <div style="margin-top:8px">
      <a class="btn" href="/trades?format=json">JSON</a>
      <a class="btn" href="/">Status</a>
      {clear_btn}
    </div>
  </div>

  <div class="card">
    <b>Derniers événements</b>
    <table>
      <thead>
        <tr>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Time</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Event</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Symbol</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">TF</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Side</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Entry</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Trade ID</th>
          <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Details</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) if rows else '<tr><td colspan="8" style="padding:10px">Aucun évènement encore.</td></tr>'}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=200)

@app.get("/trades/clear")
def trades_clear(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    TRADES.clear()
    return {"ok": True, "cleared": True}

# ============== ROUTE — WEBHOOK ==============
@app.post("/tv-webhook")
async def tv_webhook(payload: TVPayload, x_render_signature: Optional[str] = Header(None)):
    # Secret check
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    t = (payload.type or payload.tag or "").upper()
    header_emoji = "🟩" if (payload.side or "").upper() == "LONG" else ("🟥" if (payload.side or "").upper() == "SHORT" else "▫️")
    trade_id_txt = f" • ID: <code>{payload.trade_id}</code>" if payload.trade_id else ""

    # LLM pour ENTRY si manquant
    llm_out: Dict[str, Any] = {"decision": payload.decision, "confidence": payload.confidence, "reason": payload.reason}
    if t == "ENTRY" and (payload.decision is None or payload.confidence is None or payload.reason is None):
        llm_out = await call_llm_for_entry(payload)
        print(f"[LLM] used={llm_out.get('llm_used')} mode={llm_out.get('mode')} decision={llm_out.get('decision')} conf={llm_out.get('confidence')} err={llm_out.get('error', '')}")

    # Filtrage décision si besoin (mais on affiche toujours la vraie confiance)
    dec = (llm_out.get("decision") or "—")
    conf_val = llm_out.get("confidence")
    rsn = llm_out.get("reason") or "-"
    conf_pct_txt = "—" if conf_val is None else f"{float(conf_val)*100:.0f}%"

    # -------- Telegram + enregistrement --------
    if t == "ENTRY":
        # Appliquer le filtre de décision si conf < CONFIDENCE_MIN (affichage inchangé)
        final_decision = dec
        if isinstance(conf_val, (int, float)) and conf_val < (CONFIDENCE_MIN / 100.0):
            final_decision = "IGNORE"

        msg = (
            f"{header_emoji} <b>ALERTE</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{(payload.side or '—').upper()}</b> | Entry: <b>{_fmt_num(payload.entry)}</b>\n"
            f"🎯 SL: <b>{_fmt_num(payload.sl)}</b> | "
            f"TP1: <b>{_fmt_num(payload.tp1)}</b> | "
            f"TP2: <b>{_fmt_num(payload.tp2)}</b> | "
            f"TP3: <b>{_fmt_num(payload.tp3)}</b>\n"
            f"R1: <b>{_fmt_num(payload.r1)}</b>  •  S1: <b>{_fmt_num(payload.s1)}</b>\n"
            f"🤖 LLM: <b>{final_decision}</b>  | <b>Niveau de confiance: {conf_pct_txt}</b>\n"
            f"📝 Raison: {rsn}"
        )
        await send_telegram(msg)

        _push_trade({
            "event": "ENTRY",
            "time": payload.time,
            "symbol": payload.symbol,
            "tf": payload.tf,
            "side": (payload.side or "").upper(),
            "entry": payload.entry,
            "sl": payload.sl, "tp1": payload.tp1, "tp2": payload.tp2, "tp3": payload.tp3,
            "r1": payload.r1, "s1": payload.s1,
            "trade_id": payload.trade_id,
            "decision": final_decision, "confidence": conf_val, "reason": rsn,
        })

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)

        hit_price = payload.entry if payload.entry is not None else payload.dict().get("close")
        target_price = payload.tp
        if target_price is None:
            if t == "TP1_HIT":
                target_price = payload.tp1
            elif t == "TP2_HIT":
                target_price = payload.tp2
            elif t == "TP3_HIT":
                target_price = payload.tp3
            elif t == "SL_HIT":
                target_price = payload.sl

        msg = (
            f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Prix touché: <b>{_fmt_num(hit_price)}</b> • Cible: <b>{_fmt_num(target_price)}</b>"
        )
        await send_telegram(msg)

        _push_trade({
            "event": t,
            "time": payload.time,
            "symbol": payload.symbol,
            "tf": payload.tf,
            "side": (payload.side or "").upper() if payload.side else None,
            "entry": hit_price,
            "target_price": target_price,
            "trade_id": payload.trade_id,
            "decision": None, "confidence": None, "reason": None,
        })

    elif t == "TRADE_TERMINATED":
        reason = (payload.term_reason or "").upper()
        if reason == "TP3_HIT":
            title = "TRADE TERMINÉ — TP3 ATTEINT"
        elif reason in ("REVERSAL", "INVALIDATED"):
            title = "TRADE INVALIDÉ — VEUILLEZ FERMER!"
        elif reason == "SL_HIT":
            title = "TRADE TERMINÉ — SL ATTEINT"
        else:
            title = "TRADE TERMINÉ — VEUILLEZ FERMER"

        msg = (
            f"⏹ <b>{title}</b>\n"
            f"Instrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{trade_id_txt}"
        )
        await send_telegram(msg)

        _push_trade({
            "event": "TRADE_TERMINATED",
            "time": payload.time,
            "symbol": payload.symbol,
            "tf": payload.tf,
            "side": (payload.side or "").upper() if payload.side else None,
            "entry": payload.entry,
            "trade_id": payload.trade_id,
            "term_reason": reason,
            "decision": None, "confidence": None, "reason": None,
        })

    else:
        print("[tv-webhook] type non géré:", t)

    return JSONResponse({
        "ok": True,
        "event": t,
        "received": payload.dict(),
        "llm": {
            "enabled": bool(LLM_ENABLED and _openai_client),
            "decision": llm_out.get("decision"),
            "confidence": llm_out.get("confidence"),
            "reason": llm_out.get("reason"),
        },
        "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "stats": _basic_stats(),
    })
