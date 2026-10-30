"""Chat completions across providers and keys: each provider's models are tried with each of its keys, Groq before Gemini."""
import os
import re
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    keys_env: str
    models_env: str
    default_models: str
    extra: dict


PROVIDERS = (
    Provider("groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "AIRA_MODELS",
             "openai/gpt-oss-120b,openai/gpt-oss-20b", {"reasoning_effort": "low"}),
    Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY", "GEMINI_MODELS",
             "gemini-2.5-flash,gemini-2.5-flash-lite", {}),
)
REJECTED_KEY = {401, 403}


def _split(value: str | None) -> list[str]:
    return [v for v in re.split(r"[\s,]+", value or "") if v]


def keys(p: Provider) -> list[str]:
    """`<NAME>_API_KEYS` takes a comma- or newline-separated list; the single `<NAME>_API_KEY` still works."""
    return list(dict.fromkeys(_split(os.environ.get(p.keys_env + "S")) + _split(os.environ.get(p.keys_env))))


def models(p: Provider) -> list[str]:
    return _split(os.environ.get(p.models_env, p.default_models))


def configured() -> bool:
    return any(keys(p) for p in PROVIDERS)


def complete(messages: list[dict], timeout: float = 20) -> tuple[str, str] | None:
    """Returns (text, model) from the first provider, model and key that answer, or None when all fail."""
    for p in PROVIDERS:
        live = keys(p)
        for model in models(p):
            for key in list(live):
                try:
                    resp = httpx.post(p.url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
                                      json={"model": model, "temperature": 0.2, "messages": messages, **p.extra})
                except httpx.HTTPError:
                    continue
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip(), model
                if resp.status_code in REJECTED_KEY:  # expired or revoked: skip this key for the remaining models
                    live.remove(key)
    return None
