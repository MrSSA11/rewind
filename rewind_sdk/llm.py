"""One thin LLM call site, so every model call is traced and costed the same way.

Three providers, picked automatically:

  fake    a deterministic offline model. No key, no network, always works.
  gemini  Google AI Studio / Gemini API, native generateContent. Use this for
          Gemma 4 models such as gemma-4-31b-it.
  openai  any OpenAI compatible /chat/completions endpoint (Groq, OpenAI, ...).

Selection order, unless REWIND_LLM_PROVIDER forces one:

  FAKE_LLM=1                  -> fake   (the default, so the demo never needs a key)
  GEMINI_API_KEY is set       -> gemini
  GROQ_API_KEY is set         -> openai
  otherwise                   -> fake

Every provider returns the same dict, so the tracing layer above does not care
which one answered:

  {text, provider, input_tokens, output_tokens, cost_usd}
"""

from __future__ import annotations

import json
import os
import time

from . import fake_llm

# USD per million tokens: (input, output).
#
# Gemma 4 on the AI Studio free tier costs nothing in real money. The figures
# below are the published rates for comparable hosted Gemma serving, kept so
# the cost panels and the cost-spike alert have honest non-zero numbers to work
# with. Override with REWIND_PRICE_IN / REWIND_PRICE_OUT for exact billing.
PRICES = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemma-4-31b-it": (0.08, 0.16),
    "gemma-4-26b-a4b-it": (0.05, 0.10),
    "gemma-3-27b-it": (0.08, 0.16),
}
DEFAULT_PRICE = (0.20, 0.40)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE_URL = "https://api.groq.com/openai/v1"

# Worth retrying: rate limits, timeouts, transient upstream failures.
RETRY_STATUS = (408, 429, 500, 502, 503, 504)


class LLMError(RuntimeError):
    """A real provider call could not be completed."""


def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating stray whitespace and wrapping quotes."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or default


def api_key() -> str:
    """The key for whichever real provider is configured."""
    return (
        _env("GEMINI_API_KEY")
        or _env("GOOGLE_API_KEY")
        or _env("REWIND_LLM_API_KEY")
        or _env("GROQ_API_KEY")
    )


def provider() -> str:
    """Return 'fake', 'gemini', or 'openai'."""
    forced = _env("REWIND_LLM_PROVIDER").lower()
    if forced in ("fake", "gemini", "openai"):
        return forced
    if _env("FAKE_LLM", "1") == "1":
        return "fake"
    if _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"):
        return "gemini"
    if _env("GROQ_API_KEY") or _env("REWIND_LLM_API_KEY"):
        return "openai"
    # FAKE_LLM=0 with no key at all. Degrade to the offline model rather than
    # crashing a demo.
    return "fake"


def use_fake() -> bool:
    """Kept for backwards compatibility with earlier call sites."""
    return provider() == "fake"


def default_model() -> str:
    explicit = _env("REWIND_MODEL")
    if explicit:
        return explicit
    if provider() == "gemini":
        return "gemma-4-31b-it"
    return "llama-3.3-70b-versatile"


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    override_in = _env("REWIND_PRICE_IN")
    override_out = _env("REWIND_PRICE_OUT")
    if override_in and override_out:
        price_in, price_out = float(override_in), float(override_out)
    else:
        price_in, price_out = PRICES.get(model, DEFAULT_PRICE)
    total = (input_tokens * price_in + output_tokens * price_out) / 1_000_000.0
    return round(total, 8)


def _timeout() -> float:
    return float(_env("REWIND_LLM_TIMEOUT", "30"))


def _retries() -> int:
    return max(1, int(_env("REWIND_LLM_RETRIES", "3")))


def _request(url: str, payload: dict, headers: dict) -> dict:
    """POST JSON with retries on rate limits and transient failures."""
    import httpx

    attempts = _retries()
    last = ""
    for attempt in range(attempts):
        try:
            response = httpx.post(
                url, json=payload, headers=headers, timeout=_timeout()
            )
        except Exception as exc:  # network error, DNS, timeout
            last = type(exc).__name__ + ": " + str(exc)[:200]
        else:
            if response.status_code < 400:
                return response.json()
            last = "HTTP " + str(response.status_code) + ": " + response.text[:300]
            if response.status_code not in RETRY_STATUS:
                raise LLMError(last)
        if attempt + 1 < attempts:
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(
        "the model call failed after " + str(attempts) + " attempts. " + last
    )


