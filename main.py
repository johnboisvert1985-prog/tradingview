# =========================
# Section 1/6 — Imports, Config, Logging
# =========================
import os, re, json, time, sqlite3, logging, threading
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_ENABLED = os.getenv("LLM_ENABLED", "0") in ("1", "true", "True")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
FORCE_LLM = os.getenv("FORCE_LLM", "0") in ("1", "true", "True")
CONFIDENCE_MIN = float(os.getenv("CONFIDENCE_MIN", "0.0") or 0.0)

PORT = int(os.getenv("PORT", "8000"))
RISK_ACCOUNT_BAL = float(os.getenv("RISK_ACCOUNT_BAL", "0") or 0)
RISK_PCT = float(os.getenv("RISK_PCT", "1.0") or 1.0)

DB_PATH = os.getenv("DB_PATH", "data/data.db")
DEBUG_MODE = os.getenv("DEBUG", "0") in ("1", "true", "True")

ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1", "true", "True")
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

def _alt_cache_file_path() -> str:
    return os.getenv("ALT_CACHE_FILE", "/tmp/altseason_last.json")

def _load_last_snapshot() -> Optional[Dict[str, Any]]:
    try:
        p = _alt_cache_file_path()
        if not os.path.exists(p): return None
        with open(p, "r", encoding="utf-8") as f:
            snap = json.load(f)
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None

def _save_last_snapshot(snap: Dict[str, Any]) -> None:
    try:
        p = _alt_cache_file_path()
        d = os.path.dirname(p) or "/tmp"
        os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    except Exception:
        pass

TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0
# =========================
# Section 2/6 — OpenAI client + LLM scorer optionnel
# =========================
_openai_client = None
_llm_reason_down = None
if LLM_ENABLED and OPENAI_API_KEY:
    try:
        from openai import OpenAI  # type: ignore
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        _llm_reason_down = f"OpenAI client init failed: {e}"
else:
    _llm_reason_down = "LLM disabled or OPENAI_API_KEY missing"

