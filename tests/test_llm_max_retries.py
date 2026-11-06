"""Configurable LLM SDK retry budget (#1090/#1091).

A single transient 429 burst used to kill an otherwise-healthy multi-agent run
because each provider SDK's max_retries (default 2) was not exposed. This adds an
opt-in llm_max_retries knob forwarded to every provider chat client.
"""
from __future__ import annotations

import pytest

import tradingagents.default_config as default_config_module
from tradingagents.llm_clients.factory import _coerce_max_retries, build_llm_kwargs

# --- coercion / validation -------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [(0, 0), (2, 2), (10, 10), ("6", 6)])
def test_coerce_accepts_non_negative_ints_and_numeric_strings(value, expected):
    assert _coerce_max_retries(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-1, "-3"])
def test_coerce_rejects_negative(bad):
    with pytest.raises(ValueError, match=">= 0"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [True, False])
def test_coerce_rejects_booleans(bad):
    with pytest.raises(ValueError, match="boolean"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["abc", "1.5", None])
def test_coerce_rejects_non_integers(bad):
    with pytest.raises(ValueError, match="integer"):
        _coerce_max_retries(bad)


# --- forwarding into provider kwargs --------------------------------------


@pytest.mark.unit
def test_not_forwarded_when_unset():
    kwargs = build_llm_kwargs({"llm_provider": "openai", "llm_max_retries": None})
    assert "max_retries" not in kwargs


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_forwarded_across_providers(provider):
    kwargs = build_llm_kwargs({"llm_provider": provider, "llm_max_retries": 6})
    assert kwargs["max_retries"] == 6


@pytest.mark.unit
def test_forwarded_env_string_is_coerced():
    # env vars arrive as strings; the consumer coerces (like temperature)
    kwargs = build_llm_kwargs({"llm_provider": "openai", "llm_max_retries": "4"})
    assert kwargs["max_retries"] == 4


@pytest.mark.unit
def test_invalid_config_value_fails_loudly():
    with pytest.raises(ValueError):
        build_llm_kwargs({"llm_provider": "openai", "llm_max_retries": -1})


# --- env overlay -----------------------------------------------------------

def _config_with_env(monkeypatch, **overrides):
    """The defaults as the environment given here would set them."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return default_config_module.build_default_config()


@pytest.mark.unit
def test_default_is_none(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["llm_max_retries"] is None


@pytest.mark.unit
def test_env_override_sets_config(monkeypatch):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_LLM_MAX_RETRIES="8")
    # None-default key: env value arrives as a string and is coerced downstream.
    assert config["llm_max_retries"] == "8"
    assert _coerce_max_retries(config["llm_max_retries"]) == 8



# --- requests-per-minute-only retry policy --------------------------------


class _ProviderError(Exception):
    def __init__(self, message, status_code=429, headers=None):
        super().__init__(message)
        from types import SimpleNamespace
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code, headers=headers or {}, text=message
        )


@pytest.mark.unit
def test_classifies_explicit_requests_per_minute_limit_only():
    from tradingagents.llm_clients.base_client import is_rpm_rate_limit_error

    assert is_rpm_rate_limit_error(
        _ProviderError("Rate limit reached: 60 requests per minute")
    )
    assert is_rpm_rate_limit_error(
        _ProviderError("RESOURCE_EXHAUSTED: GenerateRequestsPerMinutePerProjectPerModel")
    )
    assert is_rpm_rate_limit_error(
        _ProviderError("Too many requests", headers={"x-ratelimit-remaining-requests": "0"})
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429 insufficient_quota; check billing",
        "HTTP 429: tokens per minute limit reached",
        "HTTP 429: account hard limit reached",
        "HTTP 429: generic throttling with no RPM detail",
    ],
)
def test_does_not_retry_non_rpm_429(message):
    from tradingagents.llm_clients.base_client import is_rpm_rate_limit_error

    assert not is_rpm_rate_limit_error(_ProviderError(message))


@pytest.mark.unit
def test_rpm_retry_waits_exactly_twenty_seconds(monkeypatch):
    from tradingagents.llm_clients import base_client

    monkeypatch.setenv("TRADINGAGENTS_RPM_RETRIES", "2")
    waits = []
    attempts = []

    def fake_operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise _ProviderError("requests per minute limit reached")
        return "recovered"

    monkeypatch.setattr(base_client.time, "sleep", waits.append)
    assert base_client.run_with_rpm_retries(fake_operation, agent_name="Test Analyst") == "recovered"
    assert len(attempts) == 3
    assert waits == [20, 20]


@pytest.mark.unit
def test_generic_429_is_not_retried(monkeypatch):
    from tradingagents.llm_clients import base_client

    monkeypatch.setenv("TRADINGAGENTS_RPM_RETRIES", "3")
    waits = []
    attempts = []

    def fake_operation():
        attempts.append(1)
        raise _ProviderError("HTTP 429 insufficient_quota")

    monkeypatch.setattr(base_client.time, "sleep", waits.append)
    with pytest.raises(_ProviderError):
        base_client.run_with_rpm_retries(fake_operation)
    assert len(attempts) == 1
    assert waits == []


@pytest.mark.unit
def test_structured_output_does_not_swallow_rpm_limit():
    from tradingagents.agents.structured import invoke_structured

    class StructuredModel:
        def invoke(self, _prompt):
            raise _ProviderError("requests per minute limit reached")

    with pytest.raises(_ProviderError):
        invoke_structured(StructuredModel(), [], "Test Analyst")