def _split_messages(messages: list):
    """Separate system text from the conversation turns."""
    system = []
    turns = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        if role == "system":
            system.append(content)
        else:
            turns.append((role, content))
    return "\n".join(system).strip(), turns


def _gemini(messages: list, model: str, params: dict, prompt: str) -> dict:
    """Google AI Studio / Gemini API, native generateContent."""
    key = api_key()
    if not key:
        raise LLMError(
            "no API key found. Set GEMINI_API_KEY in .env (create one at "
            "https://aistudio.google.com/apikey)."
        )

    system, turns = _split_messages(messages)
    contents = [
        {
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": content}],
        }
        for role, content in turns
    ]
    if not contents:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]

    base = _env("REWIND_GEMINI_BASE_URL", GEMINI_BASE_URL).rstrip("/")
    url = base + "/models/" + model + ":generateContent"
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    def build(with_system: bool) -> dict:
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": params.get("temperature", 0),
                "maxOutputTokens": params.get("max_tokens", 512),
            },
        }
        if system and with_system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        elif system:
            # Some open models reject a separate system role. Fold the system
            # text into the first user turn instead.
            folded = list(contents)
            first = dict(folded[0])
            first["parts"] = [{"text": system + "\n\n" + first["parts"][0]["text"]}]
            folded[0] = first
            body["contents"] = folded
        return body

    try:
        data = _request(url, build(True), headers)
    except LLMError as exc:
        if system and ("system" in str(exc).lower() or "400" in str(exc)):
            data = _request(url, build(False), headers)
        else:
            raise

    text = ""
    candidates = data.get("candidates") or []
    if candidates:
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        blocked = (data.get("promptFeedback") or {}).get("blockReason", "")
        detail = " (blocked: " + str(blocked) + ")" if blocked else ""
        raise LLMError(
            "the model returned no text" + detail + ". raw: " + json.dumps(data)[:300]
        )

    usage = data.get("usageMetadata") or {}
    input_tokens = int(usage.get("promptTokenCount") or fake_llm.count_tokens(prompt))
    output_tokens = int(
        usage.get("candidatesTokenCount") or fake_llm.count_tokens(text)
    )
    return {
        "text": text,
        "provider": "gemini",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd(model, input_tokens, output_tokens),
    }


def _openai_compatible(messages: list, model: str, params: dict, prompt: str) -> dict:
    """Any OpenAI compatible /chat/completions endpoint."""
    key = api_key()
    if not key:
        raise LLMError("no API key found. Set GROQ_API_KEY in .env.")

    base = _env("REWIND_LLM_BASE_URL", OPENAI_BASE_URL).rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": params.get("temperature", 0),
        "max_tokens": params.get("max_tokens", 512),
    }
    data = _request(
        base + "/chat/completions",
        payload,
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("the model returned no choices. raw: " + json.dumps(data)[:300])
    text = str((choices[0].get("message") or {}).get("content", "")).strip()
    if not text:
        raise LLMError("the model returned empty content.")

    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or fake_llm.count_tokens(prompt))
    output_tokens = int(usage.get("completion_tokens") or fake_llm.count_tokens(text))
    return {
        "text": text,
        "provider": "groq" if "groq" in base else "openai",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd(model, input_tokens, output_tokens),
    }


def _fake(model: str, prompt: str) -> dict:
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


def complete(messages: list, model: str = None, params: dict = None) -> dict:
    """Return {text, provider, input_tokens, output_tokens, cost_usd}."""
    model = model or default_model()
    params = params or {"temperature": 0, "max_tokens": 512, "seed": 42}
    prompt = "\n".join(str(m.get("content", "")) for m in messages)

    which = provider()
    if which == "fake":
        return _fake(model, prompt)

    try:
        if which == "gemini":
            return _gemini(messages, model, params, prompt)
        return _openai_compatible(messages, model, params, prompt)
    except Exception:
        # Opt in only. A silent downgrade would make a broken key look like a
        # working demo, so this stays off unless you ask for it.
        if _env("REWIND_LLM_FALLBACK_TO_FAKE", "0") == "1":
            result = _fake(model, prompt)
            result["provider"] = "fake (fell back)"
            return result
        raise
