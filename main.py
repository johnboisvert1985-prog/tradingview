from fastapi import FastAPI

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

@app.get("/health")
async def health():
    return {"ok": True, "model": "dummy"}

@app.post("/tv-webhook")
async def tv_webhook(payload: dict):
    # simple echo pour tester que la route existe bien
    return {"ok": True, "received": payload}
