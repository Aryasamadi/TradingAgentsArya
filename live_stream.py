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


class LiveMessage:
    """One Telegram message that grows while the run goes on.

    The text sits in an expandable blockquote: the header stays visible and the
    conversation can be folded away. When the block no longer fits in one
    Telegram message, the rest continues in the next one.
    """

    def __init__(self, client, chat_id: int, header: str, max_chars: int = MAX_BLOCK_CHARS):
        self.client = client
        self.chat_id = chat_id
        self.header = header
        self.max_chars = int(max_chars)
        self.message_id: int | None = None
        self._body = ""
        self._pending = ""

    @property
    def pending(self) -> str:
        return self._pending

    @property
    def body(self) -> str:
        """The text the current message carries."""
        return self._body

    def _render(self, body: str) -> str:
        return f"{self.header}\n<blockquote expandable>{html.escape(body)}</blockquote>"

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

    def add(self, entries) -> None:
        """Queue agent messages for the next publish."""
        for label, text in entries:
            self._pending += f"\n\n— {label} —\n{text}"

    def flush(self) -> None:
        """Publish what is queued.

        The content that could not be published stays queued, so a rate limit
        or a temporary network failure loses nothing.
        """
        pending = self._pending.strip("\n")
        if not pending:
            return
        consumed = 0
        while pending:
            room = self.max_chars - len(self._body) - (2 if self._body else 0)
            if room < 200:
                self.message_id = None
                self._body = ""
                continue
            head = pending[:room]
            self._body = (self._body + "\n\n" + head) if self._body else head
            if self.message_id is None:
                self.message_id = self._send(self._body)
            else:
                self._edit(self._body)
            consumed += len(head)
            pending = pending[room:].lstrip("\n")
            if pending:
                # The current message is full; the rest goes to the next one.
                self.message_id = None
                self._body = ""
        self._pending = self._pending[consumed:].lstrip("\n")

    def action(self) -> None:
        """Show the typing indicator."""
        self.client.call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})


def checkpoint_path(cache_dir: str, ticker: str) -> str:
    """The checkpoint database of a ticker, mirroring the package's naming."""
    if not ticker or not _TICKER_PATH_RE.match(ticker):
        return ""
    return os.path.join(cache_dir, "checkpoints", f"{ticker.upper()}.db")


def build_header(ticker: str, request_id: str = "") -> str:
    title = html.escape(str(ticker or ""))
    request = html.escape(str(request_id or "")[:10])
    header = f"🧠 <b>گفتگوی ایجنت‌ها</b> · <b>{title}</b>"
    if request:
        header += f" · <code>{request}</code>"
    return header + "\n<i>در حال اجرا — این پیام زنده به‌روز می‌شود</i>"


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
    message = LiveMessage(client, chat_id, build_header(ticker, request_id))
    should_stop = should_stop or (lambda: False)

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
        message.flush()


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
