# main.py
import os
import json
import asyncio
from typing import Optional, Union, Dict, Any, List, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

# ============== LLM (OpenAI) ==============
LLM_ENABLED = os.getenv("LLM_ENABLED", "1") not in ("0", "false", "False", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

_openai_client = None
_llm_reason_down = None
if LLM_ENABLED and OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        _openai_client = None
        _llm_reason_down = f"import_error: {e}"
else:
    if not LLM_ENABLED:
        _llm_reason_down = "disabled_by_env"
    elif not OPENAI_API_KEY:
        _llm_reason_down = "missing_api_key"

# ============== ENV (Webhook & Telegram) ==============
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT               = int(os.getenv("PORT", "8000"))
CONFIDENCE_MIN     = float(os.getenv("CONFIDENCE_MIN", "0.0"))  # ex: 0.85

# >>> PATCH (minime) — Bouton Telegram vers le dashboard
TG_BUTTONS       = os.getenv("TG_BUTTONS", "0") not in ("0", "false", "False", "")
TG_DASHBOARD_URL = os.getenv("TG_DASHBOARD_URL", "")
TG_BUTTON_TEXT   = os.getenv("TG_BUTTON_TEXT", "📊 Ouvrir le Dashboard")
# <<< PATCH

# ============== APP ==============
app = FastAPI(title="AI Trader PRO - Webhook", version="3.5.0")

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
        return f"{xf:.8f}" if xf < 1 else f"{xf:.4f}"
    except Exception:
        return str(x)

def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "-"
    try:
        return f"{x*100:.2f}%"
    except Exception:
        return "-"

# >>> PATCH — ajoute inline buttons + timeout plus court et non bloquant possible
async def send_telegram(text: str, inline_url: Optional[str] = None, inline_text: Optional[str] = None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}

    # Ajoute un bouton seulement si activé ET URL fournie
    if TG_BUTTONS and (inline_url or TG_DASHBOARD_URL):
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": inline_text or TG_BUTTON_TEXT, "url": inline_url or TG_DASHBOARD_URL}]]
        }

    timeout = httpx.Timeout(5.0, connect=3.0, read=5.0, write=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            pass
# <<< PATCH

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
        "Retourne UNIQUEMENT un JSON valide avec les clés EXACTES:\n"
        '  {"decision":"BUY|SELL|IGNORE","confidence":0..1,"reason":"français"}\n\n'
        f"Contexte JSON:\n{json.dumps(body, ensure_ascii=False)}\n\n"
        "Règles:\n"
        "- BUY si direction_raw == LONG et le contexte est favorable.\n"
        "- SELL si direction_raw == SHORT et le contexte est favorable.\n"
        "- Sinon IGNORE.\n"
        "- Réponse = JSON UNIQUEMENT."
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

async def call_llm_for_entry(p: TVPayload) -> Dict[str, Any]:
    if not (LLM_ENABLED and _openai_client):
        return {
            "decision": None, "confidence": None, "reason": None,
            "llm_used": False, "why": _llm_reason_down or "unknown"
        }
    prompt = _build_llm_prompt(p)
    try:
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Tu es un moteur de décision et tu NE renvoies que du JSON valide."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=250,
        )
        txt = comp.choices[0].message.content if comp and comp.choices else ""
    except Exception as e_chat:
        try:
            r = _openai_client.responses.create(
                model=LLM_MODEL,
                input=[
                    {"role": "system", "content": "Tu es un moteur de décision et tu NE renvoies que du JSON valide."},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=250,
                temperature=0,
                response_format={"type": "json_object"},
            )
            txt = getattr(r, "output_text", None) or str(r)
        except Exception as e_resp:
            return {
                "decision": None, "confidence": None, "reason": None,
                "llm_used": False, "why": f"chat_error={e_chat} | responses_error={e_resp}"
            }

    data = _safe_json_parse((txt or "").strip())
    decision = str(data.get("decision", "")).upper() if isinstance(data.get("decision"), str) else None
    confidence = None
    if isinstance(data.get("confidence"), (int, float, str)):
        try:
            confidence = float(data["confidence"])
            confidence = max(0.0, min(1.0, confidence))
        except Exception:
            confidence = None
    reason = str(data.get("reason")) if data.get("reason") is not None else None

    if decision not in ("BUY", "SELL", "IGNORE"):
        decision = "IGNORE"

    return {"decision": decision, "confidence": confidence, "reason": reason, "llm_used": True}

# ============== RECORDING (Dashboard basique) ==============
def _push_trade(row: Dict[str, Any]) -> None:
    TRADES.append(row)
    if len(TRADES) > MAX_TRADES:
        del TRADES[: len(TRADES) - MAX_TRADES]

def _basic_stats() -> Dict[str, Any]:
    entries = [t for t in TRADES if t.get("event") == "ENTRY"]
    tp_hits = [t for t in TRADES if t.get("event") in ("TP1_HIT", "TP2_HIT", "TP3_HIT")]
    sl_hits = [t for t in TRADES if t.get("event") == "SL_HIT"]
    wins = len(tp_hits)  # ATTENTION: compte les TP events, pas les trades
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

# ============== NEW: Agrégation par trade_id (vrai winrate) ==============
_TERMINAL = {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"}

def _group_trades_by_id() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for ev in TRADES:
        tid = ev.get("trade_id") or ""
        if not tid:
            continue
        g = groups.get(tid)
        if g is None:
            g = {
                "trade_id": tid,
                "symbol": ev.get("symbol"),
                "tf": ev.get("tf"),
                "side": ev.get("side"),
                "entry": None,
                "sl": None, "tp1": None, "tp2": None, "tp3": None,
                "entry_time": None,
                "events": [],  # (time, event, price, target_price)
            }
            groups[tid] = g

        if ev.get("event") == "ENTRY":
            g["entry"] = ev.get("entry")
            g["sl"] = ev.get("sl")
            g["tp1"] = ev.get("tp1")
            g["tp2"] = ev.get("tp2")
            g["tp3"] = ev.get("tp3")
            g["entry_time"] = ev.get("time")

        if ev.get("event") in _TERMINAL:
            g["events"].append((
                ev.get("time"),
                ev.get("event"),
                ev.get("entry"),
                ev.get("target_price"),
            ))

    summary_rows: List[Dict[str, Any]] = []
    wins = losses = open_trades = 0

    for tid, g in groups.items():
        evs = sorted(g["events"], key=lambda x: (x[0] or 0))
        status = "OPEN"
        first_hit = None
        if evs:
            first_hit = evs[0]
            e = first_hit[1]
            if e.startswith("TP"):
                status = "WIN"; wins += 1
            elif e == "SL_HIT":
                status = "LOSS"; losses += 1
        else:
            open_trades += 1

        summary_rows.append({
            "trade_id": tid,
            "symbol": g["symbol"],
            "tf": g["tf"],
            "side": g["side"],
            "entry": g["entry"],
            "sl": g["sl"],
            "tp1": g["tp1"],
            "tp2": g["tp2"],
            "tp3": g["tp3"],
            "entry_time": g["entry_time"],
            "status": status,
            "first_hit_event": first_hit[1] if first_hit else None,
            "first_hit_price": first_hit[2] if first_hit else None,
            "first_hit_target": first_hit[3] if first_hit else None,
        })

    closed = wins + losses
    true_wr = (wins / closed) if closed > 0 else None

    agg_stats = {
        "trades_total": len(summary_rows),
        "closed": closed,
        "open": open_trades,
        "wins": wins,
        "losses": losses,
        "true_winrate": true_wr,
        "true_winrate_pct": round((true_wr * 100.0), 2) if true_wr is not None else None,
    }
    return summary_rows, agg_stats

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
        ("LLM_ENABLED", str(bool(LLM_ENABLED))),
        ("LLM_CLIENT_READY", str(bool(_openai_client is not None))),
        ("LLM_DOWN_REASON", _llm_reason_down or "-"),
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
        "LLM_ENABLED": bool(LLM_ENABLED),
        "LLM_CLIENT_READY": bool(_openai_client is not None),
        "LLM_DOWN_REASON": _llm_reason_down,
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
    }

@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    # Non bloquant
    asyncio.create_task(send_telegram("✅ Test Telegram: ça fonctionne."))
    return {"ok": True, "info": "Message Telegram envoyé (si BOT + CHAT_ID configurés)."}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        comp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample[:120]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)

