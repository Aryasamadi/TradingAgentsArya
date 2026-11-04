"""Tests for the live agent stream (live_stream.py)."""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

import pytest

import live_stream

# ---- helpers ---------------------------------------------------------------


class FakeClient:
    """A Telegram client that records the calls instead of making them."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self._next_id = 1000

    def call(self, method: str, payload: dict) -> dict:
        if self.fail:
            raise live_stream.TelegramError("boom")
        self.calls.append((method, payload))
        if method == "sendMessage":
            self._next_id += 1
            return {"result": {"message_id": self._next_id}}
        return {"result": True}

    def texts(self, method: str) -> list[str]:
        return [payload["text"] for name, payload in self.calls if name == method]


def write_checkpoint(db_path: Path, namespace: str, checkpoint_id: str, messages: list) -> None:
    """Put one checkpoint into a LangGraph checkpoint database."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(db_path))
    try:
        SqliteSaver(conn).setup()
        serde = JsonPlusSerializer()
        ctype, blob = serde.dumps_typed(
            {"channel_values": {"messages": messages}, "channel_versions": {}}
        )
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("thread", namespace, checkpoint_id, None, ctype, blob, serde.dumps_typed({"step": 1})[1]),
        )
        conn.commit()
    finally:
        conn.close()


def ai(text: str, identifier: str) -> dict:
    return {"id": identifier, "type": "ai", "content": text}


def tool(text: str, identifier: str) -> dict:
    return {"id": identifier, "type": "tool", "name": "get_news", "content": text}


# ---- checkpoint reader -----------------------------------------------------


def test_reader_returns_only_the_agents_own_messages(tmp_path):
    db = tmp_path / "BTC.db"
    write_checkpoint(
        db,
        "Sentiment Analyst:abc",
        "00000000-0000-6000-8000-000000000001",
        [
            {"id": "m0", "type": "human", "content": "BTC"},
            tool("a long news dump", "m1"),
            ai("گزارش احساسات بازار", "m2"),
        ],
    )

    reader = live_stream.CheckpointReader(str(db))
    entries = reader.poll()

    assert entries == [("تحلیل‌گر احساسات", "گزارش احساسات بازار")]


def test_reader_reports_a_message_once(tmp_path):
    db = tmp_path / "BTC.db"
    write_checkpoint(
        db,
        "News Analyst:abc",
        "00000000-0000-6000-8000-000000000001",
        [ai("خبر اول", "m1")],
    )

    reader = live_stream.CheckpointReader(str(db))

    assert reader.poll() == [("تحلیل‌گر اخبار", "خبر اول")]
    assert reader.poll() == []


def test_reader_reports_the_new_messages_of_a_growing_checkpoint(tmp_path):
    db = tmp_path / "BTC.db"
    write_checkpoint(
        db,
        "Market Analyst:abc",
        "00000000-0000-6000-8000-000000000001",
        [ai("تحلیل بازار", "m1")],
    )
    reader = live_stream.CheckpointReader(str(db))
    assert reader.poll() == [("تحلیل‌گر بازار", "تحلیل بازار")]

    write_checkpoint(
        db,
        "Market Analyst:abc",
        "00000000-0000-6000-8000-000000000002",
        [ai("تحلیل بازار", "m1"), ai("جمع‌بندی بازار", "m2")],
    )

    assert reader.poll() == [("تحلیل‌گر بازار", "جمع‌بندی بازار")]


def test_reader_survives_a_missing_database(tmp_path):
    reader = live_stream.CheckpointReader(str(tmp_path / "nothing.db"))
    assert reader.poll() == []


