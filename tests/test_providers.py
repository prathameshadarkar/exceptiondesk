"""Provider adapter tests. These pass today.

The adapter is the only code that knows an API shape, so it is the only code
that can silently send a malformed request. Worth pinning.
"""

import os

import pytest

from exception_desk import providers
from exception_desk.agents import AGENTS, build_request


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_PROVIDER",
              "MODEL_FAST", "MODEL_REASON"):
        monkeypatch.delenv(k, raising=False)


def test_provider_follows_whichever_key_is_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert providers.active_provider() == "openai"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert providers.active_provider() == "anthropic"


def test_explicit_provider_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert providers.active_provider() == "anthropic"


def test_env_overrides_the_model(monkeypatch):
    monkeypatch.setenv("MODEL_FAST", "some-other-model")
    assert providers.model_for("fast", "openai") == "some-other-model"


def test_openai_request_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    url, headers, body = providers.build_http_request("SYS", "USER", "reason", "openai")
    assert url.endswith("/v1/responses")
    assert headers["authorization"] == "Bearer sk-test"
    # system prompt travels as a message, not a top-level field
    assert [m["role"] for m in body["input"]] == ["developer", "user"]
    assert body["input"][0]["content"] == "SYS"
    assert "system" not in body


def test_anthropic_request_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    url, headers, body = providers.build_http_request("SYS", "USER", "fast", "anthropic")
    assert url.endswith("/v1/messages")
    assert headers["x-api-key"] == "sk-test"
    assert body["system"] == "SYS"          # top-level, unlike OpenAI
    assert body["messages"][0]["role"] == "user"


def test_the_key_never_appears_in_the_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    _, _, body = providers.build_http_request("S", "U", "fast", "openai")
    assert "sk-secret" not in repr(body)


@pytest.mark.parametrize("payload,expected", [
    ({"output_text": "hello"}, "hello"),
    ({"output": [{"content": [{"type": "output_text", "text": "hi"}]}]}, "hi"),
    ({"choices": [{"message": {"content": "chat"}}]}, "chat"),
    ({"content": [{"type": "text", "text": "claude"}]}, "claude"),
    ({}, ""),
    ({"unexpected": True}, ""),
])
def test_extract_text_tolerates_shapes(payload, expected):
    assert providers.extract_text(payload) == expected


@pytest.mark.parametrize("payload", [
    {"usage": {"input_tokens": 10, "output_tokens": 3}},
    {"usage": {"prompt_tokens": 10, "completion_tokens": 3}},
])
def test_usage_normalises(payload):
    assert providers.usage_of(payload) == {"input": 10, "output": 3}


def test_only_the_judge_and_ask_use_the_expensive_tier():
    """Drafting work must not silently land on the reasoning model."""
    tiers = {a.id: a.tier for a in AGENTS}
    assert tiers["judge"] == "reason"
    assert tiers["broker"] == "fast"
    assert tiers["pm"] == "fast"
    assert tiers["triage"] == "fast"


def test_build_request_is_provider_agnostic():
    system, user, tier = build_request("broker", "CONTEXT")
    assert "CONTEXT" in user and system and tier == "fast"
