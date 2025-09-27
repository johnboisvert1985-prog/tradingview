# ============ main.py — SECTION 1/8 ============
import os
import re
import json
import time
import sqlite3
import logging
import threading
from typing import Optional, Dict, Any, List, Tuple
from string import Template
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("aitrader")

# -------------------------
# Config / ENV
# -------------------------
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

# DB path default = data/data.db; fallback auto to /tmp si read-only
DB_PATH = os.getenv("DB_PATH", "data/data.db")
DEBUG_MODE = os.getenv("DEBUG", "0") in ("1", "true", "True")

# -------------------------
# ALTSEASON thresholds
# -------------------------
ALT_BTC_DOM_THR = float(os.getenv("ALT_BTC_DOM_THR", "55.0"))
ALT_ETH_BTC_THR = float(os.getenv("ALT_ETH_BTC_THR", "0.045"))
ALT_ASI_THR = float(os.getenv("ALT_ASI_THR", "75.0"))
ALT_TOTAL2_THR_T = float(os.getenv("ALT_TOTAL2_THR_T", "1.78"))  # trillions
ALT_CACHE_TTL = int(os.getenv("ALT_CACHE_TTL", "120"))  # seconds
ALT_GREENS_REQUIRED = int(os.getenv("ALT_GREENS_REQUIRED", "3"))

TELEGRAM_PIN_ALTSEASON = os.getenv("TELEGRAM_PIN_ALTSEASON", "1") in ("1", "true", "True")
ALTSEASON_AUTONOTIFY = os.getenv("ALTSEASON_AUTONOTIFY", "1") in ("1", "true", "True")
ALTSEASON_POLL_SECONDS = int(os.getenv("ALTSEASON_POLL_SECONDS", "300"))
ALTSEASON_NOTIFY_MIN_GAP_MIN = int(os.getenv("ALTSEASON_NOTIFY_MIN_GAP_MIN", "60"))
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0

# -------------------------
# OpenAI client (optional)
# -------------------------
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
    """Joli libellé TF (ex: '15m', '1h', '1D')."""
    label = str(payload.get("tf_label") or payload.get("tf") or "?")
    try:
        if label.isdigit():
            n = int(label)
            if n < 60:
                return f"{n}m"
            if n % 60 == 0 and n < 1440:
                return f"{n//60}h"
            if n == 1440:
                return "1D"
    except Exception:
        pass
    return label

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

# =========================
# SQLite — init robuste
# =========================
def resolve_db_path() -> None:
    """Assure un chemin DB writable; fallback /tmp/ai_trader/data.db si besoin."""
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.info("DB dir OK: %s (using %s)", d, DB_PATH)
    except Exception as e:
        fallback_dir = "/tmp/ai_trader"
        os.makedirs(fallback_dir, exist_ok=True)
        DB_PATH = os.path.join(fallback_dir, "data.db")
        log.warning("DB dir '%s' not writable (%s). Falling back to %s", d, e, DB_PATH)

def db_conn() -> sqlite3.Connection:
    """Connexion SQLite avec options sensées."""
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
    """Crée la table events si absente."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                type TEXT,
                symbol TEXT,
                tf TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                trade_id TEXT,
                raw_json TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf ON events(tf)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

# Boot DB
resolve_db_path()
db_init()

# -------------------------
# Helpers généraux
# -------------------------
def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )

def fmt_num(v) -> str:
    try:
        if v is None:
            return "—"
        s = f"{float(v):,.6f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v or "—")

def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        if a is None or b is None or b == 0:
            return None
        return (a - b) / b * 100.0
    except Exception:
        return None

def parse_leverage_x(leverage: Optional[str]) -> Optional[float]:
    if not leverage:
        return None
    try:
        s = leverage.lower().replace("x", " ").split()
        for token in s:
            if token.replace(".", "", 1).isdigit():
                return float(token)
    except Exception:
        return None
    return None

# -------------------------
# Build trades & stats (robuste + CLOSE/OPEN)
# -------------------------
class TradeOutcome:
    NONE = "NONE"
    TP1 = "TP1_HIT"
    TP2 = "TP2_HIT"
    TP3 = "TP3_HIT"
    SL = "SL_HIT"
    CLOSE = "CLOSE"

FINAL_EVENTS = {TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3, TradeOutcome.SL, TradeOutcome.CLOSE}

def row_get(row: sqlite3.Row, key: str, default=None):
    """Accès sûr aux colonnes d'un sqlite3.Row (row['k'] ou valeur par défaut)."""
    try:
        return row[key]
    except Exception:
        return default

def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 0, 0, 0).timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 23, 59, 59).timestamp())
    except Exception:
        return None

def fetch_events_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    limit: int = 10000
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        sql += " AND symbol = ?"; args.append(symbol)
    if tf:
        sql += " AND tf = ?"; args.append(tf)
    if start_ep is not None:
        sql += " AND received_at >= ?"; args.append(start_ep)
    if end_ep is not None:
        sql += " AND received_at <= ?"; args.append(end_ep)
    sql += " ORDER BY received_at ASC"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return cur.fetchall()

def build_trades_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    max_rows: int = 20000
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)

    trades: List[Dict[str, Any]] = []
    open_by_tid: Dict[str, Dict[str, Any]] = {}                       # trades ouverts indexés par trade_id
    open_stack_by_key: Dict[Tuple[str, str], List[int]] = defaultdict(list)  # pile d'index par (symbol, tf)

    # Stats
    total = wins = losses = 0
    hit_tp1 = hit_tp2 = hit_tp3 = 0
    times_to_outcome: List[int] = []
    win_streak = loss_streak = 0
    best_win_streak = 0
    worst_loss_streak = 0

    def synth_tid(ev: sqlite3.Row) -> str:
        tid = row_get(ev, "trade_id")
        if tid:
            return tid
        return f"{row_get(ev,'symbol')}_{row_get(ev,'tf')}_{row_get(ev,'received_at')}"

    for ev in rows:
        etype = row_get(ev, "type")
        sym = row_get(ev, "symbol")
        tfv = row_get(ev, "tf")
        key = (sym, tfv)

        if etype == "ENTRY":
            tid = synth_tid(ev)
            t = {
                "trade_id": tid,
                "symbol": sym,
                "tf": tfv,
                "side": row_get(ev, "side"),
                "entry": row_get(ev, "entry"),
                "sl": row_get(ev, "sl"),
                "tp1": row_get(ev, "tp1"),
                "tp2": row_get(ev, "tp2"),
                "tp3": row_get(ev, "tp3"),
                "entry_time": row_get(ev, "received_at"),
                "outcome": TradeOutcome.NONE,
                "outcome_time": None,
                "duration_sec": None,
            }
            trades.append(t)
            open_by_tid[tid] = t
            open_stack_by_key[key].append(len(trades) - 1)
            continue

        if etype in FINAL_EVENTS:
            targ: Optional[Dict[str, Any]] = None
            tid = row_get(ev, "trade_id")
            if tid and tid in open_by_tid:
                targ = open_by_tid[tid]
            else:
                # rattacher au dernier trade encore ouvert du même (symbol, tf)
                stack = open_stack_by_key.get(key) or []
                while stack:
                    idx = stack[-1]
                    cand = trades[idx]
                    if cand["outcome"] == TradeOutcome.NONE:
                        targ = cand
                        break
                    else:
                        stack.pop()  # nettoyer si déjà clôturé

            if targ is not None and targ["outcome"] == TradeOutcome.NONE:
                targ["outcome"] = etype
                targ["outcome_time"] = row_get(ev, "received_at")
                if targ["entry_time"]:
                    targ["duration_sec"] = int(targ["outcome_time"] - targ["entry_time"])
                # fermer les index d'ouvert
                open_by_tid.pop(targ["trade_id"], None)
                if key in open_stack_by_key and open_stack_by_key[key]:
                    if open_stack_by_key[key][-1] == trades.index(targ):
                        open_stack_by_key[key].pop()

    # Stats (CLOSE = neutre, pas win/loss)
    for t in trades:
        if t["entry_time"] is not None:
            total += 1
        if t["outcome_time"] and t["entry_time"]:
            times_to_outcome.append(int(t["outcome_time"] - t["entry_time"]))

        if t["outcome"] in (TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3):
            wins += 1
            win_streak += 1
            loss_streak = 0
            best_win_streak = max(best_win_streak, win_streak)
            if t["outcome"] == TradeOutcome.TP1: hit_tp1 += 1
            elif t["outcome"] == TradeOutcome.TP2: hit_tp2 += 1
            elif t["outcome"] == TradeOutcome.TP3: hit_tp3 += 1
        elif t["outcome"] == TradeOutcome.SL:
            losses += 1
            loss_streak += 1
            win_streak = 0
            worst_loss_streak = max(worst_loss_streak, loss_streak)
        else:
            # NONE ou CLOSE -> neutre
            pass

    winrate = (wins / total * 100.0) if total else 0.0
    avg_sec = int(sum(times_to_outcome) / len(times_to_outcome)) if times_to_outcome else 0

    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "tp1_hits": hit_tp1,
        "tp2_hits": hit_tp2,
        "tp3_hits": hit_tp3,
        "avg_time_to_outcome_sec": avg_sec,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
    }
    return trades, summary

# --- Helpers d'affichage pour le dashboard trades ---
def chip_class(outcome: str) -> str:
    """Classe CSS pour le badge Outcome."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        return "chip win"
    if outcome == "SL_HIT":
        return "chip loss"
    if outcome == "CLOSE":
        return "chip close"
    return "chip open"  # NONE => trade encore ouvert

def outcome_label(outcome: str) -> str:
    """Texte lisible pour Outcome."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"):
        return outcome.replace("_HIT", "").title()  # TP1/TP2/TP3/Sl/Close
    return "OPEN"

