# ============================================================================
# ROUTES HTML
# ============================================================================

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🚀 Trading Dashboard</h1><p>Système complet <span class="live-badge">LIVE</span></p></div>{NAV}
<div class="card" style="text-align:center;">
  <h2>Dashboard Professionnel</h2>
  <p style="color:#94a3b8;margin:20px 0;">✅ Données réelles • ✅ Telegram • ✅ Analytics • 🗞️ Annonces Live</p>
  <a href="/trades" style="display:inline-block;padding:12px 24px;background:#6366f1;color:white;text-decoration:none;border-radius:8px;">Dashboard →</a>
</div>
</div></body></html>""")


@app.get("/trades", response_class=HTMLResponse)
async def trades():
    rows = build_trade_rows(50)
    stats = trading_state.get_stats()
    patterns = detect_patterns(rows)
    metrics = calc_metrics(rows)

    table = ""
    for r in rows[:20]:
        badge = (
            '<span class="badge badge-green">TP</span>'
            if r.get("row_state") == "tp"
            else ('<span class="badge badge-red">SL</span>' if r.get("row_state") == "sl" else '<span class="badge badge-yellow">En cours</span>')
        )
        pnl = ""
        if r.get('pnl_percent') is not None:
            color = '#10b981' if r['pnl_percent'] > 0 else '#ef4444'
            pnl = f'<span style="color:{color};font-weight:700">{r["pnl_percent"]:+.2f}%</span>'
        table += f"<tr><td>{r.get('symbol','N/A')}</td><td>{r.get('tf_label','N/A')}</td><td>{r.get('side','N/A')}</td><td>{r.get('entry') or 'N/A'}</td><td>{badge} {pnl}</td></tr>"

    patterns_html = "".join(f'<li style="padding:8px">{p}</li>' for p in patterns)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Dashboard</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📊 Dashboard</h1><p>Live <span class="live-badge">LIVE</span></p></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
  <div class="card"><h2>😱 Fear & Greed <span class="live-badge">LIVE</span></h2><div id="fg" style="text-align:center;padding:40px">⏳</div></div>

  <div class="card"><h2>🚀 Bull Run <span class="live-badge">LIVE</span></h2>
    <div id="br" style="text-align:center;padding:40px">⏳</div>
    <div style="margin-top:10px;color:#94a3b8;font-size:12px;line-height:1.5">
      <b>Phases du cycle (règles simples)</b><br>
      1) <b>Bitcoin Season</b> (dominance BTC &gt; 48%) — BTC mène le marché<br>
      2) <b>ETH & Large-Cap</b> (45% &lt; dominance BTC ≤ 48%) — rotation vers ETH / grandes caps<br>
      3) <b>Altcoin Season</b> (dominance BTC ≤ 45%) — altcoins surperforment<br>
      4) <b>Distribution / Fin de cycle</b> (FG ≥ 80 et volatilité élevée) — prudence sur corrections
    </div>
  </div>

  <div class="card"><h2>🤖 Patterns</h2><ul class="list">{patterns_html}</ul></div>

  <div class="card"><h2>🗞️ Annonces (Live)</h2>
    <div id="news" style="max-height:320px;overflow:auto">⏳</div>
    <div class="small" style="margin-top:8px;color:#94a3b8">
      Importance ≥ 3 • Auto-refresh 60s — <a href="/annonces" style="color:#6366f1">voir tout</a>
    </div>
  </div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
  <div class="metric"><div class="metric-label">Total</div><div class="metric-value">{stats['total_trades']}</div></div>
  <div class="metric"><div class="metric-label">Actifs</div><div class="metric-value">{stats['active_trades']}</div></div>
  <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{int(stats['win_rate'])}%</div></div>
  <div class="metric"><div class="metric-label">Capital</div><div class="metric-value" style="font-size:24px">${stats['current_equity']:.0f}</div></div>
  <div class="metric"><div class="metric-label">Return</div><div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'}">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📋 Trades</h2>
<table><thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>Status</th></tr></thead><tbody>{table}</tbody></table></div>

<script>
// F-string safe helpers (double accolades)
function escapeHtml(s){{return (s||'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}}[c]));}}
function colorByImp(n){{return n>=5?'#ef4444':(n>=4?'#f59e0b':(n>=3?'#10b981':'#6366f1'));}}

// Widgets live
fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{if(d.ok){{const f=d.fear_greed;
  document.getElementById('fg').innerHTML=`
    <div class="gauge"><div class="gauge-inner">
      <div class="gauge-value" style="color:${{f.color}}">${{f.value}}</div>
      <div class="gauge-label">/ 100</div>
    </div></div>
    <div style="text-align:center;margin-top:24px;font-size:20px;font-weight:900;color:${{f.color}}">${{f.emoji}} ${{f.sentiment}}</div>
    <p style="color:#64748b;font-size:12px;text-align:center;margin-top:8px">${{f.recommendation}}</p>`;
}}}});

fetch('/api/bullrun-phase').then(r=>r.json()).then(d=>{{if(d.ok){{const b=d.bullrun_phase;
  document.getElementById('br').innerHTML=`
    <div style="font-size:56px;margin-bottom:8px">${{b.emoji}}</div>
    <div style="font-size:20px;font-weight:900;color:${{b.color}}">${{b.phase_name}}</div>
    <p style="color:#64748b;font-size:12px;margin-top:8px">${{b.description}}</p>
    <div style="margin-top:12px;font-size:12px;color:#10b981">
      BTC: $${{(b.btc_price||0).toLocaleString()}} | MC: $${{(b.market_cap/1e12).toFixed(2)}}T
    </div>`;
}}}});

// Annonces live (mini)
async function loadNewsWidget(){{
  try{{
    const r = await fetch('/api/news?min_importance=3&limit=12');
    const d = await r.json();
    const el = document.getElementById('news');
    if(!d.ok || !d.items || !d.items.length){{ el.innerHTML='<p style="color:#64748b">Aucune annonce importante.</p>'; return; }}
    let html='';
    d.items.forEach(it=>{{
      const color = colorByImp(it.importance||1);
      html += `
        <div class="phase-indicator" style="border-left-color:${{color}};margin-bottom:8px">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <a href="${{it.link}}" target="_blank" rel="noopener" style="color:#e2e8f0;font-weight:700;text-decoration:none">${{escapeHtml(it.title)}}</a>
              <span class="badge" style="border:1px solid ${{color}};color:${{color}}">Imp:${{it.importance}}</span>
            </div>
            <div style="color:#94a3b8;font-size:12px">${{escapeHtml(it.source||'')}}${{it.published?(' • '+escapeHtml(it.published)) : ''}}</div>
          </div>
        </div>`;
    }});
    el.innerHTML = html;
  }}catch(e){{ document.getElementById('news').innerHTML='<p style="color:#ef4444">Erreur news</p>'; console.error(e); }}
}}
loadNewsWidget();
setInterval(loadNewsWidget, 60000);
</script>
</div></body></html>""")


