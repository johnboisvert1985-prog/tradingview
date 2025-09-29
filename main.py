# ============================== main.py — BLOC 1/5 ==============================
# Imports & Config de base
import os, time, json, asyncio, sqlite3, textwrap
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

# ------------------------------------------------------------------------------
# ENV / Config (garde tes valeurs existantes si tu les as déjà)
# ------------------------------------------------------------------------------
DB_DIR = os.getenv("DB_DIR", "/tmp/ai_trader")
DB_PATH = os.path.join(DB_DIR, "data.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Anti-spam Telegram + Dédoublonnage des VECTOR
TELEGRAM_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "6"))
VECTOR_DEDUP_SECONDS = int(os.getenv("VECTOR_DEDUP_SECONDS", "90"))

# Intervalle de rafraîchissement Altseason (fix ALT_INTERVAL NameError)
ALT_INTERVAL = int(os.getenv("ALTSEASON_INTERVAL_SECONDS", "60"))

# ------------------------------------------------------------------------------
# Utilitaires simples
# ------------------------------------------------------------------------------
def now_iso() -> str:
    """Horodatage ISO en UTC (fixe le NameError now_iso)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# Snapshot en mémoire (fix _altseason_snapshot not defined)
_altseason_snapshot: Dict[str, Any] = {
    "updated_at": None,
    "dominance": None,
    "breadth": None,
    "alts_up": 0,
    "alts_down": 0,
    "memo": "init",
}

# Mémoire pour dédup VECTOR
_vector_recent: Dict[str, float] = {}

def is_duplicate_vector(payload: dict) -> bool:
    """Évite le spam de VECTOR identiques pendant VECTOR_DEDUP_SECONDS."""
    if str(payload.get("type", "")).upper() != "VECTOR_CANDLE":
        return False
    key = f"{payload.get('symbol')}_{payload.get('tf')}_{payload.get('direction')}".upper()
    now = time.time()
    last = _vector_recent.get(key, 0.0)
    if now - last < VECTOR_DEDUP_SECONDS:
        return True
    _vector_recent[key] = now
    return False

# ------------------------------------------------------------------------------
# App FastAPI
# ------------------------------------------------------------------------------
app = FastAPI(title="AI Trader Dashboard", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# BDD: init + helpers
# ------------------------------------------------------------------------------
os.makedirs(DB_DIR, exist_ok=True)

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          type TEXT NOT NULL,
          symbol TEXT,
          tf TEXT,
          direction TEXT,
          price REAL,
          trade_id TEXT,
          raw TEXT
        )
    """)
    # Index utiles
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_trade_id ON events(trade_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    conn.commit()
    conn.close()

init_db()

def save_event(payload: dict):
    """Insère l’événement (corrige l’erreur now_iso not defined)."""
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO events(ts, type, symbol, tf, direction, price, trade_id, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now_iso(),
        payload.get("type"),
        payload.get("symbol"),
        str(payload.get("tf") or ""),
        payload.get("direction"),
        payload.get("price") if payload.get("price") is not None else None,
        payload.get("trade_id"),
        json.dumps(payload, ensure_ascii=False),
    ))
    conn.commit()
    conn.close()