def fmt_ts(ts: int | None, tz: timezone | None = None) -> str:
    """Formatte epoch en 'YYYY-MM-DD HH:MM:SS' (UTC par défaut)."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ts), tz or timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"
# ============ fin SECTION 1/8 ============
# ============ main.py — SECTION 2/8 (HTML templates) ============

# ---------- INDEX ----------
INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937}
th{color:#94a3b8}
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

# ---------- TRADES (PUBLIC) ----------
TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades — Dashboard</title>
<style>
:root{
  --bg:#0b1020; --card:#0f172a; --muted:#94a3b8; --border:#1e293b; --text:#e5e7eb;
  --grad1:#0ea5e9; --grad2:#8b5cf6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
  --chip:#0b1220; --chipb:#1f2937; --glass: rgba(255,255,255,.04);
}
*{box-sizing:border-box}
body{margin:0;background:
 radial-gradient(1200px 600px at 10% -10%, rgba(79,70,229,0.15), transparent 60%),
 radial-gradient(900px 500px at 120% 10%, rgba(14,165,233,0.14), transparent 60%),
 var(--bg);
 color:var(--text); font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 12px;font-size:28px;font-weight:800; letter-spacing:.2px}
h2{margin:0 0 12px;font-size:18px;color:var(--muted);font-weight:700}
.container{max-width:1180px;margin:28px auto;padding:0 16px}
.grid{display:grid;grid-template-columns:1.3fr .7fr;gap:16px}
.card{background:linear-gradient(180deg,var(--glass), transparent), var(--card);
 border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.25)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.muted{color:var(--muted)}
.kpi{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.kpi .box{background:#0b1426;border:1px solid var(--border);border-radius:12px;padding:10px}
.kpi .v{font-size:18px;font-weight:800}
.kpi .l{font-size:12px;color:var(--muted)}
.btn{display:inline-flex;gap:8px;align-items:center;padding:8px 12px;background:#0b1426;border:1px solid var(--border);color:var(--text);text-decoration:none;border-radius:10px}
.btn:hover{border-color:#334155}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;border:1px solid var(--chipb);background:var(--chip);font-size:12px}
.dot{width:8px;height:8px;border-radius:8px;background:#64748b}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.bad{background:var(--bad)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px;border-bottom:1px solid var(--border);text-align:left}
th{color:var(--muted);font-weight:700}
td small{color:var(--muted)}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;border:1px solid var(--chipb);background:var(--chip);font-weight:700}
.chip.win{color:#10b981;border-color:#164e3f;background:rgba(16,185,129,.08)}
.chip.loss{color:#ef4444;border-color:#4c1d1d;background:rgba(239,68,68,.08)}
.chip.close{color:#f59e0b;border-color:#4b3a16;background:rgba(245,158,11,.10)}
.chip.open {color:#38bdf8;border-color:#1f3a4b;background:rgba(56,189,248,.10)}
.tag{padding:.24rem .5rem;border:1px solid var(--chipb);border-radius:8px;background:#0b1426;color:var(--muted);font-size:12px}
.hr{height:1px;background:linear-gradient(90deg,transparent, #334155, transparent);margin:10px 0}
.table-wrap{overflow:auto;border-radius:12px;border:1px solid var(--border)}
tr:hover td{background:#0c1628}
.pills{display:flex;gap:4px;flex-wrap:wrap}
.pill{width:10px;height:10px;border-radius:10px;background:#475569;box-shadow:inset 0 0 0 1px #1e2937}
.pill.win{background:#16a34a} .pill.loss{background:#ef4444} .pill.none{background:#64748b}
.alt-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.alt-tbl{width:100%;border-collapse:separate;border-spacing:0 8px}
.alt-tbl th{font-size:12px;color:var(--muted);padding:4px 8px}
.alt-tbl td{padding:10px 12px;background:#0b1426;border:1px solid var(--border)}
.alt-tbl tr td:first-child{border-radius:10px 0 0 10px}
.alt-tbl tr td:last-child{border-radius:0 10px 10px 0}
.alt-h{font-weight:800;background:linear-gradient(90deg,var(--grad1),var(--grad2));-webkit-background-clip:text;background-clip:text;color:transparent}
.footer{display:flex;justify-content:space-between;align-items:center;gap:8px}
input{background:#0b1426;border:1px solid var(--border);color:#e5e7eb;padding:8px;border-radius:8px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.form-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:980px){ .grid{grid-template-columns:1fr} .form-grid{grid-template-columns:1fr 1fr} .kpi{grid-template-columns:repeat(3,1fr)} }
</style>
</head>
<body>
<div class="container">
  <div class="row" style="justify-content:space-between;margin-bottom:14px">
    <h1>Trades — Dashboard</h1>
    <div class="row">
      <a class="btn" href="/">🏠 Home</a>
      <a class="btn" href="/trades.csv?symbol=$symbol&tf=$tf&start=$start&end=$end&limit=$limit">⬇️ Export CSV</a>
    </div>
  </div>

  <!-- Filtres -->
  <div class="card">
    <form method="get">
      <div class="form-grid">
        <div><label>Symbol</label><input name="symbol" value="$symbol"></div>
        <div><label>TF</label><input name="tf" value="$tf"></div>
        <div><label>Start (YYYY-MM-DD)</label><input name="start" value="$start"></div>
        <div><label>End (YYYY-MM-DD)</label><input name="end" value="$end"></div>
        <div><label>Limit</label><input type="number" min="1" max="10000" name="limit" value="$limit"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn" type="submit">🔎 Apply</button>
      </div>
    </form>
  </div>

  <div class="grid">
    <!-- Colonne gauche : stats + tableau -->
    <div class="col">
      <div class="card">
        <h2>Résumé</h2>
        <div class="kpi">
          <div class="box"><div class="v">$total_trades</div><div class="l">Total trades</div></div>
          <div class="box"><div class="v">$winrate_pct%</div><div class="l">Winrate</div></div>
          <div class="box"><div class="v">$wins</div><div class="l">Wins</div></div>
          <div class="box"><div class="v">$losses</div><div class="l">Losses</div></div>
          <div class="box"><div class="v">$avg_time_to_outcome_sec</div><div class="l">Avg time (s)</div></div>
          <div class="box"><div class="v">$best_win_streak/$worst_loss_streak</div><div class="l">Best/Worst streak</div></div>
        </div>
        <div class="hr"></div>
        <div class="row" style="gap:12px">
          <span class="tag">TP1: <b>$tp1_hits</b></span>
          <span class="tag">TP2: <b>$tp2_hits</b></span>
          <span class="tag">TP3: <b>$tp3_hits</b></span>
          <div class="pills" id="spark-pills" title="Recent outcomes"></div>
        </div>
      </div>

      <div class="card">
        <h2>Historique</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Symbol</th><th>TF</th><th>Side</th>
                <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
                <th>Heure entrée</th>
                <th>Outcome</th><th>Duration (s)</th>
              </tr>
            </thead>
            <tbody>
              $rows_html
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Colonne droite : Altseason -->
    <div class="col">
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <h2 class="alt-h">Altseason — État rapide</h2>
          <span class="badge" id="alt-asof">Loading…</span>
        </div>
        <div class="alt-grid" style="margin-top:8px">
          <table class="alt-tbl">
            <thead><tr><th>Metric</th><th>Value</th><th>Threshold</th><th>Status</th></tr></thead>
            <tbody id="alt-rows">
              <tr>
                <td>BTC Dominance</td>
                <td><b id="alt-btc">—</b></td>
                <td>&lt; <span id="alt-btc-thr">$btc_thr</span>%</td>
                <td><span class="badge"><span class="dot" id="dot-btc"></span><span id="lab-btc">—</span></span></td>
              </tr>
              <tr>
                <td>ETH/BTC</td>
                <td><b id="alt-eth">—</b></td>
                <td>&gt; <span id="alt-eth-thr">$eth_thr</span></td>
                <td><span class="badge"><span class="dot" id="dot-eth"></span><span id="lab-eth">—</span></span></td>
              </tr>
              <tr>
                <td>Altseason Index</td>
                <td><b id="alt-asi">—</b></td>
                <td>≥ <span id="alt-asi-thr">$asi_thr</span></td>
                <td><span class="badge"><span class="dot" id="dot-asi"></span><span id="lab-asi">—</span></span></td>
              </tr>
              <tr>
                <td>TOTAL2</td>
                <td><b id="alt-t2">—</b></td>
                <td>&gt; <span id="alt-t2-thr">$t2_thr</span> T$</td>
                <td><span class="badge"><span class="dot" id="dot-t2"></span><span id="lab-t2">—</span></span></td>
              </tr>
            </tbody>
          </table>
          <div>
            <div class="row" style="gap:8px;margin-bottom:8px">
              <span class="badge" id="alt3">Prep 3/4: —</span>
              <span class="badge" id="alt4">Confirm 4/4: —</span>
            </div>
            <div class="muted">Séries (jours consécutifs): <b id="d3">0</b>d @3/4, <b id="d4">0</b>d @4/4</div>
            <div class="hr"></div>
            <div class="muted">Explication: pour considérer une Altseason active, il faut que <b id="greens-needed">3</b> signaux sur 4 soient au vert (configurable côté serveur).</div>
          </div>
        </div>
      </div>

      <div class="card footer">
        <div class="row">
          <span class="badge"><span class="dot ok"></span>TP1/TP2/TP3</span>
          <span class="badge"><span class="dot bad"></span>SL</span>
          <span class="badge"><span class="dot warn"></span>Close</span>
          <span class="badge"><span class="dot"></span>En attente</span>
        </div>
        <a class="btn" href="/trades-admin">🔐 Admin</a>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
  function setText(id, t){ const el=document.getElementById(id); if(el) el.textContent=t; }
  function setDot(id, cls){ const el=document.getElementById(id); if(el){ el.classList.remove('ok','warn','bad'); if(cls) el.classList.add(cls);} }
  function status(ok){ return ok ? "OK" : "—"; }
  function num(v){ return (v==null)? null : Number(v); }

  fetch("/altseason/check")
    .then(r=>r.json())
    .then(s=>{
      setText("alt-asof", "As of " + (s.asof || "now") + (s.stale ? " (cache)" : ""));
      const btc = num(s.btc_dominance), eth = num(s.eth_btc), t2 = num(s.total2_usd), asi = s.altseason_index;
      setText("alt-btc", (btc!=null && isFinite(btc)) ? btc.toFixed(2)+" %" : "—");
      setText("alt-eth", (eth!=null && isFinite(eth)) ? eth.toFixed(5) : "—");
      setText("alt-asi", (asi!=null) ? String(asi) : "N/A");
      setText("alt-t2",  (t2!=null && isFinite(t2)) ? (t2/1e12).toFixed(2)+" T$" : "—");
      const tr = s.triggers || {};
      setDot("dot-btc", tr.btc_dominance_ok ? "ok" : "bad"); setText("lab-btc", status(tr.btc_dominance_ok));
      setDot("dot-eth", tr.eth_btc_ok ? "ok" : "bad");       setText("lab-eth", status(tr.eth_btc_ok));
      setDot("dot-asi", tr.altseason_index_ok ? "ok":"bad"); setText("lab-asi", status(tr.altseason_index_ok));
      setDot("dot-t2",  tr.total2_ok ? "ok" : "bad");        setText("lab-t2", status(tr.total2_ok));
      const thr = s.thresholds || {};
      if (thr.greens_required != null) setText("greens-needed", String(thr.greens_required));
      if (thr.btc_dominance_max != null) setText("alt-btc-thr", Number(thr.btc_dominance_max).toFixed(2));
      if (thr.eth_btc_min != null)       setText("alt-eth-thr", Number(thr.eth_btc_min).toFixed(5));
      if (thr.total2_min_trillions != null) setText("alt-t2-thr", Number(thr.total2_min_trillions).toFixed(2));
      if (thr.altseason_index_min != null)  setText("alt-asi-thr", String(thr.altseason_index_min));
    })
    .catch(()=>{ setText("alt-asof","Erreur chargement"); });

  fetch("/altseason/streaks")
    .then(r=>r.json())
    .then(s=>{
      setText("alt3", (s.ALT3_ON ? "Prep 3/4: ON" : "Prep 3/4: OFF"));
      setText("alt4", (s.ALT4_ON ? "Confirm 4/4: ON" : "Confirm 4/4: OFF"));
      setText("d3", String(s.consec_3of4_days||0));
      setText("d4", String(s.consec_4of4_days||0));
    })
    .catch(()=>{});

  try{
    const holder = document.getElementById("pill-data");
    const raw = holder ? holder.textContent : "[]";
    const vals = JSON.parse(raw || "[]");
    const wrap = document.getElementById("spark-pills");
    vals.forEach(v=>{
      const d=document.createElement("div");
      d.className = "pill " + (v===1?"win":(v===0?"loss":"none"));
      wrap && wrap.appendChild(d);
    });
  }catch(_){}
})();
</script>
<script type="application/json" id="pill-data">$pill_values</script>
</body></html>
""")