@app.get("/equity-curve", response_class=HTMLResponse)
async def equity_curve():
    stats = trading_state.get_stats()
    curve = trading_state.equity_curve
    labels = [c['timestamp'].strftime('%H:%M') for c in curve]
    values = [c['equity'] for c in curve]

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Equity</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📈 Equity Curve</h1></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
  <div class="metric"><div class="metric-label">Initial</div><div class="metric-value">${settings.INITIAL_CAPITAL}</div></div>
  <div class="metric"><div class="metric-label">Actuel</div><div class="metric-value">${stats['current_equity']:.0f}</div></div>
  <div class="metric"><div class="metric-label">Return</div>
    <div class="metric-value" style="color:{'#10b981' if stats['total_return']>=0 else '#ef4444'}">{stats['total_return']:+.1f}%</div></div>
</div>

<div class="card"><h2>📊 Graphique</h2><canvas id="chart" width="800" height="400"></canvas></div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: {labels},
    datasets: [{{label: 'Equity', data: {values}, borderColor: '#6366f1',
      backgroundColor: 'rgba(99, 102, 241, 0.1)', borderWidth: 3, fill: true, tension: 0.4}}]
  }},
  options: {{responsive: true, scales: {{y: {{beginAtZero: false, ticks: {{color: '#64748b'}},
    grid: {{color: 'rgba(99, 102, 241, 0.1)'}}}}, x: {{ticks: {{color: '#64748b'}}, grid: {{color: 'rgba(99, 102, 241, 0.1)'}}}}}}}}
}});
</script>
</div></body></html>""")


@app.get("/journal", response_class=HTMLResponse)
async def journal():
    entries = trading_state.journal_entries
    entries_html = ""
    for entry in reversed(entries[-20:]):
        entries_html += f"""<div class="journal-entry">