# ============== ROUTE — TRADES DASHBOARD ==============
@app.get("/trades")
def trades(format: Optional[str] = Query(None), secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    if format == "json":
        groups, gstats = _group_trades_by_id()
        return {"stats_events": _basic_stats(), "stats_trades": gstats, "trades_raw": TRADES, "groups": groups}

    stats = _basic_stats()
    groups, gstats = _group_trades_by_id()

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

    grows = []
    for g in sorted(groups, key=lambda x: (x.get("entry_time") or 0), reverse=True)[:300]:
        grows.append(f"""
<tr>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('entry_time')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('symbol')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('tf')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('side') or '-'}</td>
  <td style="padding:6px;border-bottom:1px solid #eee"><code>{g.get('trade_id')}</code></td>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('status')}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{g.get('first_hit_event') or '-'}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{_fmt_num(g.get('entry'))}</td>
  <td style="padding:6px;border-bottom:1px solid #eee">{_fmt_num(g.get('first_hit_price'))}</td>
</tr>
""")

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
.btn{{display:inline-block;padding:8px 12px;border-radius:8px;border:1px solid #e5e7eb;text-decoration:none;color:#111}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
code{{background:#f9fafb;padding:2px 4px;border-radius:6px}}
th,td{{vertical-align:top}}
</style>
</head>
<body>
  <h1>Trades — AI Trader PRO</h1>

  <div class="card">
    <b>Stats (par événements)</b>
    <div>Entrées: <b>{stats['entries']}</b> • TP hits: <b>{stats['tp_hits']}</b> • SL hits: <b>{stats['sl_hits']}</b></div>
    <div>Wins (par évènement TP): <b>{stats['wins']}</b> • Losses (SL): <b>{stats['losses']}</b> • Winrate: <b>{stats['winrate_pct']}%</b></div>
    <div>Événements total: <b>{stats['events_total']}</b></div>
    <div style="margin-top:8px">
      <a class="btn" href="/trades?format=json">JSON</a>
      <a class="btn" href="/">Status</a>
      {clear_btn}
    </div>
  </div>

  <div class="card">
    <b>Résultats par trade (groupés par <code>trade_id</code>)</b>
    <div>Trades total: <b>{gstats['trades_total']}</b> • Fermés: <b>{gstats['closed']}</b> • Ouverts: <b>{gstats['open']}</b></div>
    <div>Gagnants: <b>{gstats['wins']}</b> • Perdants: <b>{gstats['losses']}</b> • <u>True Winrate</u>: <b>{_fmt_pct(gstats['true_winrate'])}</b></div>
    <div style="margin-top:10px;max-height:60vh;overflow:auto;border:1px solid #eee;border-radius:12px">
      <table>
        <thead>
          <tr>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Entry time</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Symbol</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">TF</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Side</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Trade ID</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Status</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">1er hit</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Entry</th>
            <th style="text-align:left;padding:6px;border-bottom:1px solid #ddd">Hit Price</th>
          </tr>
        </thead>
        <tbody>
          {''.join(grows) if grows else '<tr><td colspan="9" style="padding:10px">Aucun trade groupé (encore).</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <b>Derniers événements (log brut)</b>
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
    llm_out: Dict[str, Any] = {"decision": payload.decision, "confidence": payload.confidence, "reason": payload.reason, "llm_used": False}
    if t == "ENTRY" and (payload.decision is None or payload.confidence is None or payload.reason is None):
        llm_out = await call_llm_for_entry(payload)

    def _conf_ok(cv: Optional[float]) -> bool:
        return (cv is not None) and (cv >= CONFIDENCE_MIN)

    def _dir_ok(side: Optional[str], decision: Optional[str]) -> bool:
        s = (side or "").upper()
        d = (decision or "").upper()
        return (s == "LONG" and d == "BUY") or (s == "SHORT" and d == "SELL")

    if t == "ENTRY":
        dec = (llm_out.get("decision") or "—")
        conf_val = llm_out.get("confidence")
        conf_pct = "—" if conf_val is None else f"{float(conf_val)*100:.0f}%"
        rsn = llm_out.get("reason") or "-"
        llm_note = ""
        if not llm_out.get("llm_used", False):
            why = llm_out.get("why") or _llm_reason_down or "-"
            llm_note = f"\n⚠️ <i>LLM indisponible</i> (<code>{why}</code>)"

        if (not _conf_ok(conf_val)) or (not _dir_ok(payload.side, dec)):
            reason_filter = []
            if not _conf_ok(conf_val):
                reason_filter.append(f"Confiance {'n/a' if conf_val is None else f'{conf_val*100:.0f}%'} < seuil {CONFIDENCE_MIN*100:.0f}%")
            if not _dir_ok(payload.side, dec):
                reason_filter.append(f"Incohérence directionnelle (side={ (payload.side or '—').upper() } / decision={ (dec or '—').upper() })")
            return JSONResponse({
                "ok": True,
                "event": "ENTRY_FILTERED",
                "filter": {"reasons": reason_filter, "threshold": CONFIDENCE_MIN},
                "received": payload.dict(),
                "llm": {
                    "enabled": bool(LLM_ENABLED),
                    "client_ready": bool(_openai_client is not None),
                    "down_reason": _llm_reason_down,
                    "decision": llm_out.get("decision"),
                    "confidence": llm_out.get("confidence"),
                    "reason": llm_out.get("reason"),
                },
                "sent_to_telegram": False,
                "stats": _basic_stats(),
            })

        msg = (
            f"{header_emoji} <b>ALERTE!!!</b> • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{trade_id_txt}\n"
            f"Direction: <b>{(payload.side or '—').upper()}</b> | Entry: <b>{_fmt_num(payload.entry)}</b>\n"
            f"TP1: <b>{_fmt_num(payload.tp1)}</b> | TP2: <b>{_fmt_num(payload.tp2)}</b> | TP3: <b>{_fmt_num(payload.tp3)}</b>\n"
            f"🎯 SL: <b>{_fmt_num(payload.sl)}</b> | Première Résistance: <b>{_fmt_num(payload.r1)}</b>  •  Premier Support: <b>{_fmt_num(payload.s1)}</b>\n"
            f"🤖 LLM: <b>{dec}</b>  | <b>Niveau de confiance: {conf_pct}</b> (seuil {int(CONFIDENCE_MIN*100)}%)\n"
            f"📝 Raison: {rsn}{llm_note}"
        )
        # >>> PATCH: non-bloquant
        asyncio.create_task(send_telegram(msg, inline_url=TG_DASHBOARD_URL, inline_text=TG_BUTTON_TEXT))

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
            "decision": dec, "confidence": conf_val, "reason": rsn,
        })

    elif t in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"):
        nice = {
            "TP1_HIT": "🎯 TP1 touché",
            "TP2_HIT": "🎯 TP2 touché",
            "TP3_HIT": "🎯 TP3 touché FELICITATION",
            "SL_HIT":  "✖️ SL touché",
        }.get(t, t)

        hit_price = payload.entry if payload.entry is not None else payload.dict().get("close")
        target_price = payload.tp
        if target_price is None:
            if t == "TP1_HIT":   target_price = payload.tp1
            elif t == "TP2_HIT": target_price = payload.tp2
            elif t == "TP3_HIT": target_price = payload.tp3
            elif t == "SL_HIT":  target_price = payload.sl

        msg = f"{nice} • <b>{payload.symbol}</b> • <b>{payload.tf}</b>{(' • ID: <code>'+payload.trade_id+'</code>') if payload.trade_id else ''}\n" \
              f"Prix touché: <b>{_fmt_num(hit_price)}</b> • Cible: <b>{_fmt_num(target_price)}</b>"
        # >>> PATCH: non-bloquant
        asyncio.create_task(send_telegram(msg, inline_url=TG_DASHBOARD_URL, inline_text=TG_BUTTON_TEXT))

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
        if reason == "TP3_HIT":            title = "TRADE TERMINÉ — TP3 ATTEINT FELICITATION"
        elif reason in ("REVERSAL","INVALIDATED"): title = "TRADE INVALIDÉ — VEUILLEZ FERMER! MERCI"
        elif reason == "SL_HIT":           title = "TRADE TERMINÉ — SL ATTEINT DESOLE"
        else:                              title = "TRADE TERMINÉ — VEUILLEZ FERMER"

        msg = f"⏹ <b>{title}</b>\n" \
              f"Instrument: <b>{payload.symbol}</b> • TF: <b>{payload.tf}</b>{(' • ID: <code>'+payload.trade_id+'</code>') if payload.trade_id else ''}"
        # >>> PATCH: non-bloquant
        asyncio.create_task(send_telegram(msg, inline_url=TG_DASHBOARD_URL, inline_text=TG_BUTTON_TEXT))

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

    groups, gstats = _group_trades_by_id()
    return JSONResponse({
        "ok": True,
        "event": t,
        "received": payload.dict(),
        "llm": {
            "enabled": bool(LLM_ENABLED),
            "client_ready": bool(_openai_client is not None),
            "down_reason": _llm_reason_down,
            "decision": llm_out.get("decision"),
            "confidence": llm_out.get("confidence"),
            "reason": llm_out.get("reason"),
        },
        "sent_to_telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "stats_events": _basic_stats(),
        "stats_trades": gstats,
    })
