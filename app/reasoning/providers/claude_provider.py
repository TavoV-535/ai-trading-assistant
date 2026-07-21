"""Claude (Anthropic) reasoning provider."""
from __future__ import annotations

from anthropic import AsyncAnthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging import get_logger
from app.reasoning.providers.base import ReasoningProvider

log = get_logger(__name__)

try:  # pragma: no cover - depends on installed SDK version
    from anthropic import APIConnectionError, APIStatusError, RateLimitError

    _RETRYABLE = (APIConnectionError, RateLimitError, APIStatusError)
except ImportError:  # pragma: no cover
    _RETRYABLE = (Exception,)


class ClaudeProvider(ReasoningProvider):
    """Wraps the Anthropic SDK. Retries transient failures with exponential backoff."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    async def generate(self, *, system: str, prompt: str, max_tokens: int, temperature: float) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        text = "".join(parts)
        log.debug("claude_provider_response", model=self._model, chars=len(text))
        return text
