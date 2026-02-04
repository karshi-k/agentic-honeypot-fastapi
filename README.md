# Agentic Honeypot (FastAPI + Hugging Face + LangGraph)

This project implements the **Agentic Honey-Pot** API for scam detection + intelligence extraction.

✅ Accepts incoming scam messages and conversation history  
✅ Detects scam intent  
✅ Uses **LangGraph** for agentic workflow (detect → extract → decide → respond)  
✅ Uses **Hugging Face Inference API** to generate human-like replies (with timeout + fallback)  
✅ Extracts intelligence: UPI IDs, links, phone numbers, bank account numbers, suspicious keywords  
✅ Sends the **mandatory final GUVI callback** once extraction is sufficient

## 1) Setup

### Prerequisites
- Python 3.10+ recommended
- A Hugging Face token with access to your chosen model

### Install
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure environment variables
Copy `.env.example` to `.env` (optional), or export these:

```bash
export HP_API_KEY="YOUR_SECRET_API_KEY"
export HF_TOKEN="hf_xxxxxxxxxxxxxxxxx"
export HF_MODEL="meta-llama/Meta-Llama-3.1-8B-Instruct"
export HF_TIMEOUT_SECONDS="4"
```

> If you use `.env`, load it in your shell (example):
```bash
set -a; source .env; set +a
```

## 2) Run the API

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Health check:
```bash
curl http://localhost:8000/health
```

## 3) Test with cURL

### Test #1: scam detection + reply generation
```bash
curl -X POST "http://localhost:8000/message" \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_SECRET_API_KEY" \
  -d '{
    "sessionId": "s1",
    "message": {
      "sender": "scammer",
      "text": "Your bank account will be blocked today. Verify immediately.",
      "timestamp": "2026-01-21T10:15:30Z"
    },
    "conversationHistory": [],
    "metadata": { "channel": "SMS", "language": "English", "locale": "IN" }
  }'
```

Expected response format (minimum required by the problem):
```json
{
  "status": "success",
  "reply": "..."
}
```

Our API also returns extra debug fields:
- `scamDetected`
- `extractedIntelligence`
- `agentState`

### Test #2: extract UPI + trigger finalize callback
Send a message containing a UPI ID or link:
```bash
curl -X POST "http://localhost:8000/message" \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_SECRET_API_KEY" \
  -d '{
    "sessionId": "s1",
    "message": {
      "sender": "scammer",
      "text": "Pay to abc.scam@upi to avoid suspension. Also use https://bit.ly/fake-kyc",
      "timestamp": "2026-01-21T10:17:10Z"
    },
    "conversationHistory": [
      {
        "sender": "scammer",
        "text": "Your bank account will be blocked today. Verify immediately.",
        "timestamp": "2026-01-21T10:15:30Z"
      }
    ],
    "metadata": { "channel": "SMS", "language": "English", "locale": "IN" }
  }'
```

If `FINALIZE_MIN_ARTIFACTS=1`, this will usually finalize and send the GUVI callback once it sees a strong artifact
(UPI/link/phone/bank-account). The API still returns immediately with JSON.

## 4) Notes on reliability / scaling

- This template uses **in-memory sessions** + per-session **async locks** to handle multiple requests reliably.
- If you deploy with multiple Uvicorn workers (`--workers 2+`), each worker has separate memory.
  For production-grade reliability across workers, replace `SESSIONS` with Redis.

## 5) Files

- `main.py` — FastAPI server + GUVI callback
- `agent_graph.py` — LangGraph workflow + extraction + LLM reply node
- `hf_client.py` — Hugging Face Inference API client
- `requirements.txt` — dependencies
- `.env.example` — environment variable template
