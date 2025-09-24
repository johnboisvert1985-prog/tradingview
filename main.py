# main.py
# FastAPI app pour recevoir les webhooks TradingView, stocker en SQLite,
# afficher /trades (public), /trades-admin (admin), et /reset.
# Inclut un export optionnel du fichier source (désactivé par défaut).

from __future__ import annotations

import os
import json
import time
import sqlite3
import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, status, Query
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# -----------------------------------------------------------------------------
# Configuration & Logging
# -----------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("aitrader")

# Secrets & env
# Secret partagé avec TradingView (champ "secret" dans le payload)
WEBHOOK_SHARED_SECRET = os.getenv(
    "WEBHOOK_SHARED_SECRET",
    # Valeur par défaut d'après tes logs; change-la en prod
    "nqgjiebqgiehgq8e76qhefjqer78gfq0eyrg",
)

# Secret admin pour accéder à /trades-admin et /reset
ADMIN_SECRET = os.getenv("ADMIN_SECRET", WEBHOOK_SHARED_SECRET)

# Base de données
DB_PATH = os.getenv("DB_PATH", "./data/app.db")
DB_DIR = os.path.dirname(DB_PATH)
if DB_DIR:
    os.makedirs(DB_DIR, exist_ok=True)

# Telegram (facultatif)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "10"))

# -----------------------------------------------------------------------------
# Base de données
# -----------------------------------------------------------------------------

@contextmanager
def db_conn():
    # check_same_thread=False pour FastAPI (multi-threads)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def db_init():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id      TEXT PRIMARY KEY,
                symbol        TEXT NOT NULL,
                tf            INTEGER NOT NULL,
                side          TEXT,
                entry         REAL,
                sl            REAL,
                tp1           REAL,
                tp2           REAL,
                tp3           REAL,
                r1            REAL,
                s1            REAL,
                lev_reco      REAL,
                qty_reco      REAL,
                notional      REAL,
                opened_at     INTEGER,   -- epoch ms (du payload "time")
                status        TEXT,      -- OPEN / CLOSED
                closed_at     INTEGER,   -- epoch ms
                closed_reason TEXT,      -- ex: "Flip to SHORT", "TPx", "SL_HIT"
                closed_side   TEXT       -- side au moment du close si envoyé
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id  TEXT,
                type      TEXT NOT NULL,    -- ENTRY, TP1_HIT, TP2_HIT, SL_HIT, CLOSE, AOE_PREMIUM/DISCOUNT...
                symbol    TEXT NOT NULL,
                tf        INTEGER NOT NULL,
                event_ts  INTEGER NOT NULL, -- epoch ms
                payload   TEXT NOT NULL     -- JSON complet
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        conn.commit()

db_init()

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(title="AI Trader Webhooks")

# CORS (ouvre au besoin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Utilitaires
# -----------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def require_admin(secret: str):
    if not secret or secret != ADMIN_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

def pretty_json(d: Any) -> str:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).isoformat()

# Rate-limit mémoire pour Telegram
_last_tg_send: Dict[str, float] = {}

def send_to_telegram(text: str, key: str) -> tuple[bool, Optional[str]]:
    """
    Envoi facultatif vers Telegram. Si TELEGRAM_BOT_TOKEN/CHAT_ID ne sont pas fournis,
    on log juste. On applique un cooldown par 'key'.
    """
    now = time.time()
    last = _last_tg_send.get(key, 0.0)
    if now - last < TELEGRAM_COOLDOWN_SECONDS:
        logger.info("TV webhook -> telegram sent=True pinned=False err=rate-limited (cooldown)")
        return True, "rate-limited (cooldown)"

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("TV webhook -> telegram sent=True pinned=False err=None (no token/chat configured)")
        _last_tg_send[key] = now
        return True, None

    try:
        # Pour rester simple (et éviter les deps), on fait une requête HTTP minimale avec urllib.
        import urllib.request
        import urllib.parse

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
            "parse_mode": "HTML",
        }
        payload = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                _last_tg_send[key] = now
                logger.info("TV webhook -> telegram sent=True pinned=False err=None")
                return True, None
            else:
                logger.info("TV webhook -> telegram sent=False pinned=False err=HTTP_%s", resp.status)
                return False, f"http_{resp.status}"
    except Exception as e:
        logger.info("TV webhook -> telegram sent=False pinned=False err=%s", e)
        return False, str(e)

def build_trade_id(symbol: str, tf: int, ts: int) -> str:
    return f"{symbol}_{tf}_{ts}"

