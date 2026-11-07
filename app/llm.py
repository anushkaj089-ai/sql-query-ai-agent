"""Thin wrapper around an OpenAI-compatible chat model (Groq, Gemini, OpenRouter, OpenAI...)."""
import json
import os
import re
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or is not configured."""


@lru_cache(maxsize=1)
def _model() -> ChatOpenAI:
    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise LLMError("LLM_API_KEY is not set. Add your free API key to the environment (see README).")
    default_url, default_model = PROVIDERS.get(provider, PROVIDERS["groq"])
    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL", default_model),
        api_key=key,
        base_url=os.environ.get("LLM_BASE_URL", default_url),
        temperature=0,
        timeout=60,
        max_retries=2,
    )


def chat(system: str, user: str) -> str:
    """Single LLM call. Tests replace this function with a scripted fake."""
    try:
        return _model().invoke([SystemMessage(content=system), HumanMessage(content=user)]).content
    except LLMError:
        raise
    except Exception as e:  # network, auth, rate limit ...
        raise LLMError(f"The language model request failed: {type(e).__name__}: {str(e)[:200]}") from e


def parse_json(text: str) -> dict | None:
    """Tolerant JSON extraction (handles ```json fences and surrounding prose)."""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    for candidate in (t, *re.findall(r"\{.*\}", t, flags=re.S)):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            continue
    return None
