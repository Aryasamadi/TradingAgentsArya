import logging
import os
import re
import time
import warnings
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

RPM_RETRY_DELAY_SECONDS = 20
DEFAULT_RPM_RETRIES = 3

# Retry only an explicitly identified requests-per-minute limit. Token/minute,
# billing, exhausted-credit, and generic HTTP 429 errors are deliberately excluded.
_RPM_REQUEST_PATTERNS = (
    re.compile(r"requests?_per_minute", re.IGNORECASE),
    re.compile(r"requestsperminute", re.IGNORECASE),
    re.compile(r"\brequests?\s*(?:per|/)\s*(?:minute|min)\b", re.IGNORECASE),
    re.compile(r"\b(?:per|/)\s*(?:minute|min)\b.{0,50}\brequests?\b", re.IGNORECASE),
    re.compile(r"\brpm\b.{0,50}\brequests?\b", re.IGNORECASE),
)
_BILLING_PATTERNS = (
    re.compile(r"insufficient[_ ]quota", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
    re.compile(r"hard[_ ]limit", re.IGNORECASE),
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"(?i)((?:api[_ -]?key|authorization)\s*[:=]\s*)[^\s,;]+"),
)


def _error_payload(exc: BaseException) -> tuple[str, dict[str, str]]:
    """Collect provider error text and response headers without assuming an SDK."""
    parts = [str(exc)]
    headers: dict[str, str] = {}
    for value in (getattr(exc, "body", None), getattr(exc, "error", None),
                  getattr(exc, "details", None)):
        if value is not None:
            parts.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        parts.extend((str(getattr(response, "text", "")), str(getattr(response, "data", ""))))
        raw_headers = getattr(response, "headers", None)
        if raw_headers:
            try:
                headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
            except Exception:
                headers = {}
    raw_headers = getattr(exc, "headers", None)
    if raw_headers:
        try:
            headers.update({str(k).lower(): str(v) for k, v in raw_headers.items()})
        except Exception:
            pass
    return "\n".join(part for part in parts if part and part != "None"), headers


def is_rpm_rate_limit_error(exc: BaseException) -> bool:
    """True only for HTTP 429 / RESOURCE_EXHAUSTED errors explicitly tied to request RPM."""
    response = getattr(exc, "response", None)
    status_values = (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(response, "status_code", None),
        getattr(exc, "code", None),
    )
    status_429 = any(str(value).strip() == "429" for value in status_values if value is not None)
    details, headers = _error_payload(exc)
    lowered = details.lower()
    resource_exhausted = "resource_exhausted" in lowered or "resourceexhausted" in type(exc).__name__.lower()
    if not (status_429 or resource_exhausted or re.search(r"\bhttp\s*429\b", lowered)):
        return False

    # Never retry account/billing exhaustion just because the provider used 429.
    if any(pattern.search(details) for pattern in _BILLING_PATTERNS):
        return False

    # A zero remaining-request header is explicit RPM evidence.
    for name, value in headers.items():
        if "request" in name and "remaining" in name and value.strip() in {"0", "0.0"}:
            return True

    return any(pattern.search(details) for pattern in _RPM_REQUEST_PATTERNS)


def describe_error(exc: BaseException, limit: int = 1200) -> str:
    """Return a bounded diagnostic string with common credential formats redacted."""
    detail = str(exc).strip() or type(exc).__name__
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(lambda match: (match.group(1) + "[REDACTED]") if match.lastindex else "[REDACTED]", detail)
    return detail[:limit]


def rpm_retry_budget() -> int:
    """Read a bounded retry count; default to three retries after the first attempt."""
    raw = os.getenv("TRADINGAGENTS_RPM_RETRIES", str(DEFAULT_RPM_RETRIES)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid TRADINGAGENTS_RPM_RETRIES=%r; using %d", raw, DEFAULT_RPM_RETRIES)
        return DEFAULT_RPM_RETRIES
    if value < 0:
        logger.warning("Negative TRADINGAGENTS_RPM_RETRIES=%r; using %d", raw, DEFAULT_RPM_RETRIES)
        return DEFAULT_RPM_RETRIES
    return min(value, 10)


def run_with_rpm_retries(operation, *, agent_name: str = "LLM"):
    """Run an agent node, waiting exactly 20 seconds only for a classified RPM 429."""
    retries = rpm_retry_budget()
    retry_number = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            if not is_rpm_rate_limit_error(exc) or retry_number >= retries:
                raise
            retry_number += 1
            logger.warning(
                "%s hit a requests-per-minute limit; retry %d/%d in %d seconds.",
                agent_name, retry_number, retries, RPM_RETRY_DELAY_SECONDS,
            )
            time.sleep(RPM_RETRY_DELAY_SECONDS)


def normalize_content(response):
    """Normalize LLM response content to a plain string.

    Multiple providers (OpenAI Responses API, Google Gemini 3) return content
    as a list of typed blocks, e.g. [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}].
    Downstream agents expect response.content to be a string. This extracts
    and joins the text blocks, discarding reasoning/metadata blocks.
    """
    content = response.content
    if isinstance(content, list):
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    return response


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def get_provider_name(self) -> str:
        """Return the provider name used in warning messages."""
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        """Warn when the model is outside the known list for the provider."""
        if self.validate_model():
            return

        warnings.warn(
            (
                f"Model '{self.model}' is not in the known model list for "
                f"provider '{self.get_provider_name()}'. Continuing anyway."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """Return the configured LLM instance."""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """Validate that the model is supported by this client."""
        pass