<div class="journal-timestamp">{entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}{f" | Trade #{entry['trade_id']}" if entry.get('trade_id') else ""}</div>
<div>{entry['entry']}</div></div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Journal</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📝 Journal</h1></div>{NAV}

<div class="card"><h2>✍️ Nouvelle Entrée</h2>
<form id="form">
<textarea id="text" placeholder="Votre analyse..."></textarea>
<button type="submit" style="margin-top:12px">Ajouter</button>
</form></div>

<div class="card"><h2>📚 Entrées</h2>
{entries_html if entries_html else '<p style="color:#64748b">Aucune entrée</p>'}
</div>

<script>
document.getElementById('form').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const text = document.getElementById('text').value;
  if (!text) return;
  await fetch('/api/journal', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{entry: text}})}});
  location.reload();
}});
</script>
</div></body></html>""")


@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Heatmap</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🔥 Heatmap</h1></div>{NAV}

<div class="card"><h2>📊 Heatmap</h2><div id="hm">⏳</div></div>

<script>
fetch('/api/heatmap').then(r=>r.json()).then(d=>{{
  if(d.ok){{
    const hm = d.heatmap;
    let html = '<table style="width:100%"><thead><tr><th>Jour</th>';
    for(let h=8; h<20; h++) html += `<th>${{h}}:00</th>`;
    html += '</tr></thead><tbody>';
    ['Monday','Tuesday','Wednesday','Thursday','Friday'].forEach(day=>{{
      html += `<tr><td style="font-weight:700">${{day.slice(0,3)}}</td>`;
      for(let h=8; h<20; h++){{
        const key = `${{day}}_${{h.toString().padStart(2,'0')}}:00`;
        const cell = hm[key] || {{winrate:0,trades:0}};
        const wr = cell.winrate;
        const cls = wr>=70?'high':wr>=55?'medium':'low';
        html += `<td class="heatmap-cell ${{cls}}" style="text-align:center">
                   <div style="font-weight:700">${{wr}}%</div><div style="font-size:10px">${{cell.trades}}</div></td>`;
      }}
      html += '</tr>';
    }});
    html += '</tbody></table>';
    document.getElementById('hm').innerHTML = html;
  }}
}});
</script>
</div></body></html>""")


@app.get("/strategie", response_class=HTMLResponse)
async def strategie():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Stratégie</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>⚙️ Stratégie</h1></div>{NAV}

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(350px,1fr))">
  <div class="card"><h2>🎯 Paramètres</h2>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>Capital</span><span style="font-weight:700">${settings.INITIAL_CAPITAL}</span></div>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>Risk/Trade</span><span style="font-weight:700">2%</span></div>
  </div>
  <div class="card"><h2>📊 TP/SL</h2>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>TP</span><span style="font-weight:700;color:#10b981">+3%</span></div>
    <div style="padding:12px;border-bottom:1px solid rgba(99,102,241,0.1);display:flex;justify-content:space-between"><span>SL</span><span style="font-weight:700;color:#ef4444">-2%</span></div>
  </div>
</div>

<div class="card"><h2>🔔 Telegram</h2>
<p style="color:{'#10b981' if (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) else '#ef4444'}">
  {'✅ Configuré' if (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) else '⚠️ Non configuré'}