def tf_label_of(payload: Dict[str, Any]) -> str:
    label = str(payload.get("tf_label") or payload.get("tf") or "?")
    try:
        if label.isdigit():
            n = int(label)
            if n < 60: return f"{n}m"
            if n % 60 == 0 and n < 1440: return f"{n//60}h"
            if n == 1440: return "1D"
    except Exception:
        pass
    return label

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def llm_confidence_for_entry(payload: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    if not (LLM_ENABLED and _openai_client):
        return None
    try:
        sym = str(payload.get("symbol") or "?")
        side = str(payload.get("side") or "?").upper()
        tf   = tf_label_of(payload)
        entry = _to_float(payload.get("entry"))
        sl    = _to_float(payload.get("sl"))
        tp1   = _to_float(payload.get("tp1"))
        tp2   = _to_float(payload.get("tp2"))
        tp3   = _to_float(payload.get("tp3"))

        sys_prompt = (
            "Tu es un assistant de trading. Donne une estimation de confiance entre 0 et 100 "
            "pour la probabilité d'atteindre au moins TP1 avant SL, basée uniquement sur Entry/SL/TPs. "
            "Réponds STRICTEMENT en JSON: {\"confidence_pct\": <0-100>, \"rationale\": \"<raison courte>\"}."
        )
        user_prompt = (
            f"Trade: {sym} | TF={tf} | Side={side}\n"
            f"Entry={entry} | SL={sl} | TP1={tp1} | TP2={tp2} | TP3={tp3}\n"
            "Contraintes: pas d'accès marché. Utilise heuristiques R:R (distance SL/TP1, etc.)."
        )

        resp = _openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": user_prompt}],
            max_tokens=120, temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        import re as _re, json as _json
        m = _re.search(r"\{.*\}", content, _re.DOTALL)
        obj = _json.loads(m.group(0)) if m else _json.loads(content)

        conf = float(obj.get("confidence_pct"))
        rat  = str(obj.get("rationale") or "").strip()
        conf = max(0.0, min(100.0, conf))
        if len(rat) > 140: rat = rat[:137] + "..."
        return conf, rat
    except Exception as e:
        log.warning("LLM confidence failed: %s", e)
        return None
# =========================
# Section 3/6 — SQLite, Helpers, Templates communs
# =========================
def resolve_db_path() -> None:
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f: f.write("ok")
        os.remove(probe)
        log.info("DB dir OK: %s (using %s)", d, DB_PATH)
    except Exception as e:
        fallback_dir = "/tmp/ai_trader"
        os.makedirs(fallback_dir, exist_ok=True)
        DB_PATH = os.path.join(fallback_dir, "data.db")
        log.warning("DB dir '%s' not writable (%s). Falling back to %s", d, e, DB_PATH)
        resolve_db_path()

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def db_init() -> None:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                type TEXT, symbol TEXT, tf TEXT, side TEXT,
                entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
                trade_id TEXT, raw_json TEXT
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf ON events(tf)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

resolve_db_path(); db_init()

def save_event(payload: Dict[str, Any]) -> None:
    row = {
        "received_at": int(time.time()),
        "type": payload.get("type"),
        "symbol": payload.get("symbol"),
        "tf": str(payload.get("tf")) if payload.get("tf") is not None else None,
        "side": payload.get("side"),
        "entry": _to_float(payload.get("entry")),
        "sl": _to_float(payload.get("sl")),
        "tp1": _to_float(payload.get("tp1")),
        "tp2": _to_float(payload.get("tp2")),
        "tp3": _to_float(payload.get("tp3")),
        "trade_id": payload.get("trade_id"),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO events
            (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id, raw_json)
            VALUES (:received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :trade_id, :raw_json)
        """, row)
        conn.commit()
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             row["type"], row["symbol"], row["tf"], row["trade_id"])

def escape_html(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
              .replace('"',"&quot;").replace("'","&#39;"))

def fmt_num(v) -> str:
    try:
        if v is None: return ""
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "")

def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        if a is None or b is None or b == 0: return None
        return (a - b) / b * 100.0
    except Exception:
        return None

def parse_leverage_x(leverage: Optional[str]) -> Optional[float]:
    if not leverage: return None
    try:
        s = leverage.lower().replace("x"," ").split()
        for token in s:
            if token.replace(".","",1).isdigit():
                return float(token)
    except Exception:
        return None
    return None

class TradeOutcome:
    NONE="NONE"; TP1="TP1_HIT"; TP2="TP2_HIT"; TP3="TP3_HIT"; SL="SL_HIT"; CLOSE="CLOSE"

def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str: return None
    try:
        import datetime as dt
        y,m,d=map(int,date_str.split("-"))
        return int(dt.datetime(y,m,d,0,0,0).timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str: return None
    try:
        import datetime as dt
        y,m,d=map(int,date_str.split("-"))
        return int(dt.datetime(y,m,d,23,59,59).timestamp())
    except Exception:
        return None

def fetch_events_filtered(symbol: Optional[str], tf: Optional[str],
                          start_ep: Optional[int], end_ep: Optional[int],
                          limit: int=10000) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"; args: List[Any] = []
    if symbol: sql += " AND symbol = ?"; args.append(symbol)
    if tf: sql += " AND tf = ?"; args.append(tf)
    if start_ep is not None: sql += " AND received_at >= ?"; args.append(start_ep)
    if end_ep is not None: sql += " AND received_at <= ?"; args.append(end_ep)
    sql += " ORDER BY received_at ASC"
    if limit: sql += " LIMIT ?"; args.append(limit)
    with db_conn() as conn:
        cur = conn.cursor(); cur.execute(sql, tuple(args)); return cur.fetchall()

def build_trades_filtered(symbol: Optional[str], tf: Optional[str],
                          start_ep: Optional[int], end_ep: Optional[int],
                          max_rows:int=20000) -> Tuple[List[Dict[str,Any]], Dict[str,Any]]:
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)
    by_tid: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        tid = r["trade_id"] or f"noid:{r['symbol']}:{r['received_at']}"
        by_tid[tid].append(r)

    trades: List[Dict[str, Any]] = []
    total=wins=losses=0; hit_tp1=hit_tp2=hit_tp3=0
    times_to_outcome: List[int] = []
    win_streak=loss_streak=0; best_win_streak=0; worst_loss_streak=0

    for tid, items in by_tid.items():
        entry=None; outcome_type=TradeOutcome.NONE; outcome_time=None
        side=None; vsymbol=None; vtf=None
        e_entry=e_sl=e_tp1=e_tp2=e_tp3=None; entry_time=None
        for ev in items:
            etype=ev["type"]
            if etype=="ENTRY" and entry is None:
                entry=ev; vsymbol=ev["symbol"]; vtf=ev["tf"]; side=ev["side"]
                e_entry=ev["entry"]; e_sl=ev["sl"]; e_tp1=ev["tp1"]; e_tp2=ev["tp2"]; e_tp3=ev["tp3"]
                entry_time=ev["received_at"]
            elif entry is not None:
                if etype in ("TP3_HIT","TP2_HIT","TP1_HIT","SL_HIT","CLOSE") and outcome_type==TradeOutcome.NONE:
                    outcome_type=etype; outcome_time=ev["received_at"]
        if entry is not None:
            total += 1
            if outcome_time and entry_time:
                times_to_outcome.append(int(outcome_time - entry_time))
            is_win = outcome_type in (TradeOutcome.TP1,TradeOutcome.TP2,TradeOutcome.TP3)
            if is_win:
                wins += 1; win_streak += 1; best_win_streak=max(best_win_streak, win_streak); loss_streak=0
                if outcome_type==TradeOutcome.TP1: hit_tp1 += 1
                elif outcome_type==TradeOutcome.TP2: hit_tp2 += 1
                elif outcome_type==TradeOutcome.TP3: hit_tp3 += 1
            elif outcome_type==TradeOutcome.SL:
                losses += 1; loss_streak += 1; worst_loss_streak=max(worst_loss_streak, loss_streak); win_streak=0
            trades.append({
                "trade_id": tid, "symbol": vsymbol, "tf": vtf, "side": side,
                "entry": e_entry, "sl": e_sl, "tp1": e_tp1, "tp2": e_tp2, "tp3": e_tp3,
                "entry_time": entry_time, "outcome": outcome_type,
                "outcome_time": outcome_time,
                "duration_sec": (outcome_time - entry_time) if (outcome_time and entry_time) else None,
            })

    winrate = (wins/total*100.0) if total else 0.0
    avg_sec = int(sum(times_to_outcome)/len(times_to_outcome)) if times_to_outcome else 0
    summary = {
        "total_trades": total, "wins": wins, "losses": losses, "winrate_pct": round(winrate,2),
        "tp1_hits": hit_tp1, "tp2_hits": hit_tp2, "tp3_hits": hit_tp3,
        "avg_time_to_outcome_sec": avg_sec,
        "best_win_streak": best_win_streak, "worst_loss_streak": worst_loss_streak,
    }
    return trades, summary

INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 10px;border-bottom:1px solid #1f2937}th{color:#94a3b8}
.btn{display:inline-block;padding:8px 12px;border:1px solid #1f2937;color:#e5e7eb;text-decoration:none;border-radius:8px;margin-right:8px}
.chip{display:inline-block;padding:2px 8px;border:1px solid #1f2937;border-radius:999px;margin-right:8px;background:#0b1220}
.dot{display:inline-block;width:10px;height:10px;border-radius:10px;margin-left:8px}
.ok{background:#10b981}.warn{background:#fb923c}.muted{color:#94a3b8}
</style></head><body>
<h1>AI Trader PRO - Status</h1>
<div class="card">
  <h3 class="muted">Environment</h3>
  <table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>$rows_html</tbody></table>
  <div style="margin-top:8px">
    <a class="btn" href="/env-sanity">/env-sanity</a>
    <a class="btn" href="/tg-health">/tg-health</a>
    <a class="btn" href="/openai-health">/openai-health</a>
    <a class="btn" href="/trades">/trades</a>
    <a class="btn" href="/trades-admin">/trades-admin</a>
  </div>
</div>

<div class="card">
  <h3 class="muted">Webhook</h3>
  <div>POST <code>/tv-webhook</code> (JSON). Secret via query ?secret=... ou champ JSON "secret".</div>
  <div style="margin-top:8px"><span class="chip">ENTRY</span><span class="chip">TP1_HIT</span><span class="chip">TP2_HIT</span><span class="chip">TP3_HIT</span><span class="chip">SL_HIT</span><span class="chip">CLOSE</span></div>
</div>

<div class="card">
  <h3 class="muted">Altseason — État rapide</h3>
  <div id="alt-asof" class="muted">Loading…</div>
  <div>BTC Dominance: <span id="alt-btc">—</span> (thr &lt; $btc_thr) <span id="dot-btc" class="dot"></span></div>
  <div>ETH/BTC: <span id="alt-eth">—</span> (thr &gt; $eth_thr) <span id="dot-eth" class="dot"></span></div>
  <div>Altseason Index: <span id="alt-asi">N/A</span> (thr ≥ $asi_thr) <span id="dot-asi" class="dot"></span></div>
  <div>TOTAL2: <span id="alt-t2">—</span> (thr &gt; $t2_thr T$) <span id="dot-t2" class="dot"></span></div>
  <div style="margin-top:10px">
    <strong>Badges:</strong>
    <span id="alt3" class="chip">Prep 3/4: —</span>
    <span id="alt4" class="chip">Confirm 4/4: —</span>
  </div>
  <div class="muted" style="margin-top:6px">Séries (jours consécutifs): <span id="d3">0</span>d @3/4, <span id="d4">0</span>d @4/4</div>
</div>

<script>
(function(){
  function setText(id, txt){ const el = document.getElementById(id); if (el) el.textContent = txt; }
  function setDot(id, ok){ const el = document.getElementById(id); if (el) el.className = "dot " + (ok ? "ok" : "warn"); }
  function num(v){ return typeof v === "number" ? v : Number(v); }
  fetch("/altseason/check")
  .then(async r => { const t = await r.text(); if(!r.ok) throw new Error(t); return JSON.parse(t); })
  .then(s => {
    setText("alt-asof", "As of " + (s.asof || "now") + (s.stale ? " (cache)" : ""));
    const btc = num(s.btc_dominance), eth=num(s.eth_btc), t2=num(s.total2_usd), asi=s.altseason_index;
    setText("alt-btc", Number.isFinite(btc) ? btc.toFixed(2) + " %" : "—"); setDot("dot-btc", s.triggers && s.triggers.btc_dominance_ok);
    setText("alt-eth", Number.isFinite(eth) ? eth.toFixed(5) : "—"); setDot("dot-eth", s.triggers && s.triggers.eth_btc_ok);
    setText("alt-asi", (asi == null) ? "N/A" : String(asi)); setDot("dot-asi", s.triggers && s.triggers.altseason_index_ok);
    setText("alt-t2", Number.isFinite(t2) ? (t2/1e12).toFixed(2) + " T$" : "—"); setDot("dot-t2", s.triggers && s.triggers.total2_ok);
  })
  .catch(e => {
    setText("alt-asof", "Erreur: " + (e && e.message ? e.message : e));
    setDot("dot-btc", false); setDot("dot-eth", false); setDot("dot-asi", false); setDot("dot-t2", false);
  });
  fetch("/altseason/streaks")
    .then(r => r.json())
    .then(s => {
      const b3 = document.getElementById("alt3");
      const b4 = document.getElementById("alt4");
      if (b3) b3.textContent = (s.ALT3_ON ? "Prep 3/4: ON" : "Prep 3/4: OFF");
      if (b4) b4.textContent = (s.ALT4_ON ? "Confirm 4/4: ON" : "Confirm 4/4: OFF");
      const d3 = document.getElementById("d3");
      const d4 = document.getElementById("d4");
      if (d3) d3.textContent = String(s.consec_3of4_days || 0);
      if (d4) d4.textContent = String(s.consec_4of4_days || 0);
    })
    .catch(()=>{});
})();
</script>
</body></html>
""")
# =========================
# Section 4/6 — Altseason logic + endpoints
# =========================
app = FastAPI(title="AI Trader PRO")

@app.get("/ping")
def ping(): return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index():
    rows = [
        ("WEBHOOK_SECRET_set", str(bool(WEBHOOK_SECRET))),
        ("TELEGRAM_BOT_TOKEN_set", str(bool(TELEGRAM_BOT_TOKEN))),
        ("TELEGRAM_CHAT_ID_set", str(bool(TELEGRAM_CHAT_ID))),
        ("TELEGRAM_PIN_ALTSEASON", str(bool(TELEGRAM_PIN_ALTSEASON))),
        ("LLM_ENABLED", str(bool(LLM_ENABLED))),
        ("LLM_CLIENT_READY", str(bool(_openai_client is not None))),
        ("LLM_DOWN_REASON", _llm_reason_down or ""),
        ("LLM_MODEL", LLM_MODEL if (LLM_ENABLED and _openai_client) else ""),
        ("FORCE_LLM", str(bool(FORCE_LLM))),
        ("CONFIDENCE_MIN", str(CONFIDENCE_MIN)),
        ("PORT", str(PORT)),
        ("RISK_ACCOUNT_BAL", str(RISK_ACCOUNT_BAL)),
        ("RISK_PCT", str(RISK_PCT)),
        ("DB_PATH", DB_PATH),
        ("DEBUG", str(bool(DEBUG_MODE))),
        ("ALT_BTC_DOM_THR", str(ALT_BTC_DOM_THR)),
        ("ALT_ETH_BTC_THR", str(ALT_ETH_BTC_THR)),
        ("ALT_ASI_THR", str(ALT_ASI_THR)),
        ("ALT_TOTAL2_THR_T", str(ALT_TOTAL2_THR_T)),
        ("ALT_CACHE_TTL", str(ALT_CACHE_TTL)),
        ("ALT_GREENS_REQUIRED", str(ALT_GREENS_REQUIRED)),
        ("ALTSEASON_AUTONOTIFY", str(bool(ALTSEASON_AUTONOTIFY))),
        ("ALTSEASON_POLL_SECONDS", str(ALTSEASON_POLL_SECONDS)),
        ("ALTSEASON_NOTIFY_MIN_GAP_MIN", str(ALTSEASON_NOTIFY_MIN_GAP_MIN)),
    ]
    trs = "".join([f"<tr><td>{k}</td><td>{escape_html(v)}</td></tr>" for (k,v) in rows])
    html = INDEX_HTML_TPL.safe_substitute(
        rows_html=trs, btc_thr=str(int(ALT_BTC_DOM_THR)),
        eth_thr=f"{ALT_ETH_BTC_THR:.3f}", asi_thr=str(int(ALT_ASI_THR)),
        t2_thr=f"{ALT_TOTAL2_THR_T:.2f}"
    )
    return HTMLResponse(html)

@app.get("/env-sanity")
def env_sanity(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    return {
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "TELEGRAM_PIN_ALTSEASON": bool(TELEGRAM_PIN_ALTSEASON),
        "LLM_ENABLED": bool(LLM_ENABLED),
        "LLM_CLIENT_READY": bool(_openai_client is not None),
        "LLM_DOWN_REASON": _llm_reason_down,
        "LLM_MODEL": LLM_MODEL if (LLM_ENABLED and _openai_client) else None,
        "FORCE_LLM": bool(FORCE_LLM),
        "CONFIDENCE_MIN": CONFIDENCE_MIN,
        "PORT": PORT,
        "RISK_ACCOUNT_BAL": RISK_ACCOUNT_BAL,
        "RISK_PCT": RISK_PCT,
        "DB_PATH": DB_PATH,
        "DEBUG": DEBUG_MODE,
        "ALTSEASON": {
            "ALT_BTC_DOM_THR": ALT_BTC_DOM_THR,
            "ALT_ETH_BTC_THR": ALT_ETH_BTC_THR,
            "ALT_ASI_THR": ALT_ASI_THR,
            "ALT_TOTAL2_THR_T": ALT_TOTAL2_THR_T,
            "ALT_CACHE_TTL": ALT_CACHE_TTL,
            "ALT_GREENS_REQUIRED": ALT_GREENS_REQUIRED,
            "AUTONOTIFY": ALTSEASON_AUTONOTIFY,
            "POLL_SECONDS": ALTSEASON_POLL_SECONDS,
            "NOTIFY_MIN_GAP_MIN": ALTSEASON_NOTIFY_MIN_GAP_MIN,
        }
    }

@app.get("/tg-health")
def tg_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    ok = send_telegram("Test Telegram: OK")
    return {"ok": ok}

@app.get("/openai-health")
def openai_health(secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    if not (LLM_ENABLED and _openai_client):
        return {"ok": False, "enabled": bool(LLM_ENABLED), "client_ready": bool(_openai_client), "why": _llm_reason_down}
    try:
        comp = _openai_client.chat.completions.create(model=LLM_MODEL, messages=[{"role":"user","content":"ping"}], max_tokens=2)
        sample = comp.choices[0].message.content if comp and comp.choices else ""
        return {"ok": True, "model": LLM_MODEL, "sample": sample}
    except Exception as e:
        return {"ok": False, "error": str(e)}

_alt_cache: Dict[str, Any] = {"ts": 0, "snap": None}

def _altseason_fetch() -> Dict[str, Any]:
    out = {"asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "errors": []}
    try:
        import requests
    except Exception:
        out["errors"].append("Missing dependency: requests")
        return out
    headers = {"User-Agent":"altseason-bot/1.6","Accept":"*/*","Accept-Encoding":"identity","Connection":"close"}
    def get_json(url: str, timeout: int=12) -> Dict[str,Any]:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        body_preview = (r.text or "")[:220].replace("\n"," ").replace("\r"," ")
        if r.status_code != 200: raise RuntimeError(f"{url} -> HTTP {r.status_code}: {body_preview}")
        try: return r.json()
        except Exception: raise RuntimeError(f"{url} -> Non-JSON response: {body_preview}")

    mcap_usd = btc_dom = None
    try:
        alt = get_json("https://api.alternative.me/v2/global/")
        d0 = (alt.get("data") or [{}])[0]; qusd = (d0.get("quotes") or {}).get("USD") or {}
        mcap = qusd.get("total_market_cap"); dom = d0.get("bitcoin_percentage_of_market_cap")
        if mcap is not None and dom is not None: mcap_usd = float(mcap); btc_dom = float(dom)
    except Exception as e: out["errors"].append(f"alternative.me: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            g = get_json("https://api.coingecko.com/api/v3/global"); data = g.get("data") or {}
            mcap_usd = float(data["total_market_cap"]["usd"]); btc_dom = float(data["market_cap_percentage"]["btc"])
        except Exception as e: out["errors"].append(f"coingecko: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            pg = get_json("https://api.coinpaprika.com/v1/global")
            mcap_usd = float(pg["market_cap_usd"]); btc_dom = float(pg["bitcoin_dominance_percentage"])
        except Exception as e: out["errors"].append(f"coinpaprika: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            cc = get_json("https://api.coincap.io/v2/assets?limit=2000")
            assets = cc.get("data") or []; total=0.0; btc_mcap=0.0
            for a in assets:
                mc=a.get("marketCapUsd")
                if mc is not None:
                    try: total += float(mc)
                    except: pass
            for a in assets:
                if a.get("id")=="bitcoin":
                    try: btc_mcap=float(a.get("marketCapUsd") or 0.0)
                    except: btc_mcap=0.0
                    break
            if total>0: mcap_usd=total; btc_dom=(btc_mcap/total)*100.0
        except Exception as e: out["errors"].append(f"coincap: {e!r}")
    if mcap_usd is None or btc_dom is None:
        try:
            cl = get_json("https://api.coinlore.net/api/global/")
            g = cl[0] if isinstance(cl,list) and cl else cl
            mcap = g.get("total_mcap_usd") or g.get("total_mcap") or g.get("mcap_total_usd")
            dom  = g.get("btc_d") or g.get("bitcoin_dominance_percentage") or g.get("btc_dominance")
            if mcap is not None and dom is not None: mcap_usd=float(mcap); btc_dom=float(dom)
        except Exception as e: out["errors"].append(f"coinlore: {e!r}")

    out["total_mcap_usd"] = None if mcap_usd is None else float(mcap_usd)
    out["btc_dominance"]  = None if btc_dom   is None else float(btc_dom)
    out["total2_usd"]     = None if (mcap_usd is None or btc_dom is None) else float(mcap_usd*(1.0-btc_dom/100.0))

    eth_btc = None
    try:
        j = get_json("https://api.binance.com/api/v3/ticker/price?symbol=ETHBTC"); eth_btc=float(j["price"])
    except Exception as e: out["errors"].append(f"binance: {e!r}")
    if eth_btc is None:
        try:
            sp = get_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=btc,usd")
            eth_btc = float(sp["ethereum"]["btc"])
        except Exception as e: out["errors"].append(f"coingecko_simple: {e!r}")
    if eth_btc is None:
        try:
            tkr = get_json("https://api.coinpaprika.com/v1/tickers/eth-ethereum?quotes=BTC")
            eth_btc = float(tkr["quotes"]["BTC"]["price"])
        except Exception as e: out["errors"].append(f"coinpaprika_ethbtc: {e!r}")
    if eth_btc is None:
        try:
            cc_eth = get_json("https://api.coincap.io/v2/assets/ethereum")
            cc_btc = get_json("https://api.coincap.io/v2/assets/bitcoin")
            eth_usd = float(cc_eth["data"]["priceUsd"]); btc_usd=float(cc_btc["data"]["priceUsd"])
            eth_btc = eth_usd / btc_usd
        except Exception as e: out["errors"].append(f"coincap_ethbtc: {e!r}")

    out["eth_btc"] = None if eth_btc is None else float(eth_btc)

    out["altseason_index"] = None
    try:
        import requests
        from bs4 import BeautifulSoup
        html = requests.get("https://www.blockchaincenter.net/altcoin-season-index/", timeout=12, headers=headers).text
        soup = BeautifulSoup(html, "html.parser"); txt = soup.get_text(" ", strip=True)
        m = re.search(r"Altcoin Season Index[^0-9]*([0-9]{2,3})", txt)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100: out["altseason_index"] = v
    except Exception as e:
        out["errors"].append(f"altseason_index_scrape: {e!r}")
    return out

def _ok_cmp(val: Optional[float], thr: float, direction: str) -> bool:
    if val is None: return False
    return (val < thr) if direction == "below" else (val > thr)

def _altseason_summary(snap: Dict[str, Any]) -> Dict[str, Any]:
    btc=snap.get("btc_dominance"); eth=snap.get("eth_btc"); t2=snap.get("total2_usd"); asi=snap.get("altseason_index")
    btc_ok = _ok_cmp(btc, ALT_BTC_DOM_THR, "below")
    eth_ok = _ok_cmp(eth, ALT_ETH_BTC_THR, "above")
    t2_ok  = _ok_cmp(t2, ALT_TOTAL2_THR_T*1e12, "above")
    asi_ok = (asi is not None) and _ok_cmp(float(asi), ALT_ASI_THR, "above")
    greens = sum([btc_ok, eth_ok, t2_ok, asi_ok]); on = greens >= ALT_GREENS_REQUIRED
    return {
        "asof": snap.get("asof"), "stale": bool(snap.get("stale", False)), "errors": snap.get("errors", []),
        "btc_dominance": None if btc is None else float(btc),
        "eth_btc": None if eth is None else float(eth),
        "total2_usd": None if t2 is None else float(t2),
        "altseason_index": None if asi is None else int(asi),
        "thresholds": {
            "btc": ALT_BTC_DOM_THR, "eth_btc": ALT_ETH_BTC_THR, "asi": ALT_ASI_THR,
            "total2_trillions": ALT_TOTAL2_THR_T, "greens_required": ALT_GREENS_REQUIRED
        },
        "triggers": {
            "btc_dominance_ok": btc_ok, "eth_btc_ok": eth_ok,
            "total2_ok": t2_ok, "altseason_index_ok": asi_ok
        },
        "greens": greens, "ALTSEASON_ON": on
    }

def _altseason_snapshot(force: bool=False) -> Dict[str, Any]:
    now = time.time()
    if (not force) and _alt_cache["snap"] and (now - _alt_cache["ts"] < ALT_CACHE_TTL):
        snap = dict(_alt_cache["snap"]); snap.setdefault("stale", False); return snap
    try:
        snap = _altseason_fetch(); snap["stale"]=False
        _alt_cache["snap"]=snap; _alt_cache["ts"]=now; _save_last_snapshot(snap); return snap
    except Exception as e:
        if _alt_cache["snap"]:
            s=dict(_alt_cache["snap"]); s["stale"]=True
            s.setdefault("errors", []).append(f"live_fetch_exception: {e!r}"); return s
        disk=_load_last_snapshot()
        if isinstance(disk, dict):
            disk=dict(disk); disk["stale"]=True
            disk.setdefault("errors", []).append(f"live_fetch_exception: {e!r}"); return disk
        return {"asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "btc_dominance": None, "eth_btc": None, "total2_usd": None, "altseason_index": None,
                "errors": [f"live_fetch_exception: {e!r}"], "stale": True}

@app.get("/altseason/check")
def altseason_check_public():
    snap = _altseason_snapshot(force=False)
    return _altseason_summary(snap)

@app.api_route("/altseason/notify", methods=["GET","POST"])
async def altseason_notify(request: Request, secret: Optional[str]=Query(None),
                           force: Optional[bool]=Query(False),
                           message: Optional[str]=Query(None),
                           pin: Optional[bool]=Query(False)):
    body={}
    if request.method=="POST":
        try: body=await request.json()
        except Exception: body={}
    body_secret = body.get("secret") if isinstance(body, dict) else None
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")
    if request.method=="POST":
        force=bool(body.get("force", force)); message=body.get("message", message); pin=bool(body.get("pin", pin))
    pin = bool(pin or TELEGRAM_PIN_ALTSEASON)
    s = _altseason_summary(_altseason_snapshot(force=bool(force)))
    sent=None; pin_res=None
    if s["ALTSEASON_ON"] or force:
        if message: msg=message
        else:
            msg = (f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — ALTSEASON DÉBUTÉ !"
                   if s["ALTSEASON_ON"] else
                   f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — EN VEILLE (conditions insuffisantes)")
        pin_result = send_telegram_ex(msg, pin=bool(pin))
        sent = pin_result.get("ok")
        pin_res = {"pinned": pin_result.get("pinned"), "message_id": pin_result.get("message_id"), "error": pin_result.get("error")}
        log.info("Altseason notify: sent=%s pinned=%s err=%s", sent, pin_res.get("pinned"), pin_res.get("error"))
    return {"summary": s, "telegram_sent": sent, "pin_result": pin_res}

def _load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(ALTSEASON_STATE_FILE):
            with open(ALTSEASON_STATE_FILE,"r",encoding="utf-8") as f:
                d=json.load(f)
                if isinstance(d, dict): return d
    except Exception: pass
    return {"last_on": False, "last_sent_ts": 0, "last_tick_ts": 0,
            "consec_3of4_days": 0, "consec_4of4_days": 0, "last_streak_date": None}

def _save_state(state: Dict[str, Any]) -> None:
    try:
        d=os.path.dirname(ALTSEASON_STATE_FILE) or "/tmp"
        os.makedirs(d, exist_ok=True)
        with open(ALTSEASON_STATE_FILE,"w",encoding="utf-8") as f: json.dump(state,f)
    except Exception: pass

def _today_utc_str() -> str:
    import datetime as dt
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def _update_daily_streaks(state: Dict[str, Any], summary: Dict[str, Any]) -> None:
    import datetime as dt
    today = _today_utc_str(); last_date = state.get("last_streak_date")
    if last_date == today: return
    greens = int(summary.get("greens") or 0); is3 = greens >= 3; is4 = greens >= 4
    if last_date is None:
        state["consec_3of4_days"] = 1 if is3 else 0
        state["consec_4of4_days"] = 1 if is4 else 0
    else:
        try:
            d_last = dt.datetime.strptime(last_date,"%Y-%m-%d")
            d_today= dt.datetime.strptime(today,"%Y-%m-%d")
            consecutive = (d_today - d_last).days == 1
        except Exception:
            consecutive = False
        if consecutive:
            state["consec_3of4_days"] = (state.get("consec_3of4_days",0)+1) if is3 else 0
            state["consec_4of4_days"] = (state.get("consec_4of4_days",0)+1) if is4 else 0
        else:
            state["consec_3of4_days"] = 1 if is3 else 0
            state["consec_4of4_days"] = 1 if is4 else 0
    state["last_streak_date"] = today

@app.get("/altseason/streaks")
def altseason_streaks():
    st=_load_state(); s=_altseason_summary(_altseason_snapshot(force=False))
    _update_daily_streaks(st, s); _save_state(st)
    return {"asof": s.get("asof"), "greens": s.get("greens"),
            "ALT3_ON": bool(int(s.get("greens") or 0) >= 3),
            "ALT4_ON": bool(int(s.get("greens") or 0) >= 4),
            "consec_3of4_days": int(st.get("consec_3of4_days") or 0),
            "consec_4of4_days": int(st.get("consec_4of4_days") or 0)}
# =========================
# Section 5/6 — Telegram (+boutons), rendu messages, Webhook TV, Trades + Templates
# =========================
def send_telegram(text: str) -> bool:
    global _last_tg
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): return False
    try:
        now=time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS: return False
        _last_tg = now
        import urllib.request, urllib.parse
        api_url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data=urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,"text":text}).encode()
        req=urllib.request.Request(api_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp: _=resp.read()
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e); return False

def send_telegram_ex(text: str, pin: bool=False, buttons: Optional[List[List[Dict[str, str]]]] = None) -> Dict[str, Any]:
    """
    Envoie un message Telegram avec option pin + inline buttons.
    buttons: liste de lignes, chaque ligne = liste de { "text": "...", "url": "..." }
    """
    result={"ok":False,"message_id":None,"pinned":False,"error":None}
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        result["error"]="Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"; return result
    try:
        import urllib.request, urllib.parse, json as _json, time as _time
        global _last_tg
        now=_time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            result["ok"]=True; result["error"]="rate-limited (cooldown)"; return result
        _last_tg=now
        api_base=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

        send_url=f"{api_base}/sendMessage"
        payload={"chat_id":TELEGRAM_CHAT_ID,"text":text}
        if buttons:
            payload["reply_markup"] = _json.dumps({"inline_keyboard": buttons})
        data_enc = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(send_url, data=data_enc)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8","ignore"); payload_resp = _json.loads(raw)
            if not payload_resp.get("ok"):
                result["error"]=f"sendMessage failed: {raw[:200]}"; log.warning("Telegram sendMessage error: %s", result["error"]); return result
            msg = payload_resp.get("result") or {}; mid = msg.get("message_id")
            result["ok"]=True; result["message_id"]=mid

        if pin and result["message_id"] is not None:
            pin_url=f"{api_base}/pinChatMessage"
            pin_data=urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,"message_id":result["message_id"]}).encode()
            preq=urllib.request.Request(pin_url, data=pin_data)
            try:
                with urllib.request.urlopen(preq, timeout=10) as presp:
                    praw=presp.read().decode("utf-8","ignore"); pp=_json.loads(praw)
                    if pp.get("ok"): result["pinned"]=True
                    else:
                        result["error"]=f"pinChatMessage failed: {praw[:200]}"; log.warning("Telegram pinChatMessage error: %s", result["error"])
            except Exception as e:
                result["error"]=f"pinChatMessage exception: {e}"; log.warning("Telegram pin exception: %s", e)
        return result
    except Exception as e:
        result["error"]=f"send_telegram_ex exception: {e}"; log.warning("Telegram send_telegram_ex exception: %s", e); return result

def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    t = str(payload.get("type") or "EVENT").upper()
    if t in {"AOE_PREMIUM","AOE_DISCOUNT"}: return None
    sym = str(payload.get("symbol") or "?"); tf_lbl = tf_label_of(payload)
    side = str(payload.get("side") or ""); entry=_to_float(payload.get("entry"))
    sl=_to_float(payload.get("sl")); tp=_to_float(payload.get("tp"))
    tp1=_to_float(payload.get("tp1")); tp2=_to_float(payload.get("tp2")); tp3=_to_float(payload.get("tp3"))
    leverage = payload.get("leverage") or payload.get("lev") or payload.get("lev_reco")
    lev_x = parse_leverage_x(str(leverage) if leverage is not None else None)

    def num(v): return fmt_num(v) if v is not None else "—"
    if t == "ENTRY":
        lines=[]
        lines.append(f"📩 {sym} {tf_lbl}")
        if side: lines.append(("📈 Long Entry:" if side.upper()=="LONG" else "📉 Short Entry:")+f" {num(entry)}")
        if leverage: lines.append(f"💡Leverage: {leverage}")
        if tp1: lines.append(f"🎯 TP1: {num(tp1)}")
        if tp2: lines.append(f"🎯 TP2: {num(tp2)}")
        if tp3: lines.append(f"🎯 TP3: {num(tp3)}")
        if sl:  lines.append(f"❌ SL: {num(sl)}")
        try:
            if LLM_ENABLED and _openai_client and (FORCE_LLM or True):
                res=llm_confidence_for_entry(payload)
                if res:
                    conf_pct, rationale = res
                    if conf_pct >= CONFIDENCE_MIN:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}% — {rationale or 'estimation heuristique'}")
                    else:
                        lines.append(f"🧠 Confiance LLM: {conf_pct:.0f}%")
        except Exception as e:
            log.warning("LLM confidence render failed: %s", e)
        lines.append("🤖 Astuce: après TP1, placez SL au BE.")
        return "\n".join(lines)

    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        spot_pct = pct(tp, entry) if (side and tp is not None and entry is not None) else None
        lev_pct = (spot_pct * lev_x) if (spot_pct is not None and lev_x) else None
        lines=[f"✅ {label} — {sym} {tf_lbl}"]
        if tp is not None: lines.append(f"Mark price : {num(tp)}")
        if spot_pct is not None:
            base=f"Profit (spot) : {spot_pct:.2f}%"
            if lev_pct is not None: base += f" | avec {int(lev_x)}x : {lev_pct:.2f}%"
            lines.append(base)
        return "\n".join(lines)

    if t == "SL_HIT":
        lines=[f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None: lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    if t == "CLOSE":
        reason=payload.get("reason")
        lines=[f"🔔 Close — {sym} {tf_lbl}"]
        if reason: lines.append(f"Raison: {reason}")
        return "\n".join(lines)
    return f"[TV] {t} | {sym} | TF {tf_lbl}"

@app.post("/tv-webhook")
async def tv_webhook(request: Request, secret: Optional[str]=Query(None)):
    try:
        payload = await request.json()
        if not isinstance(payload, dict): raise ValueError("JSON must be an object")
    except Exception as e:
        log.error("Invalid JSON: %s", e); raise HTTPException(status_code=400, detail="Invalid JSON")
    body_secret = payload.get("secret")
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    log.info("Webhook payload: %s", json.dumps(payload)[:300])
    save_event(payload)

    try:
        msg = telegram_rich_message(payload)
        if msg:
            t = str(payload.get("type") or "").upper()
            buttons = None
            if t == "ENTRY":
                buttons = [[{"text":"📊 Voir Dashboard Trades","url":"https://tradingview-gd03.onrender.com/trades"}]]
            res = send_telegram_ex(msg, pin=False, buttons=buttons)
            log.info("TV webhook -> telegram sent=%s pinned=%s err=%s",
                     res.get("ok"), res.get("pinned"), res.get("error"))
    except Exception as e:
        log.warning("TV webhook telegram send error: %s", e)
    return {"ok": True}

TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades — AI Trader PRO</title>
<style>
:root{
  --bg:#0f172a; --card:#111827; --muted:#94a3b8; --line:#1f2937; --txt:#e5e7eb;
  --chip:#0b1220; --ok:#10b981; --warn:#fb923c; --bad:#ef4444; --good:#22c55e;
}
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;padding:20px;background:var(--bg);color:var(--txt);font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 14px 0;font-size:28px;font-weight:800}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:16px}
.card h3{margin:0 0 10px 0;color:var(--muted);font-weight:700}
label{display:block;margin:4px 0 4px 0;color:var(--muted);font-size:13px}
input,select{background:#0b1220;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:120px}
input::placeholder{color:#64748b}
.btn{display:inline-block;padding:9px 12px;border:1px solid var(--line);color:var(--txt);text-decoration:none;border-radius:10px}
.btn.primary{background:#0b1220}
.btn.ghost{background:transparent}
.btn.small{padding:6px 10px;font-size:13px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--chip);cursor:pointer;user-select:none}
.chip.active{outline:2px solid #2563eb}
.kpi{display:flex;flex-direction:column;gap:4px;min-width:120px}
.kpi .v{font-size:18px;font-weight:800}
.kpi .l{font-size:12px;color:var(--muted)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--line);font-size:12px}
.badge-win{background:#052e1f;border-color:#065f46}
.badge-loss{background:#3f1d1d;border-color:#7f1d1d}
.badge-none{background:#111827;border-color:#374151}
table{width:100%;border-collapse:collapse}
th,td{padding:10px;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--muted);position:sticky;top:0;background:var(--card);z-index:1}
tr:hover{background:#0b1325}
.hr{height:1px;background:var(--line);margin:10px 0}
.muted{color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:10px;margin-left:8px}
.ok{background:var(--ok)} .warn{background:var(--warn)}
.tag{padding:2px 8px;border:1px solid var(--line);border-radius:8px;background:#0b1220}
.toolbar{display:flex;gap:8px;flex-wrap:wrap}
#alt-bx .row{align-items:center}
#filters .chips .chip{transition:transform .06s}
#filters .chips .chip:active{transform:scale(.98)}
footer{display:flex;justify-content:space-between;align-items:center;margin-top:10px}
.pager a{color:var(--txt);text-decoration:none;border:1px solid var(--line);padding:6px 10px;border-radius:8px;margin-right:6px}
.pager .muted{margin:0 6px}
@media (max-width:780px){ .hide-sm{display:none}}
</style>
</head><body>
<h1>Trades</h1>

<!-- FILTRES -->
<div class="card" id="filters">
  <form method="get" class="row" id="qform">
    <div>
      <label>Symbol</label>
      <input name="symbol" placeholder="e.g. BTCUSDT" value="$symbol">
    </div>
    <div>
      <label>TF</label>
      <input name="tf" placeholder="e.g. 15, 60, 1D" value="$tf">
    </div>
    <div class="hide-sm">
      <label>Start (YYYY-MM-DD)</label>
      <input name="start" value="$start">
    </div>
    <div class="hide-sm">
      <label>End (YYYY-MM-DD)</label>
      <input name="end" value="$end">
    </div>
    <div>
      <label>Page</label>
      <input name="page" value="$page" style="width:80px">
    </div>
    <div>
      <label>Page size</label>
      <input name="page_size" value="$page_size" style="width:100px">
    </div>
    <div class="toolbar">
      <button class="btn primary" type="submit">Apply</button>
      <a class="btn ghost" href="/trades">Reset</a>
      <button class="btn ghost" id="btn-csv" type="button">Export CSV</button>
      <a class="btn ghost" href="/" title="Home">Home</a>
    </div>
    <div class="row chips" style="margin-top:8px">
      <div class="chip" data-filter="all">All</div>
      <div class="chip" data-filter="long">Long</div>
      <div class="chip" data-filter="short">Short</div>
      <div class="chip" data-filter="wins">Wins</div>
      <div class="chip" data-filter="losses">Losses</div>
    </div>
  </form>
</div>

<!-- ALTSEASON -->
<div class="card" id="alt-bx">
  <h3 class="muted">Altseason — État rapide</h3>
  <div id="alt-asof" class="muted">Loading…</div>
  <div class="row" style="margin-top:6px">
    <div>BTC Dominance: <span id="alt-btc">—</span> (thr &lt; $btc_thr) <span id="dot-btc" class="dot"></span></div>
    <div>ETH/BTC: <span id="alt-eth">—</span> (thr &gt; $eth_thr) <span id="dot-eth" class="dot"></span></div>
    <div>Altseason Index: <span id="alt-asi">N/A</span> (thr ≥ $asi_thr) <span id="dot-asi" class="dot"></span></div>
    <div>TOTAL2: <span id="alt-t2">—</span> (thr &gt; $t2_thr T$) <span id="dot-t2" class="dot"></span></div>
  </div>
  <div class="row" style="margin-top:6px">
    <span id="alt3" class="tag">Prep 3/4: —</span>
    <span id="alt4" class="tag">Confirm 4/4: —</span>
    <span class="muted">Séries: <span id="d3">0</span>d @3/4, <span id="d4">0</span>d @4/4</span>
  </div>
</div>

<!-- KPIs -->
<div class="card">
  <div class="row">
    <div class="kpi"><div class="v">$total_trades</div><div class="l">Total trades</div></div>
    <div class="kpi"><div class="v">$winrate_pct%</div><div class="l">Winrate</div></div>
    <div class="kpi"><div class="v">$wins / $losses</div><div class="l">W / L</div></div>
    <div class="kpi"><div class="v">$tp1_hits / $tp2_hits / $tp3_hits</div><div class="l">TP1 / TP2 / TP3</div></div>
    <div class="kpi"><div class="v">$avg_time_to_outcome_sec s</div><div class="l">Avg time to outcome</div></div>
    <div class="kpi"><div class="v">$best_win_streak / $worst_loss_streak</div><div class="l">Best / Worst streak</div></div>
  </div>
</div>

<!-- TABLE -->
<div class="card">
  <table id="tbl">
    <thead>
      <tr>
        <th>ID</th><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Outcome</th><th class="hide-sm">Duration (s)</th>
      </tr>
    </thead>
    <tbody>
      $rows_html
    </tbody>
  </table>
  <footer>
    <div class="muted">Showing <strong>$showing</strong> of <strong>$total_trades</strong></div>
    <div class="pager">
      $pager_html
    </div>
  </footer>
</div>

<script>
(function(){
  // Altseason widgets
  function setText(id, txt){ const el = document.getElementById(id); if (el) el.textContent = txt; }
  function setDot(id, ok){ const el = document.getElementById(id); if (el) el.className = "dot " + (ok ? "ok" : "warn"); }
  function num(v){ return typeof v === "number" ? v : Number(v); }

  fetch("/altseason/check")
    .then(r=>r.json())
    .then(s=>{
      setText("alt-asof", "As of " + (s.asof || "now") + (s.stale ? " (cache)" : ""));
      const btc = num(s.btc_dominance), eth = num(s.eth_btc), t2 = num(s.total2_usd), asi=s.altseason_index;
      setText("alt-btc", Number.isFinite(btc) ? btc.toFixed(2) + " %" : "—"); setDot("dot-btc", s.triggers && s.triggers.btc_dominance_ok);
      setText("alt-eth", Number.isFinite(eth) ? eth.toFixed(5) : "—"); setDot("dot-eth", s.triggers && s.triggers.eth_btc_ok);
      setText("alt-asi", (asi == null) ? "N/A" : String(asi)); setDot("dot-asi", s.triggers && s.triggers.altseason_index_ok);
      setText("alt-t2", Number.isFinite(t2) ? (t2/1e12).toFixed(2) + " T$" : "—"); setDot("dot-t2", s.triggers && s.triggers.total2_ok);
    })
    .catch(()=>{ setText("alt-asof","Altseason: error"); });

  fetch("/altseason/streaks")
    .then(r=>r.json())
    .then(s=>{
      const badge = (s.ALT4_ON ? "Confirm 4/4: ON" : (s.ALT3_ON ? "Prep 3/4: ON" : "OFF"));
      const b3 = document.getElementById("alt3"), b4 = document.getElementById("alt4");
      if (b3) b3.textContent = "Prep 3/4: " + (s.ALT3_ON ? "ON" : "OFF");
      if (b4) b4.textContent = "Confirm 4/4: " + (s.ALT4_ON ? "ON" : "OFF");
      setText("d3", String(s.consec_3of4_days||0));
      setText("d4", String(s.consec_4of4_days||0));
    })
    .catch(()=>{});

  // Table quick filters
  const chips = document.querySelectorAll('[data-filter]');
  let cur = 'all';
  function applyFilter(){
    const rows = document.querySelectorAll('#tbl tbody tr');
    rows.forEach(tr=>{
      const side = tr.getAttribute('data-side');
      const out  = tr.getAttribute('data-outcome');
      let show = true;
      if (cur === 'long')   show = (side === 'LONG');
      if (cur === 'short')  show = (side === 'SHORT');
      if (cur === 'wins')   show = (out === 'TP1_HIT' || out === 'TP2_HIT' || out === 'TP3_HIT');
      if (cur === 'losses') show = (out === 'SL_HIT');
      tr.style.display = show ? '' : 'none';
    });
    chips.forEach(c=>c.classList.toggle('active', c.getAttribute('data-filter')===cur));
  }
  chips.forEach(c=>c.addEventListener('click', ()=>{ cur = c.getAttribute('data-filter'); applyFilter(); }));
  applyFilter();

  // Export CSV (client-side)
  function tableToCSV(){
    const rows = Array.from(document.querySelectorAll('#tbl tr'));
    return rows.map(row => Array.from(row.querySelectorAll('th,td')).map(td=>{
      let t = td.innerText.replace(/\n/g,' ').trim();
      if (t.indexOf(',') !== -1) t = '"' + t.replace(/"/g,'""') + '"';
      return t;
    }).join(',')).join('\n');
  }
  document.getElementById('btn-csv')?.addEventListener('click', ()=>{
    const csv = tableToCSV();
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = 'trades.csv'; a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href), 4000);
  });

  // Keep page value sane
  document.getElementById('qform')?.addEventListener('submit', (e)=>{
    const p = e.target.querySelector('input[name="page"]');
    const s = e.target.querySelector('input[name="page_size"]');
    if (p && (!p.value || Number(p.value) < 1)) p.value = "1";
    if (s && (!s.value || Number(s.value) < 5)) s.value = "100";
  });
})();
</script>
</body></html>
""")

# =========================
# Section 6/6 — Trades/Events endpoints, Admin, Daemon, __main__
# =========================
EVENTS_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Events</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;border-bottom:1px solid #1f2937}th{color:#94a3b8}
a.btn{display:inline-block;padding:8px 12px;border:1px solid #1f2937;color:#e5e7eb;text-decoration:none;border-radius:8px}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
pre{white-space:pre-wrap;margin:0}
</style></head><body>
<h1>Events</h1>
<div class="card">
  <a class="btn" href="/">Home</a>
  <a class="btn" href="/trades-admin?secret=$secret">Trades Admin</a>
</div>
<div class="card">
  <table><thead>
    <tr><th>Time</th><th>Type</th><th>Symbol</th><th>TF</th><th>Side</th><th>Trade ID</th><th>Raw</th></tr>
  </thead><tbody>
    $rows_html
  </tbody></table>
</div>
</body></html>
""")

TRADES_ADMIN_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades (Admin)</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;border-bottom:1px solid #1f2937}th{color:#94a3b8}
.chip{display:inline-block;padding:2px 8px;border:1px solid #1f2937;border-radius:999px}
.badge-win{background:#052e1f;border-color:#065f46}.badge-loss{background:#3f1d1d}
label{display:block;margin:6px 0 2px}.row{display:flex;gap:10px;flex-wrap:wrap}
input{background:#111827;color:#e5e7eb;border:1px solid #1f2937;border-radius:6px;padding:6px}
a.btn{display:inline-block;padding:8px 12px;border:1px solid #1f2937;color:#e5e7eb;text-decoration:none;border-radius:8px}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
</style></head><body>
<h1>Trades (Admin)</h1>
<div class="card">
  <form method="get">
    <input type="hidden" name="secret" value="$secret">
    <div class="row">
      <div><label>Symbol</label><input name="symbol" value="$symbol"></div>
      <div><label>TF</label><input name="tf" value="$tf"></div>
      <div><label>Start</label><input name="start" value="$start"></div>
      <div><label>End</label><input name="end" value="$end"></div>
      <div><label>Limit</label><input name="limit" value="$limit" type="number" min="1" max="10000"></div>
    </div>
    <div style="margin-top:8px">
      <button class="btn" type="submit">Apply</button>
      <a class="btn" href="/">Home</a>
      <a class="btn" href="/events?secret=$secret">Events</a>
      <a class="btn" href="/reset?secret=$secret&confirm=yes">Reset DB</a>
    </div>
  </form>
</div>

<div class="card">
  <table><thead>
    <tr><th>ID</th><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Outcome</th><th>Duration (s)</th></tr>
  </thead><tbody>
    $rows_html
  </tbody></table>
</div>
</body></html>
""")

@app.get("/trades.json")
def trades_json(secret: Optional[str]=Query(None),
                symbol: Optional[str]=Query(None),
                tf: Optional[str]=Query(None),
                start: Optional[str]=Query(None),
                end: Optional[str]=Query(None),
                limit: int=Query(100)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep=parse_date_to_epoch(start); end_ep=parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(1000, limit*10))
    return JSONResponse({"summary": summary, "trades": trades[-limit:] if limit else trades})

@app.get("/trades.csv")
def trades_csv(secret: Optional[str]=Query(None),
               symbol: Optional[str]=Query(None),
               tf: Optional[str]=Query(None),
               start: Optional[str]=Query(None),
               end: Optional[str]=Query(None),
               limit: int=Query(1000)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep=parse_date_to_epoch(start); end_ep=parse_date_end_to_epoch(end)
    trades,_ = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit*10))
    data = trades[-limit:] if limit else trades
    headers=["trade_id","symbol","tf","side","entry","sl","tp1","tp2","tp3","entry_time","outcome","outcome_time","duration_sec"]
    lines=[",".join(headers)]
    for tr in data:
        row=[str(tr.get(h,"")) for h in headers]
        row=[("\"%s\"" % x) if ("," in x) else x for x in row]
        lines.append(",".join(row))
    return Response(content="\n".join(lines), media_type="text/csv")

@app.get("/events", response_class=HTMLResponse)
def events(secret: Optional[str]=Query(None), limit: int=Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur=conn.cursor(); cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows=cur.fetchall()

    def fmt_time(ts:int)->str:
        try:
            import datetime as dt
            return dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(ts)

    rows_html=""
    for r in rows:
        rows_html += ("<tr>"
                      f"<td>{escape_html(fmt_time(r['received_at']))}</td>"
                      f"<td>{escape_html(r['type'] or '')}</td>"
                      f"<td>{escape_html(r['symbol'] or '')}</td>"
                      f"<td>{escape_html(r['tf'] or '')}</td>"
                      f"<td>{escape_html(r['side'] or '')}</td>"
                      f"<td>{escape_html(r['trade_id'] or '')}</td>"
                      f"<td><pre style='white-space:pre-wrap;margin:0'>{escape_html(r['raw_json'] or '')}</pre></td>"
                      "</tr>")
    html = EVENTS_HTML_TPL.safe_substitute(
        secret=escape_html(secret or ""), rows_html=rows_html or '<tr><td colspan="7" class="muted">No events.</td></tr>'
    )
    return HTMLResponse(html)

@app.get("/events.json")
def events_json(secret: Optional[str]=Query(None), limit: int=Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur=conn.cursor(); cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows=[dict(r) for r in cur.fetchall()]
    return JSONResponse({"events": rows})

@app.get("/trades/secret={secret}")
def trades_alias(secret: str):
    return RedirectResponse(url=f"/trades-admin?secret={secret}", status_code=307)

@app.get("/reset")
def reset_all(secret: Optional[str]=Query(None),
              confirm: Optional[str]=Query(None),
              redirect: Optional[str]=Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="Invalid secret")
    if confirm not in ("yes","true","1","YES","True"):
        return {"ok": False, "error": "Confirmation required: add &confirm=yes"}
    with db_conn() as conn:
        cur=conn.cursor(); cur.execute("DELETE FROM events"); conn.commit()
    if redirect: return RedirectResponse(url=redirect, status_code=303)
    return {"ok": True, "deleted": "all"}

@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=5, le=2000)
):
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    # Récup (grosse marge) puis pagination côté serveur
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=100000)

    total_trades = len(trades)
    total_pages = max(1, (total_trades + page_size - 1) // page_size)
    page = min(max(1, page), total_pages)
    i0 = (page - 1) * page_size
    i1 = min(total_trades, i0 + page_size)
    data = trades[i0:i1]

    # Lignes HTML (ajout data-attrs pour filtres rapides)
    rows_html = ""
    for tr in data:
        outcome = tr.get("outcome") or "NONE"
        side    = (tr.get("side") or "").upper()
        badge_class = "badge-win" if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT") else ("badge-loss" if outcome == "SL_HIT" else "badge-none")
        outcome_html = f'<span class="badge {badge_class}">{escape_html(outcome)}</span>'
        rows_html += (
            f"<tr data-side=\"{escape_html(side)}\" data-outcome=\"{escape_html(outcome)}\">"
            f"<td>{escape_html(str(tr.get('trade_id')))}</td>"
            f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
            f"<td>{escape_html(side)}</td>"
            f"<td>{fmt_num(tr.get('entry'))}</td>"
            f"<td>{fmt_num(tr.get('sl'))}</td>"
            f"<td>{fmt_num(tr.get('tp1'))}</td>"
            f"<td>{fmt_num(tr.get('tp2'))}</td>"
            f"<td>{fmt_num(tr.get('tp3'))}</td>"
            f"<td>{outcome_html}</td>"
            f"<td class=\"hide-sm\">{tr.get('duration_sec') if tr.get('duration_sec') is not None else ''}</td>"
            "</tr>"
        )

    showing = f"{i0+1}-{i1}" if total_trades else "0-0"

    # Pagination HTML
    def q(query_overrides: Dict[str, Any]) -> str:
        from urllib.parse import urlencode
        base = dict(symbol=symbol or "", tf=tf or "", start=start or "", end=end or "", page=page, page_size=page_size)
        base.update({k:v for k,v in query_overrides.items() if v is not None})
        return "/trades?" + urlencode(base)

    prev_link = q({"page": page-1}) if page > 1 else None
    next_link = q({"page": page+1}) if page < total_pages else None
    pager = []
    if prev_link: pager.append(f'<a href="{escape_html(prev_link)}">« Prev</a>')
    pager.append(f'<span class="muted">Page {page}/{total_pages}</span>')
    if next_link: pager.append(f'<a href="{escape_html(next_link)}">Next »</a>')
    pager_html = " ".join(pager)

    html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
        symbol=escape_html(symbol or ""),
        tf=escape_html(tf or ""),
        start=escape_html(start or ""),
        end=escape_html(end or ""),
        page=str(page),
        page_size=str(page_size),
        total_trades=str(summary["total_trades"]),
        winrate_pct=str(summary["winrate_pct"]),
        wins=str(summary["wins"]),
        losses=str(summary["losses"]),
        tp1_hits=str(summary["tp1_hits"]),
        tp2_hits=str(summary["tp2_hits"]),
        tp3_hits=str(summary["tp3_hits"]),
        avg_time_to_outcome_sec=str(summary["avg_time_to_outcome_sec"]),
        best_win_streak=str(summary["best_win_streak"]),
        worst_loss_streak=str(summary["worst_loss_streak"]),
        rows_html=rows_html or '<tr><td colspan="11" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
        showing=showing,
        pager_html=pager_html,
        btc_thr=str(int(ALT_BTC_DOM_THR)),
        eth_thr=f"{ALT_ETH_BTC_THR:.3f}",
        asi_thr=str(int(ALT_ASI_THR)),
        t2_thr=f"{ALT_TOTAL2_THR_T:.2f}"
    )
    return HTMLResponse(html)

    html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
        symbol=escape_html(symbol or ""), tf=escape_html(tf or ""),
        start=escape_html(start or ""), end=escape_html(end or ""),
        limit=str(limit),
        total_trades=str(summary["total_trades"]), winrate_pct=str(summary["winrate_pct"]),
        wins=str(summary["wins"]), losses=str(summary["losses"]),
        tp1_hits=str(summary["tp1_hits"]), tp2_hits=str(summary["tp2_hits"]), tp3_hits=str(summary["tp3_hits"]),
        avg_time_to_outcome_sec=str(summary["avg_time_to_outcome_sec"]),
        best_win_streak=str(summary["best_win_streak"]), worst_loss_streak=str(summary["worst_loss_streak"]),
        rows_html=rows_html or '<tr><td colspan="11" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>'
    )
    return HTMLResponse(html)

# ----- Altseason Daemon -----
_daemon_stop = threading.Event()
_daemon_thread: Optional[threading.Thread] = None

@app.on_event("startup")
def _start_daemon():
    global _daemon_thread
    if ALTSEASON_AUTONOTIFY and _daemon_thread is None:
        _daemon_stop.clear()
        _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True)
        _daemon_thread.start()

@app.on_event("shutdown")
def _stop_daemon():
    if _daemon_thread is not None:
        _daemon_stop.set()

def _daemon_loop():
    state = _load_state()
    log.info("Altseason daemon started (autonotify=%s, poll=%ss, min_gap=%smin, greens_required=%s)",
             ALTSEASON_AUTONOTIFY, ALTSEASON_POLL_SECONDS, ALTSEASON_NOTIFY_MIN_GAP_MIN, ALT_GREENS_REQUIRED)
    while not _daemon_stop.wait(ALTSEASON_POLL_SECONDS):
        try:
            state["last_tick_ts"] = int(time.time())
            s = _altseason_summary(_altseason_snapshot(force=False))
            now = time.time()
            need_send = False
            _update_daily_streaks(state, s)
            if s["ALTSEASON_ON"] and not state.get("last_on", False):
                need_send = True
            elif s["ALTSEASON_ON"]:
                min_gap = ALTSEASON_NOTIFY_MIN_GAP_MIN * 60
                if now - state.get("last_sent_ts", 0) >= min_gap:
                    need_send = True
            if need_send:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — ALTSEASON DÉBUTÉ !"
                res = send_telegram_ex(msg, pin=TELEGRAM_PIN_ALTSEASON)
                log.info("Altseason auto-notify: sent=%s pinned=%s err=%s", res.get("ok"), res.get("pinned"), res.get("error"))
                if res.get("ok"): state["last_sent_ts"] = int(now)
            state["last_on"] = bool(s["ALTSEASON_ON"])
            _save_state(state)
        except Exception as e:
            log.warning("Altseason daemon tick error: %s", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

