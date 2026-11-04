"""Stream the agents' live conversation from the engine into the chat.

The engine runs the TradingAgents graph on a GitHub runner. Its stdout carries
only setup noise: the framework prints the agents' messages in its debug mode
alone, and the engine does not use that mode. The graph's checkpoint, on the
other hand, is written after every step and keeps the message history of every
agent, so a read-only watcher can follow the conversation while the run is
still going.

The watcher changes nothing about the run. It opens the checkpoint database
read-only, picks up the messages the agents have produced so far, and publishes
them to the chat that asked for the analysis: one message that keeps growing
inside an expandable blockquote, with a typing indicator while it is busy.
When the block no longer fits in a Telegram message, the rest continues in the
next one.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request

TELEGRAM_API = "https://api.telegram.org"

# Telegram takes 4096 characters per message; the block stays below that so
# the header and the tags always fit.
MAX_BLOCK_CHARS = 2900
POLL_SECONDS = 3.0
# Telegram allows about 30 edits and 20 actions a minute; stay well below.
EDIT_SECONDS = 10.0
ACTION_SECONDS = 5.0

# The same shape the package accepts for a ticker in a path.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")

AGENT_LABELS = {
    "market analyst": "تحلیل‌گر بازار",
    "sentiment analyst": "تحلیل‌گر احساسات",
    "news analyst": "تحلیل‌گر اخبار",
    "fundamentals analyst": "تحلیل‌گر بنیادهای مالی",
    "bull researcher": "پژوهشگر صعودی",
    "bear researcher": "پژوهشگر نزولی",
    "bull analyst": "تحلیل‌گر صعودی",
    "bear analyst": "تحلیل‌گر نزولی",
    "risky analyst": "تحلیل‌گر پرریسک",
    "safe analyst": "تحلیل‌گر کم‌ریسک",
    "neutral analyst": "تحلیل‌گر خنثی",
    "trader": "معامله‌گر",
    "trader team": "تیم معامله‌گر",
    "fund manager": "مدیر پورتفویو",
    "portfolio manager": "مدیر پورتفویو",
    "risk manager": "مدیر ریسک",
    "risk management": "مدیریت ریسک",
}


class TelegramError(RuntimeError):
    """A Telegram call failed."""


class TelegramRateLimited(TelegramError):
    """Telegram asked us to slow down."""

    def __init__(self, retry_after: int):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = int(retry_after)


class TelegramClient:
    """A small Telegram Bot API client (the standard library is enough)."""

    def __init__(self, token: str, timeout: float = 20.0):
        self.token = token
        self.timeout = timeout

    def call(self, method: str, payload: dict) -> dict:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
            if error.code == 429:
                retry_after = parsed.get("parameters", {}).get("retry_after", 5)
                raise TelegramRateLimited(int(retry_after)) from error
            raise TelegramError(f"{method} failed with HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TelegramError(f"{method} failed: {error}") from error


def agent_label(namespace: str) -> str:
    """The readable name of the agent that owns a checkpoint namespace."""
    name = str(namespace or "").split(":", 1)[0].strip()
    if not name:
        return "گفتگوی تیم"
    return AGENT_LABELS.get(name.lower(), name)


def message_role(message) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", "") or getattr(message, "role", "") or "")


def message_id(message) -> str:
    if isinstance(message, dict):
        return str(message.get("id") or "")
    return str(getattr(message, "id", "") or "")


def message_text(message) -> str:
    """The plain text of one checkpoint message."""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        content = " ".join(part for part in parts if part)
    return str(content or "").strip()


class CheckpointReader:
    """Read the agents' messages out of a LangGraph checkpoint database.

    The database is opened read-only; the engine keeps writing to it while the
    watcher reads, so a locked database is a normal condition and not an error.
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._seen: dict[str, int] = {}  # namespace -> messages already read
        self._reported: set[str] = set()  # message ids already reported
        self._cache: dict[str, tuple] = {}  # namespace -> (checkpoint id, channels)

    def _channels(self, checkpoint_id: str, ctype: str, blob: bytes) -> dict:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        data = JsonPlusSerializer().loads_typed((ctype, blob))
        return data.get("channel_values") or data

    def poll(self) -> list[tuple[str, str]]:
        """The agent messages that appeared since the last poll."""
        if not os.path.exists(self.db_path):
            return []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "SELECT checkpoint_ns, checkpoint_id, type, checkpoint FROM checkpoints "
                "ORDER BY checkpoint_id"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        latest: dict[str, tuple] = {}
        for namespace, checkpoint_id, ctype, blob in rows:
            latest[str(namespace or "")] = (str(checkpoint_id), ctype, blob)

        found: list[tuple[str, str]] = []
        # Checkpoint ids sort by time, so the namespaces are visited in the
        # order the agents spoke.
        for namespace in sorted(latest, key=lambda name: latest[name][0]):
            checkpoint_id, ctype, blob = latest[namespace]
            cached = self._cache.get(namespace)
            if cached and cached[0] == checkpoint_id:
                continue
            try:
                channels = self._channels(checkpoint_id, ctype, blob)
            except Exception:
                continue
            self._cache[namespace] = (checkpoint_id, channels)

            messages = channels.get("messages") or []
            start = self._seen.get(namespace, 0)
            for message in messages[start:]:
                if message_role(message) != "ai":
                    continue
                text = message_text(message)
                if not text:
                    continue
                identifier = message_id(message)
                if identifier and identifier in self._reported:
                    continue
                if identifier:
                    self._reported.add(identifier)
                found.append((agent_label(namespace), text))
            self._seen[namespace] = len(messages)
        return found


