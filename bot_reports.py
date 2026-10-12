"""Presentation-only helpers for Telegram summaries.

The independent Telegram summary is a separate field. Original report files and
agent-produced content are never rewritten by this module.
"""
from __future__ import annotations

import html
import json
from typing import Any, Mapping


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def make_summary_text(row: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"
    mode = row["mode"]
    label = "تحلیل" if mode == "analysis" else "بک‌تست"
    try:
        run_number = row["run_no"] or row["request_id"][:8]
    except (KeyError, IndexError):
        run_number = "—"
    try:
        request_short = str(row["request_id"])[:10]
    except (KeyError, IndexError):
        request_short = "—"
    lines = [
        f"<b>📊 گزارش #{_esc(run_number)} · <code>{_esc(request_short)}</code></b>",
        f"{label}: <code>{_esc(subject)}</code>",
    ]

    try:
        provider = str(row["provider_name_snapshot"] or "—")
    except (KeyError, IndexError):
        provider = "—"
    try:
        model = str(row["model_name_snapshot"] or "—")
    except (KeyError, IndexError):
        model = "—"
    lines.append(f"🔌 Provider: <b>{_esc(provider)}</b>")
    lines.append(f"🤖 Model: <b>{_esc(model)}</b>")

    field_labels = (
        ("rating", "نتیجه نهایی"),
        ("action", "اقدام / سفارش"),
        ("entry", "نقطه ورود"),
        ("exit", "نقطه خروج"),
        ("target", "هدف قیمتی"),
        ("stop", "حد ضرر"),
        ("amount", "مقدار / حجم"),
        ("value", "ارزش / ارزش موقعیت"),
        ("confidence", "سطح اطمینان"),
        ("horizon", "افق زمانی"),
    )
    missing_labels = {"amount": "در خروجی صریحاً مشخص نشده", "value": "در خروجی صریحاً مشخص نشده"}
    for key, label_text in field_labels:
        value = info.get(key)
        if value is None or str(value).strip() in ("", "—", "-"):
            if key in missing_labels:
                lines.append(f"📌 {label_text}: <i>{missing_labels[key]}</i>")
            continue
        # The value itself is not numerically normalized, rounded, or reformatted.
        lines.append(f"📌 {label_text}: <code>{_esc(value)}</code>")

    independent_summary = str(info.get("telegram_summary") or "").strip()
    lines.append("")
    if independent_summary:
        lines.append("<b>خلاصه مستقل و بی‌طرف:</b>")
        lines.append(_esc(independent_summary))
    else:
        lines.append("<i>خلاصه مستقل در دسترس نیست؛ متن اصلی گزارش تغییری نکرده است.</i>")

    return "\n".join(lines)
