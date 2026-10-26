"""Meta Model API (spec 03): OpenAI-compatible Chat Completions.

Standard models only — a -contributor model is refused, because Meta may
train on what it is sent and the house sends photos and conversations.
"""
from __future__ import annotations

import copy
import json
import os

from .. import apilog
from . import Step, ToolCall, ToolResult, ToolSpec

VALID_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
CONTRIBUTOR_ERROR = ("contributor models are cheaper because Meta may train on what "
                     "is sent — the house's photos and conversations; use the standard model")


class MetaConversation:
    def __init__(self, provider: "MetaProvider", label: str, system: str,
                 tools: list[ToolSpec], saved: dict | None = None) -> None:
        self.provider = provider
        self.label = label
        self.tools = tools
        if saved and saved.get("provider") == provider.name \
                and saved.get("model") == provider.model \
                and isinstance(saved.get("messages"), list) and saved["messages"]:
            self.messages: list[dict] = copy.deepcopy(saved["messages"])
        else:  # another provider or model: discard, memories carry over instead
            self.messages = [{"role": "system", "content": system}]
        self._logged = len(self.messages)

    # -- adding ----------------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_note(self, text: str) -> None:
        self.messages.append({"role": "system", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        images: list[str] = []
        for result in results:
            content = result.text or "ok"
            image = result.image_b64 if not result.is_error else None  # errors never carry one
            if image:
                content += " (photo attached below)"
                images.append(image)
            self.messages.append({"role": "tool", "tool_call_id": result.id,
                                  "content": content})
        if images:
            parts: list[dict] = [{"type": "text", "text": "Photo from the tool call:"}]
            for image in images:
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
            self.messages.append({"role": "user", "content": parts})

    # -- the model ---------------------------------------------------------------
    async def step(self) -> Step:
        kwargs: dict = {
            "model": self.provider.model,
            "messages": self.messages,
            "reasoning_effort": self.provider.effort,
            "extra_body": {"prompt_cache_key": f"house-{self.label}"},
        }
        if self.tools:
            kwargs["tools"] = [{"type": "function",
                                "function": {"name": t.name, "description": t.description,
                                             "parameters": t.params}} for t in self.tools]
        try:
            response = await self.provider.client.chat.completions.create(**kwargs)
        except Exception as exc:
            apilog.model_call(self.label, self.provider.model,
                              self.messages[self._logged:], self.messages,
                              error=f"{type(exc).__name__}: {exc}")
            raise
        choice = response.choices[0].message
        text = (choice.content or "")
        calls: list[ToolCall] = []
        raw_calls: list[dict] = []
        for call in choice.tool_calls or []:
            raw = call.function.arguments or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}  # the tool reports the missing argument instead
            calls.append(ToolCall(id=call.id, name=call.function.name, args=args))
            raw_calls.append({"id": call.id, "type": "function",
                              "function": {"name": call.function.name, "arguments": raw}})
        entry: dict = {"role": "assistant", "content": text}
        if raw_calls:
            entry["tool_calls"] = raw_calls
        self.messages.append(entry)
        usage_obj = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        details = getattr(usage_obj, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage = {"in": max(0, prompt_tokens - cached),
                 "out": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                 "cache_read": cached}
        apilog.model_call(self.label, self.provider.model,
                          self.messages[self._logged:], self.messages,
                          reply=text,
                          tools=[{"name": c.name, "args": c.args} for c in calls],
                          usage=usage)
        self._logged = len(self.messages)
        return Step(text=text.strip(), tool_calls=calls, usage=usage)

    # -- bookkeeping --------------------------------------------------------------
    def checkpoint(self) -> int:
        return len(self.messages)

    def rollback(self, mark: int) -> None:
        del self.messages[mark:]
        self._logged = min(self._logged, len(self.messages))

    def has_history(self) -> bool:
        return len(self.messages) > 1

    def dump(self) -> dict:
        return {"provider": self.provider.name, "model": self.provider.model,
                "messages": copy.deepcopy(self.messages)}

    def forget_image(self, fp: str, note: str) -> int:
        return apilog.forget_image(self.messages, fp, note)


class MetaProvider:
    name = "meta"

    def __init__(self, model: str, effort: str, client=None) -> None:
        if model.endswith("-contributor"):
            raise ValueError(CONTRIBUTOR_ERROR)
        if effort not in VALID_EFFORTS:
            raise ValueError(f"unknown effort {effort!r}: valid efforts are "
                             f"{', '.join(VALID_EFFORTS)} (the API does not support 'none')")
        self.model = model
        self.effort = effort
        if client is not None:
            self.client = client
        else:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                base_url=os.environ.get("META_API_BASE", "https://api.meta.ai/v1"),
                api_key=os.environ.get("META_API_KEY") or "not-set-yet")

    def conversation(self, system: str, tools: list[ToolSpec],
                     saved: dict | None = None, label: str = "") -> MetaConversation:
        conv = MetaConversation(self, label, system, tools, saved)
        conv.label = label or conv.label
        return conv

    async def complete(self, system: str, prompt: str, max_tokens: int = 4000,
                       who: str = "") -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt}]
        try:
            response = await self.client.chat.completions.create(
                model=self.model, messages=messages, reasoning_effort=self.effort,
                max_completion_tokens=max_tokens + 4000)  # reasoning counts against the limit
        except Exception as exc:
            apilog.model_call(who or "complete", self.model, messages, messages,
                              error=f"{type(exc).__name__}: {exc}")
            raise
        text = (response.choices[0].message.content or "").strip()
        usage_obj = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        details = getattr(usage_obj, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        apilog.model_call(who or "complete", self.model, messages, messages, reply=text,
                          usage={"in": max(0, prompt_tokens - cached),
                                 "out": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                                 "cache_read": cached})
        return text