def test_reader_survives_a_database_without_the_table(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    reader = live_stream.CheckpointReader(str(db))
    assert reader.poll() == []


# ---- the growing message ---------------------------------------------------


def test_the_message_is_sent_once_and_then_grows():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")

    message.add([("تحلیل‌گر بازار", "اولین گفتگو")])
    message.flush()

    message.add([("پژوهشگر صعودی", "دومین گفتگو")])
    message.flush()

    assert [name for name, _ in client.calls] == ["sendMessage", "editMessageText"]
    sent = client.texts("sendMessage")[0]
    edited = client.texts("editMessageText")[0]
    assert "اولین گفتگو" in sent
    assert "اولین گفتگو" in edited  # the block grows, it does not replace
    assert "دومین گفتگو" in edited
    assert edited.startswith("📊 <b>BTC</b> · <i>thinking…</i>\n<blockquote expandable>")
    assert edited.endswith("</blockquote>")


def test_the_text_sits_in_an_expandable_blockquote():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.add([("تحلیل‌گر", "سلام")])
    message.flush()

    assert "<blockquote expandable>" in client.texts("sendMessage")[0]


def test_html_in_the_agents_text_is_escaped():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.add([("تحلیل‌گر", "<b>bold</b> & <script>")])
    message.flush()

    text = client.texts("sendMessage")[0]
    assert html.escape("<b>bold</b> & <script>") in text
    assert "<script>" not in text


def test_a_full_block_continues_in_the_next_message():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC", max_chars=500)

    # what fits stays in the first message and grows it
    message.add([("تحلیل‌گر", "x" * 200)])
    message.flush()
    message.add([("تحلیل‌گر", "y" * 100)])
    message.flush()

    # what does not fit any more continues in the next message
    message.add([("تحلیل‌گر", "z" * 400)])
    message.flush()

    sends = client.texts("sendMessage")
    assert len(sends) == 2
    assert "x" * 200 in sends[0]
    # the second part grew the first message
    assert "y" * 100 in client.texts("editMessageText")[0]
    assert "z" * 400 in sends[1]
    # the first message was edited while it still had room
    assert len(client.texts("editMessageText")) == 1


def test_a_failed_call_keeps_the_content_queued():
    client = FakeClient(fail=True)
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.add([("تحلیل‌گر", "مهم")])

    with pytest.raises(live_stream.TelegramError):
        message.flush()

    assert "مهم" in message.pending


def test_the_typing_action_is_sent():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.action()

    method, payload = client.calls[0]
    assert method == "sendChatAction"
    assert payload["action"] == "typing"
    assert payload["chat_id"] == 42


# ---- names and paths -------------------------------------------------------


def test_agent_labels_are_readable():
    assert live_stream.agent_label("Sentiment Analyst:abc") == "تحلیل‌گر احساسات"
    assert live_stream.agent_label("Bull Researcher:abc") == "پژوهشگر صعودی"
    assert live_stream.agent_label("") == "گفتگوی تیم"
    assert live_stream.agent_label("Some New Agent") == "Some New Agent"


def test_the_checkpoint_path_matches_the_package_naming(tmp_path):
    from tradingagents.dataflows.symbols import safe_ticker_component

    ticker = "BTC"
    expected = tmp_path / "checkpoints" / f"{safe_ticker_component(ticker).upper()}.db"
    assert live_stream.checkpoint_path(str(tmp_path), ticker) == str(expected)


def test_a_ticker_that_is_not_a_path_is_refused():
    assert live_stream.checkpoint_path("/tmp", "../etc/passwd") == ""
    assert live_stream.checkpoint_path("/tmp", "") == ""


# ---- the watcher -----------------------------------------------------------


def test_the_watcher_publishes_what_the_agents_say(tmp_path):
    db = tmp_path / "BTC.db"
    write_checkpoint(
        db,
        "Bull Researcher:abc",
        "00000000-0000-6000-8000-000000000001",
        [ai("من bullish هستم", "m1")],
    )
    client = FakeClient()
    polls = {"count": 0}

    def should_stop():
        polls["count"] += 1
        return polls["count"] > 2

    live_stream.watch(
        str(db),
        42,
        "token",
        "BTC",
        "request-123",
        client=client,
        should_stop=should_stop,
        interval=0,
        edit_seconds=0,
        action_seconds=0,
    )

    sent = client.texts("sendMessage")
    assert sent, "the watcher published nothing"
    # the card with the ticker goes out before the first agent speaks
    assert sent[0].startswith("📊 <b>BTC</b> · <i>thinking…</i>")
    edited = client.texts("editMessageText")
    assert edited, "the watcher published nothing"
    assert "من bullish هستم" in edited[-1]
    assert "BTC" in edited[-1]


# ---- markdown as Telegram HTML --------------------------------------------


def test_a_markdown_heading_becomes_bold_text():
    assert live_stream.render_blocks("## گزارش بازار") == ["<b>گزارش بازار</b>"]


def test_a_horizontal_rule_disappears():
    blocks = live_stream.render_blocks("متن\n\n---\n\nمتن دیگر")
    assert "---" not in "\n".join(blocks)
    assert len(blocks) == 2


def test_a_table_becomes_readable_lines():
    table = "| سیگنال | جهت |\n|---|---|\n| معرفی محصول | مثبت |"
    rendered = "\n".join(live_stream.render_blocks(table))
    assert "|" not in rendered
    assert "<b>سیگنال:</b> معرفی محصول" in rendered
    assert "<b>جهت:</b> مثبت" in rendered


def test_a_table_with_more_cells_than_headers_stays_readable():
    table = "| بخش | نکته |\n|---|---|\n| ETF | نماینده | BTC | بورس |"
    rendered = "\n".join(live_stream.render_blocks(table))
    assert "|" not in rendered
    assert "بورس" in rendered


def test_a_code_block_becomes_a_pre_block():
    assert live_stream.render_blocks('```python\nprint("hi")\n```') == ['<pre>print("hi")</pre>']


def test_inline_code_and_links_are_rendered():
    rendered = live_stream.render_inline("یک `code` و یک [لینک](https://example.com)")
    assert "<code>code</code>" in rendered
    assert '<a href="https://example.com">لینک</a>' in rendered


def test_no_markdown_source_leaks_into_the_rendered_text():
    source = """**درش:** خوب

## بخش

- یکی
- دویی

| ستون | دیگر |
|---|---|
| یک | دو |

```
code
```
"""
    rendered = "\n".join(live_stream.render_blocks(source))
    assert "**" not in rendered
    assert not rendered.lstrip().startswith("#")
    assert "|---" not in rendered
    assert "```" not in rendered
    assert rendered.count("<b>") == rendered.count("</b>")


def test_a_long_text_is_split_without_breaking_the_tags():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC", max_chars=500)
    message.add([("تحلیل‌گر", "یک پاراگراف بلند. " * 200)])
    message.flush()

    sends = client.texts("sendMessage")
    assert len(sends) > 1
    for text in sends:
        assert len(text) <= 4096
        assert text.count("<b>") == text.count("</b>")
        assert text.endswith("</blockquote>")


def test_the_header_names_the_ticker_and_the_state():
    assert live_stream.build_header("btc") == "📊 <b>BTC</b> · <i>thinking…</i>"
    assert live_stream.build_header("BTC", "done") == "📊 <b>BTC</b> · <i>done</i>"


def test_the_message_starts_with_the_thinking_card():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.start()
    assert client.texts("sendMessage")[0] == (
        "📊 <b>BTC</b> · <i>thinking…</i>\n<blockquote expandable></blockquote>"
    )


def test_the_message_ends_as_done():
    client = FakeClient()
    message = live_stream.LiveMessage(client, 42, "BTC")
    message.start()
    message.add([("تحلیل‌گر", "سلام")])
    message.flush()
    message.finish()
    assert client.texts("editMessageText")[-1].startswith("📊 <b>BTC</b> · <i>done</i>")