# ---- markdown as Telegram HTML --------------------------------------------

_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+(.*)$")
_RULE_RE = re.compile(r"^([-*_])(?:\s*\1){2,}$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def render_inline(text: str) -> str:
    """The inline markdown of a text as Telegram HTML."""
    parts: list[str] = []
    for chunk in re.split(r"(`[^`]+`)", str(text)):
        if len(chunk) > 2 and chunk.startswith("`") and chunk.endswith("`"):
            parts.append(f"<code>{html.escape(chunk[1:-1].strip(), quote=False)}</code>")
            continue
        parts.append(_render_span(chunk))
    return "".join(parts)


def _render_span(text: str) -> str:
    """Escape a plain span and apply the inline markdown of it."""
    out = html.escape(text, quote=False)

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return label
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    out = _LINK_RE.sub(link, out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"__([^_]+)__", r"<b>\1</b>", out)
    out = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", out)
    return out


def render_blocks(text: str) -> list[str]:
    """Render markdown into Telegram-HTML blocks.

    Every block is a complete piece of HTML on its own, and every line inside
    a block carries balanced tags, so a block can be sent or split without
    breaking the markup.
    """
    lines = str(text or "").splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if _CODE_FENCE_RE.match(line):
            fence = stripped[:3]
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith(fence):
                code.append(lines[index])
                index += 1
            index += 1
            content = "\n".join(code).strip("\n")
            if content:
                blocks.append(f"<pre>{html.escape(content, quote=False)}</pre>")
            continue

        if _RULE_RE.match(stripped):
            index += 1
            continue

        if stripped.startswith("|"):
            table: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table.append(lines[index].strip())
                index += 1
            rendered = _render_table(table)
            if rendered:
                blocks.append(rendered)
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            blocks.append(f"<b>{render_inline(heading.group(2).strip())}</b>")
            index += 1
            continue

        if stripped.startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(f"<blockquote>{render_inline(' '.join(quoted))}</blockquote>")
            continue

        if _LIST_RE.match(stripped):
            items: list[str] = []
            while index < len(lines):
                current = lines[index]
                match = _LIST_RE.match(current.strip())
                if not match:
                    break
                depth = (len(current) - len(current.lstrip())) // 2
                marker = "• " if depth == 0 else "↳ "
                items.append(f"{marker}{render_inline(match.group(2))}")
                index += 1
            blocks.append("\n".join(items))
            continue

        if stripped:
            paragraph = [stripped]
            index += 1
            while index < len(lines):
                following = lines[index].strip()
                if (
                    not following
                    or following.startswith(("#", ">", "|", "```", "~~~"))
                    or _LIST_RE.match(following)
                    or _RULE_RE.match(following)
                ):
                    break
                paragraph.append(following)
                index += 1
            blocks.append(render_inline(" ".join(paragraph)))
            continue

        index += 1

    return [block for block in blocks if block.strip()]


def _table_cells(row: str) -> list[str]:
    """The cells of one markdown table row, in their columns."""
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """A row of dashes is the separator of a markdown table, not data."""
    filled = [cell for cell in cells if cell]
    return bool(filled) and all(_SEPARATOR_CELL_RE.match(cell) for cell in filled)


def _render_table(rows: list[str]) -> str:
    """Render a markdown table as readable lines; Telegram has no tables."""
    parsed = [_table_cells(row) for row in rows]
    if not parsed:
        return ""
    has_header = len(parsed) > 1 and _is_separator_row(parsed[1])
    header = parsed[0] if has_header else []
    data = [cells for cells in parsed[1:] if not _is_separator_row(cells)] if has_header else []
    if not has_header:
        data = [cells for cells in parsed if not _is_separator_row(cells)]
    lines: list[str] = []
    for cells in data:
        pieces: list[str] = []
        for position, value in enumerate(cells):
            if not value:
                continue
            label = header[position] if position < len(header) else ""
            text = render_inline(value)
            if label:
                pieces.append(f"<b>{render_inline(label)}:</b> {text}")
            else:
                pieces.append(text)
        if pieces:
            lines.append("• " + " · ".join(pieces))
    return "\n".join(lines)


def _tags_balanced(text: str) -> bool:
    """Whether every tag of the text is closed."""
    stack: list[str] = []
    for match in re.finditer(r"<(/?)([a-zA-Z]+)[^>]*>", text):
        closing, name = match.group(1), match.group(2)
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif not match.group(0).endswith("/>"):
            stack.append(name)
    return not stack


def _cut_balanced(text: str, limit: int) -> str:
    """The longest prefix that fits in `limit` and whose tags are balanced."""
    if len(text) <= limit:
        return text
    newline = text.rfind("\n", 0, limit + 1)
    if newline > 0 and _tags_balanced(text[:newline]):
        return text[:newline]
    stack: list[str] = []
    index = 0
    cut = 0
    while index < len(text):
        if text[index] == "<":
            end = text.find(">", index)
            if end == -1:
                break
            tag = text[index : end + 1]
            if tag.startswith("</"):
                name = tag[2:-1].strip()
                if stack and stack[-1] == name:
                    stack.pop()
            elif not tag.endswith("/>") and not tag.startswith("<!"):
                parts = tag[1:-1].split()
                stack.append(parts[0] if parts else "")
            index = end + 1
        else:
            index += 1
        if not stack and index <= limit:
            cut = index
    if cut <= 0:
        cut = text.rfind("<", 0, limit + 1)
        if cut <= 0:
            cut = limit
    return text[:cut]


class LiveMessage:
    """One Telegram message that grows while the run goes on.

    The header names the ticker and the state of the run. The conversation
    sits in an expandable blockquote, so the header stays visible and the text
    can be folded away. The agents write markdown, so their words are rendered
    into Telegram HTML first: a heading becomes bold text, a table becomes
    readable lines and a horizontal rule disappears, instead of the markdown
    source leaking into the chat. When the block no longer fits in one
    Telegram message, the rest continues in the next one.
    """

    def __init__(self, client, chat_id, ticker: str, max_chars: int = MAX_BLOCK_CHARS):
        self.client = client
        self.chat_id = chat_id
        self.ticker = ticker
        self.state = "thinking"
        self.header = build_header(ticker, self.state)
        self.max_chars = int(max_chars)
        self.message_id: int | None = None
        self._body = ""
        self._pending: list[str] = []

    @property
    def pending(self) -> str:
        return "\n".join(self._pending)

    @property
    def body(self) -> str:
        """The text the current message carries."""
        return self._body

    def _render(self, body: str) -> str:
        return f"{self.header}\n<blockquote expandable>{body}</blockquote>"

    def _send(self, body: str) -> int:
        response = self.client.call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": self._render(body),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return int(response["result"]["message_id"])

    def _edit(self, body: str) -> None:
        if self.message_id is None:
            return
        self.client.call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": self._render(body),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def start(self) -> None:
        """Publish the empty card, so the chat shows the run right away."""
        if self.message_id is not None:
            return
        self.message_id = self._send(self._body)

    def add(self, entries) -> None:
        """Queue agent messages for the next publish."""
        for label, text in entries:
            if self._pending:
                self._pending.append("")
            self._pending.append(f"<b>{html.escape(label, quote=False)}</b>")
            self._pending.extend(render_blocks(text))

    def flush(self) -> None:
        """Publish what is queued.

        The content that could not be published stays queued, so a rate limit
        or a temporary network failure loses nothing.
        """
        if not self._pending:
            return

        # Work on a copy: the queue is only shortened after a successful call.
        pending = list(self._pending)
        body = self._body
        original = self._body
        chunks: list[str] = []
        while pending:
            room = self.max_chars - len(body) - (2 if body else 0)
            if room < 200:
                # The current message is full; the rest goes to the next one.
                chunks.append(body)
                body = ""
                continue
            head = pending[0]
            if not head:
                pending.pop(0)
                if body:
                    body += "\n"
                continue
            if len(head) > room:
                # Cut the block at a boundary that keeps the tags balanced.
                piece = _cut_balanced(head, room)
                if not piece:
                    chunks.append(body)
                    body = ""
                    continue
                body = (body + "\n\n" + piece) if body else piece
                pending[0] = head[len(piece) :]
                chunks.append(body)
                body = ""
                continue
            body = (body + "\n\n" + head) if body else head
            pending.pop(0)
        chunks.append(body)

        for index, text in enumerate(chunks):
            if index:
                # The rest of the conversation continues in the next message.
                self.message_id = None
            elif text == original:
                # Nothing new for the message that is already open.
                continue
            if index or text:
                self._publish(text)
                self._body = text
        self._pending = pending

    def _publish(self, body: str) -> None:
        if self.message_id is None:
            self.message_id = self._send(body)
        else:
            self._edit(body)

    def finish(self) -> None:
        """Mark the run as finished and publish the last of the conversation."""
        self.state = "done"
        self.header = build_header(self.ticker, self.state)
        self.flush()
        if self.message_id is not None:
            self._edit(self._body)

    def action(self) -> None:
        """Show the typing indicator."""
        self.client.call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})


