# AI Trade Pro — LLM Bridge

FastAPI webhook that receives TradingView JSON alerts, asks an LLM for a verdict
(`BUY` / `SELL` / `IGNORE`) and returns a compact JSON. Ready for Docker deploy.

## Endpoints
- `GET /health` — quick status
- `POST /tv-webhook` — TradingView should POST here (Content-Type: application/json)

## Env Vars
- `OPENAI_API_KEY` — **required** (do not commit your real key)
- `LLM_MODEL` — default `gpt-4o-mini`
- `WEBHOOK_SECRET` — optional shared secret (recommended). If set, TradingView payload must include `"secret":"<same>"`

## Run locally (Python)
```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="sk-xxxxx"
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Run with Docker
```bash
docker build -t ai-trade-server .
docker run --name ai-trade-server --env-file .env -p 8000:8000 ai-trade-server
```

## Deploy (Render example)
1. Push this folder to GitHub.
2. Create a new **Web Service** in Render, connect the repo (Dockerfile will be detected).
3. Add env vars (`OPENAI_API_KEY`, `LLM_MODEL`, optional `WEBHOOK_SECRET`).
4. Deploy and use the URL `https://.../tv-webhook` as TradingView Webhook URL.

## TradingView
Create an alert on **Any alert() function call**, Once per bar close, Webhook URL = your server URL.
If you set `WEBHOOK_SECRET`, include `"secret":"<same>"` in your Pine payload JSON.

## Test
```bash
curl -X POST http://localhost:8000/tv-webhook \
 -H "Content-Type: application/json" \
 -d '{"tag":"ai-trade-pro","symbol":"BINANCE:ETHUSDT","tf":"15","time":1725360000000,"close":2485.13,"direction":"LONG","features":{"trend":1,"rejcount":2,"volatility_atr":7.42,"sr":{"R1":2510.5,"S1":2468.2},"vectorStreak":{"5":0,"15":2,"60":1,"240":0,"D":0},"mtfSignal":{"5":1,"15":1,"60":-1,"240":0,"D":1}},"levels":{"SL":2448.7,"TP1":2501.2,"TP2":2517.3,"TP3":2533.4}, "secret":"change-me"}'
```

## Notes
- Keep temperature low for stable outputs.
- The model **does not predict**; it filters based on your features.
- Hook your own actions where indicated (exchange, Telegram, DB, etc.).
