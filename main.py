from fastapi import FastAPI, Request, HTTPException
import os, json
from pydantic import BaseModel
from typing import Literal

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

app = FastAPI(title="AI Trade Pro — LLM Bridge", version="1.0.0")

def get_client():
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY not configured on server")
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)
