# main.py (merged)
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
"FORCE_LLM": bool(FORCE_LLM),
"CONFIDENCE_MIN": CONFIDENCE_MIN,
"PORT": PORT,
"RISK_ACCOUNT_BAL": RISK_ACCOUNT_BAL,
"RISK_PCT": RISK_PCT,
}




@app.get("/tg-health")
async def tg_health(secret: Optional[str] = Query(None)):
if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
raise HTTPException(status_code=401, detail="Invalid secret")
await send_telegram("✅ Test Telegram: ça fonctionne.")
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
r
