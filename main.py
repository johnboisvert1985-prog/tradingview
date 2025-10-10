@app.get("/trades", response_class=HTMLResponse)
async def trades():
    rows = build_trade_rows(50)
    stats = trading_state.get_stats()
    patterns = detect_patterns(rows)

    table = ""
    for r in rows[:20]:
        if r.get("row_state") == "tp":
            badge = '<span class="badge badge-green">TP</span>'
        elif r.get("row_state") == "sl":
            badge = '<span class="badge badge-red">SL</span>'
        else:
            badge = '<span class="badge badge-yellow">En cours</span>'
        pnl = ""
        if r.get('pnl_percent') is not None:
            color = '#10b981' if (r.get('pnl_percent') or 0) > 0 else '#ef4444'
            pnl = f'<span style="color:{color};font-weight:700">{(r.get("pnl_percent") or 0):+.2f}%</span>'
        table += (
            "<tr>"
            f"<td>{r.get('symbol','N/A')}</td>"
            f"<td>{r.get('tf_label','N/A')}</td>"
            f"<td>{r.get('side','N/A')}</td>"
            f"<td>{r.get('entry') or 'N/A'}</td>"
            f"<td>{badge} {pnl}</td>"
            "</tr>"
        )

    patterns_html = "".join(f'<li style="padding:8px">{p}</li>' for p in patterns)

    html = (
        "<!DOCTYPE html>"
        "<html><head><title>Dashboard</title><meta charset='UTF-8'>"
        + CSS +
        "</head><body><div class='container'>"
        "<div class='header'><h1>📊 Dashboard</h1><p>Live <span class='live-badge'>LIVE</span></p></div>"
        + NAV +
        "<div class='grid' style='grid-template-columns:repeat(auto-fit,minmax(300px,1fr))'>"

            # ——— Carte Fear & Greed
            "<div class='card'><h2>😱 Fear & Greed <span class='live-badge'>LIVE</span></h2>"
            "<div id='fg' style='text-align:center;padding:40px'>⏳</div></div>"

            # ——— Carte Bull Run + NOTE EXPLICATIVE
            "<div class='card'><h2>🚀 Bull Run <span class='live-badge'>LIVE</span></h2>"
            "<div id='br' style='text-align:center;padding:40px'>⏳</div>"
            "<details style='margin-top:8px;background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.2);border-radius:8px;padding:12px;'>"
            "<summary style='cursor:pointer;font-weight:700;color:#94a3b8'>ℹ️ Phases du bull run & critères</summary>"
            "<div style='margin-top:10px;font-size:14px;color:#cbd5e1;line-height:1.5'>"
            "<ol style='padding-left:18px'>"
            "<li><b>Phase 0 – Accumulation</b> : marché encore hésitant, flux institutionnels discrets, volumes en hausse lente. "
            "Indices : Fear & Greed < 55, <i>funding</i> modéré, BTC stabilise au-dessus de ses moyennes de long terme.</li>"
            "<li><b>Phase 1 – Bitcoin Season</b> : <b>BTC domine</b> la performance et la capitalisation. "
            "Indices : <b>BTC.D ≳ 48%</b>, nouveaux plus hauts/ruptures de range sur BTC, Fear & Greed ≈ 55–70, afflux sur BTC/ETF.</li>"
            "<li><b>Phase 2 – ETH & Large Caps</b> : rotation vers ETH et grosses capitalisations. "
            "Indices : <b>BTC.D en repli vers 45–48%</b>, ETH/BTC se renforce, Fear & Greed ≈ 60–75, volumes en expansion sur top 10–20.</li>"
            "<li><b>Phase 3 – Altseason</b> : surperformance des mid/small caps, meme-coins et narratives. "
            "Indices : <b>BTC.D ≲ 45%</b> et baisse, Fear & Greed > 75 (euphorie), hausses étendues et volatiles, risques élevés.</li>"
            "</ol>"
            "<div style='margin-top:8px;color:#94a3b8'>"
            "⚠️ Les seuils sont indicatifs. Le modèle en temps réel utilise <b>dominance BTC</b> & <b>sentiment</b> pour estimer la phase affichée."
            "</div>"
            "</div>"
            "</details>"
            "</div>"

            # ——— Carte Patterns
            "<div class='card'><h2>🤖 Patterns</h2><ul class='list'>" + patterns_html + "</ul></div>"

        "</div>"

        # ——— Métriques
        "<div class='grid' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>"
            "<div class='metric'><div class='metric-label'>Total</div><div class='metric-value'>" + str(stats['total_trades']) + "</div></div>"
            "<div class='metric'><div class='metric-label'>Actifs</div><div class='metric-value'>" + str(stats['active_trades']) + "</div></div>"
            "<div class='metric'><div class='metric-label'>Win Rate</div><div class='metric-value'>" + str(int(stats['win_rate'])) + "%</div></div>"
            "<div class='metric'><div class='metric-label'>Capital</div><div class='metric-value' style='font-size:24px'>$" + f"{stats['current_equity']:.0f}" + "</div></div>"
            "<div class='metric'><div class='metric-label'>Return</div>"
                "<div class='metric-value' style='color:" + ("#10b981" if stats['total_return']>=0 else "#ef4444") + "'>"
                + f"{stats['total_return']:+.1f}%" +
                "</div></div>"
        "</div>"

        # ——— Tableau
        "<div class='card'><h2>📊 Trades</h2>"
        "<table><thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>Status</th></tr></thead>"
        "<tbody>" + table + "</tbody></table></div>"

        # ——— JS
        "<script>"
        "fetch('/api/fear-greed').then(r=>r.json()).then(d=>{"
        "  if(d.ok){"
        "    const f=d.fear_greed;"
        "    document.getElementById('fg').innerHTML="
        "      `<div class=\"gauge\"><div class=\"gauge-inner\">"
        "         <div class=\"gauge-value\" style=\"color:${f.color}\">${f.value}</div>"
        "         <div class=\"gauge-label\">/ 100</div>"
        "       </div></div>"
        "       <div style=\"text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${f.color}\">${f.emoji} ${f.sentiment}</div>"
        "       <p style=\"color:#64748b;font-size:12px;text-align:center;margin-top:8px\">${f.recommendation}</p>`;"
        "  }"
        "});"
        "fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{"
        "  if(d.ok){"
        "    const b=d.bullrun_phase;"
        "    document.getElementById('br').innerHTML="
        "      `<div style=\"font-size:56px;margin-bottom:8px\">${b.emoji}</div>"
        "       <div style=\"font-size:20px;font-weight:900;color:${b.color}\">${b.phase_name}</div>"
        "       <p style=\"color:#64748b;font-size:12px;margin-top:8px\">${b.description}</p>"
        "       <div style=\"margin-top:12px;font-size:12px;color:#10b981\">"
        "         BTC: $${(b.btc_price||0).toLocaleString()} | MC: $${(b.market_cap/1e12).toFixed(2)}T"
        "       </div>`;"
        "  }"
        "});"
        "</script>"

        "</div></body></html>"
    )
    return HTMLResponse(html)
