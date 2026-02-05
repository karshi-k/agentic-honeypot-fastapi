import re
import os
from typing import TypedDict, List, Dict, Set

from langgraph.graph import StateGraph, END

# --- regex extractors ---
RE_URL = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
RE_SHORT = re.compile(r"\b(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|cutt\.ly|rb\.gy)/[A-Za-z0-9_-]+\b", re.IGNORECASE)
RE_UPI = re.compile( r"(?<![\w.-])([a-zA-Z0-9._-]{2,})@([a-zA-Z0-9]{2,})(?![\w.-])")
RE_PHONE = re.compile(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b")
RE_BANK_AC = re.compile(r"\b\d{9,18}\b")

SUSPICIOUS_KEYWORDS = [
    "urgent", "verify", "account blocked", "blocked today", "suspended", "freeze",
    "kyc", "otp", "pin", "cvv", "click", "link", "refund", "cashback",
    "upi", "bank account", "share details", "immediately"
]

FINALIZE_MIN_ARTIFACTS = int(os.getenv("FINALIZE_MIN_ARTIFACTS", "3"))

class AgentState(TypedDict, total=False):
    sessionId: str
    incoming_text: str
    sender: str
    history: List[Dict[str, str]]  # {"sender":..., "text":...}

    scamDetected: bool
    confidence: float
    stage: str

    bankAccounts: Set[str]
    upiIds: Set[str]
    phishingLinks: Set[str]
    phoneNumbers: Set[str]
    suspiciousKeywords: Set[str]

    reply: str
    shouldFinalize: bool
    agentNotes: str

def _score_scam(text: str) -> float:
    t = text.lower()
    score = 0.0

    high = [
        "otp", "cvv", "pin", "verify immediately", "blocked today",
        "account will be blocked", "share your upi", "click the link",
        "refund", "cashback", "kyc update", "suspended"
    ]
    for p in high:
        if p in t:
            score += 0.18

    for kw in SUSPICIOUS_KEYWORDS:
        if kw in t:
            score += 0.05

    if RE_URL.search(text) or RE_SHORT.search(text):
        score += 0.25
    if RE_UPI.search(text):
        score += 0.25
    if RE_PHONE.search(text):
        score += 0.10

    return min(score, 1.0)

def node_detect(state: AgentState) -> AgentState:
    conf = _score_scam(state["incoming_text"])
    scam = conf >= 0.35
    state["confidence"] = conf
    state["scamDetected"] = scam
    state["stage"] = "engage" if scam else "observe"
    return state

def node_extract(state: AgentState) -> AgentState:
    text = state["incoming_text"]

    state.setdefault("bankAccounts", set())
    state.setdefault("upiIds", set())
    state.setdefault("phishingLinks", set())
    state.setdefault("phoneNumbers", set())
    state.setdefault("suspiciousKeywords", set())

    for m in RE_URL.findall(text):
        state["phishingLinks"].add(m.strip().rstrip(").,;"))
    for m in RE_SHORT.findall(text):
        state["phishingLinks"].add(m.strip().rstrip(").,;"))

    for m in RE_UPI.findall(text):
        state["upiIds"].add(m)

    for m in RE_PHONE.findall(text):
        state["phoneNumbers"].add(m.strip())

    for m in RE_BANK_AC.findall(text):
        if len(m) >= 9:
            state["bankAccounts"].add(m)

    lower = text.lower()
    for kw in SUSPICIOUS_KEYWORDS:
        if kw in lower:
            state["suspiciousKeywords"].add(kw)

    return state

def _should_finalize(state: AgentState) -> bool:
    strong = 0
    if len(state.get("phishingLinks", set())) > 0:
        strong += 1
    if len(state.get("upiIds", set())) > 0:
        strong += 1
    if len(state.get("phoneNumbers", set())) > 0:
        strong += 1
    if len(state.get("bankAccounts", set())) > 0:
        strong += 1
    return strong >= FINALIZE_MIN_ARTIFACTS

def node_decide(state: AgentState) -> AgentState:
    state["shouldFinalize"] = bool(state.get("scamDetected")) and _should_finalize(state)
    return state

async def node_reply_llm(state: AgentState, hf_chat) -> AgentState:
    if not state.get("scamDetected"):
        state["reply"] = "Sorry—who is this and which bank/service is this about? I didn’t request anything."
        return state

    system = (
        "You are a normal person in India replying over SMS/WhatsApp. "
        "You are anxious and slightly confused, willing to cooperate. "
        "Goal: ask questions that make the other person reveal details (UPI ID, phone number, link, bank account, steps). "
        "Never share OTP, PIN, CVV, passwords or any real personal info. "
        "Keep replies short (1-2 sentences), natural, non-robotic."
    )

    artifacts = []
    if state.get("phishingLinks"):
        artifacts.append("They already sent a link; ask to resend / domain name.")
    if state.get("upiIds") or "upi" in state["incoming_text"].lower():
        artifacts.append("Try to get their UPI ID / receiver name shown on screen.")
    if "otp" in state["incoming_text"].lower():
        artifacts.append("Say OTP not received; ask steps/link instead.")
    hint = " ".join(artifacts) if artifacts else "Ask which bank, exact steps, and link/UPI shown."

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Latest scammer message: {state['incoming_text']}\n\nGuidance: {hint}"}
    ]

    try:
        reply = await hf_chat(messages, max_tokens=1000)
        first_line = reply.strip().split("\n")[0].strip()
        state["reply"] = (first_line[:500]) #if first_line else "Okay—what do I need to do first? Can you resend the link?"
    except Exception:
        state["reply"] = "I’m confused—can you resend the link and tell me the exact steps? My app isn’t opening properly."

    return state

def build_graph(hf_client):
    g = StateGraph(AgentState)
    g.add_node("detect", node_detect)
    g.add_node("extract", node_extract)
    g.add_node("decide", node_decide)

    async def reply_node(state: AgentState) -> AgentState:
        return await node_reply_llm(state, hf_client.chat)

    g.add_node("reply", reply_node)

    g.set_entry_point("detect")
    g.add_edge("detect", "extract")
    g.add_edge("extract", "decide")
    g.add_edge("decide", "reply")
    g.add_edge("reply", END)

    return g.compile()
