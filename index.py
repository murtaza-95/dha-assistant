import json
import os
import re
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError
from pydantic import BaseModel

from engine import AVAILABLE_TOOLS, ENGINE_VERSION, TOOLS, query_dha_data_engine
from page import HTML
from prompts import MASTER_SYSTEM_PROMPT

MODEL_NAME = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "3500"))  # keep under Groq's per-minute limit
# gpt-oss models accept low / medium / high. Low keeps answers fast and token-cheap.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT") or ("low" if "gpt-oss" in MODEL_NAME else None)
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


def wait_text(seconds):
    """Turn a wait time in seconds into friendly text."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "a minute"
    if seconds < 60:
        return f"{max(1, round(seconds))} seconds"
    if seconds < 3600:
        m = round(seconds / 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    h = round(seconds / 3600, 1)
    return f"about {h:g} hours"


def limit_message(exc):
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    wait = wait_text(headers.get("retry-after")) if headers.get("retry-after") else "a minute"
    return f"The usage limit has been reached. Please try again in {wait}."


def llm_kwargs():
    kw = {"model": MODEL_NAME, "max_tokens": MAX_OUTPUT_TOKENS}
    if REASONING_EFFORT:
        kw["extra_body"] = {"reasoning_effort": REASONING_EFFORT}
    return kw


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "engine_version": ENGINE_VERSION,
            "key_set": bool(os.environ.get("GROQ_API_KEY"))}


@app.get("/api/test")
def test(block: str = None, size: str = None, category: str = None, keyword: str = None):
    """Shows what the listing search returns, with no AI involved. Example: /api/test?size=1%20Kanal"""
    r = query_dha_data_engine(block=block, size=size, category=category,
                              search_keyword=keyword, limit=100)
    return {"engine_version": ENGINE_VERSION,
            "search": {"block": block, "size": size, "category": category, "keyword": keyword},
            "result": r}


@app.post("/api/chat")
def chat(req: ChatRequest):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(500, "GROQ_API_KEY is not set on the server.")
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(400, "Last message must be from the user.")

    client = Groq(api_key=api_key, max_retries=0)
    started = time.monotonic()

    def time_left():
        """Seconds left before Vercel's 60s limit, keeping a 5s safety margin."""
        left = 55 - (time.monotonic() - started)
        if left < 6:
            raise HTTPException(504, "This is taking too long. Please try again in a moment.")
        return left
    history = [{"role": "system", "content": MASTER_SYSTEM_PROMPT}]
    history += [m.model_dump() for m in req.messages[-MAX_HISTORY:]]

    filters_used = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = client.chat.completions.create(
                messages=history, tools=TOOLS, tool_choice="auto",
                timeout=min(30, time_left()), **llm_kwargs()
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                reply = strip_thinking(msg.content)
                if resp.choices[0].finish_reason == "length":
                    reply += "\n\n_The reply was cut off. Type \"continue\" to see the rest._"
                return {"reply": reply, "filters_used": filters_used}

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
        final = client.chat.completions.create(messages=history, timeout=min(30, time_left()), **llm_kwargs())
        return {"reply": strip_thinking(final.choices[0].message.content), "filters_used": filters_used}

    except HTTPException:
        raise
    except (APITimeoutError, APIConnectionError):
        raise HTTPException(504, "The assistant took too long to respond. Please try again in a moment.")
    except RateLimitError as e:
        raise HTTPException(429, limit_message(e))
    except APIStatusError as e:
        if e.status_code == 413:
            raise HTTPException(429, "That request was too big for the current usage limit. Please try again in a minute.")
        raise HTTPException(502, "The assistant could not answer right now. Please try again.")
    except Exception as e:
        raise HTTPException(502, f"Model request failed: {e}")
