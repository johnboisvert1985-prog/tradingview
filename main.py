import os
import json
import time
import sqlite3
import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

import urllib.request
import urllib.error

from fastapi import FastAPI, Request, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTasks

# ----------------------------------
# Config & Logging
# ----------------------------------
logging.basicConfig(level=logging.INFO, format="INFO:%(name)s:%(message)s")
log = logging.getLogger("aitrader")

WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "change-me")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "30"))

DB_PATH = os.getenv("DB_PATH", "/var/data/trades.sqlite3")

# Création du dossier pour DB persistante
db_dir = os.path.dirname(DB_PATH) or "."
os.makedirs(db_dir, exist_ok=True)

# State Telegram rate-limit
_last_telegram_sent_ts = 0.0

# ----------------------------------
# App
# ----------------------------------
app = FastAPI(title="TV Webhook → Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------
# DB helpers
# ----------------------------------
@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def db_init():
    with db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            symbol TEXT,
            tf TEXT,
            tf_label TEXT,
            time INTEGER,
            side TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            r1 REAL,
            s1 REAL,
            lev_reco REAL,
            qty_reco REAL,
            notional REAL,
            reason TEXT,
            trade_id TEXT,
            note TEXT,
            extra TEXT
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_tradeid ON events(trade_id);")

def save_event(payload: Dict[str, Any]) -> None:
    # Sélectionne uniquement les colonnes connues
    cols = ["type","symbol","tf","tf_label","time","side","entry","sl","tp1","tp2","tp3",
            "r1","s1","lev_reco","qty_reco","notional","reason","trade_id","note"]
    row = {c: payload.get(c) for c in cols}
    # Le reste en extra JSON
    extra = {k: v for k, v in payload.items() if k not in row}
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO events(type,symbol,tf,tf_label,time,side,entry,sl,tp1,tp2,tp3,
                               r1,s1,lev_reco,qty_reco,notional,reason,trade_id,note,extra)
            VALUES(:type,:symbol,:tf,:tf_label,:time,:side,:entry,:sl,:tp1,:tp2,:tp3,
                   :r1,:s1,:lev_reco,:qty_reco,:notional,:reason,:trade_id,:note,:extra)
        """, {**row, "extra": json.dumps(extra) if extra else None})

def query_events(limit: int = 300) -> list[sqlite3.Row]:
    with db_conn() as conn:
        cur = conn.execute("""
            SELECT * FROM events
            ORDER BY time DESC
            LIMIT ?
        """, (limit,))
        return cur.fetchall()

def query_latest_altseason() -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        cur = conn.execute("""
            SELECT * FROM events
            WHERE type = 'ALTSEASON'
            ORDER BY time DESC
            LIMIT 1
        """)
        return cur.fetchone()

def hard_reset():
    with db_conn() as conn:
        conn.execute("DELETE FROM events;")

# ----------------------------------
# Telegram
# ----------------------------------
def _telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def try_send_telegram(text: str) -> tuple[bool, Optional[str]]:
    global _last_telegram_sent_ts
    if not _telegram_enabled():
        return (False, "telegram-disabled")

    now = time.time()
    if now - _last_telegram_sent_ts < TELEGRAM_COOLDOWN_SECONDS:
        return (False, "rate-limited (cooldown)")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
        _last_telegram_sent_ts = now
        return (True, None)
    except urllib.error.HTTPError as e:
        return (False, f"HTTP Error {e.code}: {e.reason}")
    except Exception as e:
        return (False, str(e))

# ----------------------------------
# Utils
# ----------------------------------
def as_int_ms(v: Any) -> int:
    """
    Normalise le champ "time" des webhooks.
    Accepte millis, secondes, str, ou fallback = now_ms.
    """
    if v is None:
        return int(time.time() * 1000)
    try:
        iv = int(v)
        # si secondes (10 digits), on convertit en ms
        if iv < 10_000_000_000:
            return iv * 1000
        return iv
    except Exception:
        return int(time.time() * 1000)

# ----------------------------------
# Routes
# ----------------------------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "ok"

@app.head("/", response_class=Response)
def root_head():
    # Répond 200 aux HEAD checks Render
    return Response(status_code=200)

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.post("/tv-webhook")
async def tv_webhook(request: Request, background: BackgroundTasks):
    data = await request.json()
    log.info("Webhook payload: %s", json.dumps(data, ensure_ascii=False))
    # Secret
    secret = str(data.get("secret", ""))
    if secret != WEBHOOK_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Type d'événement
    evt_type = str(data.get("type", "")).upper().strip()
    # Normalisation champs fréquents
    payload: Dict[str, Any] = dict(data)  # shallow copy
    payload["type"] = evt_type or data.get("event") or "UNKNOWN"
    payload["time"] = as_int_ms(data.get("time"))

    # Valeurs par défaut raisonnables
    payload.setdefault("symbol", "N/A")
    payload.setdefault("tf", data.get("tf") or "")
    payload.setdefault("tf_label", data.get("tf_label") or "")
    payload.setdefault("note", data.get("note") or "")

    # Branches connues (pas strict, on sauvegarde tout de toute façon)
    known_types = {
        "ENTRY", "CLOSE",
        "TP1_HIT", "TP2_HIT", "TP3_HIT",
        "SL_HIT",
        "AOE_PREMIUM", "AOE_DISCOUNT",
        "ALTSEASON"
    }
    if payload["type"] not in known_types:
        # On garde quand même l’event pour debug/traçabilité
        payload["note"] = (payload.get("note") or "") + " (unhandled-type)"

    # Sauvegarde DB
    save_event(payload)
    log.info("Saved event: type=%s symbol=%s tf=%s trade_id=%s",
             payload["type"], payload.get("symbol"), payload.get("tf"), payload.get("trade_id"))

    # Message Telegram simple (facultatif)
    def fmt_num(v, digits=4):
        try:
            return f"{float(v):.{digits}f}"
        except Exception:
            return str(v)

    tg_msg = None
    tfl = payload.get("tf_label") or payload.get("tf") or ""
    sym = payload.get("symbol", "N/A")
    if payload["type"] == "ALTSEASON":
        note = payload.get("note") or "Altseason signal"
        tg_msg = f"🚀 <b>ALTSEASON</b> 🔔 {sym} ({tfl}) — {note}"
    elif payload["type"] == "ENTRY":
        side = payload.get("side", "")
        entry = fmt_num(payload.get("entry"))
        sl = fmt_num(payload.get("sl"))
        tp1 = fmt_num(payload.get("tp1"))
        tg_msg = f"🟢 <b>ENTRY</b> {sym} ({tfl}) {side} @ {entry} | SL {sl} | TP1 {tp1}"
    elif payload["type"] == "CLOSE":
        side = payload.get("side", "")
        reason = payload.get("reason", "")
        tg_msg = f"🔻 <b>CLOSE</b> {sym} ({tfl}) {side} — {reason}"
    elif payload["type"] in {"TP1_HIT","TP2_HIT","TP3_HIT"}:
        side = payload.get("side", "")
        tp = fmt_num(payload.get("tp"))
        tg_msg = f"✅ <b>{payload['type']}</b> {sym} ({tfl}) {side} hit {tp}"
    elif payload["type"] == "SL_HIT":
        side = payload.get("side", "")
        tp = fmt_num(payload.get("tp"))
        tg_msg = f"❌ <b>SL_HIT</b> {sym} ({tfl}) {side} at {tp}"
    elif payload["type"] in {"AOE_PREMIUM", "AOE_DISCOUNT"}:
        level = payload.get("hiWin") or payload.get("loWin") or payload.get("close")
        lvl = fmt_num(level)
        tg_msg = f"📈 <b>{payload['type']}</b> {sym} ({tfl}) @ {lvl}"

    sent, err = (False, None)
    if tg_msg:
        def _bg_send():
            s, e = try_send_telegram(tg_msg)
            log.info("TV webhook -> telegram sent=%s pinned=False err=%s", s, e)
        background.add_task(_bg_send)

    return {"ok": True}

# -------- Admin: reset & dashboard ----------

@app.get("/reset")
def admin_reset(
    secret: str = Query(...),
    confirm: str = Query("no"),
    redirect: str = Query("/trades-admin")
):
    if secret != WEBHOOK_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm.lower() != "yes":
        # Page de confirmation
        return HTMLResponse(f"""
        <html><body style="font-family: sans-serif">
            <h2>CONFIRM RESET</h2>
            <p>Cette action supprime TOUT l'historique des événements.</p>
            <a href="/reset?secret={secret}&confirm=yes&redirect={redirect}">
                <button style="padding:8px 14px">CONFIRMER</button>
            </a>
            <a href="{redirect}?secret={secret}">
                <button style="padding:8px 14px;margin-left:8px">Annuler</button>
            </a>
        </body></html>
        """)

    hard_reset()
    return Response(status_code=303, headers={"Location": f"{redirect}&secret={secret}"})

@app.get("/admin/altseason-test")
def altseason_test(secret: str = Query(...)):
    if secret != WEBHOOK_SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    now = int(time.time() * 1000)
    payload = {
        "type": "ALTSEASON",
        "symbol": "ALT-INDEX",
        "tf": "D",
        "tf_label": "1D",
        "time": now,
        "note": "Test signal"
    }
    save_event(payload)
    try_send_telegram("🚀 ALTSEASON 🔔 ALT-INDEX (1D) — Test signal")
    return {"ok": True}

@app.get("/trades-admin", response_class=HTMLResponse)
def trades_admin(secret: str = Query(...)):
    if secret != WEBHOOK_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    latest_alt = query_latest_altseason()
    events = query_events(limit=400)

    def ts_to_str(ms: int) -> str:
        try:
            s = ms / 1000.0
            return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s))
        except Exception:
            return str(ms)

    alt_html = ""
    if latest_alt:
        alt_html = f"""
        <div style="padding:12px;border:1px solid #2e7d32;background:#e8f5e9;border-radius:8px;margin-bottom:16px">
          <b>Dernier signal ALTSEASON :</b>
          <div>Symbol: {latest_alt['symbol']} | TF: {latest_alt['tf_label'] or latest_alt['tf'] or ""} | Time: {ts_to_str(latest_alt['time'])} UTC</div>
          <div>Note: {(latest_alt['note'] or '')}</div>
        </div>
        """
    else:
        alt_html = """
        <div style="padding:12px;border:1px solid #9e9e9e;background:#f5f5f5;border-radius:8px;margin-bottom:16px">
          <b>Aucun signal ALTSEASON enregistré.</b>
          <div>Utilisez <code>/admin/altseason-test?secret=...</code> pour tester l'affichage.</div>
        </div>
        """

    rows = []
    for r in events:
        rows.append(f"""
            <tr>
                <td>{r['id']}</td>
                <td>{r['type']}</td>
                <td>{r['symbol']}</td>
                <td>{r['tf_label'] or r['tf'] or ""}</td>
                <td>{ts_to_str(r['time'])}</td>
                <td>{r['side'] or ""}</td>
                <td>{r['entry'] or ""}</td>
                <td>{r['sl'] or ""}</td>
                <td>{r['tp1'] or ""}</td>
                <td>{r['tp2'] or ""}</td>
                <td>{r['tp3'] or ""}</td>
                <td>{r['reason'] or ""}</td>
                <td>{r['trade_id'] or ""}</td>
            </tr>
        """)

    html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>Trades Admin</title>
      <style>
        body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; padding:16px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 6px 8px; font-size: 13px; }}
        th {{ background: #fafafa; text-align: left; position: sticky; top: 0; }}
        .bar {{ display:flex; gap:8px; align-items:center; margin-bottom:12px; }}
        .btn {{ padding:8px 12px; border:1px solid #1976d2; background:#e3f2fd; color:#0d47a1; border-radius:6px; text-decoration:none; font-weight:600 }}
        .btn.danger {{ border-color:#d32f2f; background:#ffebee; color:#b71c1c }}
        .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace }}
        .small {{ color:#666; font-size:12px }}
      </style>
    </head>
    <body>
      <h2>📊 Trades Admin</h2>

      {alt_html}

      <div class="bar">
        <a class="btn" href="/admin/altseason-test?secret={secret}">➕ Inject ALTSEASON (test)</a>
        <a class="btn danger" href="/reset?secret={secret}&confirm=no&redirect=/trades-admin">🧹 Reset</a>
        <span class="small mono">DB: {DB_PATH}</span>
      </div>

      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Type</th>
            <th>Symbol</th>
            <th>TF</th>
            <th>Time (UTC)</th>
            <th>Side</th>
            <th>Entry</th>
            <th>SL</th>
            <th>TP1</th>
            <th>TP2</th>
            <th>TP3</th>
            <th>Reason</th>
            <th>Trade ID</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html)

# ----------------------------------
# Startup
# ----------------------------------
@app.on_event("startup")
def on_startup():
    db_init()
    log.info("Application startup complete.")

# ----------------------------------
# Uvicorn run (local)
# ----------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
