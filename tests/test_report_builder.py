"""The report builder: one source of truth, and fields read where they are defined.

These tests reproduce the defects the builder had before it was extracted
from the workflow: every analyst's text appearing three times in the PDF,
and the headline fields holding fragments of unrelated prose.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from report_builder import (
    build_report,
    collect_sections,
    extract_fields,
    find_report_tree,
    render_html,
)

TICKER = "NVDA"


def write_tree(root: pathlib.Path) -> pathlib.Path:
    """A report tree shaped like the one upstream 0.7.x writes."""
    tree = root / "logs" / "reports" / f"{TICKER}_20261027_120000"
    (tree / "1_analysts").mkdir(parents=True)
    (tree / "2_research").mkdir(parents=True)
    (tree / "3_trading").mkdir(parents=True)
    (tree / "4_risk").mkdir(parents=True)
    (tree / "5_portfolio").mkdir(parents=True)

    (tree / "1_analysts" / "market.md").write_text(
        "## بازار\n\nروند صعودی. PROPOSAL: نگهداری", encoding="utf-8"
    )
    (tree / "1_analysts" / "sentiment.md").write_text(
        "## احساسات\n\nاحساسات مثبت", encoding="utf-8"
    )
    (tree / "1_analysts" / "news.md").write_text(
        "## اخبار\n\nاخبار مهمی نبود", encoding="utf-8"
    )
    (tree / "1_analysts" / "fundamentals.md").write_text(
        "## بنیادی\n\nبنیادی سالم", encoding="utf-8"
    )
    (tree / "2_research" / "bull.md").write_text("## صعودی\n\nاستدلال صعودی", encoding="utf-8")
    (tree / "2_research" / "bear.md").write_text("## نزولی\n\nاستدلال نزولی", encoding="utf-8")
    (tree / "2_research" / "manager.md").write_text("## مدیر پژوهش\n\nپلن", encoding="utf-8")
    (tree / "3_trading" / "trader.md").write_text(
        "**Action**: BUY\n\n"
        "**Reasoning**: دلیل خرید\n\n"
        "**Entry Price**: 214.13\n\n"
        "**Stop Loss**: 202.66\n\n"
        "**Position Sizing**: 5% of portfolio\n\n"
        "FINAL TRANSACTION PROPOSAL: **BUY**\n",
        encoding="utf-8",
    )
    (tree / "4_risk" / "aggressive.md").write_text("## تهاجمی\n\nریسک بالا", encoding="utf-8")
    (tree / "4_risk" / "conservative.md").write_text(
        "## محافظه‌کارانه\n\nریسک کم", encoding="utf-8"
    )
    (tree / "4_risk" / "neutral.md").write_text("## خنثی\n\nریسک متعادل", encoding="utf-8")
    (tree / "5_portfolio" / "decision.md").write_text(
        "**Rating**: Underweight\n\n"
        "**Executive Summary**: کاهش موقعیت تا ۵۰٪ در ۲۱۴.۱۳\n\n"
        "**Investment Thesis**: بنیاد بلندمدت سالم است\n\n"
        "**Price Target**: 223.00\n\n"
        "**Time Horizon**: 3-6 ماه\n",
        encoding="utf-8",
    )
    (tree / "complete_report.md").write_text(
        "# Trading Analysis Report: NVDA\n\n"
        + "\n\n".join(
            f"## {name}\n\n{body}"
            for name, body in [
                ("I. Analyst Team Reports", "متن تحلیل‌گران"),
                ("III. Trading Team Plan", "**Action**: BUY"),
                ("V. Portfolio Manager Decision", "**Rating**: Underweight"),
            ]
        ),
        encoding="utf-8",
    )
    return tree


def test_find_report_tree_picks_the_newest(tmp_path: pathlib.Path) -> None:
    logs = tmp_path / "logs"
    older = logs / "reports" / f"{TICKER}_20261020_090000"
    newer = logs / "reports" / f"{TICKER}_20261027_120000"
    for tree in (older, newer):
        (tree / "5_portfolio").mkdir(parents=True)
        (tree / "complete_report.md").write_text("x", encoding="utf-8")
    import os

    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert find_report_tree(logs) == newer


def test_find_report_tree_returns_none_without_a_run(tmp_path: pathlib.Path) -> None:
    assert find_report_tree(tmp_path / "logs") is None


def test_sections_appear_once_each(tmp_path: pathlib.Path) -> None:
    tree = write_tree(tmp_path)
    sections = collect_sections(tree)
    titles = [section["title"] for section in sections]

    assert titles == [
        "تحلیل بازار",
        "تحلیل احساسات",
        "تحلیل اخبار",
        "تحلیل بنیادی",
        "پژوهشگر صعودی",
        "پژوهشگر نزولی",
        "مدیر پژوهش",
        "معامله‌گر",
        "تحلیل‌گر ریسک تهاجمی",
        "تحلیل‌گر ریسک محافظه‌کارانه",
        "تحلیل‌گر ریسک خنثی",
        "تصمیم نهایی",
    ]
    # The consolidated report is the body, not a section of its own.
    assert "complete_report.md" not in [pathlib.Path(s["source"]).name for s in sections]


def test_fields_come_from_the_documents_that_define_them() -> None:
    decision = (
        "**Rating**: Underweight\n\n"
        "**Executive Summary**: کاهش موقعیت\n\n"
        "**Price Target**: 223.00\n\n"
        "**Time Horizon**: 3-6 ماه\n"
    )
    trader = (
        "**Action**: BUY\n\n"
        "**Entry Price**: 214.13\n\n"
        "**Stop Loss**: 202.66\n\n"
        "FINAL TRANSACTION PROPOSAL: **BUY**\n"
    )
    fields = extract_fields(decision, trader)

    assert fields["rating"] == "Underweight"
    assert fields["action"] == "BUY"
    assert fields["entry"] == "214.13"
    assert fields["stop"] == "202.66"
    assert fields["target"] == "223.00"
    assert fields["horizon"] == "3-6 ماه"
    assert fields["summary"] == "کاهش موقعیت"


def test_prose_never_becomes_a_field() -> None:
    """The old builder matched these words in analyst prose."""
    decision = (
        "تحلیل‌گر در متن خود می‌نویسد که هدف قیمتی در افق زمانی کوتاه\n"
        "قابل دستیابی نیست و خروج کامل زیان‌بار خواهد بود.\n\n"
        "**Rating**: Hold\n"
    )
    fields = extract_fields(decision)

    assert fields["rating"] == "Hold"
    assert fields["target"] == ""
    assert fields["exit"] == ""
    assert fields["horizon"] == ""


def test_a_field_read_from_the_wrong_paragraph_is_not_taken() -> None:
    """A label inside a sentence is not the label the document defines."""
    decision = (
        "The analyst mentions exit price briefly, but the decision itself\n"
        "does not define one.\n\n"
        "**Rating**: Sell\n"
    )
    fields = extract_fields(decision)

    assert fields["rating"] == "Sell"
    assert fields["exit"] == ""


def test_not_provided_is_an_absent_value() -> None:
    decision = "**Rating**: Hold\n\n**Price Target**: not provided\n"
    fields = extract_fields(decision)

    assert fields["target"] == ""


def test_report_is_built_once_per_section(tmp_path: pathlib.Path) -> None:
    write_tree(tmp_path)
    out = tmp_path / "out"
    report = build_report(tmp_path, out, TICKER, "analysis", "2026-10-27")

    doc = (out / "report.html").read_text(encoding="utf-8")
    # The market analyst's text appears once, not once per copy of the run.
    assert doc.count("روند صعودی") == 1
    assert doc.count("تحلیل بازار") == 1
    # The decision is printed once, not once as a block and once as a section.
    assert doc.count("بنیاد بلندمدت سالم است") == 1

    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["rating"] == "Underweight"
    assert data["action"] == "BUY"
    assert data["entry"] == "214.13"
    assert data["stop"] == "202.66"
    assert data["target"] == "223.00"
    assert data["status"] == "ok"
    assert report["has_pdf"] in (True, False)


def test_a_run_without_a_report_tree_says_so(tmp_path: pathlib.Path) -> None:
    """No tree means no report: the fields say that, they do not invent one."""
    (tmp_path / "logs").mkdir()
    out = tmp_path / "out"
    build_report(tmp_path, out, TICKER, "analysis", "2026-10-27")

    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["rating"] == "—"
    assert data["action"] == "—"
    assert data["entry"] == "—"
    assert data["stop"] == "—"
    assert data["target"] == "—"
    assert data["status"] == "no_report"


def test_html_is_rtl_and_carries_the_header() -> None:
    doc = render_html(
        {"rating": "Hold", "action": "BUY", "entry": "1", "exit": "", "target": "",
         "stop": "2", "confidence": "", "horizon": "", "summary": "خلاصه", "decision": ""},
        [],
        TICKER,
        "analysis",
        "2026-10-27",
    )
    assert '<html dir="rtl" lang="fa">' in doc
    assert "Hold" in doc
    assert "خلاصه" in doc


@pytest.mark.parametrize("value", ["", "   ", None])
def test_missing_values_render_as_a_dash(value: str | None) -> None:
    doc = render_html(
        {"rating": value or "", "action": "", "entry": "", "exit": "", "target": "",
         "stop": "", "confidence": "", "horizon": "", "summary": "", "decision": ""},
        [],
        TICKER,
        "analysis",
        "2026-10-27",
    )
    assert "—" in doc