</p></div>
</div></body></html>""")


@app.get("/backtest", response_class=HTMLResponse)
async def backtest():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Backtest</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>⏮️ Backtest Engine</h1><p>Testez votre stratégie</p></div>{NAV}

<div class="card"><h2>🎯 Paramètres Backtest</h2>
  <div style="display:grid;gap:16px">
    <div>
      <label style="display:block;margin-bottom:8px;color:#64748b">Symbole</label>
      <select id="symbol" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
        <option value="BTCUSDT">BTCUSDT</option>
        <option value="ETHUSDT">ETHUSDT</option>
        <option value="BNBUSDT">BNBUSDT</option>
      </select>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <label style="display:block;margin-bottom:8px;color:#64748b">TP (%)</label>
        <input type="number" id="tp" value="3" step="0.1" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      </div>
      <div>
        <label style="display:block;margin-bottom:8px;color:#64748b">SL (%)</label>
        <input type="number" id="sl" value="2" step="0.1" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      </div>
    </div>
    <div>
      <label style="display:block;margin-bottom:8px;color:#64748b">Nombre de bougies (limit)</label>
      <input type="number" id="limit" value="500" min="50" max="1000" step="50" style="width:100%;padding:12px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
    </div>
    <button onclick="runBacktest()" id="runBtn">🚀 Lancer Backtest</button>
  </div>
</div>

<div id="results" style="display:none">
  <div class="card"><h2>📊 Résultats</h2>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
      <div class="metric"><div class="metric-label">Total Trades</div><div class="metric-value" id="totalTrades">-</div></div>
      <div class="metric"><div class="metric-label">Wins / Losses</div><div class="metric-value" style="font-size:24px"><span id="wins" style="color:#10b981">-</span> / <span id="losses" style="color:#ef4444">-</span></div></div>
      <div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value" id="winRate">-</div></div>
      <div class="metric"><div class="metric-label">Return Total</div><div class="metric-value" id="totalReturn">-</div></div>
      <div class="metric"><div class="metric-label">Avg Win / Loss</div><div class="metric-value" style="font-size:24px"><span id="avgWin" style="color:#10b981">-</span> / <span id="avgLoss" style="color:#ef4444">-</span></div></div>
      <div class="metric"><div class="metric-label">Max Drawdown</div><div class="metric-value" id="maxDD" style="color:#ef4444">-</div></div>
      <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value" id="sharpe">-</div></div>
      <div class="metric"><div class="metric-label">Final Equity</div><div class="metric-value" id="finalEquity" style="font-size:24px">-</div></div>
    </div>
  </div>

  <div class="card"><h2>📈 Equity Curve</h2>
    <canvas id="equityChart" width="800" height="400"></canvas>
  </div>

  <div class="card"><h2>📋 Derniers Trades</h2>
    <div style="max-height:400px;overflow-y:auto">
      <table id="tradesTable">
        <thead><tr><th>Entry Time</th><th>Entry</th><th>Exit</th><th>Result</th><th>P&L %</th><th>Equity</th></tr></thead>
        <tbody id="tradesBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
let chart = null;

async function runBacktest(){{
  const btn = document.getElementById('runBtn');
  btn.disabled = true; btn.textContent='⏳ Calcul en cours...';
  const symbol = document.getElementById('symbol').value;
  const tp = document.getElementById('tp').value;
  const sl = document.getElementById('sl').value;
  const limit = document.getElementById('limit').value;

  try {{
    const url = `/api/backtest?symbol=${{encodeURIComponent(symbol)}}&interval=1h&limit=${{limit}}&tp_percent=${{tp}}&sl_percent=${{sl}}`;
    const r = await fetch(url);
    const d = await r.json();
    if(!d.ok) throw new Error(d.error||'API error');
    displayResults(d.backtest);
    document.getElementById('results').style.display='block';
  }} catch(e) {{
    alert('Erreur lors du backtest'); console.error(e);
  }} finally {{
    btn.disabled = false; btn.textContent='🚀 Lancer Backtest';
  }}
}}

function displayResults(res){{
  const s = res.stats;
  // Stats
  document.getElementById('totalTrades').textContent = s.total_trades;
  document.getElementById('wins').textContent = s.wins;
  document.getElementById('losses').textContent = s.losses;
  document.getElementById('winRate').textContent = s.win_rate + '%';
  document.getElementById('totalReturn').textContent = (s.total_return>=0?'+':'') + s.total_return + '%';
  document.getElementById('totalReturn').style.color = s.total_return>=0 ? '#10b981' : '#ef4444';
  document.getElementById('avgWin').textContent = '+' + s.avg_win + '%';
  document.getElementById('avgLoss').textContent = s.avg_loss + '%';
  document.getElementById('maxDD').textContent = s.max_drawdown + '%';
  document.getElementById('sharpe').textContent = s.sharpe_ratio;
  document.getElementById('finalEquity').textContent = s.final_equity.toLocaleString();
  document.getElementById('finalEquity').style.color = s.total_return>=0 ? '#10b981' : '#ef4444';

  // Graph
  const ctx = document.getElementById('equityChart').getContext('2d');
  if(chart) chart.destroy();
  chart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: s.equity_curve.map((_,i)=>i), datasets: [{{ label:'Equity', data:s.equity_curve,
      borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,0.1)', borderWidth:3, fill:true, tension:0.4 }}] }},
    options: {{ responsive:true, plugins: {{ legend: {{display:false}} }}, scales: {{ y: {{ beginAtZero:false }} }} }}
  }});

  // Table trades
  const tbody = document.getElementById('tradesBody');
  tbody.innerHTML='';
  (s.trades||[]).slice(-50).reverse().forEach(t => {{
    const resultColor = t.result === 'TP' ? '#10b981' : '#ef4444';
    const pnlColor = t.pnl_percent >= 0 ? '#10b981' : '#ef4444';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-size:12px">${{t.entry_time}}</td>
      <td>${{t.entry_price}}</td>
      <td>${{t.exit_price}}</td>
      <td><span style="color:${{resultColor}};font-weight:700">${{t.result}}</span></td>
      <td style="color:${{pnlColor}};font-weight:700">${{t.pnl_percent>=0?'+':''}}${{t.pnl_percent}}%</td>
      <td>${{Number(t.equity||0).toLocaleString()}}</td>`;
    tbody.appendChild(tr);
  }});
}}

// Auto-run une fois au chargement
window.addEventListener('load', () => {{ setTimeout(runBacktest, 500); }});
</script>
</div></body></html>""")