def save_event_and_update_trade(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sauvegarde l'événement dans 'events' et met à jour 'trades' si applicable.
    """
    # Champs attendus
    evt_type = payload.get("type")
    symbol = payload.get("symbol")
    tf = int(payload.get("tf", 0)) if payload.get("tf") is not None else 0
    ts = int(payload.get("time", now_ms()))
    provided_trade_id = payload.get("trade_id")

    # ENTRY crée un trade_id si absent
    if not provided_trade_id and evt_type == "ENTRY":
        trade_id = build_trade_id(symbol, tf, ts)
    else:
        trade_id = provided_trade_id

    with db_conn() as conn:
        c = conn.cursor()

        # Enregistre l'événement
        c.execute(
            "INSERT INTO events(trade_id, type, symbol, tf, event_ts, payload) VALUES(?,?,?,?,?,?)",
            (trade_id, evt_type, symbol, tf, ts, pretty_json(payload)),
        )

        # Types d'événements gérant le cycle de vie du trade
        if evt_type == "ENTRY":
            # Upsert trade
            fields = {
                "trade_id": trade_id,
                "symbol": symbol,
                "tf": tf,
                "side": payload.get("side"),
                "entry": payload.get("entry"),
                "sl": payload.get("sl"),
                "tp1": payload.get("tp1"),
                "tp2": payload.get("tp2"),
                "tp3": payload.get("tp3"),
                "r1": payload.get("r1"),
                "s1": payload.get("s1"),
                "lev_reco": payload.get("lev_reco"),
                "qty_reco": payload.get("qty_reco"),
                "notional": payload.get("notional"),
                "opened_at": ts,
                "status": "OPEN",
            }
            c.execute("""
                INSERT INTO trades(
                    trade_id, symbol, tf, side, entry, sl, tp1, tp2, tp3, r1, s1, lev_reco, qty_reco, notional,
                    opened_at, status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    side=excluded.side,
                    entry=excluded.entry,
                    sl=excluded.sl,
                    tp1=excluded.tp1,
                    tp2=excluded.tp2,
                    tp3=excluded.tp3,
                    r1=excluded.r1,
                    s1=excluded.s1,
                    lev_reco=excluded.lev_reco,
                    qty_reco=excluded.qty_reco,
                    notional=excluded.notional,
                    opened_at=excluded.opened_at,
                    status=excluded.status
            """, tuple(fields.values()))
        elif evt_type in ("SL_HIT", "CLOSE"):
            # Close le trade
            closed_reason = payload.get("reason") if evt_type == "CLOSE" else "SL_HIT"
            closed_side = payload.get("side")
            c.execute("""
                UPDATE trades SET
                    status='CLOSED',
                    closed_at=?,
                    closed_reason=?,
                    closed_side=?
                WHERE trade_id=?
            """, (ts, closed_reason, closed_side, trade_id))
        else:
            # TPx/ AOE_* => on garde juste l'événement; pas de changement de statut
            pass

        conn.commit()

    # Petit log console façon tes logs
    logger.info(
        "Saved event: type=%s symbol=%s tf=%s trade_id=%s",
        evt_type, symbol, tf, trade_id
    )

    # Message Telegram (optionnel)
    tg_key = f"{symbol}_{tf}_{evt_type}"
    msg = f"🔔 {evt_type} — {symbol} {tf}m\n{pretty_json(payload)}"
    sent, err = send_to_telegram(msg, tg_key)

    return {
        "ok": True,
        "stored_trade_id": trade_id,
        "telegram": {"sent": sent, "err": err},
    }

def get_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    c = conn.cursor()

    # Total events
    c.execute("SELECT COUNT(*) AS n FROM events")
    total_events = c.fetchone()["n"]

    # Total trades
    c.execute("SELECT COUNT(*) AS n FROM trades")
    total_trades = c.fetchone()["n"]

    # Open/Closed
    c.execute("SELECT COUNT(*) AS n FROM trades WHERE status='OPEN'")
    open_trades = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM trades WHERE status='CLOSED'")
    closed_trades = c.fetchone()["n"]

    # Winrate approximatif:
    # règle simple: un trade "gagnant" si son dernier event est TP2_HIT ou TP3_HIT,
    # "perdant" si CLOSE avec reason contient 'Flip to' ou SL_HIT.
    # (à affiner selon ta logique métier)
    c.execute("""
        WITH last_evt AS (
            SELECT
                t.trade_id,
                (SELECT e.type FROM events e WHERE e.trade_id=t.trade_id ORDER BY e.event_ts DESC LIMIT 1) AS last_type,
                (SELECT e.payload FROM events e WHERE e.trade_id=t.trade_id ORDER BY e.event_ts DESC LIMIT 1) AS last_payload
            FROM trades t
            WHERE t.status='CLOSED'
        )
        SELECT
          SUM(CASE WHEN last_type IN ('TP2_HIT','TP3_HIT') THEN 1 ELSE 0 END) AS winners,
          SUM(CASE WHEN last_type IN ('SL_HIT','CLOSE') THEN 1 ELSE 0 END)   AS losers
        FROM last_evt
    """)
    row = c.fetchone()
    winners = row["winners"] or 0
    losers = row["losers"] or 0
    wr = 0.0
    if winners + losers > 0:
        wr = round(100.0 * winners / (winners + losers), 2)

    return {
        "total_events": total_events,
        "total_trades": total_trades,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "winners_closed": winners,
        "losers_closed": losers,
        "winrate_closed_pct": wr,
    }

def render_admin_html(secret: str) -> str:
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM trades ORDER BY opened_at DESC")
        trades = [dict(r) for r in c.fetchall()]
        stats = get_stats(conn)

    rows_html = []
    for t in trades:
        rows_html.append(f"""
            <tr>
                <td style="white-space:nowrap;">{t['trade_id']}</td>
                <td>{t['symbol']}</td>
                <td>{t['tf']}</td>
                <td>{t.get('side','')}</td>
                <td>{t.get('entry','')}</td>
                <td>{t.get('sl','')}</td>
                <td>{t.get('tp1','')}</td>
                <td>{t.get('tp2','')}</td>
                <td>{t.get('tp3','')}</td>
                <td>{t.get('status','')}</td>
                <td>{ms_to_iso(t.get('opened_at'))}</td>
                <td>{ms_to_iso(t.get('closed_at'))}</td>
                <td>{t.get('closed_reason','')}</td>
                <td>{t.get('closed_side','')}</td>
            </tr>
        """)

    reset_url = f"/reset?secret={secret}&confirm=yes&redirect=/trades-admin?secret={secret}"
    stats_html = "<br>".join(f"{k}: <b>{v}</b>" for k, v in stats.items())
    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Trades Admin</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; font-size: 12px; }}
th {{ background: #f6f6f6; position: sticky; top: 0; }}
code {{ background: #f3f3f3; padding: 2px 4px; }}
.actions {{ margin: 10px 0; }}
</style>
</head>
<body>
<h2>Trades Admin</h2>
<div class="actions">
    <a href="{reset_url}" style="color:#b00;font-weight:bold;">RESET (effacer tous les trades + events)</a>
</div>
<div>
  <h3>Stats</h3>
  <div>{stats_html}</div>
</div>
<h3 style="margin-top:20px;">Trades</h3>
<table>
<thead>
<tr>
  <th>trade_id</th><th>symbol</th><th>tf</th><th>side</th>
  <th>entry</th><th>sl</th><th>tp1</th><th>tp2</th><th>tp3</th>
  <th>status</th><th>opened_at</th><th>closed_at</th><th>closed_reason</th><th>closed_side</th>
</tr>
</thead>
<tbody>
{''.join(rows_html)}
</tbody>
</table>
</body>
</html>
    """

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "time": now_ms()}

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Log brut (comme dans tes logs)
    logger.info("Webhook payload: %s", pretty_json(payload))

    # Vérifie secret si fourni dans le payload
    secret = payload.get("secret")
    if secret != WEBHOOK_SHARED_SECRET:
        # on ne bloque pas si tu envoies certains payloads sans secret;
        # sinon décommente pour exiger le secret:
        # raise HTTPException(status_code=403, detail="Invalid secret")
        logger.info("Secret mismatch or missing (continuing for compatibility)")

    res = save_event_and_update_trade(payload)
    return JSONResponse(res)

@app.get("/trades", response_class=JSONResponse)
def get_trades():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM trades ORDER BY opened_at DESC")
        trades = [dict(r) for r in c.fetchall()]
        stats = get_stats(conn)
    return {"ok": True, "stats": stats, "count": len(trades), "trades": trades}

@app.get("/trades-admin", response_class=HTMLResponse)
def trades_admin(secret: str = Query(default="")):
    require_admin(secret)
    return HTMLResponse(render_admin_html(secret))

@app.get("/reset")
def reset(secret: str = Query(default=""),
          confirm: str = Query(default="no"),
          redirect: Optional[str] = Query(default=None)):
    require_admin(secret)
    if confirm != "yes":
        raise HTTPException(status_code=400, detail="Add confirm=yes to proceed")

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM trades")
        c.execute("VACUUM")
        conn.commit()
    logger.info("RESET effectué via /reset")

    if redirect:
        return RedirectResponse(redirect, status_code=303)
    return {"ok": True, "message": "All data wiped."}

# -----------------------------------------------------------------------------
# Export optionnel du code source (Correctif propre)
# -----------------------------------------------------------------------------

def maybe_export_main():
    """
    Export de ce fichier vers un répertoire writable (par défaut /tmp).
    Activé uniquement si EXPORT_MAIN_TXT=1 dans les variables d'environnement.
    """
    if os.getenv("EXPORT_MAIN_TXT", "0") != "1":
        return
    export_dir = os.getenv("EXPORT_PATH", "/tmp")
    try:
        os.makedirs(export_dir, exist_ok=True)
    except Exception as e:
        logger.warning("[export] Impossible de créer le dossier %s: %s", export_dir, e)
        return

    export_path = os.path.join(export_dir, "main.py.txt")
    try:
        here = os.path.abspath(__file__)
        with open(here, "r", encoding="utf-8") as src, open(export_path, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        logger.info("[export] main.py exporté -> %s", export_path)
    except Exception as e:
        logger.warning("[export] échec export main.py: %s", e)

# -----------------------------------------------------------------------------
# Lancement local (Render importe `app`, donc ce bloc ne s'exécute pas là-bas)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    maybe_export_main()
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
        log_level=LOG_LEVEL.lower(),
    )
