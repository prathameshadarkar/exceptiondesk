"""Model provider adapter.

The agents (prompts, context assembly, the deterministic boundary) know nothing
about who serves them. This module is the only place that knows about an API
shape, so switching provider or changing a model is a config change rather than
a rewrite — and "the model layer is swappable" is a better answer than "it is
hardcoded to one vendor" when someone asks about the architecture.

Two tiers, not one model:

    fast    — formatting and translation work (broker note, PM brief, triage).
              Latency is more noticeable here than quality; a cheap model is the
              right call, not a compromise.
    reason  — the handover judge, which reads free text and compares it against
              a checklist. That is the one place where model quality shows, so
              it gets the capable model.

Mirrored in web/api/agent.js and dev_server.py, which do the same job at
request time. If you change a model id, change it in all three — or better,
read them from env, which is what DEFAULTS below allows.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Per-provider defaults. Override any of these with env vars of the same name
#: so a model swap needs no code change at all.
DEFAULTS = {
    "openai": {
        "fast": "gpt-6-luna",
        "reason": "gpt-6-sol",
        "url": "https://api.openai.com/v1/responses",
        "key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "fast": "claude-haiku-4-5-20251001",
        "reason": "claude-sonnet-5",
        "url": "https://api.anthropic.com/v1/messages",
        "key_env": "ANTHROPIC_API_KEY",
    },
}

MAX_OUTPUT = 1400


def active_provider() -> str:
    """Which provider to use.

    Explicit env wins; otherwise whichever key is present. Checking for the key
    rather than demanding configuration means `OPENAI_API_KEY=... make serve`
    just works.
    """
    explicit = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    if explicit in DEFAULTS:
        return explicit
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def model_for(tier: str, provider: str | None = None) -> str:
    """Resolve a tier to a model id, honouring env overrides."""
    provider = provider or active_provider()
    conf = DEFAULTS[provider]
    env_name = f"MODEL_{tier.upper()}"
    return os.environ.get(env_name) or conf[tier]


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------


def build_http_request(system: str, user: str, tier: str = "fast",
                       provider: str | None = None) -> tuple[str, dict, dict]:
    """Return ``(url, headers, body)`` for one completion.

    Pure: no network, no key lookup beyond the header. Unit-testable, and the
    serverless function and dev server build the same shapes.
    """
    provider = provider or active_provider()
    conf = DEFAULTS[provider]
    model = model_for(tier, provider)
    key = os.environ.get(conf["key_env"], "")

    if provider == "openai":
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {key}"}
        body = {
            "model": model,
            # OpenAI carries the system prompt as a message in the input array
            # rather than as a top-level field, which is the main shape
            # difference from Anthropic.
            "input": [
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ],
            "max_output_tokens": MAX_OUTPUT,
        }
    else:
        headers = {"content-type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
        body = {
            "model": model,
            "max_tokens": MAX_OUTPUT,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    return conf["url"], headers, body


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def extract_text(data: dict) -> str:
    """Pull the generated text out of a response, tolerantly.

    Walks several known shapes rather than indexing one path: OpenAI's
    Responses output list, its older chat-completions choices, and Anthropic's
    content blocks. A response-shape change should degrade to "no text" and a
    visible error, never to a stack trace in front of an interviewer.
    """
    if not isinstance(data, dict):
        return ""

    # OpenAI Responses: convenience field when present
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]

    parts: list[str] = []

    # OpenAI Responses: output -> [{content: [{type: output_text, text}]}]
    for item in data.get("output") or []:
        for block in (item or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                if block.get("text"):
                    parts.append(block["text"])
    if parts:
        return "\n".join(parts)

    # OpenAI chat completions
    for choice in data.get("choices") or []:
        msg = (choice or {}).get("message") or {}
        if isinstance(msg.get("content"), str) and msg["content"]:
            parts.append(msg["content"])
    if parts:
        return "\n".join(parts)

    # Anthropic messages
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts)


def usage_of(data: dict) -> dict:
    """Normalise token usage across providers, for the cost readout."""
    u = (data or {}).get("usage") or {}
    return {
        "input": u.get("input_tokens") or u.get("prompt_tokens") or 0,
        "output": u.get("output_tokens") or u.get("completion_tokens") or 0,
    }


# --------------------------------------------------------------------------
# Calling (used by the offline cache generator; the web path has its own)
# --------------------------------------------------------------------------


def complete(system: str, user: str, tier: str = "fast", timeout: int = 90) -> dict:
    """One completion. Returns ``{text, usage, model, provider}`` or raises."""
    provider = active_provider()
    conf = DEFAULTS[provider]
    if not os.environ.get(conf["key_env"]):
        raise RuntimeError(f"{conf['key_env']} is not set")

    url, headers, body = build_http_request(system, user, tier, provider)
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # An unknown output-cap parameter is the one failure worth retrying:
        # the field name differs between API generations and it is not load
        # bearing — the prompts already constrain length.
        if exc.code == 400 and "max_output_tokens" in detail:
            body.pop("max_output_tokens", None)
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        else:
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    return {
        "text": extract_text(data),
        "usage": usage_of(data),
        "model": body["model"],
        "provider": provider,
    }
