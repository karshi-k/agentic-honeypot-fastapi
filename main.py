import os
import time
import asyncio
from datetime import datetime
from typing import Dict, Optional, List
from typing_extensions import TypedDict

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hf_client import HFChatClient
from agent_graph import build_graph
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("HP_API_KEY", "CHANGE_ME")
GUVI_CALLBACK_URL = "https://hackathon.guvi.in/api/updateHoneyPotFinalResult"
GUVI_TIMEOUT_SECONDS = float(os.getenv("GUVI_TIMEOUT_SECONDS", "5"))

class Message(BaseModel):
    sender: str
    text: str
    timestamp: int  

class Metadata(BaseModel):
    channel: Optional[str] = "SMS"
    language: Optional[str] = "English"
    locale: Optional[str] = "IN"

class IncomingEvent(BaseModel):
    sessionId: str
    message: Message
    conversationHistory: List[Message] = Field(default_factory=list)
    metadata: Optional[Metadata] = None

# In-memory session storage. If you run multiple workers, use Redis for shared state.
SESSIONS: Dict[str, dict] = {}
LOCKS: Dict[str, asyncio.Lock] = {}
GLOBAL_LOCK = asyncio.Lock()

def now_ts() -> float:
    return time.time()

async def get_lock(session_id: str) -> asyncio.Lock:
    async with GLOBAL_LOCK:
        if session_id not in LOCKS:
            LOCKS[session_id] = asyncio.Lock()
        return LOCKS[session_id]

async def get_session(session_id: str) -> dict:
    async with GLOBAL_LOCK:
        if session_id not in SESSIONS:
            SESSIONS[session_id] = {
                "createdAt": now_ts(),
                "updatedAt": now_ts(),
                "totalMessagesExchanged": 0,
                "finalized": False,
                "agentNotes": "",
                # intelligence sets for dedupe
                "bankAccounts": set(),
                "upiIds": set(),
                "phishingLinks": set(),
                "phoneNumbers": set(),
                "suspiciousKeywords": set(),
            }
        return SESSIONS[session_id]

async def send_guvi_callback(session_id: str, sess: dict) -> None:
    payload = {
        "sessionId": session_id,
        "scamDetected": True,
        "totalMessagesExchanged": sess["totalMessagesExchanged"],
        "extractedIntelligence": {
            "bankAccounts": sorted(list(sess["bankAccounts"])),
            "upiIds": sorted(list(sess["upiIds"])),
            "phishingLinks": sorted(list(sess["phishingLinks"])),
            "phoneNumbers": sorted(list(sess["phoneNumbers"])),
            "suspiciousKeywords": sorted(list(sess["suspiciousKeywords"])),
        },
        "agentNotes": sess.get("agentNotes") or "Scammer used urgency + verification tactics; extracted artifacts."
    }

    async with httpx.AsyncClient(timeout=GUVI_TIMEOUT_SECONDS) as client:
        r = await client.post(GUVI_CALLBACK_URL, json=payload)
        if r.status_code >= 400:
            sess["agentNotes"] = (sess.get("agentNotes", "") + " ") + f"GUVI callback failed: {r.status_code}"

app = FastAPI(title="Agentic Honeypot API", version="2.0.0")

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"status": "error", "reply": "Something went wrong. Please try again."})

@app.get("/health")
async def health():
    return {"status": "ok"}

# Init HF + graph once at startup for low latency
hf_client = HFChatClient()
graph = build_graph(hf_client)

@app.post("/message")
async def handle_message(
    event: IncomingEvent,
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key")
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    lock = await get_lock(event.sessionId)
    async with lock:
        sess = await get_session(event.sessionId)
        sess["updatedAt"] = now_ts()
        sess["totalMessagesExchanged"] += 1

        # Small history for context (kept minimal for latency)
        history_norm = []
        for m in event.conversationHistory[-6:]:
            history_norm.append({"sender": m.sender, "text": m.text})

        state_in = {
            "sessionId": event.sessionId,
            "incoming_text": event.message.text,
            "sender": event.message.sender,
            "history": history_norm,
            # seed intel sets
            "bankAccounts": sess["bankAccounts"],
            "upiIds": sess["upiIds"],
            "phishingLinks": sess["phishingLinks"],
            "phoneNumbers": sess["phoneNumbers"],
            "suspiciousKeywords": sess["suspiciousKeywords"],
        }

        state_out = await graph.ainvoke(state_in)

        # Persist intel back
        sess["bankAccounts"] = state_out.get("bankAccounts", sess["bankAccounts"])
        sess["upiIds"] = state_out.get("upiIds", sess["upiIds"])
        sess["phishingLinks"] = state_out.get("phishingLinks", sess["phishingLinks"])
        sess["phoneNumbers"] = state_out.get("phoneNumbers", sess["phoneNumbers"])
        sess["suspiciousKeywords"] = state_out.get("suspiciousKeywords", sess["suspiciousKeywords"])

        scam = bool(state_out.get("scamDetected", False))
        conf = float(state_out.get("confidence", 0.0))
        reply = state_out.get("reply") or "Okay—what should I do next?"
        should_finalize = bool(state_out.get("shouldFinalize", False))

        if scam and (not sess["finalized"]) and should_finalize:
            sess["finalized"] = True
            sess["agentNotes"] = "Detected scam intent; extracted artifacts from conversation."
            try:
                await send_guvi_callback(event.sessionId, sess)
            except Exception:
                sess["agentNotes"] = (sess.get("agentNotes", "") + " ") + "GUVI callback exception."

        # Always include required format: status + reply
        resp = {
            "status": "success",
            "reply": reply,
            "scamDetected": scam,
            "extractedIntelligence": {
                "bankAccounts": sorted(list(sess["bankAccounts"])),
                "upiIds": sorted(list(sess["upiIds"])),
                "phishingLinks": sorted(list(sess["phishingLinks"])),
                "phoneNumbers": sorted(list(sess["phoneNumbers"])),
                "suspiciousKeywords": sorted(list(sess["suspiciousKeywords"])),
            },
            "agentState": {
                "confidence": round(conf, 3),
                "totalMessagesExchanged": sess["totalMessagesExchanged"],
                "finalized": sess["finalized"],
            }
        }
        return JSONResponse(status_code=200, content=resp)
