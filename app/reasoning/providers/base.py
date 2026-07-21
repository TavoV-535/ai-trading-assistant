"""The interface every reasoning/LLM provider implements — swap Claude for
another provider without touching :class:`~app.reasoning.engine.ReasoningEngine`."""
from __future__ import annotations

from abc import ABC, abstractmethod


class ReasoningProvider(ABC):
    @abstractmethod
    async def generate(self, *, system: str, prompt: str, max_tokens: int, temperature: float) -> str:
        """Return the raw text completion for ``prompt`` given ``system`` instructions."""
