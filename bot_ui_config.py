"""Validated settings shared by the Telegram controller and its tests.

This module intentionally contains no Telegram, GitHub, or LLM SDK calls.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

DEFAULT_RUN_SETTINGS: dict[str, Any] = {
    "output_language": "Persian",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_tool_rounds": 20,
    "max_tokens": 8192,
    "temperature": 0.0,
    "checkpoint_enabled": True,
    "google_thinking_level": "medium",
    "openai_reasoning_effort": "medium",
    "anthropic_effort": "high",
    "holding_period_days": 5,
    "benchmark_ticker": "",
    "data_vendors": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "sec_edgar,yfinance",
        "news_data": "yfinance",
        "macro_data": "fred",
        "prediction_markets": "polymarket",
    },
}

DATA_VENDOR_OPTIONS = {
    "core_stock_apis": ("yfinance", "alpha_vantage", "yfinance,alpha_vantage", "alpha_vantage,yfinance"),
    "technical_indicators": ("yfinance", "alpha_vantage", "yfinance,alpha_vantage", "alpha_vantage,yfinance"),
    "fundamental_data": ("sec_edgar,yfinance", "sec_edgar", "alpha_vantage", "yfinance"),
    "news_data": ("yfinance", "alpha_vantage", "yfinance,alpha_vantage"),
    "macro_data": ("fred",),
    "prediction_markets": ("polymarket",),
}

SETTING_CHOICES = {
    "output_language": ("Persian", "English", "German", "French", "Spanish", "Arabic", "Chinese", "Japanese"),
    "max_debate_rounds": (0, 1, 2, 3, 4),
    "max_risk_discuss_rounds": (0, 1, 2, 3, 4),
    "max_tool_rounds": (5, 10, 15, 20, 30, 40),
    "max_tokens": (None, 2048, 4096, 8192, 16384, 32768),
    "temperature": (None, 0.0, 0.2, 0.5, 0.7, 1.0),
    "checkpoint_enabled": (True, False),
    "google_thinking_level": (None, "minimal", "low", "medium", "high"),
    "openai_reasoning_effort": (None, "low", "medium", "high"),
    "anthropic_effort": (None, "low", "medium", "high"),
    "holding_period_days": (1, 3, 5, 10, 20, 60),
}

def normalize_run_settings(value: Any = None) -> dict[str, Any]:
    """Return a validated settings snapshot, filling omitted values from defaults."""
    if value is not None and not isinstance(value, dict):
        raise ValueError("تنظیمات اجرا باید یک شیء JSON باشند.")
    result = deepcopy(DEFAULT_RUN_SETTINGS)
    value = value or {}
    for key in SETTING_CHOICES:
        if key not in value:
            continue
        candidate = value[key]
        allowed = SETTING_CHOICES[key]
        if candidate not in allowed:
            raise ValueError(f"مقدار نامعتبر برای {key}: {candidate!r}")
        result[key] = candidate
    if "benchmark_ticker" in value:
        ticker = str(value["benchmark_ticker"] or "").strip().upper()
        if ticker and not re.fullmatch(r"[A-Z0-9._^=-]{1,32}", ticker):
            raise ValueError("نماد Benchmark نامعتبر است.")
        result["benchmark_ticker"] = "" if ticker == "-" else ticker
    vendors = value.get("data_vendors", {})
    if vendors is not None and not isinstance(vendors, dict):
        raise ValueError("data_vendors باید یک شیء JSON باشد.")
    for key, allowed in DATA_VENDOR_OPTIONS.items():
        candidate = (vendors or {}).get(key, result["data_vendors"][key])
        if candidate not in allowed:
            raise ValueError(f"منبع داده نامعتبر برای {key}: {candidate!r}")
        result["data_vendors"][key] = candidate
    return result

def apply_setting(value: Any, key: str, raw_value: str) -> dict[str, Any]:
    """Return a new validated settings object with one whitelisted value changed."""
    current = normalize_run_settings(value)
    if key not in SETTING_CHOICES:
        raise ValueError("تنظیم ناشناخته است.")
    candidate: Any = raw_value
    if raw_value == "default":
        candidate = None
    elif key in {"max_debate_rounds", "max_risk_discuss_rounds", "max_tool_rounds", "max_tokens", "holding_period_days"}:
        candidate = int(raw_value)
    elif key == "temperature":
        candidate = float(raw_value)
    elif key == "checkpoint_enabled":
        normalized_bool = raw_value.strip().lower()
        if normalized_bool not in {"true", "false"}:
            raise ValueError(f"Invalid boolean value for {key}: {raw_value!r}")
        candidate = normalized_bool == "true"
    if candidate not in SETTING_CHOICES[key]:
        raise ValueError(f"مقدار نامعتبر برای {key}: {raw_value}")
    current[key] = candidate
    return normalize_run_settings(current)

def set_data_vendor(value: Any, category: str, vendor: str) -> dict[str, Any]:
    current = normalize_run_settings(value)
    if category not in DATA_VENDOR_OPTIONS or vendor not in DATA_VENDOR_OPTIONS[category]:
        raise ValueError("ترکیب منبع داده پشتیبانی نمی‌شود.")
    current["data_vendors"][category] = vendor
    return normalize_run_settings(current)

def validate_portfolio(value: Any) -> dict[str, Any]:
    """Validate the JSON portfolio accepted by the CLI before it enters a workflow."""
    if not isinstance(value, dict):
        raise ValueError("پورتفولیو باید یک شیء JSON باشد.")
    if set(value) - {"cash", "currency", "positions"}:
        raise ValueError("فقط فیلدهای cash، currency و positions مجاز هستند.")
    result: dict[str, Any] = {"cash": None, "currency": None, "positions": []}
    cash = value.get("cash")
    if cash is not None:
        if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(float(cash)):
            raise ValueError("cash باید یک عدد متناهی باشد.")
        result["cash"] = float(cash)
    currency = str(value.get("currency") or "").strip().upper()
    if currency and not re.fullmatch(r"[A-Z0-9._-]{1,12}", currency):
        raise ValueError("کد currency معتبر نیست.")
    result["currency"] = currency or None
    positions = value.get("positions", [])
    if not isinstance(positions, list) or len(positions) > 100:
        raise ValueError("positions باید یک فهرست با حداکثر ۱۰۰ موقعیت باشد.")
    for item in positions:
        if not isinstance(item, dict) or set(item) - {"ticker", "quantity", "average_price"}:
            raise ValueError("هر موقعیت فقط باید ticker، quantity و average_price داشته باشد.")
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or not re.fullmatch(r"[A-Z0-9._^=-]{1,32}", ticker):
            raise ValueError("Ticker یکی از موقعیت‌های پورتفولیو نامعتبر است.")
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(float(quantity)):
            raise ValueError(f"quantity برای {ticker} باید عدد متناهی باشد.")
        average = item.get("average_price")
        if average is not None:
            if isinstance(average, bool) or not isinstance(average, (int, float)) or not math.isfinite(float(average)) or float(average) <= 0:
                raise ValueError(f"average_price برای {ticker} باید عددی مثبت باشد.")
            average = float(average)
        result["positions"].append({"ticker": ticker, "quantity": float(quantity), "average_price": average})
    if result["cash"] is None and result["currency"] is None and not result["positions"]:
        raise ValueError("پورتفولیو هیچ داده‌ای ندارد.")
    return result
