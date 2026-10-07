import json
import os
import re
from typing import Literal

from fastapi import FastAPI, HTTPException
from groq import Groq
from pydantic import BaseModel

from engine import AVAILABLE_TOOLS, TOOLS
from prompts import MASTER_SYSTEM_PROMPT

MODEL_NAME = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
MAX_HISTORY = 20      # messages kept per request
MAX_TOOL_ROUNDS = 3   # safety cap on tool-call loops

app = FastAPI(title="DHA Phase 8 Assistant")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks some reasoning models emit."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "key_set": bool(os.environ.get("GROQ_API_KEY"))}


@app.post("/api/chat")
def chat(req: ChatRequest):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(500, "GROQ_API_KEY is not set on the server.")
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(400, "Last message must be from the user.")

    client = Groq(api_key=api_key)
    history = [{"role": "system", "content": MASTER_SYSTEM_PROMPT}]
    history += [m.model_dump() for m in req.messages[-MAX_HISTORY:]]

    filters_used = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = client.chat.completions.create(
                model=MODEL_NAME, messages=history, tools=TOOLS, tool_choice="auto"
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                return {"reply": strip_thinking(msg.content), "filters_used": filters_used}

            history.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                clean = {k: v for k, v in args.items() if v is not None}
                filters_used.append(clean)
                fn = AVAILABLE_TOOLS.get(tc.function.name)
                result = fn(**clean) if fn else {"error": f"Unknown tool {tc.function.name}"}
                history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

        # Tool rounds exhausted: force a final text answer
        final = client.chat.completions.create(model=MODEL_NAME, messages=history)
        return {"reply": strip_thinking(final.choices[0].message.content), "filters_used": filters_used}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Model request failed: {e}")
