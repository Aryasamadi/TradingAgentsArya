from __future__ import annotations

import pytest

from bot_ui_config import (
    DEFAULT_RUN_SETTINGS,
    apply_setting,
    normalize_run_settings,
    set_data_vendor,
    validate_portfolio,
)


def test_defaults_match_controller_workflow_defaults():
    settings = normalize_run_settings()
    assert settings["output_language"] == "Persian"
    assert settings["max_debate_rounds"] == 1
    assert settings["max_risk_discuss_rounds"] == 1
    assert settings["max_tool_rounds"] == 20
    assert settings["checkpoint_enabled"] is True
    assert settings["data_vendors"] == DEFAULT_RUN_SETTINGS["data_vendors"]


def test_apply_setting_changes_only_one_allowed_value():
    original = normalize_run_settings()
    changed = apply_setting(original, "max_debate_rounds", "2")
    assert changed["max_debate_rounds"] == 2
    assert changed["max_risk_discuss_rounds"] == original["max_risk_discuss_rounds"]
    assert original["max_debate_rounds"] == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_tool_rounds", "999"),
        ("temperature", "nan"),
        ("checkpoint_enabled", "maybe"),
        ("output_language", "unknown"),
        ("not_a_setting", "1"),
    ],
)
def test_invalid_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        apply_setting({}, key, value)


def test_data_vendor_selection_is_validated_and_isolated():
    changed = set_data_vendor({}, "news_data", "alpha_vantage")
    assert changed["data_vendors"]["news_data"] == "alpha_vantage"
    assert changed["data_vendors"]["macro_data"] == "fred"
    with pytest.raises(ValueError):
        set_data_vendor({}, "macro_data", "yfinance")


def test_portfolio_validation_normalizes_values():
    portfolio = validate_portfolio(
        {
            "cash": 1250,
            "currency": "usd",
            "positions": [
                {"ticker": "nvda", "quantity": 2, "average_price": 900},
                {"ticker": "AAPL", "quantity": -1},
            ],
        }
    )
    assert portfolio["cash"] == 1250.0
    assert portfolio["currency"] == "USD"
    assert portfolio["positions"][0] == {
        "ticker": "NVDA",
        "quantity": 2.0,
        "average_price": 900.0,
    }
    assert portfolio["positions"][1]["average_price"] is None


@pytest.mark.parametrize(
    "portfolio",
    [
        {},
        {"cash": float("inf")},
        {"positions": [{"ticker": "NVDA", "quantity": float("nan")}]},
        {"positions": [{"ticker": "NVDA", "quantity": 1, "average_price": 0}]},
        {"positions": [{"ticker": "NVDA", "quantity": 1, "extra": "x"}]},
        {"positions": [{"ticker": "../secret", "quantity": 1}]},
    ],
)
def test_invalid_portfolios_are_rejected(portfolio):
    with pytest.raises(ValueError):
        validate_portfolio(portfolio)


def test_engine_config_accepts_structured_vendor_overrides(monkeypatch):
    from tradingagents.default_config import build_default_config

    monkeypatch.setenv("TRADINGAGENTS_HOLDING_PERIOD_DAYS", "10")
    monkeypatch.setenv("TRADINGAGENTS_DATA_VENDORS_JSON", '{"news_data":"alpha_vantage"}')
    monkeypatch.setenv("TRADINGAGENTS_TOOL_VENDORS_JSON", '{"get_stock_data":"yfinance"}')
    cfg = build_default_config()
    assert cfg["holding_period_days"] == 10
    assert cfg["data_vendors"]["news_data"] == "alpha_vantage"
    assert cfg["tool_vendors"]["get_stock_data"] == "yfinance"


def test_engine_config_rejects_invalid_vendor_json(monkeypatch):
    from tradingagents.default_config import build_default_config

    monkeypatch.setenv("TRADINGAGENTS_DATA_VENDORS_JSON", "[]")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        build_default_config()
