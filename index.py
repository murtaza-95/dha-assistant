import json
import os
import re
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError
from pydantic import BaseModel

from engine import AVAILABLE_TOOLS, ENGINE_VERSION, TOOLS, query_dha_data_engine, render_full_list
from page import HTML
from prompts import MASTER_SYSTEM_PROMPT

MODEL_NAME = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# Used when the main model hits its rate limit. Groq tracks limits per model.
FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "1500"))
# gpt-oss models accept low / medium / high. Low keeps answers fast and token-cheap.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low")
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "8"))  # per visitor IP
MAX_HISTORY = 20      # messages kept per request
MAX_TOOL_ROUNDS = 3   # safety cap on tool-call loops
# Total seconds one chat request may take before giving up
REQUEST_TIME_BUDGET = int(os.environ.get("REQUEST_TIME_BUDGET", "90"))
CUT_OFF_NOTE = "\n\n_The reply was cut off. Type \"continue\" to see the rest._"

app = FastAPI(title="DHA Phase 8 Assistant")
_hits = defaultdict(deque)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks some reasoning models emit."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def check_rate_limit(request: Request):
    """Simple per-IP limit so strangers can't drain the Groq quota.
    Kept in memory; Render runs one long-lived process, so this holds across requests."""
    forwarded = request.headers.get("x-forwarded-for") or ""
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "You're sending messages too quickly. Please wait a minute and try again.")
    q.append(now)
    if len(_hits) > 5000:
        _hits.clear()


def llm_kwargs(model):
    kw = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS}
    if "gpt-oss" in model and REASONING_EFFORT:
        kw["extra_body"] = {"reasoning_effort": REASONING_EFFORT}
    return kw


def complete(client, history, time_left, **extra):
    """Call the main model; on a rate limit, retry once with the fallback model."""
    models = [MODEL_NAME]
    if FALLBACK_MODEL and FALLBACK_MODEL != MODEL_NAME:
        models.append(FALLBACK_MODEL)
    for i, model in enumerate(models):
        try:
            return client.chat.completions.create(
                messages=history, timeout=min(30, time_left()), **llm_kwargs(model), **extra
            )
        except RateLimitError:
            if i == len(models) - 1:
                raise


def reply_text(choice):
    reply = strip_thinking(choice.message.content)
    if not reply:
        reply = "Sorry, I couldn't put an answer together. Please ask again."
    if choice.finish_reason == "length":
        reply += CUT_OFF_NOTE
    return reply


def run_tool(tc, filters_used):
    try:
        args = json.loads(tc.function.arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return {"error": "Tool arguments were not valid JSON. Call the tool again with valid arguments."}

    clean = {k: v for k, v in args.items() if v is not None}
    filters_used.append(clean)
    fn = AVAILABLE_TOOLS.get(tc.function.name)
    if fn is None:
        return {"error": f"Unknown tool {tc.function.name}"}
    try:
        return fn(**clean)
    except TypeError as e:
        return {"error": f"Bad tool arguments: {e}"}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "fallback_model": FALLBACK_MODEL,
            "engine_version": ENGINE_VERSION, "key_set": bool(os.environ.get("GROQ_API_KEY"))}


@app.get("/api/test")
def test(block: str = None, size: str = None, category: str = None, keyword: str = None):
    """Shows what the listing search returns, with no AI involved. Example: /api/test?size=1%20Kanal"""
    r = query_dha_data_engine(block=block, size=size, category=category,
                              search_keyword=keyword, limit=100)
    return {"engine_version": ENGINE_VERSION,
            "search": {"block": block, "size": size, "category": category, "keyword": keyword},
            "result": r}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(500, "GROQ_API_KEY is not set on the server.")
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(400, "Last message must be from the user.")
    check_rate_limit(request)

    client = Groq(api_key=api_key, max_retries=0)
    started = time.monotonic()

    def time_left():
        """Seconds left in this request's time budget."""
        left = REQUEST_TIME_BUDGET - (time.monotonic() - started)
        if left < 6:
            raise HTTPException(504, "This is taking too long. Please try again in a moment.")
        return left

    history = [{"role": "system", "content": MASTER_SYSTEM_PROMPT}]
    history += [m.model_dump() for m in req.messages[-MAX_HISTORY:]]

    filters_used = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = complete(client, history, time_left, tools=TOOLS, tool_choice="auto")
            choice = resp.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                return {"reply": reply_text(choice), "filters_used": filters_used}

            history.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })

            full_list = None
            for tc in msg.tool_calls:
                result = run_tool(tc, filters_used)
                if isinstance(result, dict) and result.get("full_list"):
                    full_list = result
                history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

            # "Show all" requests: build the table here instead of making the model rewrite it
            if full_list is not None:
                return {"reply": render_full_list(full_list), "filters_used": filters_used}

        # Tool rounds exhausted: force a final text answer
        final = complete(client, history, time_left)
        return {"reply": reply_text(final.choices[0]), "filters_used": filters_used}

    except HTTPException:
        raise
    except (APITimeoutError, APIConnectionError):
        raise HTTPException(504, "The assistant took too long to respond. Please try again in a moment.")
    except RateLimitError:
        raise HTTPException(429, "The assistant is very busy right now. Please try again in a minute.")
    except APIStatusError as e:
        if e.status_code == 413:
            raise HTTPException(429, "That request was too big for the current usage limit. Please try again in a minute.")
        raise HTTPException(502, "The assistant could not answer right now. Please try again.")
    except Exception:
        raise HTTPException(502, "The assistant could not answer right now. Please try again.")