def compute_outcome_for_trade(trade_id: str) -> Optional[str]:
    """
    Retourne 'TP1','TP2','TP3','SL','CLOSE' ou None en scannant les events du trade.
    Sert à colorer les colonnes TP/SL dans /trades.
    """
    if not trade_id:
        return None
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT type FROM events
        WHERE trade_id = ?
        ORDER BY id DESC
        LIMIT 100
    """, (trade_id,))
    types = [row["type"].upper() for row in cur.fetchall()]
    conn.close()
    for t in types:
        if t in {"TP3_HIT", "TP2_HIT", "TP1_HIT", "SL_HIT", "CLOSE"}:
            return t.replace("_HIT", "")
    return None

# ------------------------------------------------------------------------------
# Telegram sender avec cooldown (évite 429 + bruit)
# ------------------------------------------------------------------------------
async def send_telegram_ex(text: str, payload: Optional[dict] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    # throttle surtout les VECTOR
    t = str((payload or {}).get("type", "")).upper()
    throttle = (t == "VECTOR_CANDLE")

    # Stockage du dernier envoi dans l'attribut de fonction pour éviter global
    last = getattr(send_telegram_ex, "_last", 0.0)
    if throttle and (time.time() - last) < TELEGRAM_COOLDOWN_SECONDS:
        # Pas une erreur : volontaire pour limiter le spam
        print("WARNING: Telegram send skipped due to cooldown")
        return

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text}
            )
        send_telegram_ex._last = time.time()
    except Exception as e:
        print(f"WARNING: Telegram send_telegram_ex exception: {e}")

# ------------------------------------------------------------------------------
# Altseason: calcul snapshot + daemon périodique
# ------------------------------------------------------------------------------
async def compute_altseason_snapshot() -> dict:
    """
    TODO: branche tes vrais calculs ici.
    Valeurs factices pour éviter l'erreur `_altseason_snapshot is not defined`.
    """
    # Exemple: à remplacer par tes métriques réelles
    return {
        "updated_at": now_iso(),
        "dominance": 52.3,   # BTC.D
        "breadth": +12,      # Nb d'alts > 0% jour - Nb d'alts < 0% jour
        "alts_up": 145,
        "alts_down": 87,
        "memo": "ok"
    }

async def run_altseason_daemon(interval: int = ALT_INTERVAL):
    global _altseason_snapshot
    while True:
        try:
            snap = await compute_altseason_snapshot()
            if snap:
                _altseason_snapshot = snap
        except Exception as e:
            print(f"WARNING: Altseason summary error: {e}")
        await asyncio.sleep(interval)

@app.on_event("startup")
async def _startup_tasks():
    try:
        asyncio.create_task(run_altseason_daemon())
    except Exception as e:
        print(f"WARNING: Altseason daemon not started: {e}")

# ------------------------------------------------------------------------------
# Root -> redirect vers /trades (corrige 404 Not Found sur '/')
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/trades")
# ============================ /main.py — BLOC 1/5 ==============================
# ============================== main.py — BLOC 2/5 ==============================
from pydantic import BaseModel

TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "changeme")  # mets ta vraie valeur

class TVPayload(BaseModel):
    type: str
    symbol: Optional[str] = None
    tf: Optional[str] = None
    tf_label: Optional[str] = None
    time: Optional[int] = None
    side: Optional[str] = None
    direction: Optional[str] = None  # "UP" | "DOWN" pour VECTOR_CANDLE
    price: Optional[float] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    r1: Optional[float] = None
    s1: Optional[float] = None
    lev_reco: Optional[float] = None
    qty_reco: Optional[float] = None
    notional: Optional[float] = None
    confidence: Optional[int] = None
    horizon: Optional[str] = None
    leverage: Optional[str] = None
    note: Optional[str] = None
    trade_id: Optional[str] = None
    secret: Optional[str] = None

def _ensure_trade_id(p: dict) -> str:
    """Si TradingView n’envoie pas de trade_id, on en forge un stable au milliseconde."""
    tid = p.get("trade_id")
    if tid:
        return tid
    symbol = (p.get("symbol") or "UNK").upper()
    tf = str(p.get("tf") or "UNK")
    # s'il y a un timestamp TV (en ms) on l’utilise, sinon on prend maintenant
    ts_ms = int(p.get("time") or int(time.time() * 1000))
    return f"{symbol}_{tf}_{ts_ms}"

def _fmt_num(x: Optional[float]) -> str:
    if x is None:
        return "—"
    # joli format court
    return f"{x:.6g}"

def _vector_square(direction: Optional[str]) -> str:
    """Carré couleur pour VECTOR: UP=vert, DOWN=rouge, sinon violet."""
    d = (direction or "").upper()
    if d == "UP":
        return "🟩"
    if d == "DOWN":
        return "🟥"
    return "🟪"

def _fmt_vector_msg(p: dict) -> str:
    sym = p.get("symbol") or "?"
    tf = p.get("tf_label") or p.get("tf") or "?"
    sq = _vector_square(p.get("direction"))
    d = (p.get("direction") or "").upper() or "?"
    pr = _fmt_num(p.get("price"))
    note = p.get("note") or f"Vector Candle {d}"
    return f"{sq} Vector Candle — {sym} {tf}\n{note}\nPrix: {pr}"

def _fmt_entry_msg(p: dict) -> str:
    sym = p.get("symbol") or "?"
    tf = p.get("tf_label") or p.get("tf") or "?"
    sd = (p.get("side") or "?").upper()
    enr, sl, tp1, tp2, tp3 = map(_fmt_num, [p.get("entry"), p.get("sl"), p.get("tp1"), p.get("tp2"), p.get("tp3")])
    lev = p.get("leverage") or "—"
    conf = p.get("confidence") or "—"
    return textwrap.dedent(f"""
    🚀 Entrée détectée — {sym} {tf}
    Côté: {sd} | Lev: {lev} | Confiance: {conf}%
    Entry: {enr}
    TP1: {tp1} | TP2: {tp2} | TP3: {tp3}
    SL: {sl}
    """).strip()

def _fmt_close_msg(p: dict) -> str:
    sym = p.get("symbol") or "?"
    tf = p.get("tf_label") or p.get("tf") or "?"
    reason = p.get("reason") or "Fermeture"
    sd = (p.get("side") or "—").upper()
    return f"✅ CLOSE — {sym} {tf}\nRaison: {reason} | Côté précédent: {sd}"

def _fmt_tp_sl_msg(p: dict) -> str:
    sym = p.get("symbol") or "?"
    tf = p.get("tf_label") or p.get("tf") or "?"
    typ = (p.get("type") or "").upper()
    if typ.endswith("_HIT"):
        badge = "🎯" if typ.startswith("TP") else "⛔"
        lvl = p.get("tp") or p.get("price")
        return f"{badge} {typ.replace('_',' ')} — {sym} {tf}\nNiveau: {_fmt_num(lvl)}"
    return f"ℹ️ {typ} — {sym} {tf}"

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    # unifie clés (TradingView peut envoyer str ou dict déjà propre)
    payload = dict(data)
    t = (str(payload.get("type") or "")).upper().strip()

    # vérifie le secret si présent
    if TV_WEBHOOK_SECRET and payload.get("secret") and payload["secret"] != TV_WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)

    # génère trade_id s’il manque (corrige les 'trade_id=None' dans tes logs)
    payload["trade_id"] = _ensure_trade_id(payload)

    # dédup VECTOR pour réduire le bruit
    if t == "VECTOR_CANDLE" and is_duplicate_vector(payload):
        return JSONResponse({"ok": True, "dedup": True})

    # enregistre dans la base
    try:
        save_event(payload)
    except Exception as e:
        print(f"ERROR: save_event failed: {e}")
        return JSONResponse({"ok": False, "error": "save_event failed"}, status_code=500)

    # Telegram: formate un message propre et court
    try:
        msg = None
        if t == "VECTOR_CANDLE":
            msg = _fmt_vector_msg(payload)
        elif t == "ENTRY":
            msg = _fmt_entry_msg(payload)
        elif t == "CLOSE":
            msg = _fmt_close_msg(payload)
        elif t in {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"}:
            msg = _fmt_tp_sl_msg(payload)
        elif t in {"AOE_PREMIUM", "AOE_DISCOUNT"}:
            # messages courts pour les signaux AOE
            sym = payload.get("symbol") or "?"
            tf = payload.get("tf_label") or payload.get("tf") or "?"
            flag = "💎 Premium" if t == "AOE_PREMIUM" else "🏷️ Discount"
            msg = f"{flag} — {sym} {tf}"

        if msg:
            asyncio.create_task(send_telegram_ex(msg, payload))
    except Exception as e:
        print(f"WARNING: Telegram send skipped because: {e}")

    return JSONResponse({"ok": True, "type": t, "trade_id": payload["trade_id"]})
# ============================ /main.py — BLOC 2/5 ==============================
# ============================== main.py — BLOC 3/5 ==============================
from fastapi.responses import HTMLResponse, RedirectResponse
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("AI_TRADER_DB", "/tmp/ai_trader/data.db"))

# --- Fallbacks Altseason (corrige l'erreur "_altseason_snapshot is not defined") ---
_altseason_snapshot: Dict[str, Any] = {
    "dominance_btc": None,
    "btc_trend": None,
    "alts_btc_corr": None,
    "alts_usdt_momo": None,
    "alts_btc_momo": None,
    "market_breadth": None,
    "stamp": int(time.time()),
}

def altseason_get_snapshot_safe() -> Dict[str, Any]:
    global _altseason_snapshot
    try:
        # si un autre daemon met à jour un snapshot global ailleurs, on le renvoie
        snap = _altseason_snapshot or {}
        # normalise
        return {
            "dominance_btc": snap.get("dominance_btc"),
            "btc_trend": snap.get("btc_trend"),
            "alts_btc_corr": snap.get("alts_btc_corr"),
            "alts_usdt_momo": snap.get("alts_usdt_momo"),
            "alts_btc_momo": snap.get("alts_btc_momo"),
            "market_breadth": snap.get("market_breadth"),
            "stamp": snap.get("stamp", int(time.time()))
        }
    except Exception:
        return {
            "dominance_btc": None,
            "btc_trend": None,
            "alts_btc_corr": None,
            "alts_usdt_momo": None,
            "alts_btc_momo": None,
            "market_breadth": None,
            "stamp": int(time.time())
        }

# --- Accès DB robuste (quel que soit le schéma d'events) ---
def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def _row_get(row: sqlite3.Row, *keys, default=None):
    for k in keys:
        if k in row.keys():
            return row[k]
    return default

def _fetch_recent_events(limit: int = 5000) -> List[Dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    try:
        with _db_connect() as con:
            # on tente table "events", sinon "logs"
            for table in ("events", "logs"):
                try:
                    cur = con.execute(f"SELECT * FROM {table} ORDER BY ROWID DESC LIMIT ?", (limit,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        return rows
                except Exception:
                    continue
    except Exception as e:
        print(f"WARNING: _fetch_recent_events failed: {e}")
    return []

def _aggregate_trades_for_ui() -> List[Dict[str, Any]]:
    """
    Construit un snapshot UI par trade_id à partir des évènements:
    - ENTRY crée la “ligne”
    - TP1_HIT/TP2_HIT/TP3_HIT activent les flags
    - SL_HIT marque stop touché
    - CLOSE ferme la ligne
    - VECTOR_CANDLE ignoré pour le tableau trade (mais visible ailleurs si besoin)
    """
    rows = _fetch_recent_events()
    by_tid: Dict[str, Dict[str, Any]] = {}

    def ensure_tid(d: Dict[str, Any]) -> str:
        tid = d.get("trade_id")
        if tid:
            return tid
        symbol = (d.get("symbol") or "UNK").upper()
        tf = str(d.get("tf") or "UNK")
        ts_ms = int(d.get("time") or int(time.time() * 1000))
        return f"{symbol}_{tf}_{ts_ms}"

    for r in rows[::-1]:  # remonte dans le temps
        typ = (str(r.get("type") or "")).upper()
        if not typ:
            continue
        if typ == "VECTOR_CANDLE":
            continue  # on ne l’intègre pas au tableau des trades
        tid = ensure_tid(r)
        sym = r.get("symbol") or "?"
        tf = r.get("tf_label") or r.get("tf") or "?"
        if tid not in by_tid:
            by_tid[tid] = {
                "trade_id": tid,
                "symbol": sym,
                "tf": tf,
                "side": r.get("side"),
                "entry": r.get("entry"),
                "sl": r.get("sl"),
                "tp1": r.get("tp1"),
                "tp2": r.get("tp2"),
                "tp3": r.get("tp3"),
                "created_at": r.get("time") or r.get("created_at"),
                "closed": False,
                "sl_hit": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
            }
        # mise à jour
        trow = by_tid[tid]
        if typ == "ENTRY":
            trow.update({
                "side": r.get("side"),
                "entry": r.get("entry"),
                "sl": r.get("sl"),
                "tp1": r.get("tp1"),
                "tp2": r.get("tp2"),
                "tp3": r.get("tp3"),
            })
        elif typ in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
            if typ == "TP1_HIT": trow["tp1_hit"] = True
            if typ == "TP2_HIT": trow["tp2_hit"] = True
            if typ == "TP3_HIT": trow["tp3_hit"] = True
        elif typ == "SL_HIT":
            trow["sl_hit"] = True
        elif typ == "CLOSE":
            trow["closed"] = True

    # tri: trades les plus récents en haut
    return sorted(by_tid.values(), key=lambda x: (x.get("created_at") or 0), reverse=True)

# --------- API JSON pour la page (utile si plus tard on veut du live refresh) ----------
@app.get("/api/altseason")
def api_altseason():
    return altseason_get_snapshot_safe()

@app.get("/api/trades")
def api_trades():
    try:
        return {"ok": True, "trades": _aggregate_trades_for_ui()}
    except Exception as e:
        return {"ok": False, "error": str(e), "trades": []}

# ---------------------------- ROUTES PAGES -------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    # redirige gentiment vers /trades (corrige les 404 GET /)
    return RedirectResponse(url="/trades")

@app.get("/trades", response_class=HTMLResponse)
def trades_page():
    # on rend côté serveur (SSR) avec un snapshot immédiat
    trades = _aggregate_trades_for_ui()
    alt = altseason_get_snapshot_safe()

    def fmt(x):
        if x is None: return "—"
        if isinstance(x, (int, float)):
            return f"{x:.6g}"
        return str(x)

    # ---- HTML / CSS / JS : design pro + header Altseason + tableau trades ----
    html = f"""
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader — Dashboard</title>
<style>
  :root {{
    --bg:#0b0f17;
    --panel:#121826;
    --muted:#8391a6;
    --text:#d9e1f2;
    --accent:#6ca8ff;
    --ok:#2ecc71;
    --warn:#f1c40f;
    --err:#e74c3c;
    --tp:#1db954;
    --sl:#ff4d4f;
    --soft:#1a2234;
    --chip:#23304a;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:linear-gradient(180deg,#0b0f17 0%, #0a0e16 100%);
    color:var(--text); font: 14px/1.5 system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, "Helvetica Neue", Arial, "Apple Color Emoji","Segoe UI Emoji";
  }}
  .wrap {{ max-width:1200px; margin:24px auto; padding:0 16px; }}
  .grid {{
    display:grid; gap:16px;
    grid-template-columns: 1fr;
  }}
  @media (min-width: 1080px) {{
    .grid {{ grid-template-columns: 1fr; }}
  }}

  /* ===== Altseason header ===== */
  .alt-card {{
    background: radial-gradient(1200px 400px at 0% 0%, rgba(108,168,255,0.10), transparent 40%), var(--panel);
    border:1px solid #1e2a44; border-radius:16px; padding:18px;
    box-shadow: 0 8px 24px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.02);
  }}
  .alt-title {{
    display:flex; align-items:center; gap:10px; margin-bottom:10px;
    font-weight:700; letter-spacing:.3px;
  }}
  .alt-sub {{ color:var(--muted); font-size:12px; margin-bottom:12px }}
  .alt-kpis {{
    display:grid; gap:8px; grid-template-columns: repeat(6, minmax(140px,1fr));
  }}
  .kpi {{
    background: var(--soft); border:1px solid #202c48; border-radius:12px; padding:10px;
  }}
  .kpi .lbl {{ color:var(--muted); font-size:12px }}
  .kpi .val {{ font-size:16px; margin-top:2px; font-weight:600 }}
  .chip {{
    display:inline-flex; align-items:center; gap:6px;
    background:var(--chip); border:1px solid #203152; padding:3px 8px; border-radius:999px; font-size:12px; color:#b9c7e3;
  }}
  .chip.ok {{ border-color:#1f6d46; background:#143022; color:#b7f3cf }}
  .chip.warn {{ border-color:#6d611f; background:#2c2812; color:#ffeaa7 }}
  .chip.err {{ border-color:#6d1f1f; background:#311818; color:#ffbcbc }}

  /* ===== Table Trades ===== */
  .card {{
    background: var(--panel); border:1px solid #1e2a44; border-radius:16px; overflow:hidden;
    box-shadow: 0 8px 24px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.02);
  }}
  .card-header {{
    display:flex; justify-content:space-between; align-items:center; padding:14px 16px;
    border-bottom:1px solid #1e2a44; background:linear-gradient(0deg, rgba(108,168,255,0.08), transparent);
  }}
  .card-title {{ font-weight:700; letter-spacing:.2px }}
  .toolbar {{ display:flex; gap:8px; align-items:center; }}
  .toolbar .badge {{ background:#172136; border:1px solid #263353; padding:4px 8px; border-radius:8px; color:#b9c7e3; font-size:12px }}

  table {{
    width:100%; border-collapse:collapse; font-size:13px;
  }}
  thead th {{
    text-align:left; padding:10px 12px; color:#9fb1d6; background:#121a2a; position:sticky; top:0; z-index:1;
    border-bottom:1px solid #1f2b47;
  }}
  tbody td {{ padding:10px 12px; border-bottom:1px solid #162138; color:#d9e1f2 }}
  tbody tr:hover {{ background:#121a2a }}
  .num {{ text-align:right; font-variant-numeric: tabular-nums; }}

  .pill {{
    display:inline-block; padding:2px 8px; border-radius:999px; font-weight:700; font-size:12px;
  }}
  .pill.long {{ color:#b7f3cf; background:#143022; border:1px solid #1f6d46 }}
  .pill.short{{ color:#ffbcbc; background:#311818; border:1px solid #6d1f1f }}
  .pill.closed{{ color:#b9c7e3; background:#1b2438; border:1px solid #314266 }}

  .tpcell {{
    background:#0f182a; border:1px solid #203152; color:#9fb1d6; padding:4px 8px; border-radius:8px; display:inline-block;
    min-width:80px; text-align:center;
  }}
  .tpcell.hit {{ background:#0f2a1a; border-color:#1f6d46; color:#b7f3cf; font-weight:700; }}
  .slcell {{ color:#ffbcbc }}
  .slcell.hit {{ background:#311818; border:1px solid #6d1f1f; color:#ffbcbc; padding:2px 6px; border-radius:6px }}

  .footnote {{ color:var(--muted); font-size:12px; padding:10px 2px }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="grid">
      <!-- ====== ALTSEASON HEADER ====== -->
      <section class="alt-card">
        <div class="alt-title">
          <span style="font-size:18px">📈 Indicateurs Altseason</span>
          <span class="chip">Mise à jour: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alt.get('stamp', int(time.time()))))}</span>
        </div>
        <div class="alt-sub">Baromètre multi-facteurs pour juger l’environnement (BTC vs Alts, momentum & breadth). Recalcul auto.</div>
        <div class="alt-kpis">
          <div class="kpi">
            <div class="lbl">Dominance BTC</div>
            <div class="val">{fmt(alt.get('dominance_btc'))}</div>
          </div>
          <div class="kpi">
            <div class="lbl">Trend BTC</div>
            <div class="val">
              {"<span class='chip ok'>Haussière</span>" if (alt.get('btc_trend') == "UP") else "<span class='chip err'>Baissière</span>" if (alt.get('btc_trend') == "DOWN") else "<span class='chip warn'>Neutre</span>"}
            </div>
          </div>
          <div class="kpi">
            <div class="lbl">Corrélation Alts↔BTC</div>
            <div class="val">{fmt(alt.get('alts_btc_corr'))}</div>
          </div>
          <div class="kpi">
            <div class="lbl">Momentum Alts (USDT)</div>
            <div class="val">{fmt(alt.get('alts_usdt_momo'))}</div>
          </div>
          <div class="kpi">
            <div class="lbl">Momentum Alts (BTC)</div>
            <div class="val">{fmt(alt.get('alts_btc_momo'))}</div>
          </div>
          <div class="kpi">
            <div class="lbl">Market Breadth</div>
            <div class="val">{fmt(alt.get('market_breadth'))}</div>
          </div>
        </div>
      </section>

      <!-- ====== TRADES TABLE ====== -->
      <section class="card">
        <div class="card-header">
          <div class="card-title">🧾 Trades en cours & récents</div>
          <div class="toolbar">
            <span class="badge">Auto-refresh 30s</span>
          </div>
        </div>
        <div style="overflow:auto;">
          <table id="tradesTable">
            <thead>
              <tr>
                <th>Symbole</th>
                <th>TF</th>
                <th>Côté</th>
                <th class="num">Entry</th>
                <th class="num">SL</th>
                <th>TP1</th>
                <th>TP2</th>
                <th>TP3</th>
                <th>Statut</th>
              </tr>
            </thead>
            <tbody>
              {"".join([
                f"<tr data-tp1='{1 if t.get('tp1_hit') else 0}' data-tp2='{1 if t.get('tp2_hit') else 0}' data-tp3='{1 if t.get('tp3_hit') else 0}' data-sl='{1 if t.get('sl_hit') else 0}' data-closed='{1 if t.get('closed') else 0}'>"
                f"<td><b>{t.get('symbol')}</b></td>"
                f"<td>{t.get('tf')}</td>"
                f"<td><span class='pill {'long' if (str(t.get('side') or '').upper()=='LONG') else 'short' if (str(t.get('side') or '').upper()=='SHORT') else 'closed' if t.get('closed') else ''}'>{(t.get('side') or '—').upper()}</span></td>"
                f"<td class='num'>{fmt(t.get('entry'))}</td>"
                f"<td class='num slcell {'hit' if t.get('sl_hit') else ''}'>{fmt(t.get('sl'))}</td>"
                f"<td><span class='tpcell {'hit' if t.get('tp1_hit') else ''}'>{fmt(t.get('tp1'))}</span></td>"
                f"<td><span class='tpcell {'hit' if t.get('tp2_hit') else ''}'>{fmt(t.get('tp2'))}</span></td>"
                f"<td><span class='tpcell {'hit' if t.get('tp3_hit') else ''}'>{fmt(t.get('tp3'))}</span></td>"
                f"<td>{'Fermé' if t.get('closed') else ('SL touché' if t.get('sl_hit') else 'En cours')}</td>"
                "</tr>"
              ])}
            </tbody>
          </table>
        </div>
        <div class="footnote">
          Astuce: TP en <b>vert</b> = atteint, SL en <b style="color:var(--sl)">rouge</b> = touché. Les lignes “Fermé” sont conservées pour l’historique récent.
        </div>
      </section>
    </div>
  </div>

<script>
  // Auto-refresh doux (30s) : recharge seulement le tbody
  async function refreshTrades() {{
    try {{
      const r = await fetch('/api/trades', {{cache:'no-store'}});
      const j = await r.json();
      if (!j.ok) return;
      const rows = j.trades || [];
      const tbody = document.querySelector('#tradesTable tbody');
      const fmt = (x) => {{
        if (x === null || x === undefined) return '—';
        if (typeof x === 'number') {{
          // format court
          return Number.parseFloat(x).toPrecision(6);
        }}
        return x;
      }};
      tbody.innerHTML = rows.map(t => {{
        const side = (t.side || '—').toUpperCase();
        let pill = 'closed';
        if (side === 'LONG') pill = 'long'; else if (side === 'SHORT') pill='short';
        const slHit = !!t.sl_hit;
        const closed = !!t.closed;
        return `
        <tr data-tp1="${{t.tp1_hit?1:0}}" data-tp2="${{t.tp2_hit?1:0}}" data-tp3="${{t.tp3_hit?1:0}}" data-sl="${{slHit?1:0}}" data-closed="${{closed?1:0}}">
          <td><b>${{t.symbol}}</b></td>
          <td>${{t.tf}}</td>
          <td><span class="pill ${{pill}}">${{side}}</span></td>
          <td class="num">${{fmt(t.entry)}}</td>
          <td class="num slcell ${slHit?'hit':''}">${{fmt(t.sl)}}</td>
          <td><span class="tpcell ${t.tp1_hit?'hit':''}">${{fmt(t.tp1)}}</span></td>
          <td><span class="tpcell ${t.tp2_hit?'hit':''}">${{fmt(t.tp2)}}</span></td>
          <td><span class="tpcell ${t.tp3_hit?'hit':''}">${{fmt(t.tp3)}}</span></td>
          <td>${{closed?'Fermé':(slHit?'SL touché':'En cours')}}</td>
        </tr>`;
      }}).join('');
    }} catch(e) {{
      console.warn('refreshTrades error', e);
    }}
  }}
  setInterval(refreshTrades, 30000);
</script>
</body>
</html>
"""
    return HTMLResponse(html)
# ============================ /main.py — BLOC 3/5 ==============================
# ============================== main.py — BLOC 4/5 ==============================
import os, json, time, threading, sqlite3
from typing import Dict, Any, List, Optional, Tuple
from fastapi import Body

# ---------- Constantes & Utils ----------
ALT_INTERVAL = int(os.getenv("ALT_INTERVAL", "120"))     # secondes pour le daemon altseason (si utilisé)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")     # ex: "8478...:AA..."
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")     # ex: "-1001234567890"
TELEGRAM_COOLDOWN = int(os.getenv("TELEGRAM_COOLDOWN", "10"))  # anti-spam (secondes)

_last_telegram_send = 0.0
_telegram_pin_last_message = False  # optionnel

def now_ts_ms() -> int:
    return int(time.time() * 1000)

def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

# ---------- Base SQLite ----------
def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def _ensure_tables():
    with _db_connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            type TEXT,
            symbol TEXT,
            tf TEXT,
            tf_label TEXT,
            side TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            price REAL,
            time INTEGER,
            trade_id TEXT,
            note TEXT,
            direction TEXT,
            extra TEXT
        )
        """)
        con.commit()

_ensure_tables()

# ---------- Telegram ----------
def send_telegram_ex(text: str, pin: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Envoi Telegram simple avec cooldown. Retourne (ok, err).
    S'il n'y a pas de TOKEN/CHAT, on considère comme OK (no-op).
    """
    global _last_telegram_send
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return True, None  # pas configuré → no-op silencieux

        now = time.time()
        if now - _last_telegram_send < TELEGRAM_COOLDOWN:
            print("WARNING:aitrader:Telegram send skipped due to cooldown")
            return True, None

        import urllib.request
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        req = urllib.request.Request(api, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            _last_telegram_send = now
            # Optionnel pin
            if pin and _telegram_pin_last_message:
                pass
        return True, None
    except Exception as e:
        print(f"WARNING:aitrader:Telegram send_telegram_ex exception: {e}")
        return False, str(e)

def _format_telegram(event: Dict[str, Any]) -> Optional[str]:
    """
    Compose un message Telegram compact & lisible.
    - Vector Candle: 🟩 (UP) / 🟥 (DOWN) (remplace l’ancien 🟪)
    - TP1/2/3 hit en vert dans le texte
    """
    t = (event.get("type") or "").upper()
    sym = event.get("symbol") or "?"
    tf = event.get("tf_label") or event.get("tf") or "?"
    side = (event.get("side") or "").upper() or "—"
    price = event.get("price")
    entry = event.get("entry")
    sl = event.get("sl")
    tp1, tp2, tp3 = event.get("tp1"), event.get("tp2"), event.get("tp3")

    if t == "ENTRY":
        return (f"🚀 <b>ENTRY</b> — <b>{sym}</b> {tf}\n"
                f"Côté: <b>{side}</b>\n"
                f"Entry: <code>{entry}</code>\n"
                f"SL: <code>{sl}</code>\n"
                f"TP1: <code>{tp1}</code> • TP2: <code>{tp2}</code> • TP3: <code>{tp3}</code>")

    if t in ("TP1_HIT", "TP2_HIT", "TP3_HIT"):
        tp_txt = {"TP1_HIT": "TP1", "TP2_HIT": "TP2", "TP3_HIT": "TP3"}[t]
        return f"✅ <b>{tp_txt} atteint</b> — <b>{sym}</b> {tf} • Côté <b>{side}</b>"

    if t == "SL_HIT":
        return f"⛔ <b>SL touché</b> — <b>{sym}</b> {tf} • Côté <b>{side}</b>"

    if t == "CLOSE":
        reason = event.get("reason")
        reason_txt = f" — {reason}" if reason else ""
        return f"📕 <b>Position fermée</b> — <b>{sym}</b> {tf}{reason_txt}"

    if t == "VECTOR_CANDLE":
        direction = (event.get("direction") or "").upper()
        mark = "🟩" if direction == "UP" else "🟥" if direction == "DOWN" else "🟪"
        px = f" @ <code>{price}</code>" if price is not None else ""
        note = event.get("note") or (f"Vector Candle {direction}" if direction else "Vector Candle")
        return f"{mark} <b>Vector Candle</b> — <b>{sym}</b> {tf}{px}\n{note}"

    if t == "AOE_PREMIUM":
        return f"💠 <b>AOE Premium</b> — <b>{sym}</b> {tf}"
    if t == "AOE_DISCOUNT":
        return f"🌀 <b>AOE Discount</b> — <b>{sym}</b> {tf}"

    # fallback
    return f"ℹ️ <b>{t}</b> — <b>{sym}</b> {tf}"

# ---------- Sauvegarde d'évènements ----------
def _derive_trade_id(ev: Dict[str, Any]) -> str:
    if ev.get("trade_id"):
        return ev["trade_id"]
    symbol = (ev.get("symbol") or "UNK").upper()
    tf = str(ev.get("tf") or "UNK")
    # pour éviter 'name now_iso not defined', on s’appuie sur timestamp ms
    ts_ms = int(ev.get("time") or now_ts_ms())
    return f"{symbol}_{tf}_{ts_ms}"

def save_event(ev: Dict[str, Any], notify: bool = True) -> bool:
    """
    Sauvegarde l’évènement dans SQLite, quel que soit le payload entrant.
    Gère les champs manquants; crée un trade_id si absent.
    Envoie Telegram formaté (avec cooldown) si notify=True.
    """
    try:
        _ensure_tables()
        e = {
            "created_at": now_iso(),
            "type": (ev.get("type") or "").upper(),
            "symbol": ev.get("symbol"),
            "tf": str(ev.get("tf") or ""),
            "tf_label": ev.get("tf_label"),
            "side": ev.get("side"),
            "entry": ev.get("entry"),
            "sl": ev.get("sl"),
            "tp1": ev.get("tp1"),
            "tp2": ev.get("tp2"),
            "tp3": ev.get("tp3"),
            "price": ev.get("price"),
            "time": int(ev.get("time") or now_ts_ms()),
            "trade_id": _derive_trade_id(ev),
            "note": ev.get("note"),
            "direction": ev.get("direction"),
            "extra": None
        }
        # stocke le reste du payload brut dans extra
        try:
            extra = {k: v for k, v in ev.items() if k not in e}
            e["extra"] = json.dumps(extra) if extra else None
        except Exception:
            e["extra"] = None

        with _db_connect() as con:
            con.execute("""
                INSERT INTO events (created_at,type,symbol,tf,tf_label,side,entry,sl,tp1,tp2,tp3,price,time,trade_id,note,direction,extra)
                VALUES (:created_at,:type,:symbol,:tf,:tf_label,:side,:entry,:sl,:tp1,:tp2,:tp3,:price,:time,:trade_id,:note,:direction,:extra)
            """, e)
            con.commit()

        # Telegram
        if notify:
            msg = _format_telegram(e)
            if msg:
                ok, err = send_telegram_ex(msg, pin=False)
                if not ok and err:
                    print(f"WARNING:aitrader:Telegram send failed: {err}")

        print(f"INFO:aitrader:Saved event: type={e['type']} symbol={e['symbol']} tf={e['tf']} trade_id={e['trade_id']}")
        return True
    except Exception as ex:
        print(f"ERROR:aitrader:save_event failed: {ex}")
        return False

# ---------- Webhook (si non défini ailleurs) ----------
@app.post("/tv-webhook")
def tv_webhook(payload: Dict[str, Any] = Body(...)):
    """
    Accepte les payloads TradingView variés.
    On ne bloque pas si 'secret' absent (à activer si besoin).
    """
    try:
        # (Optionnel) contrôle secret
        secret_env = os.getenv("TV_WEBHOOK_SECRET")
        if secret_env:
            if (payload.get("secret") or "") != secret_env:
                return {"ok": False, "error": "forbidden"}

        # Sauvegarde & notify
        save_event(payload, notify=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- Flux brut pour debug / intégrations ----------
@app.get("/api/feed")
def api_feed(limit: int = 200):
    try:
        with _db_connect() as con:
            cur = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))
            rows = [dict(r) for r in cur.fetchall()]
        return {"ok": True, "events": rows}
    except Exception as e:
        return {"ok": False, "error": str(e), "events": []}

# ---------- Altseason Daemon (optionnel) ----------
def run_altseason_daemon(interval: int = ALT_INTERVAL):
    """
    Exemple d’actualisation périodique du snapshot Altseason.
    Met à jour le _altseason_snapshot sans crash si erreur.
    """
    global _altseason_snapshot
    while True:
        try:
            # TODO: brancher vos vraies métriques ici
            _altseason_snapshot.update({
                "dominance_btc": _altseason_snapshot.get("dominance_btc"),
                "btc_trend": _altseason_snapshot.get("btc_trend"),  # "UP"/"DOWN"/None
                "alts_btc_corr": _altseason_snapshot.get("alts_btc_corr"),
                "alts_usdt_momo": _altseason_snapshot.get("alts_usdt_momo"),
                "alts_btc_momo": _altseason_snapshot.get("alts_btc_momo"),
                "market_breadth": _altseason_snapshot.get("market_breadth"),
                "stamp": int(time.time())
            })
        except Exception as e:
            print(f"WARNING:aitrader:Altseason daemon error: {e}")
        time.sleep(max(30, int(interval)))

# Lancement optionnel (désactivé par défaut pour Render)
if os.getenv("RUN_ALTSEASON_DAEMON", "0") == "1":
    threading.Thread(target=run_altseason_daemon, args=(ALT_INTERVAL,), daemon=True).start()
# ============================ /main.py — BLOC 4/5 ==============================
# ============================== main.py — BLOC 5/5 ==============================
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

# --------------------------------- Middlewares ---------------------------------
# (sécurisé: n'ouvre qu’aux origines déclarées si variable présente)
_allow_origins = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=512)

# ----------------------------------- Health ------------------------------------
@app.get("/health", response_class=PlainTextResponse)
def health():
    try:
        # ping DB
        with _db_connect() as con:
            con.execute("SELECT 1")
        return "ok"
    except Exception as e:
        return PlainTextResponse(f"db_error: {e}", status_code=500)

# ---------------------------------- Metrics ------------------------------------
@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """
    Prometheus-like métriques très simples.
    """
    try:
        with _db_connect() as con:
            total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last = con.execute("SELECT MAX(time) FROM events").fetchone()[0] or 0
        lines = [
            "# HELP ai_trader_events_total Nombre d'événements enregistrés",
            "# TYPE ai_trader_events_total counter",
            f"ai_trader_events_total {total}",
            "# HELP ai_trader_last_event_ts_ms Timestamp ms du dernier événement",
            "# TYPE ai_trader_last_event_ts_ms gauge",
            f"ai_trader_last_event_ts_ms {int(last)}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return PlainTextResponse(f"# error {e}", status_code=500)

# --------------------------- Aide: stats & recompute ----------------------------
@app.get("/api/stats", response_class=JSONResponse)
def api_stats():
    """
    Petites stats résumées pour la page trades (et le dashboard).
    """
    try:
        with _db_connect() as con:
            cur = con.execute("""
                SELECT type, COUNT(*) c FROM events GROUP BY type ORDER BY c DESC
            """)
            by_type = {r["type"]: r["c"] for r in cur.fetchall()}
            cur = con.execute("""
                SELECT symbol, COUNT(*) c FROM events GROUP BY symbol ORDER BY c DESC LIMIT 10
            """)
            top_symbols = [{"symbol": r["symbol"], "count": r["c"]} for r in cur.fetchall()]
            last_ts = con.execute("SELECT MAX(time) m FROM events").fetchone()["m"] or 0
        return {
            "ok": True,
            "by_type": by_type,
            "top_symbols": top_symbols,
            "last_event_ts": last_ts,
            "altseason": _altseason_snapshot,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/recompute-tp", response_class=JSONResponse)
def api_recompute_tp():
    """
    (Hook optionnel) Si vous avez une logique de recalcul externe, branchez-la ici.
    Pour l’instant, ne fait que renvoyer ok=True.
    """
    try:
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ------------------------------- Assets front -----------------------------------
_TRADES_CSS = r"""
:root{
  --bg:#0b0e11; --card:#12161c; --muted:#6b7280; --txt:#e5e7eb;
  --green:#10b981; --red:#ef4444; --amber:#f59e0b; --cyan:#06b6d4;
  --purple:#8b5cf6;
  --border: #1f2937;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.container{max-width:1200px;margin:24px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;box-shadow:0 0 0 1px rgba(255,255,255,0.02) inset}
.header{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:12px}
.header h2{margin:0;font-size:18px}
.kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}
.kpi{background:#0f1318;border:1px solid var(--border);border-radius:14px;padding:12px}
.kpi .label{font-size:12px;color:var(--muted)}
.kpi .value{font-weight:700;margin-top:4px}
.kpi .hint{font-size:11px;color:var(--muted)}

.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border-radius:999px;font-size:12px;background:#0f1318;border:1px solid var(--border);color:var(--txt)}
.badge .dot{width:8px;height:8px;border-radius:50%}
.dot.up{background:var(--green)}
.dot.down{background:var(--red)}
.dot.neutral{background:var(--muted)}

.table{width:100%;border-collapse:separate;border-spacing:0 8px}
.table thead th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);text-align:left;padding:0 10px}
.table tbody tr{background:#0f1318;border:1px solid var(--border)}
.table tbody tr td{padding:10px}
.table tbody tr:first-child td{border-top-left-radius:12px;border-top-right-radius:12px}
.table tbody tr:last-child td{border-bottom-left-radius:12px;border-bottom-right-radius:12px}

.tag{padding:2px 8px;border-radius:8px;border:1px solid var(--border);background:#0c1117;font-size:12px;color:var(--muted)}
.tag.long{color:var(--green);border-color:#094c3a;background:#071b15}
.tag.short{color:var(--red);border-color:#4c0913;background:#1b0709}

.tp{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:8px;border:1px solid var(--border);font-size:12px;background:#0c1117}
.tp.hit{background:#071b15;border-color:#094c3a;color:var(--green)}
.tp.pending{color:var(--muted)}
.tp .dot{width:8px;height:8px;border-radius:50%}
.tp.hit .dot{background:var(--green)}
.tp.pending .dot{background:var(--muted)}

.legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.legend .item{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.legend .sw{width:12px;height:12px;border-radius:4px}
.sw.vec-up{background:var(--green)}
.sw.vec-down{background:var(--red)}
.sw.tp-hit{background:var(--green)}
.sw.tp-pend{background:var(--muted)}

@media (max-width:900px){
  .kpi-row{grid-template-columns:repeat(2,1fr)}
  .table thead{display:none}
  .table tbody tr td{display:block}
}
"""

_TRADES_JS = r"""
/* petites aides front pour la page trades */
function applyTpBadges() {
  document.querySelectorAll('[data-tp]').forEach(cell => {
    const status = cell.getAttribute('data-tp'); // 'hit' ou 'pending'
    cell.classList.add('tp', status === 'hit' ? 'hit' : 'pending');
    if (!cell.querySelector('.dot')) {
      const dot = document.createElement('span');
      dot.className = 'dot';
      cell.prepend(dot);
    }
  });
}

function colorVectorBadges() {
  document.querySelectorAll('[data-vdir]').forEach(el => {
    const dir = (el.getAttribute('data-vdir') || '').toUpperCase();
    const dot = el.querySelector('.dot') || (function(){
      const s=document.createElement('span'); s.className='dot'; el.prepend(s); return s;
    })();
    dot.classList.remove('up','down','neutral');
    if (dir === 'UP') dot.classList.add('up');
    else if (dir === 'DOWN') dot.classList.add('down');
    else dot.classList.add('neutral');
  });
}

window.addEventListener('DOMContentLoaded', () => {
  applyTpBadges();
  colorVectorBadges();
});
"""

@app.get("/assets/trades.css", response_class=PlainTextResponse)
def assets_css():
    return PlainTextResponse(_TRADES_CSS, media_type="text/css; charset=utf-8")

@app.get("/assets/trades.js", response_class=PlainTextResponse)
def assets_js():
    return PlainTextResponse(_TRADES_JS, media_type="application/javascript; charset=utf-8")

# --------------------------- Page d’accueil simple ------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse("""
<!doctype html>
<html lang="fr">
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AI Trader — Dashboard</title>
<link rel="stylesheet" href="/assets/trades.css"/>
<body>
  <div class="container">
    <div class="card header">
      <h2>AI Trader — Dashboard</h2>
      <div class="legend">
        <div class="item"><span class="sw vec-up"></span> Vector UP</div>
        <div class="item"><span class="sw vec-down"></span> Vector DOWN</div>
        <div class="item"><span class="sw tp-hit"></span> TP atteint</div>
        <div class="item"><span class="sw tp-pend"></span> TP en attente</div>
      </div>
    </div>

    <!-- Altseason en haut -->
    <div class="card">
      <div class="header">
        <h2>Indicateurs Altseason</h2>
        <span class="badge"><span class="dot neutral"></span> Live</span>
      </div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="label">Dominance BTC</div>
          <div class="value" id="kpi-dbtc">–</div>
          <div class="hint" id="kpi-dbtc-hint">impact sur alts</div>
        </div>
        <div class="kpi">
          <div class="label">Tendance BTC</div>
          <div class="value" id="kpi-btc-trend">–</div>
          <div class="hint">UP = favorable altseason</div>
        </div>
        <div class="kpi">
          <div class="label">Corrélation Alts↔BTC</div>
          <div class="value" id="kpi-corr">–</div>
          <div class="hint">faible corrélation ⇒ +alts</div>
        </div>
        <div class="kpi">
          <div class="label">Momentum Alts/USDT</div>
          <div class="value" id="kpi-momo-usdt">–</div>
          <div class="hint">moyenne du marché</div>
        </div>
        <div class="kpi">
          <div class="label">Breadth Marché</div>
          <div class="value" id="kpi-breadth">–</div>
          <div class="hint">% alts en hausse</div>
        </div>
      </div>
      <div class="legend">
        <div class="item">Mise à jour: <span id="alt-stamp">–</span></div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="header">
        <h2>Derniers trades</h2>
        <a class="tag" href="/api/feed?limit=200" target="_blank">/api/feed</a>
      </div>
      <table class="table">
        <thead>
          <tr>
            <th>Quand</th><th>Type</th><th>Symb.</th><th>TF</th><th>Côté</th>
            <th>Entrée</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
            <th>Prix</th><th>Note</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>

<script src="/assets/trades.js"></script>
<script>
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const j = await r.json();
    if (!j.ok) return;

    const a = j.altseason || {};
    document.querySelector('#kpi-dbtc').textContent = (a.dominance_btc ?? '–') + '%';
    document.querySelector('#kpi-btc-trend').textContent = a.btc_trend ?? '–';
    document.querySelector('#kpi-corr').textContent = a.alts_btc_corr ?? '–';
    document.querySelector('#kpi-momo-usdt').textContent = a.alts_usdt_momo ?? '–';
    document.querySelector('#kpi-breadth').textContent = a.market_breadth ?? '–';
    const stamp = a.stamp ? new Date(a.stamp*1000).toLocaleString() : '–';
    document.querySelector('#alt-stamp').textContent = stamp;
  } catch(e) {}
}

function badgeForType(ev){
  const t = ev.type || '';
  if (t === 'ENTRY') return '<span class="tag long">ENTRY</span>';
  if (t === 'SL_HIT') return '<span class="tag short">SL</span>';
  if (t === 'CLOSE') return '<span class="tag">CLOSE</span>';
  if (t === 'VECTOR_CANDLE'){
    const d = (ev.direction||'').toUpperCase();
    const cls = d==='UP' ? 'up' : (d==='DOWN'?'down':'neutral');
    return `<span class="badge" data-vdir="${d}"><span class="dot ${cls}"></span>Vector</span>`;
  }
  if (t.endsWith('_HIT')) return '<span class="badge"><span class="dot up"></span>TP hit</span>';
  return `<span class="tag">${t}</span>`;
}

function tdTp(val, hit){
  const status = hit ? 'hit' : 'pending';
  const v = (val==null || val==='') ? '—' : val;
  return `<td data-tp="${status}">${v}</td>`;
}

function deriveHitFlags(ev){
  // heuristique: si type=TPx_HIT, on colore celui atteint en vert.
  const t = ev.type || '';
  return {
    tp1: t==='TP1_HIT' || t==='TP2_HIT' || t==='TP3_HIT',
    tp2: t==='TP2_HIT' || t==='TP3_HIT',
    tp3: t==='TP3_HIT'
  };
}

async function loadFeed(){
  const r = await fetch('/api/feed?limit=120');
  const j = await r.json();
  const rows = j.events || [];
  const tb = document.querySelector('#rows');
  tb.innerHTML = '';
  for (const ev of rows){
    const when = ev.created_at || '—';
    const typeBadge = badgeForType(ev);
    const side = ev.side || '—';
    const entry = ev.entry ?? '—';
    const sl = ev.sl ?? '—';
    const price = ev.price ?? '—';
    const note = ev.note ?? '';
    const tf = ev.tf_label || ev.tf || '—';
    const flags = deriveHitFlags(ev);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${when}</td>
      <td>${typeBadge}</td>
      <td>${ev.symbol || '—'}</td>
      <td>${tf}</td>
      <td>${side? `<span class="tag ${side.toLowerCase()}">${side}</span>` : '—'}</td>
      <td>${entry}</td>
      <td>${sl}</td>
      ${tdTp(ev.tp1, flags.tp1)}
      ${tdTp(ev.tp2, flags.tp2)}
      ${tdTp(ev.tp3, flags.tp3)}
      <td>${price}</td>
      <td>${note}</td>
    `;
    tb.appendChild(tr);
  }
  // active styles
  applyTpBadges();
  colorVectorBadges();
}

loadStats();
loadFeed();
setInterval(loadStats, 15000);
setInterval(loadFeed, 8000);
</script>
</body>
</html>
    """)

# ============================ /main.py — BLOC 5/5 ==============================

# ------------------------------- Récap rapide ----------------------------------
# - /health : ping de l’app
# - /metrics : métriques simples
# - /      : dashboard minimal, Altseason en haut, tableau trades dessous
# - /assets/trades.css & /assets/trades.js : styles & logique front
# - TP1/TP2/TP3 deviennent verts (classe .tp.hit) dès qu’un TPx_HIT arrive
# - Vector UP s’affiche avec pastille verte (et Telegram remplace 🟪 par 🟩)
