@app.get("/trades", response_class=HTMLResponse)
async def trades_page():
    rows = build_trade_rows(50)
    patterns = detect_trading_patterns(rows)
    metrics = calculate_advanced_metrics(rows)
    closed = [r for r in rows if r.get("row_state") in ("tp", "sl")]
    wr = (sum(1 for r in closed if r.get("row_state")=="tp") / len(closed) * 100) if closed else 0
    
    table = ""
    for r in rows[:20]:
        badge = f'<span class="badge badge-green">TP</span>' if r.get("row_state")=="tp" else (f'<span class="badge badge-red">SL</span>' if r.get("row_state")=="sl" else f'<span class="badge badge-yellow">En cours</span>')
        table += f"""<tr style="border-bottom:1px solid rgba(99,102,241,0.1)"><td style="padding:12px">{r.get('symbol','N/A')}</td><td style="padding:12px">{r.get('tf_label','N/A')}</td><td style="padding:12px">{r.get('side','N/A')}</td><td style="padding:12px">{r.get('entry') or 'N/A'}</td><td style="padding:12px">{badge}</td></tr>"""
    
    patterns_html = "".join(f'<li style="padding:8px;font-size:14px">{p}</li>' for p in patterns[:5])
    curve = calculate_equity_curve(rows)
    curr_equity = curve[-1]["equity"] if curve else settings.INITIAL_CAPITAL
    total_return = ((curr_equity - settings.INITIAL_CAPITAL) / settings.INITIAL_CAPITAL) * 100
    
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Dashboard</title>{CSS}</head><body><div class="container"><div class="header"><h1>📊 Dashboard Principal</h1><p>Vue complète 🔴 <strong>MARCHÉ RÉEL</strong> + 🔔 <strong>Telegram</strong></p></div>{NAV}
    
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
        <div class="card"><h2>😱 Fear & Greed Index</h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>
        <div class="card"><h2>🚀 Bull Run Phase <span style="color:#10b981;font-size:14px">● LIVE</span></h2><div id="br" style="text-align:center;padding:40px">⏳</div></div>
        <div class="card"><h2>🤖 AI Patterns</h2><ul class="list" style="margin:0">{patterns_html if patterns_html else '<li style="padding:8px;color:#64748b">Pas de patterns</li>'}</ul><a href="/patterns" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir tous les patterns</a></div>
    </div>
    
    <div class="card" id="phases" style="display:none"><h2>📈 Phases du Bull Run (Marché Réel)</h2>
        <div id="p1" class="phase-indicator" style="color:#f7931a"><div class="phase-number">₿</div><div style="flex:1"><div style="font-weight:700">Phase 1: Bitcoin Season</div><div style="font-size:12px;color:#64748b" id="p1s">--</div></div></div>
        <div id="p2" class="phase-indicator" style="color:#627eea"><div class="phase-number">💎</div><div style="flex:1"><div style="font-weight:700">Phase 2: ETH & Large-Cap</div><div style="font-size:12px;color:#64748b" id="p2s">--</div></div></div>
        <div id="p3" class="phase-indicator" style="color:#10b981"><div class="phase-number">🚀</div><div style="flex:1"><div style="font-weight:700">Phase 3: Altcoin Season</div><div style="font-size:12px;color:#64748b" id="p3s">--</div></div></div>
    </div>
    
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
        <div class="metric"><div class="metric-label">Total Trades</div><div class="metric-value">{len(rows)}</div></div>
        <div class="metric"><div class="metric-label">Trades Actifs</div><div class="metric-value">{sum(1 for r in rows if r.get('row_state')=='normal')}</div></div>
        <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int(wr)}%</div></div>
        <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value">{metrics['sharpe_ratio']}</div><p style="font-size:11px;color:#64748b;margin-top:4px"><a href="/advanced-metrics" style="color:#6366f1;text-decoration:none">→ Metrics</a></p></div>
        <div class="metric"><div class="metric-label">Capital Actuel</div><div class="metric-value" style="font-size:24px">${curr_equity:.0f}</div><p style="font-size:11px;color:#64748b;margin-top:4px"><a href="/equity-curve" style="color:#6366f1;text-decoration:none">→ Equity</a></p></div>
        <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" style="color:{'#10b981' if total_return>=0 else '#ef4444'};font-size:24px">{total_return:+.1f}%</div></div>
    </div>
    
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
        <div class="card">
            <h2>📈 Performance</h2>
            <div style="display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                <span>Expectancy</span><span style="font-weight:700;color:#6366f1">{metrics['expectancy']:.2f}%</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)">
                <span>Sortino Ratio</span><span style="font-weight:700;color:#6366f1">{metrics['sortino_ratio']}</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding:12px">
                <span>Max Drawdown</span><span style="font-weight:700;color:#ef4444">-{metrics['max_drawdown']:.1f}%</span>
            </div>
            <a href="/advanced-metrics" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir toutes les métriques</a>
        </div>
        
        <div class="card">
            <h2>🔥 Best Time to Trade</h2>
            <div id="heatmap-preview">⏳ Chargement...</div>
            <a href="/heatmap" style="display:block;margin-top:12px;color:#6366f1;text-decoration:none;font-size:14px">→ Voir la heatmap complète</a>
        </div>
        
        <div class="card">
            <h2>📝 Quick Actions</h2>
            <div style="display:flex;flex-direction:column;gap:12px">
                <a href="/backtest" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">⏮️ Lancer un Backtest</a>
                <a href="/journal" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">📝 Ouvrir le Journal</a>
                <a href="/strategie" style="padding:12px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#6366f1;text-decoration:none;font-weight:600;text-align:center">⚙️ Voir la Stratégie</a>
            </div>
        </div>
    </div>
    
    <div class="card"><h2>📊 Derniers Trades</h2>
    <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:2px solid rgba(99,102,241,0.2)">
            <th style="padding:12px;text-align:left;color:#64748b">Symbol</th>
            <th style="padding:12px;text-align:left;color:#64748b">TF</th>
            <th style="padding:12px;text-align:left;color:#64748b">Side</th>
            <th style="padding:12px;text-align:left;color:#64748b">Entry</th>
            <th style="padding:12px;text-align:left;color:#64748b">Status</th>
        </tr></thead><tbody>{table}</tbody>
    </table></div>
    
    <script>
    // Fear & Greed
    fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
    document.getElementById('fg').innerHTML=`<div class="gauge"><div class="gauge-inner">
    <div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
    <div class="gauge-label">/ 100</div></div></div>
    <div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
    <p style="color:#64748b;font-size:12px;text-align:center;margin-top:8px">${{f.recommendation}}</p>`;}}}}).catch(e=>{{document.getElementById('fg').innerHTML='<p style="color:#ef4444">Erreur</p>';}});
    
    // Bull Run Phase
    fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
    document.getElementById('br').innerHTML=`<div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div>
    <div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>
    <p style="color:#64748b;font-size:12px;margin-top:8px">${{b.description}}</p>
    <div style="margin-top:12px;font-size:12px;color:#10b981">BTC: $${{b.btc_price?.toLocaleString() || 'N/A'}} | MC: $${{(b.market_cap/1e12).toFixed(2)}}T</div>
    <span class="badge" style="background:rgba(99,102,241,0.15);color:#6366f1;margin-top:8px">Conf: ${{b.confidence}}%</span>`;
    document.getElementById('phases').style.display='block';
    ['p1','p2','p3'].forEach((id,i)=>{{const el=document.getElementById(id);
    if(i+1===b.phase)el.classList.add('active');else el.classList.remove('active');}});
    const det=b.details;
    document.getElementById('p1s').textContent=`Perf 30d: ${{det.btc.performance_30d}}% | Dom: ${{det.btc.dominance}}%`;
    document.getElementById('p2s').textContent=`ETH: ${{det.eth.performance_30d}}% | LC: ${{det.large_cap.avg_performance_30d}}%`;
    document.getElementById('p3s').textContent=`Alts: ${{det.small_alts.avg_performance_30d}}% | ${{det.small_alts.trades}} coins`;}}}}).catch(e=>{{document.getElementById('br').innerHTML='<p style="color:#ef4444">Erreur</p>';}});
    
    // Heatmap preview
    fetch('/api/heatmap').then(r=>r.json()).then(d=>{{if(d.ok){{
    const hm=d.heatmap;
    const best=Object.entries(hm).sort((a,b)=>b[1].winrate-a[1].winrate).slice(0,3);
    let html='<div style="font-size:14px">';
    best.forEach(([k,v])=>{{
    const [day,hour]=k.split('_');
    html+=`<div style="display:flex;justify-content:space-between;padding:8px;border-bottom:1px solid rgba(99,102,241,0.1)">
    <span>${{day.slice(0,3)}} ${{hour}}</span>
    <span style="font-weight:700;color:#10b981">${{v.winrate}}%</span></div>`;
    }});
    html+='</div>';
    document.getElementById('heatmap-preview').innerHTML=html;}}}}).catch(e=>{{document.getElementById('heatmap-preview').innerHTML='<p style="color:#64748b;font-size:14px">Pas assez de données</p>';}});
    </script>
    </div></body></html>""")
