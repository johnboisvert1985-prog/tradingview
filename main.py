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
def trades_public(symbol: Optional[str]=Query(None),
                  tf: Optional[str]=Query(None),
                  start: Optional[str]=Query(None),
                  end: Optional[str]=Query(None),
                  limit: int=Query(100)):
    start_ep=parse_date_to_epoch(start); end_ep=parse_date_end_to_epoch(end)
    trades, summary = build_trades_filtered(symbol, tf, start_ep, end_ep, max_rows=max(5000, limit*10))
    rows_html=""; data = trades[-limit:] if limit else trades
    for tr in data:
        outcome = tr["outcome"] or "NONE"
        badge_class = "badge-win" if outcome in ("TP1_HIT","TP2_HIT","TP3_HIT") else ("badge-loss" if outcome=="SL_HIT" else "")
        outcome_html = f'<span class="chip {badge_class}">{escape_html(outcome)}</span>'
        rows_html += ("<tr>"
                      f"<td>{escape_html(str(tr['trade_id']))}</td>"
                      f"<td>{escape_html(str(tr.get('symbol') or ''))}</td>"
                      f"<td>{escape_html(str(tr.get('tf') or ''))}</td>"
                      f"<td>{escape_html(str(tr.get('side') or ''))}</td>"
                      f"<td>{fmt_num(tr.get('entry'))}</td>"
                      f"<td>{fmt_num(tr.get('sl'))}</td>"
                      f"<td>{fmt_num(tr.get('tp1'))}</td>"
                      f"<td>{fmt_num(tr.get('tp2'))}</td>"
                      f"<td>{fmt_num(tr.get('tp3'))}</td>"
                      f"<td>{outcome_html}</td>"
                      f"<td>{tr.get('duration_sec') if tr.get('duration_sec') is not None else ''}</td>"
                      "</tr>")
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
