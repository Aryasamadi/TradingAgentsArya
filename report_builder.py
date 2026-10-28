"""Build the Telegram/PDF report from a completed TradingAgents run.

The engine writes its report tree under ``~/.tradingagents/logs``. Upstream
0.7.x lays it out like this::

    logs/reports/<TICKER>_<stamp>/          <- write_report_tree (canonical)
        complete_report.md                   <- every section, concatenated
        complete_report.html
        1_analysts/market.md
        2_research/manager.md
        3_trading/trader.md
        4_risk/aggressive.md
        5_portfolio/decision.md
    logs/<TICKER>/<date>/reports/*.md       <- the CLI's per-section files

Two things used to go wrong here, and both are fixed at the source rather
than papered over:

*Every file was scraped.*  The old builder walked the whole home directory
and appended each ``.md`` it found as its own section.  The analyst text
therefore appeared three times over (CLI report, report-tree section, and
``complete_report.md``), and a 43-page PDF carried the word "PROPOSAL" 22
times.  Now the canonical report tree is the single source of truth, and
``complete_report.md`` is the body while the tree's per-section files supply
the section boundaries.

*The fields were matched against the whole corpus.*  Every ``Key: value``
pattern ran over all the text and the first hit won, so "stop loss" matched
the prose of some analyst and "target price" matched an unrelated sentence.
Now each field is read from the document that actually defines it: the
portfolio decision for the rating, target and horizon, the trader proposal
for the entry, stop and action.

The module is deliberately free of the LLM stack so the workflow can run it
without importing the agent framework, and so the tests can exercise it
directly.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import pathlib
import re

# --------------------------------------------------------------------------
# Locating the run's report tree
# --------------------------------------------------------------------------

# The canonical tree upstream writes.  ``complete_report.md`` holds every
# section; the numbered folders hold one file per section.
_TREE_SECTIONS = (
    ("1_analysts", "market.md", "تحلیل بازار"),
    ("1_analysts", "sentiment.md", "تحلیل احساسات"),
    ("1_analysts", "news.md", "تحلیل اخبار"),
    ("1_analysts", "fundamentals.md", "تحلیل بنیادی"),
    ("2_research", "bull.md", "پژوهشگر صعودی"),
    ("2_research", "bear.md", "پژوهشگر نزولی"),
    ("2_research", "manager.md", "مدیر پژوهش"),
    ("3_trading", "trader.md", "معامله‌گر"),
    ("4_risk", "aggressive.md", "تحلیل‌گر ریسک تهاجمی"),
    ("4_risk", "conservative.md", "تحلیل‌گر ریسک محافظه‌کارانه"),
    ("4_risk", "neutral.md", "تحلیل‌گر ریسک خنثی"),
    ("5_portfolio", "decision.md", "تصمیم نهایی"),
)

# Files that are never a report section: logs, checkpoints, cached data.
_NOT_A_SECTION = (
    "log", "debug", "trace", "checkpoint", "state", "memory",
    "cache", "request.json", "command.txt", "agent.log",
)


def find_report_tree(logs_dir: pathlib.Path) -> pathlib.Path | None:
    """The newest canonical report tree under ``logs_dir``, if there is one.

    Upstream stamps each tree with the time it was written
    (``<TICKER>_<YYYYMMDD>_<HHMMSS>``), so the newest one is the run we
    want.  The search is deliberately shallow: only ``logs/reports/*`` holds
    a report tree, and the CLI's own ``logs/<TICKER>/<date>/reports`` tree is
    a fallback for older engines.
    """
    candidates: list[pathlib.Path] = []

    reports_root = logs_dir / "reports"
    if reports_root.is_dir():
        for entry in reports_root.iterdir():
            if entry.is_dir() and (entry / "complete_report.md").is_file():
                candidates.append(entry)

    if not candidates:
        # Older engines wrote only the CLI tree.
        for ticker_dir in sorted(logs_dir.glob("*")):
            if not ticker_dir.is_dir() or ticker_dir.name == "reports":
                continue
            for date_dir in sorted(ticker_dir.iterdir()):
                cli_reports = date_dir / "reports"
                if not cli_reports.is_dir():
                    continue
                if any(p.suffix == ".md" for p in cli_reports.iterdir()):
                    candidates.append(cli_reports)

    if not candidates:
        return None

    def stamp(path: pathlib.Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=stamp)


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def clean_text(text: str) -> str:
    """Strip ANSI colour, fenced code and log noise from a body of text."""
    text = text or ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if re.match(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", stripped):
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", stripped) and re.search(
            r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", stripped
        ):
            continue
        if stripped.startswith(("Traceback (", "    at ", '  File "')):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------

# The portfolio manager's decision is asked for these lines, in this shape:
#   **Rating**: Underweight
#   **Executive Summary**: ...
#   **Price Target**: 223.00
#   **Time Horizon**: 3-6 months
# and the trader proposal for:
#   **Action**: BUY
#   **Entry Price**: 214.13
#   **Stop Loss**: 202.66
#   FINAL TRANSACTION PROPOSAL: **BUY**
#
# Each pattern is anchored to the label so it reads the line the label
# defines, not a phrase that happens to contain the word.

_LABEL_VALUE = r"^[ \t]*(?:[-*•][ \t]*)?\**\s*{label}\s*\**\s*[:：]\s*(?P<value>.+?)\s*$"


def _labelled(text: str, labels: tuple[str, ...], flags: int = re.IGNORECASE | re.MULTILINE) -> str:
    """The value of the first line labelled with one of ``labels``."""
    if not text:
        return ""
    for label in labels:
        pattern = _LABEL_VALUE.format(label=re.escape(label))
        try:
            match = re.search(pattern, text, flags=flags)
        except re.error:
            continue
        if match:
            value = match.group("value").strip()
            value = value.strip("*_` ").strip()
            if value:
                return value
    return ""


def _not_provided(value: str) -> str:
    """Empty when upstream explicitly said it did not provide the value."""
    if not value:
        return ""
    if value.strip().lower() in {"not provided", "n/a", "na", "none", "null", "-", "—"}:
        return ""
    return value


def extract_fields(decision: str, trader: str = "", complete: str = "") -> dict[str, str]:
    """The report's headline fields, each read from the document defining it.

    ``decision`` is the portfolio manager's decision, ``trader`` the trader
    proposal, ``complete`` the consolidated report (a fallback for anything
    the two do not carry).
    """
    fields: dict[str, str] = {}

    rating = _labelled(decision, ("Rating", "Final Rating"))
    if not rating:
        rating = _labelled(complete, ("Rating", "Final Rating"))
    fields["rating"] = rating

    action = _labelled(trader, ("Action",))
    if not action:
        proposal = re.search(
            r"FINAL TRANSACTION PROPOSAL\s*[:：]\s*\**\s*(?P<value>[A-Za-z]{3,12})",
            trader or "",
            flags=re.IGNORECASE,
        )
        action = proposal.group("value").strip() if proposal else ""
    if not action:
        action = _labelled(decision, ("Action", "Recommendation"))
    fields["action"] = action

    entry = _not_provided(_labelled(trader, ("Entry Price", "Entry Point", "Entry")))
    if not entry:
        entry = _not_provided(_labelled(decision, ("Entry Price", "Entry Point", "Entry")))
    fields["entry"] = entry

    stop = _not_provided(_labelled(trader, ("Stop Loss", "Stop-Loss", "Stop")))
    if not stop:
        stop = _not_provided(_labelled(decision, ("Stop Loss", "Stop-Loss", "Stop")))
    fields["stop"] = stop

    target = _not_provided(_labelled(decision, ("Price Target", "Target Price", "Target")))
    if not target:
        target = _not_provided(_labelled(trader, ("Price Target", "Target Price", "Target")))
    fields["target"] = target

    horizon = _not_provided(_labelled(decision, ("Time Horizon", "Horizon")))
    fields["horizon"] = horizon

    confidence = _not_provided(_labelled(decision, ("Confidence", "Level of Confidence")))
    if not confidence:
        confidence = _not_provided(_labelled(trader, ("Confidence",)))
    fields["confidence"] = confidence

    # The exit price is not a field upstream's schemas define; read it only
    # from an explicit label so no prose is mistaken for one.
    fields["exit"] = _not_provided(
        _labelled(decision, ("Exit Price", "Exit Point", "Exit"))
    )

    summary = _labelled(decision, ("Executive Summary", "Summary"))
    fields["summary"] = summary
    fields["decision"] = (decision or "").strip()

    return fields


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def collect_sections(tree: pathlib.Path) -> list[dict[str, str]]:
    """The report's sections, each appearing exactly once.

    The canonical tree names its files (``1_analysts/market.md`` and so on),
    so the sections are read by name with a readable Persian title.  A tree
    that does not follow that layout falls back to its own ``.md`` files,
    skipping the consolidated ``complete_report.md`` — that file is the body,
    not a section of itself, and including it is what duplicated everything.
    """
    sections: list[dict[str, str]] = []

    for folder, filename, title in _TREE_SECTIONS:
        path = tree / folder / filename
        if not path.is_file():
            continue
        body = clean_text(_read(path))
        if not body:
            continue
        sections.append({"title": title, "body": body, "source": str(path)})

    if sections:
        return sections

    for path in sorted(tree.rglob("*.md")):
        if path.name == "complete_report.md":
            continue
        low = path.name.lower()
        if any(part in low for part in _NOT_A_SECTION):
            continue
        body = clean_text(_read(path))
        if not body:
            continue
        sections.append({"title": path.stem, "body": body, "source": str(path)})

    return sections


def find_memory_log(logs_dir: pathlib.Path) -> pathlib.Path | None:
    """The newest memory log under ``logs_dir``, if there is one.

    A backtest writes no report tree: it records one decision per cell in a
    memory log.  That log is the only trace of the sweep, so it is what the
    report has to be built from.
    """
    candidates = [
        path for path in logs_dir.rglob("trading_memory.md")
        if path.is_file()
    ]
    if not candidates:
        return None

    def stamp(path: pathlib.Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=stamp)


def sections_from_memory_log(log: pathlib.Path) -> list[dict[str, str]]:
    """Each decision in a memory log as its own section.

    The log's entries look like ``[date | ticker | rating | pending]``
    followed by the decision text; the rating in the tag is the run's own
    verdict, so it is read from there rather than guessed from the prose.
    """
    sections: list[dict[str, str]] = []
    raw = _read(log)
    if not raw:
        return sections

    entry_re = re.compile(
        r"^\[(?P<date>[^|]+)\|\s*(?P<ticker>[^|]+)\|\s*(?P<rating>[^|]*)\|\s*(?P<state>[^\]]*)\]\s*$"
        r"(?P<body>.*?)(?=\n\[[^|]+\|[^|]+\|[^|]*\|[^\]]*\]\s*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    for match in entry_re.finditer(raw):
        body = clean_text(match.group("body"))
        body = re.sub(r"\nDECISION:\s*", "\n", body, count=1).strip()
        body = re.sub(r"<!--\s*ENTRY_END\s*-->", "", body).strip()
        if not body:
            continue
        ticker = match.group("ticker").strip()
        date = match.group("date").strip()
        rating = match.group("rating").strip()
        title = f"{ticker} · {date}" + (f" · {rating}" if rating else "")
        sections.append({"title": title, "body": body, "source": str(log)})

    return sections


def report_body(tree: pathlib.Path, sections: list[dict[str, str]]) -> str:
    """The consolidated report text, used for the summary and as a fallback."""
    complete = tree / "complete_report.md"
    if complete.is_file():
        text = clean_text(_read(complete))
        if text:
            return text
    return "\n\n".join(section["body"] for section in sections)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def md_to_html(text: str) -> str:
    """Markdown to HTML, with a plain fallback when the library is absent."""
    text = text or ""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = text.replace("`", "")
    try:
        import markdown as md_lib
    except Exception:
        md_lib = None
    if md_lib:
        try:
            return md_lib.markdown(text, extensions=["tables", "nl2br"])
        except Exception:
            pass
    escaped = html.escape(text)
    escaped = escaped.replace("\n", "<br>")
    return f"<p>{escaped}</p>"


_STYLE = """
    @page {{ size: A4; margin: 12mm 10mm; }}
    body {{
      font-family: "Noto Sans Arabic", "Noto Sans", "Segoe UI", Tahoma, Arial, sans-serif;
      direction: rtl; color: #141414; background: #ffffff;
      font-size: 12px; line-height: 1.95; margin: 0;
    }}
    .container {{ max-width: 820px; margin: 0 auto; }}
    .header {{
      background: linear-gradient(135deg, #111827, #1f2937); color: #ffffff;
      padding: 18px; border-radius: 16px; margin-bottom: 16px;
    }}
    .header h1 {{ margin: 0 0 8px 0; font-size: 21px; }}
    .meta {{ color: #d1d5db; font-size: 11px; line-height: 1.8; }}
    .badge {{
      display: inline-block; background: rgba(255,255,255,0.14);
      border: 1px solid rgba(255,255,255,0.18); color: #ffffff;
      padding: 4px 10px; border-radius: 999px; margin-left: 6px; margin-top: 6px;
      font-size: 11px;
    }}
    table.keys {{
      width: 100%; border-collapse: collapse; margin: 14px 0 18px 0; page-break-inside: avoid;
    }}
    table.keys th, table.keys td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: right; vertical-align: top; }}
    table.keys th {{ width: 170px; background: #f9fafb; color: #374151; font-weight: bold; }}
    table.keys td.value {{ font-weight: bold; color: #111827; }}
    .section {{ margin-top: 18px; page-break-inside: auto; }}
    .section h2 {{
      font-size: 15px; color: #111827; border-right: 5px solid #2563eb;
      padding-right: 8px; margin: 0 0 8px 0;
    }}
    p {{ margin: 6px 0; text-align: justify; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 11px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 6px; text-align: right; vertical-align: top; }}
    th {{ background: #f3f4f6; }}
    code, pre {{ display: none !important; }}
    hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 14px 0; }}
"""


def render_html(fields: dict[str, str], sections: list[dict[str, str]],
                ticker: str, mode: str, date_text: str) -> str:
    """The report as a self-contained RTL HTML page.

    Nothing is printed twice: the portfolio decision has its own block, so the
    section carrying the same text is dropped, and the executive summary is
    skipped when it is already part of that decision.
    """

    def value_of(key: str) -> str:
        return str(fields.get(key) or "").strip() or "—"

    rows = (
        ("نتیجه نهایی", value_of("rating")),
        ("اقدام / سفارش", value_of("action")),
        ("نقطه ورود", value_of("entry")),
        ("نقطه خروج", value_of("exit")),
        ("هدف قیمتی", value_of("target")),
        ("حد ضرر", value_of("stop")),
        ("سطح اطمینان", value_of("confidence")),
        ("افق زمانی", value_of("horizon")),
    )
    key_html = ""
    for label, value in rows:
        key_html += (
            f"\n              <tr>\n"
            f"                <th>{html.escape(label)}</th>\n"
            f"                <td class=\"value\">{html.escape(value)}</td>\n"
            f"              </tr>\n            "
        )

    decision = str(fields.get("decision") or "").strip()
    summary = str(fields.get("summary") or "").strip()

    sections_html = ""
    if summary and summary not in decision:
        sections_html += (
            f"\n              <div class=\"section\">\n"
            f"                <h2>📌 خلاصه</h2>\n"
            f"                {md_to_html(summary)}\n"
            f"              </div>\n            "
        )
    if decision:
        sections_html += (
            f"\n              <div class=\"section\">\n"
            f"                <h2>🧠 تصمیم نهایی</h2>\n"
            f"                {md_to_html(decision)}\n"
            f"              </div>\n            "
        )

    for section in sections:
        title = str(section.get("title", "")).strip()
        body = str(section.get("body", "")).strip()
        if not title or not body:
            continue
        # The decision is already rendered above; a section repeating it
        # verbatim is the same text twice, which is what the old builder did.
        if decision and body == decision:
            continue
        sections_html += (
            f"\n              <div class=\"section\">\n"
            f"                <h2>{html.escape(title)}</h2>\n"
            f"                {md_to_html(body)}\n"
            f"              </div>\n            "
        )

    generated = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html dir="rtl" lang="fa">
<head>
<meta charset="utf-8">
<title>TradingAgents Report</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 گزارش TradingAgents</h1>
    <div class="meta">
      <div>نماد / نمادها: <b>{html.escape(ticker)}</b></div>
      <div>حالت اجرا: <b>{html.escape(mode)}</b></div>
      <div>تاریخ / بازه: <b>{html.escape(date_text)}</b></div>
      <div>زمان تولید گزارش: <b>{html.escape(generated)}</b></div>
    </div>
    <div>
      <span class="badge">نتیجه: {html.escape(value_of('rating'))}</span>
      <span class="badge">اقدام: {html.escape(value_of('action'))}</span>
    </div>
  </div>
  <table class="keys">{key_html}
  </table>
  {sections_html}
</div>
</body>
</html>
"""


def render_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path) -> bool:
    """Render the HTML page to PDF with Chromium; False when it cannot."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.goto(html_path.absolute().as_uri(), wait_until="domcontentloaded")
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
            )
            browser.close()
        return pdf_path.exists()
    except Exception:
        return False


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

_KEY_FIELDS = ("rating", "action", "entry", "exit", "target", "stop", "confidence", "horizon")


def build_report(home: str | pathlib.Path, out_dir: str | pathlib.Path,
                 ticker: str = "—", mode: str = "", date_text: str = "",
                 logs_dir: str | pathlib.Path | None = None) -> dict:
    """Build the report for the newest run; return its summary.

    Writes ``report.html``, ``report.pdf`` and ``report.json`` into
    ``out_dir``.  A run that produced no report tree yields a report whose
    fields say so, rather than a plausible-looking one built from leftovers.

    ``logs_dir`` is where the engine wrote this run's reports; it defaults to
    ``home/logs``, which is where upstream keeps them.
    """
    home = pathlib.Path(home)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if logs_dir is None:
        logs_dir = home / "logs"
    tree = find_report_tree(pathlib.Path(logs_dir))

    fields: dict[str, str] = dict.fromkeys(_KEY_FIELDS, "")
    fields["summary"] = ""
    fields["decision"] = ""
    sections: list[dict[str, str]] = []
    source = ""

    if tree is not None:
        sections = collect_sections(tree)
        complete = report_body(tree, sections)
        decision = ""
        trader = ""
        for folder, filename, _title in _TREE_SECTIONS:
            path = tree / folder / filename
            if filename == "decision.md":
                decision = _read(path)
            elif filename == "trader.md":
                trader = _read(path)
        if not decision:
            for section in sections:
                if section["title"] == "تصمیم نهایی":
                    decision = section["body"]
        if not trader:
            for section in sections:
                if section["title"] == "معامله‌گر":
                    trader = section["body"]
        fields = extract_fields(decision, trader, complete)
        source = "report"
    else:
        # A backtest writes no report tree; its memory log holds one decision
        # per cell, and that is the sweep's only trace.
        log = find_memory_log(pathlib.Path(logs_dir))
        if log is not None:
            sections = sections_from_memory_log(log)
            if sections:
                complete = "\n\n".join(s["body"] for s in sections)
                fields = extract_fields("", "", complete)
                fields["decision"] = ""
                fields["summary"] = (
                    f"{len(sections)} تصمیم در این بک‌تست ثبت شد."
                    if len(sections) > 1 else "یک تصمیم در این بک‌تست ثبت شد."
                )
                source = "memory_log"

    for key in _KEY_FIELDS:
        fields[key] = (fields.get(key) or "").strip() or "—"

    if not fields.get("summary"):
        # Fall back to the decision's own text, then to the first section.
        decision_text = fields.get("decision", "").strip()
        if decision_text:
            fields["summary"] = decision_text[:1800]
        elif sections:
            fields["summary"] = sections[0]["body"][:1200]

    html_doc = render_html(fields, sections, ticker, mode, date_text)
    html_path = out_dir / "report.html"
    html_path.write_text(html_doc, encoding="utf-8")

    pdf_ok = render_pdf(html_path, out_dir / "report.pdf")

    report = {
        "ticker": ticker,
        "mode": mode,
        "date": date_text,
        "generated": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "rating": fields["rating"],
        "action": fields["action"],
        "entry": fields["entry"],
        "exit": fields["exit"],
        "target": fields["target"],
        "stop": fields["stop"],
        "confidence": fields["confidence"],
        "horizon": fields["horizon"],
        "summary": fields["summary"][:2500],
        "decision": fields["decision"][:3000],
        "has_pdf": pdf_ok,
        "status": "ok" if source == "report" else ("memory_log" if source else "no_report"),
        "sections": len(sections),
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def logs_dir_from_env() -> pathlib.Path:
    """Where the engine wrote this run's reports.

    Upstream keeps them under ``results_dir`` (``~/.tradingagents/logs`` by
    default); the workflow points ``TRADINGAGENTS_RESULTS_DIR`` at its own
    runtime directory, so the tree has to be read from there.
    """
    results = os.environ.get("TRADINGAGENTS_RESULTS_DIR")
    if results:
        return pathlib.Path(results).expanduser()
    home = os.environ.get("TRADINGAGENTS_HOME", "~/.tradingagents")
    return pathlib.Path(home).expanduser() / "logs"


def main() -> int:
    home = os.path.expanduser(os.environ.get("TRADINGAGENTS_HOME", "~/.tradingagents"))
    out = pathlib.Path(os.environ.get("REPORT_OUT_DIR", ".runtime/results"))
    ticker = os.environ.get("TICKER") or os.environ.get("TICKERS") or "—"
    mode = os.environ.get("REQUEST_MODE", "")
    date_text = (
        os.environ.get("ANALYSIS_DATE")
        or f'{os.environ.get("START_DATE", "")} → {os.environ.get("END_DATE", "")}'.strip(" →")
        or "—"
    )
    report = build_report(home, out, ticker, mode, date_text,
                          logs_dir=logs_dir_from_env())
    print(f"Report generated. PDF exists: {report['has_pdf']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
