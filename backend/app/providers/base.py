"""Общие типы для провайдеров моделей."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: str = ""


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Optional[Dict[str, Any]] = None


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"
    supports_tools = True
    supports_vision = False
    free = True

    async def available(self) -> bool:
        raise NotImplementedError

    async def chat(self, messages: List[Dict[str, Any]], *, tools=None,
                   temperature: float = 0.4, max_tokens: int = 1500,
                   model: str = "") -> LLMResult:
        raise NotImplementedError