# ---------- TRADES (ADMIN) ----------
TRADES_ADMIN_HTML_TPL = Template(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades (Admin)</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937}
th{color:#94a3b8}
.chip{display:inline-block;padding:2px 8px;border:1px solid #1f2937;border-radius:999px}
.badge-win{background:#052e1f;border-color:#065f46}
.badge-loss{background:#3f1d1d}
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
  <div class="row">
    <div>Total trades: <strong>$total_trades</strong></div>
    <div>Winrate: <strong>$winrate_pct%</strong></div>
    <div>W/L: <strong>$wins</strong>/<strong>$losses</strong></div>
    <div>TP1/2/3: <strong>$tp1_hits</strong>/<strong>$tp2_hits</strong>/<strong>$tp3_hits</strong></div>
    <div>Avg time (s): <strong>$avg_time_to_outcome_sec</strong></div>
    <div>Best/Worst streak: <strong>$best_win_streak</strong>/<strong>$worst_loss_streak</strong></div>
  </div>
</div>

<div class="card">
  <table><thead>
    <tr>
      <th>ID</th><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Heure entrée</th><th>Outcome</th><th>Duration (s)</th>
    </tr>
  </thead><tbody>
    $rows_html
  </tbody></table>
</div>
</body></html>
""")

# ---------- EVENTS ----------
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
# ============ fin SECTION 2/8 ============
# ============ main.py — SECTION 3/8 (Telegram utils + messages) ============

# Anti-spam (cooldown)
TELEGRAM_COOLDOWN_SECONDS = float(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1.5") or 1.5)
_last_tg = 0.0

def send_telegram(text: str) -> bool:
    """Envoi Telegram minimal (sans pin, sans inline keyboard)."""
    global _last_tg
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        now = time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            return False
        _last_tg = now

        import urllib.request, urllib.parse
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(api_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False

def send_telegram_ex(text: str, pin: bool = False) -> Dict[str, Any]:
    """
    Envoi enrichi (inline button vers /trades) + option pin.
    Retour: {ok, message_id, pinned, error}
    """
    result = {"ok": False, "message_id": None, "pinned": False, "error": None}
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        result["error"] = "Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
        return result

    try:
        import urllib.request, urllib.parse, json as _json, time as _time
        global _last_tg
        now = _time.time()
        if now - _last_tg < TELEGRAM_COOLDOWN_SECONDS:
            # on ne renvoie pas d'erreur dure : juste rate-limited
            result["ok"] = True
            result["error"] = "rate-limited (cooldown)"
            return result
        _last_tg = now

        api_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

        # sendMessage avec bouton "Voir les trades"
        send_url = f"{api_base}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "reply_markup": _json.dumps({
                "inline_keyboard": [[
                    {"text": "📊 Voir les trades", "url": "https://tradingview-gd03.onrender.com/trades"}
                ]]
            })
        }
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(send_url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            p = _json.loads(raw)
            if not p.get("ok"):
                result["error"] = f"sendMessage failed: {raw[:200]}"
                log.warning("Telegram sendMessage error: %s", result["error"])
                return result
            msg = p.get("result") or {}
            result["ok"] = True
            result["message_id"] = msg.get("message_id")

        # Pin optionnel
        if pin and result["message_id"] is not None:
            pin_url = f"{api_base}/pinChatMessage"
            pin_data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": result["message_id"],
            }).encode()
            preq = urllib.request.Request(pin_url, data=pin_data)
            try:
                with urllib.request.urlopen(preq, timeout=10) as presp:
                    praw = presp.read().decode("utf-8", "ignore")
                    pp = _json.loads(praw)
                    if pp.get("ok"):
                        result["pinned"] = True
                    else:
                        result["error"] = f"pinChatMessage failed: {praw[:200]}"
                        log.warning("Telegram pinChatMessage error: %s", result["error"])
            except Exception as e:
                result["error"] = f"pinChatMessage exception: {e}"
                log.warning("Telegram pin exception: %s", e)

        return result

    except Exception as e:
        result["error"] = f"send_telegram_ex exception: {e}"
        log.warning("Telegram send_telegram_ex exception: %s", e)
        return result

def telegram_rich_message(payload: Dict[str, Any]) -> Optional[str]:
    """
    Construit un message Telegram lisible pour les événements TradingView.
    Retourne None pour ignorer certains types (ex: AOE_*).
    """
    t = str(payload.get("type") or "EVENT").upper()
    if t in {"AOE_PREMIUM", "AOE_DISCOUNT"}:
        return None

    sym = str(payload.get("symbol") or "?")
    tf_lbl = tf_label_of(payload)
    side = str(payload.get("side") or "")
    entry = _to_float(payload.get("entry"))
    sl = _to_float(payload.get("sl"))
    tp = _to_float(payload.get("tp"))   # pour TP/SL hits: niveau exécuté
    tp1 = _to_float(payload.get("tp1"))
    tp2 = _to_float(payload.get("tp2"))
    tp3 = _to_float(payload.get("tp3"))
    leverage = payload.get("leverage") or payload.get("lev") or payload.get("lev_reco")
    lev_x = parse_leverage_x(str(leverage) if leverage is not None else None)

    def num(v): return fmt_num(v) if v is not None else "—"

    # ENTRY
    if t == "ENTRY":
        lines = []
        lines.append(f"📩 {sym} {tf_lbl}")
        if side:
            lines.append(("📈 Long Entry:" if side.upper()=="LONG" else "📉 Short Entry:") + f" {num(entry)}")
        if leverage:
            lines.append(f"💡Leverage: {leverage}")
        if tp1: lines.append(f"🎯 TP1: {num(tp1)}")
        if tp2: lines.append(f"🎯 TP2: {num(tp2)}")
        if tp3: lines.append(f"🎯 TP3: {num(tp3)}")
        if sl:  lines.append(f"❌ SL: {num(sl)}")
        # Option: Confiance LLM si activé côté env (laisse silencieux si indispo)
        try:
            if LLM_ENABLED and _openai_client and (FORCE_LLM or True):
                res = llm_confidence_for_entry(payload)
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

    # TP HITS
    if t in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        label = {"TP1_HIT":"Target #1","TP2_HIT":"Target #2","TP3_HIT":"Target #3"}[t]
        spot_pct = pct(tp, entry) if (side and tp is not None and entry is not None) else None
        lev_pct = (spot_pct * lev_x) if (spot_pct is not None and lev_x) else None
        lines = []
        lines.append(f"✅ {label} — {sym} {tf_lbl}")
        if tp is not None:
            lines.append(f"Mark price : {num(tp)}")
        if spot_pct is not None:
            base = f"Profit (spot) : {spot_pct:.2f}%"
            if lev_pct is not None:
                base += f" | avec {int(lev_x)}x : {lev_pct:.2f}%"
            lines.append(base)
        return "\n".join(lines)

    # SL
    if t == "SL_HIT":
        lines = [f"🟥 Stop-Loss — {sym} {tf_lbl}"]
        if tp is not None:
            lines.append(f"Exécuté : {num(tp)}")
        return "\n".join(lines)

    # CLOSE (fermeture neutre, ex flip de signal)
    if t == "CLOSE":
        reason = payload.get("reason")
        lines = [f"🔔 Close — {sym} {tf_lbl}"]
        if reason:
            lines.append(f"Raison: {reason}")
        # Note: côté dashboard, CLOSE est affiché en badge 'close' (neutre).
        return "\n".join(lines)

    # Fallback générique
    return f"[TV] {t} | {sym} | TF {tf_lbl}"
# ============ fin SECTION 3/8 ============
# ============ main.py — SECTION 4/8 (DB + modèles & builder des trades) ============

# --- SQLite: chemin résilient + connexion + init ---
DB_PATH = os.getenv("DB_PATH", "data/data.db")

def _resolve_db_path() -> None:
    """Assure un chemin DB inscriptible; fallback /tmp/ai_trader/data.db si besoin."""
    global DB_PATH
    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.info("DB dir OK: %s (using %s)", d, DB_PATH)
    except Exception as e:
        fallback_dir = "/tmp/ai_trader"
        os.makedirs(fallback_dir, exist_ok=True)
        DB_PATH = os.path.join(fallback_dir, "data.db")
        log.warning("DB dir '%s' not writable (%s). Falling back to %s", d, e, DB_PATH)

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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                type TEXT,
                symbol TEXT,
                tf TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                trade_id TEXT,
                raw_json TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_time  ON events(received_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_tf     ON events(tf)")
        conn.commit()
    log.info("DB initialized at %s", DB_PATH)

# Init au chargement
try:
    _resolve_db_path()
    db_init()
except Exception as e:
    log.warning("DB init skipped: %s", e)

# --- Persistance des events (webhook) ---
def save_event(payload: dict) -> None:
    """Insère un event TradingView tel quel dans la table `events`."""
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
        cur.execute(
            """
            INSERT INTO events (received_at, type, symbol, tf, side, entry, sl, tp1, tp2, tp3, trade_id, raw_json)
            VALUES (:received_at, :type, :symbol, :tf, :side, :entry, :sl, :tp1, :tp2, :tp3, :trade_id, :raw_json)
            """,
            row,
        )
        conn.commit()
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             row["type"], row["symbol"], row["tf"], row["trade_id"])

# --- Modèles & helpers pour la reconstruction des trades ---
class TradeOutcome:
    NONE  = "NONE"
    TP1   = "TP1_HIT"
    TP2   = "TP2_HIT"
    TP3   = "TP3_HIT"
    SL    = "SL_HIT"
    CLOSE = "CLOSE"

FINAL_EVENTS = {TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3,
                TradeOutcome.SL, TradeOutcome.CLOSE}

def row_get(row: sqlite3.Row, key: str, default=None):
    """Accès sûr aux colonnes d'un sqlite3.Row (pas de .get())."""
    try:
        return row[key]
    except Exception:
        return default

def parse_date_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 0, 0, 0).timestamp())
    except Exception:
        return None

def parse_date_end_to_epoch(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        y, m, d = map(int, date_str.split("-"))
        return int(dt.datetime(y, m, d, 23, 59, 59).timestamp())
    except Exception:
        return None

def fetch_events_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    limit: int = 10000
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: List[Any] = []
    if symbol:
        sql += " AND symbol = ?"; args.append(symbol)
    if tf:
        sql += " AND tf = ?"; args.append(tf)
    if start_ep is not None:
        sql += " AND received_at >= ?"; args.append(start_ep)
    if end_ep is not None:
        sql += " AND received_at <= ?"; args.append(end_ep)
    sql += " ORDER BY received_at ASC"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(args))
        return cur.fetchall()

def build_trades_filtered(
    symbol: Optional[str],
    tf: Optional[str],
    start_ep: Optional[int],
    end_ep: Optional[int],
    max_rows: int = 20000
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Reconstitue les trades à partir de la séquence d'events.
    - ENTRY crée un trade ouvert
    - TPx_HIT / SL_HIT / CLOSE ferment le trade le plus récent correspondant
      (par trade_id s'il est fourni, sinon par paire (symbol, tf) en LIFO).
    """
    rows = fetch_events_filtered(symbol, tf, start_ep, end_ep, max_rows)

    trades: List[Dict[str, Any]] = []
    open_by_tid: Dict[str, Dict[str, Any]] = {}                       # trades ouverts indexés par trade_id
    open_stack_by_key: Dict[Tuple[str, str], List[int]] = defaultdict(list)  # pile d'index par (symbol, tf)

    # Statistiques
    total = wins = losses = 0
    hit_tp1 = hit_tp2 = hit_tp3 = 0
    times_to_outcome: List[int] = []
    win_streak = loss_streak = 0
    best_win_streak = 0
    worst_loss_streak = 0

    def synth_tid(ev: sqlite3.Row) -> str:
        tid = row_get(ev, "trade_id")
        if tid:
            return tid
        return f"{row_get(ev,'symbol')}_{row_get(ev,'tf')}_{row_get(ev,'received_at')}"

    for ev in rows:
        etype = row_get(ev, "type")
        sym = row_get(ev, "symbol")
        tfv = row_get(ev, "tf")
        key = (sym, tfv)

        # OUVERTURE
        if etype == "ENTRY":
            tid = synth_tid(ev)
            t = {
                "trade_id": tid,
                "symbol": sym,
                "tf": tfv,
                "side": row_get(ev, "side"),
                "entry": row_get(ev, "entry"),
                "sl": row_get(ev, "sl"),
                "tp1": row_get(ev, "tp1"),
                "tp2": row_get(ev, "tp2"),
                "tp3": row_get(ev, "tp3"),
                "entry_time": row_get(ev, "received_at"),
                "outcome": TradeOutcome.NONE,
                "outcome_time": None,
                "duration_sec": None,
            }
            trades.append(t)
            open_by_tid[tid] = t
            open_stack_by_key[key].append(len(trades) - 1)
            continue

        # CLÔTURES
        if etype in FINAL_EVENTS:
            targ: Optional[Dict[str, Any]] = None
            tid = row_get(ev, "trade_id")

            # essaie par trade_id
            if tid and tid in open_by_tid:
                targ = open_by_tid[tid]
            else:
                # sinon, dernier trade ouvert pour (symbol, tf)
                stack = open_stack_by_key.get(key) or []
                while stack:
                    idx = stack[-1]
                    cand = trades[idx]
                    if cand["outcome"] == TradeOutcome.NONE:
                        targ = cand
                        break
                    else:
                        stack.pop()  # nettoie les fermés

            if targ is not None and targ["outcome"] == TradeOutcome.NONE:
                targ["outcome"] = etype
                targ["outcome_time"] = row_get(ev, "received_at")
                if targ["entry_time"] and targ["outcome_time"]:
                    targ["duration_sec"] = int(targ["outcome_time"] - targ["entry_time"])
                # fermer l'état ouvert
                open_by_tid.pop(targ["trade_id"], None)
                if key in open_stack_by_key and open_stack_by_key[key]:
                    if open_stack_by_key[key][-1] == trades.index(targ):
                        open_stack_by_key[key].pop()

    # Agrégation stats (CLOSE = neutre, ne compte pas en win/loss mais ferme le trade)
    for t in trades:
        if t["entry_time"] is not None:
            total += 1
        if t["outcome_time"] and t["entry_time"]:
            times_to_outcome.append(int(t["outcome_time"] - t["entry_time"]))

        if t["outcome"] in (TradeOutcome.TP1, TradeOutcome.TP2, TradeOutcome.TP3):
            wins += 1
            win_streak += 1
            loss_streak = 0
            best_win_streak = max(best_win_streak, win_streak)
            if t["outcome"] == TradeOutcome.TP1: hit_tp1 += 1
            elif t["outcome"] == TradeOutcome.TP2: hit_tp2 += 1
            elif t["outcome"] == TradeOutcome.TP3: hit_tp3 += 1
        elif t["outcome"] == TradeOutcome.SL:
            losses += 1
            loss_streak += 1
            win_streak = 0
            worst_loss_streak = max(worst_loss_streak, loss_streak)
        else:
            # NONE (toujours ouvert) ou CLOSE (neutre)
            pass

    winrate = (wins / total * 100.0) if total else 0.0
    avg_sec = int(sum(times_to_outcome) / len(times_to_outcome)) if times_to_outcome else 0

    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "tp1_hits": hit_tp1,
        "tp2_hits": hit_tp2,
        "tp3_hits": hit_tp3,
        "avg_time_to_outcome_sec": avg_sec,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
    }
    return trades, summary

# --- Helpers d'affichage pour le dashboard trades ---
from datetime import datetime, timezone

def chip_class(outcome: str) -> str:
    """Classe CSS pour le badge Outcome."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        return "chip win"
    if outcome == "SL_HIT":
        return "chip loss"
    if outcome == "CLOSE":
        return "chip close"
    return "chip open"  # NONE => trade encore ouvert

def outcome_label(outcome: str) -> str:
    """Texte lisible pour Outcome (NONE -> OPEN)."""
    if outcome in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CLOSE"):
        return outcome.replace("_HIT", "").title()  # TP1/TP2/TP3/Sl/Close
    return "OPEN"

def fmt_ts(ts: int | None, tz: timezone | None = None) -> str:
    """Formatte epoch en 'YYYY-MM-DD HH:MM:SS' (UTC par défaut)."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ts), tz or timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"

# ============ fin SECTION 4/8 ============
# ============ main.py — SECTION 5/8 (Altseason: fetch + cache + endpoints) ============

# ---------- Cache & fichiers ----------
_alt_cache: Dict[str, Any] = {"ts": 0, "snap": None}
ALTSEASON_STATE_FILE = os.getenv("ALTSEASON_STATE_FILE", "/tmp/altseason_state.json")

def _alt_cache_file_path() -> str:
    return os.getenv("ALT_CACHE_FILE", "/tmp/altseason_last.json")

def _load_last_snapshot() -> Optional[Dict[str, Any]]:
    try:
        p = _alt_cache_file_path()
        if not os.path.exists(p):
            return None
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

# ---------- Récupération snapshot marché (best-effort multi-fournisseurs) ----------
def _altseason_fetch() -> Dict[str, Any]:
    out = {"asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "errors": []}
    try:
        import requests
    except Exception:
        out["errors"].append("Missing dependency: requests")
        return out

    headers = {
        "User-Agent": "altseason-bot/1.6",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    def get_json(url: str, timeout: int = 12) -> Dict[str, Any]:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        body_preview = (r.text or "")[:220].replace("\n", " ").replace("\r", " ")
        if r.status_code != 200:
            raise RuntimeError(f"{url} -> HTTP {r.status_code}: {body_preview}")
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"{url} -> Non-JSON response: {body_preview}")

    # ===== Global mcap & BTC dominance =====
    mcap_usd = btc_dom = None
    try:
        alt = get_json("https://api.alternative.me/v2/global/")
        d0 = (alt.get("data") or [{}])[0]
        qusd = (d0.get("quotes") or {}).get("USD") or {}
        mcap = qusd.get("total_market_cap")
        dom = d0.get("bitcoin_percentage_of_market_cap")
        if mcap is not None and dom is not None:
            mcap_usd = float(mcap); btc_dom = float(dom)
    except Exception as e:
        out["errors"].append(f"alternative.me: {e!r}")

    if mcap_usd is None or btc_dom is None:
        try:
            g = get_json("https://api.coingecko.com/api/v3/global")
            data = g.get("data") or {}
            mcap_usd = float(data["total_market_cap"]["usd"])
            btc_dom = float(data["market_cap_percentage"]["btc"])
        except Exception as e:
            out["errors"].append(f"coingecko: {e!r}")

    if mcap_usd is None or btc_dom is None:
        try:
            pg = get_json("https://api.coinpaprika.com/v1/global")
            mcap_usd = float(pg["market_cap_usd"])
            btc_dom = float(pg["bitcoin_dominance_percentage"])
        except Exception as e:
            out["errors"].append(f"coinpaprika: {e!r}")

    if mcap_usd is None or btc_dom is None:
        try:
            cc = get_json("https://api.coincap.io/v2/assets?limit=2000")
            assets = cc.get("data") or []
            total = 0.0; btc_mcap = 0.0
            for a in assets:
                mc = a.get("marketCapUsd")
                if mc is not None:
                    try: total += float(mc)
                    except: pass
            for a in assets:
                if a.get("id") == "bitcoin":
                    try: btc_mcap = float(a.get("marketCapUsd") or 0.0)
                    except: btc_mcap = 0.0
                    break
            if total > 0:
                mcap_usd = total; btc_dom = (btc_mcap / total) * 100.0
        except Exception as e:
            out["errors"].append(f"coincap: {e!r}")

    if mcap_usd is None or btc_dom is None:
        try:
            cl = get_json("https://api.coinlore.net/api/global/")
            g = cl[0] if isinstance(cl, list) and cl else cl
            mcap = g.get("total_mcap_usd") or g.get("total_mcap") or g.get("mcap_total_usd")
            dom = g.get("btc_d") or g.get("bitcoin_dominance_percentage") or g.get("btc_dominance")
            if mcap is not None and dom is not None:
                mcap_usd = float(mcap); btc_dom = float(dom)
        except Exception as e:
            out["errors"].append(f"coinlore: {e!r}")

    out["total_mcap_usd"] = (None if mcap_usd is None else float(mcap_usd))
    out["btc_dominance"] = (None if btc_dom is None else float(btc_dom))
    out["total2_usd"] = (None if (mcap_usd is None or btc_dom is None) else float(mcap_usd * (1.0 - btc_dom/100.0)))

    # ===== ETH/BTC =====
    eth_btc = None
    try:
        j = get_json("https://api.binance.com/api/v3/ticker/price?symbol=ETHBTC")
        eth_btc = float(j["price"])
    except Exception as e:
        out["errors"].append(f"binance: {e!r}")

    if eth_btc is None:
        try:
            sp = get_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=btc,usd")
            eth_btc = float(sp["ethereum"]["btc"])
        except Exception as e:
            out["errors"].append(f"coingecko_simple: {e!r}")

    if eth_btc is None:
        try:
            tkr = get_json("https://api.coinpaprika.com/v1/tickers/eth-ethereum?quotes=BTC")
            eth_btc = float(tkr["quotes"]["BTC"]["price"])
        except Exception as e:
            out["errors"].append(f"coinpaprika_ethbtc: {e!r}")

    if eth_btc is None:
        try:
            cc_eth = get_json("https://api.coincap.io/v2/assets/ethereum")
            cc_btc = get_json("https://api.coincap.io/v2/assets/bitcoin")
            eth_usd = float(cc_eth["data"]["priceUsd"])
            btc_usd = float(cc_btc["data"]["priceUsd"])
            eth_btc = eth_usd / btc_usd
        except Exception as e:
            out["errors"].append(f"coincap_ethbtc: {e!r}")

    out["eth_btc"] = (None if eth_btc is None else float(eth_btc))

    # ===== Altseason Index (scrape léger) =====
    out["altseason_index"] = None
    try:
        import requests
        from bs4 import BeautifulSoup
        html = requests.get("https://www.blockchaincenter.net/altcoin-season-index/",
                            timeout=12, headers=headers).text
        soup = BeautifulSoup(html, "html.parser")
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"Altcoin Season Index[^0-9]*([0-9]{2,3})", txt)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100:
                out["altseason_index"] = v
    except Exception as e:
        out["errors"].append(f"altseason_index_scrape: {e!r}")

    return out

# ---------- Résumé et comparaison aux seuils ----------
def _ok_cmp(val: Optional[float], thr: float, direction: str) -> bool:
    if val is None:
        return False
    return (val < thr) if direction == "below" else (val > thr)

def _altseason_summary(snap: Dict[str, Any]) -> Dict[str, Any]:
    btc = snap.get("btc_dominance")
    eth = snap.get("eth_btc")
    t2  = snap.get("total2_usd")
    asi = snap.get("altseason_index")

    btc_ok = _ok_cmp(btc, ALT_BTC_DOM_THR, "below")
    eth_ok = _ok_cmp(eth, ALT_ETH_BTC_THR, "above")
    t2_ok  = _ok_cmp(t2,  ALT_TOTAL2_THR_T * 1e12, "above")
    asi_ok = (asi is not None) and _ok_cmp(float(asi), ALT_ASI_THR, "above")

    greens = sum([btc_ok, eth_ok, t2_ok, asi_ok])
    on = greens >= ALT_GREENS_REQUIRED

    return {
        "asof": snap.get("asof"),
        "stale": bool(snap.get("stale", False)),
        "errors": snap.get("errors", []),
        "btc_dominance": (None if btc is None else float(btc)),
        "eth_btc": (None if eth is None else float(eth)),
        "total2_usd": (None if t2 is None else float(t2)),
        "altseason_index": (None if asi is None else int(asi)),
        "thresholds": {
            "btc_dominance_max": ALT_BTC_DOM_THR,
            "eth_btc_min": ALT_ETH_BTC_THR,
            "altseason_index_min": ALT_ASI_THR,
            "total2_min_trillions": ALT_TOTAL2_THR_T,
            "greens_required": ALT_GREENS_REQUIRED
        },
        "triggers": {
            "btc_dominance_ok": btc_ok,
            "eth_btc_ok": eth_ok,
            "total2_ok": t2_ok,
            "altseason_index_ok": asi_ok
        },
        "greens": greens,
        "ALTSEASON_ON": on
    }

# ---------- Cache mémoire/disque ----------
def _altseason_snapshot(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if (not force) and _alt_cache["snap"] and (now - _alt_cache["ts"] < ALT_CACHE_TTL):
        snap = dict(_alt_cache["snap"])
        snap.setdefault("stale", False)
        return snap
    try:
        snap = _altseason_fetch()
        snap["stale"] = False
        _alt_cache["snap"] = snap
        _alt_cache["ts"] = now
        _save_last_snapshot(snap)
        return snap
    except Exception as e:
        # fallback: dernier en mémoire/disque
        if _alt_cache["snap"]:
            s = dict(_alt_cache["snap"])
            s["stale"] = True
            s.setdefault("errors", []).append(f"live_fetch_exception: {e!r}")
            return s
        disk = _load_last_snapshot()
        if isinstance(disk, dict):
            disk = dict(disk)
            disk["stale"] = True
            disk.setdefault("errors", []).append(f"live_fetch_exception: {e!r}")
            return disk
        return {
            "asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "btc_dominance": None, "eth_btc": None, "total2_usd": None, "altseason_index": None,
            "errors": [f"live_fetch_exception: {e!r}"], "stale": True,
        }

# ---------- Endpoints publics Altseason ----------
@app.get("/altseason/check")
def altseason_check_public():
    snap = _altseason_snapshot(force=False)
    return _altseason_summary(snap)

# ---------- Streaks (3/4 et 4/4) ----------
def _load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(ALTSEASON_STATE_FILE):
            with open(ALTSEASON_STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {
        "last_on": False, "last_sent_ts": 0, "last_tick_ts": 0,
        "consec_3of4_days": 0, "consec_4of4_days": 0,
        "last_streak_date": None  # "YYYY-MM-DD" UTC
    }

def _save_state(state: Dict[str, Any]) -> None:
    try:
        d = os.path.dirname(ALTSEASON_STATE_FILE) or "/tmp"
        os.makedirs(d, exist_ok=True)
        with open(ALTSEASON_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def _today_utc_str() -> str:
    import datetime as dt
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def _update_daily_streaks(state: Dict[str, Any], summary: Dict[str, Any]) -> None:
    """Met à jour les compteurs journaliers 3/4 et 4/4 au changement de date UTC."""
    import datetime as dt
    today = _today_utc_str()
    last_date = state.get("last_streak_date")
    if last_date == today:
        return

    greens = int(summary.get("greens") or 0)
    is3 = greens >= 3
    is4 = greens >= 4

    if last_date is None:
        state["consec_3of4_days"] = 1 if is3 else 0
        state["consec_4of4_days"] = 1 if is4 else 0
    else:
        try:
            d_last = dt.datetime.strptime(last_date, "%Y-%m-%d")
            d_today = dt.datetime.strptime(today, "%Y-%m-%d")
            consecutive = (d_today - d_last).days == 1
        except Exception:
            consecutive = False

        if consecutive:
            state["consec_3of4_days"] = (state.get("consec_3of4_days", 0) + 1) if is3 else 0
            state["consec_4of4_days"] = (state.get("consec_4of4_days", 0) + 1) if is4 else 0
        else:
            state["consec_3of4_days"] = 1 if is3 else 0
            state["consec_4of4_days"] = 1 if is4 else 0

    state["last_streak_date"] = today

@app.get("/altseason/streaks")
def altseason_streaks():
    """Expose l'état 3/4 et 4/4 + compteurs de jours consécutifs (basé sur UTC)."""
    st = _load_state()
    s = _altseason_summary(_altseason_snapshot(force=False))
    _update_daily_streaks(st, s)
    _save_state(st)
    return {
        "asof": s.get("asof"),
        "greens": s.get("greens"),
        "ALT3_ON": bool(int(s.get("greens") or 0) >= 3),
        "ALT4_ON": bool(int(s.get("greens") or 0) >= 4),
        "consec_3of4_days": int(st.get("consec_3of4_days") or 0),
        "consec_4of4_days": int(st.get("consec_4of4_days") or 0),
    }

@app.get("/altseason/daemon-status")
def altseason_daemon_status():
    st = _load_state()
    return {
        "autonotify_enabled": ALTSEASON_AUTONOTIFY,
        "poll_seconds": ALTSEASON_POLL_SECONDS,
        "notify_min_gap_min": ALTSEASON_NOTIFY_MIN_GAP_MIN,
        "greens_required": ALT_GREENS_REQUIRED,
        "state": st
    }

# ---------- Notify manuel (protégé) ----------
@app.api_route("/altseason/notify", methods=["GET", "POST"])
async def altseason_notify(
    request: Request,
    secret: Optional[str] = Query(None),
    force: Optional[bool] = Query(False),
    message: Optional[str] = Query(None),
    pin: Optional[bool] = Query(False)
):
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    body_secret = body.get("secret") if isinstance(body, dict) else None
    if WEBHOOK_SECRET and (secret != WEBHOOK_SECRET and body_secret != WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")

    if request.method == "POST":
        force = bool(body.get("force", force))
        message = body.get("message", message)
        pin = bool(body.get("pin", pin))
    pin = bool(pin or TELEGRAM_PIN_ALTSEASON)

    s = _altseason_summary(_altseason_snapshot(force=bool(force)))
    sent = None
    pin_res = None
    if s["ALTSEASON_ON"] or force:
        if message:
            msg = message
        else:
            if s["ALTSEASON_ON"]:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — ALTSEASON DÉBUTÉ !"
            else:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — EN VEILLE (conditions insuffisantes)"
        pin_result = send_telegram_ex(msg, pin=bool(pin))
        sent = pin_result.get("ok")
        pin_res = {"pinned": pin_result.get("pinned"),
                   "message_id": pin_result.get("message_id"),
                   "error": pin_result.get("error")}
        log.info("Altseason notify: sent=%s pinned=%s err=%s",
                 sent, pin_res.get("pinned"), pin_res.get("error"))

    return {"summary": s, "telegram_sent": sent, "pin_result": pin_res}

# ============ fin SECTION 5/8 ============
# ============ main.py — SECTION 6/8 (HTML templates) ============

from string import Template

INDEX_HTML_TPL = Template(r"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader PRO - Status</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0;font-size:28px;font-weight:700}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937}
th{color:#94a3b8}
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
  <div style="margin-top:8px">
    <span class="chip">ENTRY</span><span class="chip">TP1_HIT</span><span class="chip">TP2_HIT</span>
    <span class="chip">TP3_HIT</span><span class="chip">SL_HIT</span><span class="chip">CLOSE</span>
  </div>
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
  <div class="muted" style="margin-top:6px">Séries: <span id="d3">0</span>d @3/4, <span id="d4">0</span>d @4/4</div>
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

TRADES_PUBLIC_HTML_TPL = Template(r"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades — Dashboard</title>
<style>
:root{
  --bg:#0b1020; --card:#0f172a; --muted:#94a3b8; --border:#1e293b; --text:#e5e7eb;
  --grad1:#0ea5e9; --grad2:#8b5cf6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
  --chip:#0b1220; --chipb:#1f2937; --glass: rgba(255,255,255,.04);
}
*{box-sizing:border-box}
body{margin:0;background:
 radial-gradient(1200px 600px at 10% -10%, rgba(79,70,229,0.15), transparent 60%),
 radial-gradient(900px 500px at 120% 10%, rgba(14,165,233,0.14), transparent 60%),
 var(--bg);
 color:var(--text); font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 12px;font-size:28px;font-weight:800; letter-spacing:.2px}
h2{margin:0 0 12px;font-size:18px;color:var(--muted);font-weight:700}
.container{max-width:1180px;margin:28px auto;padding:0 16px}
.grid{display:grid;grid-template-columns:1.3fr .7fr;gap:16px}
.card{background:linear-gradient(180deg,var(--glass), transparent), var(--card);
 border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.25)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.muted{color:var(--muted)}
.kpi{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.kpi .box{background:#0b1426;border:1px solid var(--border);border-radius:12px;padding:10px}
.kpi .v{font-size:18px;font-weight:800}
.kpi .l{font-size:12px;color:var(--muted)}
.btn{display:inline-flex;gap:8px;align-items:center;padding:8px 12px;background:#0b1426;border:1px solid var(--border);color:var(--text);text-decoration:none;border-radius:10px}
.btn:hover{border-color:#334155}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;border:1px solid var(--chipb);background:var(--chip);font-size:12px}
.dot{width:8px;height:8px;border-radius:8px;background:#64748b}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.bad{background:var(--bad)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px;border-bottom:1px solid var(--border);text-align:left}
th{color:var(--muted);font-weight:700}
td small{color:var(--muted)}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;border:1px solid var(--chipb);background:var(--chip);font-weight:700}
.chip.win{color:#10b981;border-color:#164e3f;background:rgba(16,185,129,.08)}
.chip.loss{color:#ef4444;border-color:#4c1d1d;background:rgba(239,68,68,.08)}
.chip.close{color:#f59e0b;border-color:#4b3a16;background:rgba(245,158,11,.10)}
.chip.open {color:#38bdf8;border-color:#1f3a4b;background:rgba(56,189,248,.10)}
.tag{padding:.24rem .5rem;border:1px solid var(--chipb);border-radius:8px;background:#0b1426;color:var(--muted);font-size:12px}
.hr{height:1px;background:linear-gradient(90deg,transparent, #334155, transparent);margin:10px 0}
.table-wrap{overflow:auto;border-radius:12px;border:1px solid var(--border)}
tr:hover td{background:#0c1628}
.pills{display:flex;gap:4px;flex-wrap:wrap}
.pill{width:10px;height:10px;border-radius:10px;background:#475569;box-shadow:inset 0 0 0 1px #1e2937}
.pill.win{background:#16a34a} .pill.loss{background:#ef4444} .pill.none{background:#64748b}
.alt-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.alt-tbl{width:100%;border-collapse:separate;border-spacing:0 8px}
.alt-tbl th{font-size:12px;color:var(--muted);padding:4px 8px}
.alt-tbl td{padding:10px 12px;background:#0b1426;border:1px solid var(--border)}
.alt-tbl tr td:first-child{border-radius:10px 0 0 10px}
.alt-tbl tr td:last-child{border-radius:0 10px 10px 0}
.alt-h{font-weight:800;background:linear-gradient(90deg,var(--grad1),var(--grad2));-webkit-background-clip:text;background-clip:text;color:transparent}
.footer{display:flex;justify-content:space-between;align-items:center;gap:8px}
input{background:#0b1426;border:1px solid var(--border);color:var(--text);padding:8px;border-radius:8px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.form-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:980px){ .grid{grid-template-columns:1fr} .form-grid{grid-template-columns:1fr 1fr} .kpi{grid-template-columns:repeat(3,1fr)} }
</style>
</head>
<body>
<div class="container">
  <div class="row" style="justify-content:space-between;margin-bottom:14px">
    <h1>Trades — Dashboard</h1>
    <div class="row">
      <a class="btn" href="/">🏠 Home</a>
      <a class="btn" href="/trades.csv?symbol=$symbol&tf=$tf&start=$start&end=$end&limit=$limit">⬇️ Export CSV</a>
    </div>
  </div>

  <div class="card">
    <form method="get">
      <div class="form-grid">
        <div><label>Symbol</label><input name="symbol" value="$symbol"></div>
        <div><label>TF</label><input name="tf" value="$tf"></div>
        <div><label>Start (YYYY-MM-DD)</label><input name="start" value="$start"></div>
        <div><label>End (YYYY-MM-DD)</label><input name="end" value="$end"></div>
        <div><label>Limit</label><input type="number" min="1" max="10000" name="limit" value="$limit"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn" type="submit">🔎 Apply</button>
      </div>
    </form>
  </div>

  <div class="grid">
    <div class="col">
      <div class="card">
        <h2>Résumé</h2>
        <div class="kpi">
          <div class="box"><div class="v">$total_trades</div><div class="l">Total trades</div></div>
          <div class="box"><div class="v">$winrate_pct%</div><div class="l">Winrate</div></div>
          <div class="box"><div class="v">$wins</div><div class="l">Wins</div></div>
          <div class="box"><div class="v">$losses</div><div class="l">Losses</div></div>
          <div class="box"><div class="v">$avg_time_to_outcome_sec</div><div class="l">Avg time (s)</div></div>
          <div class="box"><div class="v">$best_win_streak/$worst_loss_streak</div><div class="l">Best/Worst streak</div></div>
        </div>
        <div class="hr"></div>
        <div class="row" style="gap:12px">
          <span class="tag">TP1: <b>$tp1_hits</b></span>
          <span class="tag">TP2: <b>$tp2_hits</b></span>
          <span class="tag">TP3: <b>$tp3_hits</b></span>
          <div class="pills" id="spark-pills" title="Recent outcomes"></div>
        </div>
      </div>

      <div class="card">
        <h2>Historique</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Symbol</th><th>TF</th><th>Side</th>
                <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
                <th>Heure entrée</th>
                <th>Outcome</th><th>Duration (s)</th>
              </tr>
            </thead>
            <tbody>
              $rows_html
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="col">
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <h2 class="alt-h">Altseason — État rapide</h2>
          <span class="badge" id="alt-asof">Loading…</span>
        </div>
        <div class="alt-grid" style="margin-top:8px">
          <table class="alt-tbl">
            <thead><tr><th>Metric</th><th>Value</th><th>Threshold</th><th>Status</th></tr></thead>
            <tbody id="alt-rows">
              <tr>
                <td>BTC Dominance</td>
                <td><b id="alt-btc">—</b></td>
                <td>&lt; <span id="alt-btc-thr">$btc_thr</span>%</td>
                <td><span class="badge"><span class="dot" id="dot-btc"></span><span id="lab-btc">—</span></span></td>
              </tr>
              <tr>
                <td>ETH/BTC</td>
                <td><b id="alt-eth">—</b></td>
                <td>&gt; <span id="alt-eth-thr">$eth_thr</span></td>
                <td><span class="badge"><span class="dot" id="dot-eth"></span><span id="lab-eth">—</span></span></td>
              </tr>
              <tr>
                <td>Altseason Index</td>
                <td><b id="alt-asi">—</b></td>
                <td>≥ <span id="alt-asi-thr">$asi_thr</span></td>
                <td><span class="badge"><span class="dot" id="dot-asi"></span><span id="lab-asi">—</span></span></td>
              </tr>
              <tr>
                <td>TOTAL2</td>
                <td><b id="alt-t2">—</b></td>
                <td>&gt; <span id="alt-t2-thr">$t2_thr</span> T$</td>
                <td><span class="badge"><span class="dot" id="dot-t2"></span><span id="lab-t2">—</span></span></td>
              </tr>
            </tbody>
          </table>
          <div>
            <div class="row" style="gap:8px;margin-bottom:8px">
              <span class="badge" id="alt3">Prep 3/4: —</span>
              <span class="badge" id="alt4">Confirm 4/4: —</span>
            </div>
            <div class="muted">Séries (jours consécutifs): <b id="d3">0</b>d @3/4, <b id="d4">0</b>d @4/4</div>
            <div class="hr"></div>
            <div class="muted">Explication: <b id="greens-needed">3</b> signaux sur 4 au vert ⇒ Altseason.</div>
          </div>
        </div>
      </div>

      <div class="card footer">
        <div class="row">
          <span class="badge"><span class="dot ok"></span>TP1/TP2/TP3</span>
          <span class="badge"><span class="dot bad"></span>SL</span>
          <span class="badge"><span class="dot warn"></span>Close</span>
          <span class="badge"><span class="dot"></span>En attente</span>
        </div>
        <a class="btn" href="/trades-admin">🔐 Admin</a>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
  function setText(id, t){ const el=document.getElementById(id); if(el) el.textContent=t; }
  function setDot(id, cls){ const el=document.getElementById(id); if(el){ el.classList.remove('ok','warn','bad'); if(cls) el.classList.add(cls);} }
  function status(ok){ return ok ? "OK" : "—"; }
  function num(v){ return (v==null)? null : Number(v); }

  fetch("/altseason/check")
    .then(r=>r.json())
    .then(s=>{
      setText("alt-asof", "As of " + (s.asof || "now") + (s.stale ? " (cache)" : ""));
      const btc = num(s.btc_dominance), eth = num(s.eth_btc), t2 = num(s.total2_usd), asi = s.altseason_index;
      setText("alt-btc", (btc!=null && isFinite(btc)) ? btc.toFixed(2)+" %" : "—");
      setText("alt-eth", (eth!=null && isFinite(eth)) ? eth.toFixed(5) : "—");
      setText("alt-asi", (asi!=null) ? String(asi) : "N/A");
      setText("alt-t2",  (t2!=null && isFinite(t2)) ? (t2/1e12).toFixed(2)+" T$" : "—");

      const tr = s.triggers || {};
      setDot("dot-btc", tr.btc_dominance_ok ? "ok" : "bad"); setText("lab-btc", status(tr.btc_dominance_ok));
      setDot("dot-eth", tr.eth_btc_ok ? "ok" : "bad");       setText("lab-eth", status(tr.eth_btc_ok));
      setDot("dot-asi", tr.altseason_index_ok ? "ok":"bad"); setText("lab-asi", status(tr.altseason_index_ok));
      setDot("dot-t2",  tr.total2_ok ? "ok" : "bad");        setText("lab-t2", status(tr.total2_ok));

      const thr = s.thresholds || {};
      if (thr.greens_required != null) setText("greens-needed", String(thr.greens_required));
      if (thr.btc_dominance_max != null) setText("alt-btc-thr", Number(thr.btc_dominance_max).toFixed(2));
      if (thr.eth_btc_min != null)       setText("alt-eth-thr", Number(thr.eth_btc_min).toFixed(5));
      if (thr.total2_min_trillions != null) setText("alt-t2-thr", Number(thr.total2_min_trillions).toFixed(2));
      if (thr.altseason_index_min != null)  setText("alt-asi-thr", String(thr.altseason_index_min));
    })
    .catch(()=>{ setText("alt-asof","Erreur chargement"); });

  fetch("/altseason/streaks")
    .then(r=>r.json())
    .then(s=>{
      setText("alt3", (s.ALT3_ON ? "Prep 3/4: ON" : "Prep 3/4: OFF"));
      setText("alt4", (s.ALT4_ON ? "Confirm 4/4: ON" : "Confirm 4/4: OFF"));
      setText("d3", String(s.consec_3of4_days||0));
      setText("d4", String(s.consec_4of4_days||0));
    })
    .catch(()=>{});

  try{
    const holder = document.getElementById("pill-data");
    const raw = holder ? holder.textContent : "[]";
    const vals = JSON.parse(raw || "[]");
    const wrap = document.getElementById("spark-pills");
    vals.forEach(v=>{
      const d=document.createElement("div");
      d.className = "pill " + (v===1?"win":(v===0?"loss":"none"));
      wrap && wrap.appendChild(d);
    });
  }catch(_){}
})();
</script>

<script type="application/json" id="pill-data">$pill_values</script>
</body></html>
""")

TRADES_ADMIN_HTML_TPL = Template(r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades (Admin)</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937}
th{color:#94a3b8}
.chip{display:inline-block;padding:2px 8px;border:1px solid #1f2937;border-radius:999px}
.badge-win{background:#052e1f;border-color:#065f46}
.badge-loss{background:#3f1d1d}
label{display:block;margin:6px 0 2px}
.row{display:flex;gap:10px;flex-wrap:wrap}
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
  <div class="row">
    <div>Total trades: <strong>$total_trades</strong></div>
    <div>Winrate: <strong>$winrate_pct%</strong></div>
    <div>W/L: <strong>$wins</strong>/<strong>$losses</strong></div>
    <div>TP1/2/3: <strong>$tp1_hits</strong>/<strong>$tp2_hits</strong>/<strong>$tp3_hits</strong></div>
    <div>Avg time (s): <strong>$avg_time_to_outcome_sec</strong></div>
    <div>Best/Worst streak: <strong>$best_win_streak</strong>/<strong>$worst_loss_streak</strong></div>
  </div>
</div>

<div class="card">
  <table><thead>
    <tr>
      <th>ID</th><th>Symbol</th><th>TF</th><th>Side</th>
      <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
      <th>Heure entrée</th>
      <th>Outcome</th><th>Duration (s)</th>
    </tr>
  </thead><tbody>
    $rows_html
  </tbody></table>
</div>
</body></html>
""")

EVENTS_HTML_TPL = Template(r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Events</title>
<style>
body{margin:0;padding:24px;background:#0f172a;color:#e5e7eb;font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial}
h1{margin:0 0 16px 0}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937}
th{color:#94a3b8}
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
# ============ fin SECTION 6/8 ============
# ============ main.py — SECTION 7/8 (Routes Trades/Events + Exports) ============

from fastapi import Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# -------- Trades JSON (PROTÉGÉ) --------
@app.get("/trades.json")
def trades_json(
    secret: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit * 10))
    data = trades[-limit:] if limit else trades
    return JSONResponse({"summary": summary, "trades": data})

# -------- Trades CSV (PROTÉGÉ) --------
@app.get("/trades.csv")
def trades_csv(
    secret: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(1000)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, _ = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(10000, limit * 10))
    data = trades[-limit:] if limit else trades
    headers = ["trade_id","symbol","tf","side","entry","sl","tp1","tp2","tp3",
               "entry_time","outcome","outcome_time","duration_sec"]
    lines = [",".join(headers)]
    for tr in data:
        row = [str(tr.get(h, "")) for h in headers]
        row = [("\"%s\"" % x) if ("," in x) else x for x in row]
        lines.append(",".join(row))
    return Response(content="\n".join(lines), media_type="text/csv")

# -------- Events (HTML, PROTÉGÉ) --------
@app.get("/events", response_class=HTMLResponse)
def events(secret: Optional[str] = Query(None), limit: int = Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()

    def fmt_time(ts: int) -> str:
        try:
            import datetime as dt
            return dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(ts)

    rows_html = ""
    for r in rows:
        rows_html += (
            "<tr>"
            f"<td>{escape_html(fmt_time(r['received_at']))}</td>"
            f"<td>{escape_html(r['type'] or '')}</td>"
            f"<td>{escape_html(r['symbol'] or '')}</td>"
            f"<td>{escape_html(r['tf'] or '')}</td>"
            f"<td>{escape_html(r['side'] or '')}</td>"
            f"<td>{escape_html(r['trade_id'] or '')}</td>"
            f"<td><pre style='white-space:pre-wrap;margin:0'>{escape_html(r['raw_json'] or '')}</pre></td>"
            "</tr>"
        )

    html = EVENTS_HTML_TPL.safe_substitute(
        secret=escape_html(secret or ""),
        rows_html=rows_html or '<tr><td colspan="7" class="muted">No events.</td></tr>'
    )
    return HTMLResponse(html)

# -------- Events JSON (PROTÉGÉ) --------
@app.get("/events.json")
def events_json(secret: Optional[str] = Query(None), limit: int = Query(200)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    return JSONResponse({"events": rows})

# -------- Alias admin --------
@app.get("/trades/secret={secret}")
def trades_alias(secret: str):
    return RedirectResponse(url=f"/trades-admin?secret={secret}", status_code=307)

# -------- Reset (PROTÉGÉ) --------
@app.get("/reset")
def reset_all(
    secret: Optional[str] = Query(None),
    confirm: Optional[str] = Query(None),
    redirect: Optional[str] = Query(None)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    if confirm not in ("yes","true","1","YES","True"):
        return {"ok": False, "error": "Confirmation required: add &confirm=yes"}

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM events")
        conn.commit()

    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    return {"ok": True, "deleted": "all"}

# -------- Trades PUBLIC (avec Heure entrée + badges OPEN/CLOSE/WIN/LOSS) --------
@app.get("/trades", response_class=HTMLResponse)
def trades_public(
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit * 10))

    rows_html = ""
    data = trades[-limit:] if limit else trades
    for tr in data:
        outcome = tr.get("outcome") or "NONE"
        badge = chip_class(outcome)         # chip win / chip loss / chip close / chip open
        label = outcome_label(outcome)      # TP1 / TP2 / TP3 / SL / Close / OPEN
        rows_html += (
            "<tr>"
            f"<td>{escape_html(str(tr.get('trade_id')))}</td>"
            f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
            f"<td>{fmt_num(tr.get('entry'))}</td>"
            f"<td>{fmt_num(tr.get('sl'))}</td>"
            f"<td>{fmt_num(tr.get('tp1'))}</td>"
            f"<td>{fmt_num(tr.get('tp2'))}</td>"
            f"<td>{fmt_num(tr.get('tp3'))}</td>"
            f"<td>{escape_html(fmt_ts(tr.get('entry_time')))}</td>"
            f"<td><span class='{badge}'>{escape_html(label)}</span></td>"
            f"<td>{tr.get('duration_sec') if tr.get('duration_sec') is not None else ''}</td>"
            "</tr>"
        )

    html = TRADES_PUBLIC_HTML_TPL.safe_substitute(
        symbol=escape_html(symbol or ""),
        tf=escape_html(tf or ""),
        start=escape_html(start or ""),
        end=escape_html(end or ""),
        limit=str(limit),
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
        rows_html=rows_html or '<tr><td colspan="12" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>',
        pill_values="[]"
    )
    return HTMLResponse(html)

# -------- Trades ADMIN (protégé, mêmes colonnes) --------
@app.get("/trades-admin", response_class=HTMLResponse)
def trades_admin(
    secret: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    tf: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100)
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    start_ep = parse_date_to_epoch(start)
    end_ep = parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit * 10))

    rows_html = ""
    data = trades[-limit:] if limit else trades
    for tr in data:
        outcome = tr.get("outcome") or "NONE"
        badge = chip_class(outcome)
        label = outcome_label(outcome)
        rows_html += (
            "<tr>"
            f"<td>{escape_html(str(tr.get('trade_id')))}</td>"
            f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
            f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
            f"<td>{fmt_num(tr.get('entry'))}</td>"
            f"<td>{fmt_num(tr.get('sl'))}</td>"
            f"<td>{fmt_num(tr.get('tp1'))}</td>"
            f"<td>{fmt_num(tr.get('tp2'))}</td>"
            f"<td>{fmt_num(tr.get('tp3'))}</td>"
            f"<td>{escape_html(fmt_ts(tr.get('entry_time')))}</td>"
            f"<td><span class='{badge}'>{escape_html(label)}</span></td>"
            f"<td>{tr.get('duration_sec') if tr.get('duration_sec') is not None else ''}</td>"
            "</tr>"
        )

    html = TRADES_ADMIN_HTML_TPL.safe_substitute(
        secret=escape_html(secret or ""),
        symbol=escape_html(symbol or ""),
        tf=escape_html(tf or ""),
        start=escape_html(start or ""),
        end=escape_html(end or ""),
        limit=str(limit),
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
        rows_html=rows_html or '<tr><td colspan="12" class="muted">No trades yet. Send a webhook to /tv-webhook.</td></tr>'
    )
    return HTMLResponse(html)
# ============ fin SECTION 7/8 ============
# ============ main.py — SECTION 8/8 (Daemon Altseason + __main__) ============

import threading

_daemon_stop = threading.Event()
_daemon_thread: Optional[threading.Thread] = None

def _daemon_loop():
    """Boucle d’arrière-plan: surveille l’Altseason et notifie Telegram selon la config."""
    state = _load_state()
    log.info(
        "Altseason daemon started (autonotify=%s, poll=%ss, min_gap=%smin, greens_required=%s)",
        ALTSEASON_AUTONOTIFY, ALTSEASON_POLL_SECONDS, ALTSEASON_NOTIFY_MIN_GAP_MIN, ALT_GREENS_REQUIRED
    )
    while not _daemon_stop.wait(ALTSEASON_POLL_SECONDS):
        try:
            state["last_tick_ts"] = int(time.time())
            s = _altseason_summary(_altseason_snapshot(force=False))
            now = time.time()

            # MAJ des streaks (au changement de jour UTC)
            _update_daily_streaks(state, s)

            need_send = False
            if s["ALTSEASON_ON"] and not state.get("last_on", False):
                # OFF -> ON
                need_send = True
            elif s["ALTSEASON_ON"]:
                # throttle
                min_gap = ALTSEASON_NOTIFY_MIN_GAP_MIN * 60
                if now - state.get("last_sent_ts", 0) >= min_gap:
                    need_send = True

            if need_send and ALTSEASON_AUTONOTIFY:
                msg = f"[ALERTE ALTSEASON] {s['asof']} — Greens={s['greens']} — ALTSEASON DÉBUTÉ !"
                res = send_telegram_ex(msg, pin=TELEGRAM_PIN_ALTSEASON)
                log.info("Altseason auto-notify: sent=%s pinned=%s err=%s", res.get("ok"), res.get("pinned"), res.get("error"))
                if res.get("ok"):
                    state["last_sent_ts"] = int(now)

            state["last_on"] = bool(s["ALTSEASON_ON"])
            _save_state(state)
        except Exception as e:
            log.warning("Altseason daemon tick error: %s", e)

@app.on_event("startup")
def _start_daemon():
    """Lance le daemon au démarrage si activé."""
    global _daemon_thread
    if ALTSEASON_AUTONOTIFY and _daemon_thread is None:
        _daemon_stop.clear()
        _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True)
        _daemon_thread.start()
        log.info("Daemon thread spawned.")

@app.on_event("shutdown")
def _stop_daemon():
    """Arrête proprement le daemon à l’extinction."""
    if _daemon_thread is not None:
        _daemon_stop.set()
        log.info("Daemon stop signal sent.")

# ============ Run local ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
# ============ fin SECTION 8/8 ============
