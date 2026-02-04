import os
import httpx
from typing import List, Dict

HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_MODEL = os.getenv("HF_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")
HF_TIMEOUT_SECONDS = float(os.getenv("HF_TIMEOUT_SECONDS", "4"))

API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"

class HFChatClient:
    """Minimal Hugging Face Inference API chat client with safe fallbacks."""

    def __init__(self) -> None:
        if not HF_TOKEN:
            raise RuntimeError("HF_TOKEN env var not set")

    async def chat(self, messages: List[Dict[str, str]], max_tokens: int = 180) -> str:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}

        # Many HF endpoints accept chat-style JSON (TGI-compatible).
        payload = {
            "inputs": {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "top_p": 0.9,
            }
        }

        async with httpx.AsyncClient(timeout=HF_TIMEOUT_SECONDS) as client:
            r = await client.post(API_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()

            # Common response variants across backends:
            # A) {"choices":[{"message":{"content":"..."}}]}
            if isinstance(data, dict) and "choices" in data:
                return (data["choices"][0]["message"].get("content") or "").strip()

            # B) [{"generated_text":"..."}]
            if isinstance(data, list) and data and isinstance(data[0], dict):
                if "generated_text" in data[0]:
                    return (data[0].get("generated_text") or "").strip()

            # C) {"generated_text":"..."}
            if isinstance(data, dict) and "generated_text" in data:
                return (data.get("generated_text") or "").strip()

            # Fallback stringify (kept short)
            return str(data)[:500].strip()
