"""look_up (spec 03 §4b): one question, a live web search, a short answer.

Only the question is sent, to a cheap search model via the Responses API
(Chat Completions does not support search). Search results never enter the
robot's own conversation.
"""
from __future__ import annotations

import os
import time

from . import apilog, pricing

INSTRUCTIONS = (
    "You look things up on the web for a home robot, who will say your answer out loud. "
    "Search when the answer depends on anything current or specific. Answer in one or "
    "two short spoken sentences: the facts asked for, numbers and names as people say "
    "them. No markdown, lists or URLs. If the search didn't settle it, say so."
)


def user_location() -> dict | None:
    raw = os.environ.get("HOUSE_LOCATION", "")
    parts = [p.strip() for p in raw.split(",")] if raw else []
    if len(parts) < 1 or len(parts[0]) != 2:
        return None
    location: dict = {"type": "approximate", "country": parts[0].upper()}
    if len(parts) > 1 and parts[1]:
        location["city"] = parts[1]
    if len(parts) > 2 and parts[2]:
        location["timezone"] = parts[2]
    return location


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def read_response(response) -> tuple[str, list[dict], int, dict]:
    """(answer, sources, searches, usage) out of a Responses API reply."""
    sources: list[dict] = []
    seen_urls: set[str] = set()
    searches = 0
    answer = ""
    for item in _get(response, "output", []) or []:
        itype = _get(item, "type")
        if itype == "web_search_call":
            searches += 1
            for result in _get(item, "results", []) or []:
                url = _get(result, "url") or ""
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append({"title": _get(result, "title") or url, "url": url})
        elif itype == "message":
            if _get(item, "phase") == "commentary":
                continue  # the preamble ("I'll look that up") is not the answer
            texts = [_get(block, "text", "") for block in _get(item, "content", []) or []
                     if _get(block, "type") == "output_text"]
            if texts:
                answer = "".join(texts)
    usage_obj = _get(response, "usage")
    input_tokens = int(_get(usage_obj, "input_tokens", 0) or 0)
    cached = int(_get(_get(usage_obj, "input_tokens_details"), "cached_tokens", 0) or 0)
    usage = {"in": max(0, input_tokens - cached),
             "out": int(_get(usage_obj, "output_tokens", 0) or 0),
             "cache_read": cached}
    return answer.strip(), sources, searches, usage


async def ask(question: str, who: str = "", client=None) -> dict:
    """Run one search. Returns {answer, sources, searches, usd, ms}."""
    started = time.time()
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url="https://api.meta.ai/v1",
                             api_key=os.environ.get("META_API_KEY") or "not-set-yet")
    model = os.environ.get("HOUSE_SEARCH_MODEL", "muse-spark-1.3-contributor")
    tool: dict = {"type": "web_search", "search_context_size": "low"}
    location = user_location()
    if location:
        tool["user_location"] = location
    try:
        response = await client.responses.create(
            model=model, instructions=INSTRUCTIONS, input=question,
            max_output_tokens=3000,
            reasoning={"effort": os.environ.get("HOUSE_SEARCH_EFFORT", "minimal")},
            tools=[tool], include=["web_search_call.results"])
    except Exception as exc:
        apilog.write({"t": time.time(), "kind": "look_up", "who": who or "look_up",
                      "error": f"{type(exc).__name__}: {exc}"})
        raise
    answer, sources, searches, usage = read_response(response)
    usd = (pricing.cost(model, usage) or 0.0) + searches * pricing.SEARCH_USD
    apilog.write({"t": time.time(), "kind": "look_up", "who": who or "look_up",
                  "question": question[:200], "answer": answer[:300],
                  "searches": searches, "usd": usd})
    return {"answer": answer or "I couldn't find an answer to that.",
            "sources": sources[:3], "searches": searches, "usd": usd,
            "ms": int((time.time() - started) * 1000)}


# a second spelling for callers that think in questions
ask_question = ask