@app.get("/patterns", response_class=HTMLResponse)
async def patterns():
    patterns_list = detect_patterns(build_trade_rows(50))
    patterns_html = "".join(f"<li style='padding:12px;border-bottom:1px solid rgba(99,102,241,0.1)'>{p}</li>" for p in patterns_list)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Patterns</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🤖 Patterns</h1></div>{NAV}
<div class="card"><h2>Patterns</h2><ul class="list">{patterns_html}</ul></div>
</div></body></html>""")


@app.get("/advanced-metrics", response_class=HTMLResponse)
async def advanced_metrics():
    metrics = calc_metrics(build_trade_rows(50))
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Metrics</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>📊 Metrics</h1></div>{NAV}
<div class="card"><h2>Métriques</h2>
  <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px'>
    <div class='metric'><div class='metric-label'>Sharpe</div><div class='metric-value'>{metrics['sharpe_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Sortino</div><div class='metric-value'>{metrics['sortino_ratio']}</div></div>
    <div class='metric'><div class='metric-label'>Expectancy</div><div class='metric-value'>{metrics['expectancy']:.2f}%</div></div>
    <div class='metric'><div class='metric-label'>Max DD</div><div class='metric-value' style='color:#ef4444'>-{metrics['max_drawdown']:.1f}%</div></div>
  </div>
</div>
</div></body></html>""")


@app.get("/annonces", response_class=HTMLResponse)
async def annonces():
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Annonces</title>{CSS}</head>
<body><div class="container">
<div class="header"><h1>🗞️ Annonces</h1><p>Flux agrégé (CoinDesk, CoinTelegraph, Binance)</p></div>{NAV}