def checkpoint_path(cache_dir: str, ticker: str) -> str:
    """The checkpoint database of a ticker, mirroring the package's naming."""
    if not ticker or not _TICKER_PATH_RE.match(ticker):
        return ""
    return os.path.join(cache_dir, "checkpoints", f"{ticker.upper()}.db")


def build_header(ticker: str, state: str = "thinking") -> str:
    """The header of the live card: the ticker first, then the state."""
    title = html.escape(str(ticker or "").upper())
    label = "thinking…" if state == "thinking" else "done"
    if not title:
        return f"📊 <i>{label}</i>"
    return f"📊 <b>{title}</b> · <i>{label}</i>"


def watch(
    db_path: str,
    chat_id: int,
    token: str,
    ticker: str,
    request_id: str = "",
    client=None,
    should_stop=None,
    interval: float = POLL_SECONDS,
    edit_seconds: float = EDIT_SECONDS,
    action_seconds: float = ACTION_SECONDS,
) -> None:
    """Follow the agents until the run ends."""
    client = client or TelegramClient(token)
    reader = CheckpointReader(db_path)
    message = LiveMessage(client, chat_id, ticker)
    should_stop = should_stop or (lambda: False)

    # The card goes out right away, so the chat shows the run while the first
    # agent is still thinking.
    with contextlib.suppress(TelegramError):
        message.start()
    print(f"live stream: following {ticker} (request {request_id})", flush=True)

    last_edit = 0.0
    last_action = 0.0
    while not should_stop():
        try:
            entries = reader.poll()
        except Exception:
            entries = []
        if entries:
            message.add(entries)

        now = time.monotonic()
        if message.pending and now - last_edit >= edit_seconds:
            try:
                message.flush()
                last_edit = now
                print(f"live stream: published up to {len(message.body)} characters", flush=True)
            except TelegramRateLimited:
                last_edit = now - edit_seconds + 1
            except TelegramError as error:
                print(f"live stream: {error}", flush=True)
        if now - last_action >= action_seconds:
            with contextlib.suppress(TelegramError):
                message.action()
            last_action = now
        time.sleep(interval)

    with contextlib.suppress(TelegramError):
        message.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream the agents' conversation to Telegram.")
    parser.add_argument("--cache-dir", default=".runtime/cache/data")
    parser.add_argument("--ticker", default="")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--interval", type=float, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    if not args.token:
        print("TELEGRAM_BOT_TOKEN is missing", file=sys.stderr)
        return 2

    db_path = checkpoint_path(args.cache_dir, args.ticker)
    if not db_path:
        print(f"Unsupported ticker: {args.ticker!r}", file=sys.stderr)
        return 2

    stopping = {"flag": False}

    def stop(*_args):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        watch(
            db_path,
            int(args.chat_id),
            args.token,
            args.ticker,
            args.request_id,
            interval=args.interval,
            should_stop=lambda: stopping["flag"],
        )
    except Exception as error:  # the run must never depend on the watcher
        print(f"live stream stopped: {error}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
