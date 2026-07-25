"""One thin LLM call site, so every model call is traced and costed the same way.

Defaults to the deterministic offline model. Set FAKE_LLM=0 and provide
GROQ_API_KEY to hit a real OpenAI-compatible endpoint.
"""

from __future__ import annotations

import os

from . import fake_llm

# USD per million tokens: (input, output)
PRICES = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "gemini-2.0-flash": (0.10, 0.40),
}
DEFAULT_PRICE = (0.20, 0.40)


def use_fake() -> bool:
    if os.getenv("FAKE_LLM", "1") == "1":
        return True
    return not os.getenv("GROQ_API_KEY")


def default_model() -> str:
    return os.getenv("REWIND_MODEL", "llama-3.3-70b-versatile")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, DEFAULT_PRICE)
    total = (input_tokens * price_in + output_tokens * price_out) / 1_000_000.0
    return round(total, 8)


def complete(messages: list, model: str = None, params: dict = None) -> dict:
    """Return {text, provider, input_tokens, output_tokens, cost_usd}."""
    model = model or default_model()
    params = params or {"temperature": 0, "max_tokens": 512, "seed": 42}
    prompt = "\n".join(str(m.get("content", "")) for m in messages)

    if use_fake():
        text = fake_llm.complete(prompt)
        input_tokens = fake_llm.count_tokens(prompt)
        output_tokens = fake_llm.count_tokens(text)
        return {
            "text": text,
            "provider": "fake",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd(model, input_tokens, output_tokens),
        }

    import httpx

    base = os.getenv("REWIND_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": params.get("temperature", 0),
        "max_tokens": params.get("max_tokens", 512),
    }
    response = httpx.post(
        base.rstrip("/") + "/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer " + os.getenv("GROQ_API_KEY", "")},
        timeout=float(os.getenv("REWIND_LLM_TIMEOUT", "30")),
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage", {})
    text = data["choices"][0]["message"]["content"]
    input_tokens = int(usage.get("prompt_tokens", fake_llm.count_tokens(prompt)))
    output_tokens = int(usage.get("completion_tokens", fake_llm.count_tokens(text)))
    return {
        "text": text,
        "provider": "groq",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd(model, input_tokens, output_tokens),
    }
