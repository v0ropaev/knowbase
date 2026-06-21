"""LLM providers behind a thin adapter — answerer + judge for the nightly A/B (DESIGN.md §1, §9).

Mirrors ``kb.embed.providers``: a ``Protocol`` + lazy heavy imports + ``default_llm_provider()`` via
env. Anthropic is the default; OpenAI is an optional alternative. Nothing here runs on the index or
serve path — only the optional, key-gated, NON-gating ``tier3_llm_judge_test`` uses it. SDK imports
are lazy so this module imports cleanly (and the judge test collects + skips) without the packages.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


@runtime_checkable
class LLMProvider(Protocol):
    model_id: str

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str: ...


class AnthropicProvider:
    """Anthropic Messages API adapter (default). Reads ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL) -> None:
        from anthropic import Anthropic  # lazy: keeps the SDK off the import/serve path

        self._client = Anthropic()
        self._model = model
        self.model_id = f"anthropic:{model}"

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in message.content if block.type == "text")


class OpenAIChatProvider:
    """OpenAI Chat Completions adapter (optional). Reads ``OPENAI_API_KEY``."""

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL) -> None:
        from openai import OpenAI  # lazy

        self._client = OpenAI()
        self._model = model
        self.model_id = f"openai:{model}"

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


def _provider_name() -> str:
    return os.environ.get("KB_LLM_PROVIDER", "anthropic").lower()


def default_llm_provider(model: str | None = None) -> LLMProvider:
    """Select the provider via ``KB_LLM_PROVIDER`` in {"anthropic","openai"}; default "anthropic".

    ``model`` (or ``KB_LLM_MODEL``) overrides the provider's default model.
    """
    chosen = model or os.environ.get("KB_LLM_MODEL")
    if _provider_name() == "openai":
        return OpenAIChatProvider(chosen or DEFAULT_OPENAI_MODEL)
    return AnthropicProvider(chosen or DEFAULT_ANTHROPIC_MODEL)


def has_llm_key() -> bool:
    """Whether the selected provider's API key is present (drives the judge test's ``skipif``)."""
    if _provider_name() == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