<div class="card">
  <h2>📡 Fil d'actualités</h2>
  <div style="display:flex;gap:12px;margin:8px 0;flex-wrap:wrap">
    <input id="q" placeholder="Filtrer (ex: etf, hack, listing…)" 
      style="flex:1;min-width:250px;padding:10px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
    <select id="imp" style="padding:10px;background:rgba(99,102,241,0.05);border:1px solid rgba(99,102,241,0.3);border-radius:8px;color:#e2e8f0">
      <option value="1">Importance ≥ 1</option>
      <option value="2">Importance ≥ 2</option>
      <option value="3" selected>Importance ≥ 3</option>
      <option value="4">Importance ≥ 4</option>
      <option value="5">Importance ≥ 5</option>
    </select>
    <button onclick="loadNews()">🔎 Rechercher</button>
  </div>
  <div id="list" style="margin-top:12px;max-height:70vh;overflow:auto">⏳</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
    <button onclick="prevPage()">⬅️</button>
    <button onclick="nextPage()">➡️</button>
  </div>
  <div id="meta" style="margin-top:8px;color:#94a3b8;font-size:12px"></div>
</div>

<script>
let offset=0, limit=25;
function escapeHtml(s){{return (s||'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}}[c]));}}
function colorByImp(n){{return n>=5?'#ef4444':(n>=4?'#f59e0b':(n>=3?'#10b981':'#6366f1'));}}

async function loadNews(){{
  const q = document.getElementById('q').value.trim();
  const imp = document.getElementById('imp').value;
  const el = document.getElementById('list');
  el.innerHTML='⏳';
  try{{
    const url = `/api/news?min_importance=${{imp}}&limit=${{limit}}&offset=${{offset}}` + (q?`&q=${{encodeURIComponent(q)}}`:'');
    const r = await fetch(url);
    const d = await r.json();
    if(!d.ok) throw new Error('API');
    if(!d.items.length) {{
      el.innerHTML = '<p style="color:#64748b">Aucune annonce.</p>';
    }} else {{
      let html='';
      d.items.forEach(it=>{{
        const color = colorByImp(it.importance||1);
        html += `
          <div class="phase-indicator" style="border-left-color:${{color}};margin-bottom:10px">
            <div style="flex:1">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                <a href="${{it.link}}" target="_blank" rel="noopener" style="color:#e2e8f0;font-weight:700;text-decoration:none">${{escapeHtml(it.title)}}</a>
                <span class="badge" style="border:1px solid ${{color}};color:${{color}}">Imp:${{it.importance}}</span>
              </div>
              <div style="color:#94a3b8;font-size:12px;margin-bottom:4px">${{escapeHtml(it.summary)}}</div>
              <div style="color:#64748b;font-size:11px">Source: ${{escapeHtml(it.source||'')}}${{it.published?(' • '+escapeHtml(it.published)) : ''}}</div>
            </div>
          </div>`;
      }});
      el.innerHTML = html;
    }}
    document.getElementById('meta').textContent = `Affichés ${{Math.min(d.count, limit)}} sur ${{d.total}} (offset ${{offset}})`;
  }}catch(e){{
    console.error(e);
    el.innerHTML = '<p style="color:#ef4444">Erreur chargement news</p>';
  }}
}}
function nextPage(){{offset+=limit; loadNews();}}
function prevPage(){{offset=Math.max(0, offset-limit); loadNews();}}

loadNews();
setInterval(loadNews, 60000);
</script>
</div></body></html>""")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*70)
    print("🚀 TRADING DASHBOARD - VERSION FINALE")
    print("="*70)
    print("📍 http://localhost:8000")
    print("📊 Dashboard: http://localhost:8000/trades")
    print("🗞️ Annonces:  http://localhost:8000/annonces")
    print("\n✅ PAGES COMPLÈTES:")
    print("  • Dashboard avec données LIVE (+ Annonces)")
    print("  • Equity Curve avec graphique")
    print("  • Journal de trading")
    print("  • Heatmap visuelle")
    print("  • Configuration stratégie")
    print("  • Backtest (interface)")
    print("  • Patterns")
    print("  • Metrics")
    print("\n📥 WEBHOOK:")
    print("  URL: http://localhost:8000/tv-webhook")
    print("\n🔔 TELEGRAM:")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        print("  ✅ CONFIGURÉ ET ACTIF")
    else:
        print("  ⚠️  NON CONFIGURÉ")
        print("  export TELEGRAM_BOT_TOKEN='...'")
        print("  export TELEGRAM_CHAT_ID='...'")
    print("="*70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
