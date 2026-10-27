"""The provider interface (spec 03): what keeps the model swappable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict  # JSON Schema, type object


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    id: str = ""
    text: str = ""
    image_b64: str | None = None
    is_error: bool = False


@dataclass
class Step:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # in (uncached), out, cache_read


class Conversation(Protocol):
    label: str

    def add_user(self, text: str) -> None: ...
    def add_note(self, text: str) -> None: ...
    def add_tool_results(self, results: list[ToolResult]) -> None: ...
    async def step(self) -> Step: ...
    def checkpoint(self) -> int: ...
    def rollback(self, mark: int) -> None: ...
    def has_history(self) -> bool: ...
    def dump(self) -> dict: ...
    def forget_image(self, fp: str, note: str) -> int: ...


class Provider(Protocol):
    name: str
    model: str

    def conversation(self, system: str, tools: list[ToolSpec],
                     saved: dict | None = None) -> Conversation: ...
    async def complete(self, system: str, prompt: str, max_tokens: int = 4000,
                       who: str = "") -> str: ...


def get_provider(role: str = "") -> Provider:
    """'' = souls, floor, births, portraits; 'builder' = spec 09."""
    from .meta import MetaProvider

    if role == "builder":
        return MetaProvider(
            os.environ.get("HOUSE_BUILDER_MODEL")
            or os.environ.get("HOUSE_MODEL", "muse-spark-1.3"),
            os.environ.get("HOUSE_BUILDER_EFFORT")
            or os.environ.get("HOUSE_EFFORT", "low"))
    return MetaProvider(os.environ.get("HOUSE_MODEL", "muse-spark-1.3"),
                        os.environ.get("HOUSE_EFFORT", "low"))


class StubProvider:
    """Step 1's placeholder, kept for tests that build a House with no brain."""

    name = "meta"

    def __init__(self) -> None:
        self.model = os.environ.get("HOUSE_MODEL", "muse-spark-1.3")
