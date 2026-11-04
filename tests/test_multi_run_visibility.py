"""Several runs in flight must all be visible.

Three things used to hide the second, third and later run:

* the run confirmation was written into the single UI message of the chat, so
  the next dispatch replaced the announcement of the previous one;
* only the "queued" and "in_progress" statuses were read from the API, while
  GitHub reports a run that waits behind a concurrency group as "pending";
* the list of active runs was not scoped to the chat, so it showed (and could
  cancel) runs of other users.

The tests below pin the fixed behaviour. They only touch the controller's own
helpers and never reach the network.
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The controller opens its state database while it is imported, so point it at
# a throwaway file before the import happens.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="bot-visibility-"))
os.environ["BOT_STATE_PATH"] = str(_STATE_DIR / "bot_state.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("BOT_GITHUB_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "999")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "999")

import bot as controller  # noqa: E402  (the state path above must be set first)


@contextmanager
def _patched(obj, name, value):
    """Set an attribute for the duration of the block, then put it back."""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _api_run(run_id, request_id, status, created="2026-11-04T10:00:00Z"):
    return {
        "id": run_id,
        "event": "repository_dispatch",
        "status": status,
        "display_title": f"Engine · {request_id}",
        "created_at": created,
    }


def _db_row(
    request_id,
    chat_id=5,
    ticker="AAPL",
    status="queued",
    workflow_run_id=None,
    created="2026-11-04T10:00:00Z",
):
    return {
        "request_id": request_id,
        "chat_id": chat_id,
        "mode": "analysis",
        "payload_json": json.dumps({"ticker": ticker}),
        "model_id": "model-1",
        "status": status,
        "conclusion": "",
        "notified": 0,
        "created_at": created,
        "updated_at": created,
        "workflow_run_id": workflow_run_id,
    }


def test_fetch_active_runs_reads_every_non_terminal_status():
    """A run waiting in the queue is "pending", not "queued"."""
    requested = []

    def fake_gh(method, path, data=None, timeout=30):
        requested.append(path)
        status = path.split("status=")[1].split("&")[0]
        runs = {
            "in_progress": [_api_run(1, "r1", "in_progress")],
            "pending": [_api_run(2, "r2", "pending")],
        }.get(status, [])
        return {"workflow_runs": runs}

    with _patched(controller, "gh", fake_gh):
        runs = controller.fetch_active_runs()

    queried = {path.split("status=")[1].split("&")[0] for path in requested}
    assert queried == set(controller.ACTIVE_STATUSES)
    assert "pending" in queried
    assert {run["id"] for run in runs} == {1, 2}


def test_every_queued_run_is_visible_with_a_queue_position():
    api_runs = [
        _api_run(11, "r1", "in_progress", "2026-11-04T10:00:00Z"),
        _api_run(12, "r2", "pending", "2026-11-04T10:01:00Z"),
        _api_run(13, "r3", "queued", "2026-11-04T10:02:00Z"),
    ]
    db_rows = [
        _db_row("r1", status="in_progress", workflow_run_id=11),
        _db_row("r2", status="pending", workflow_run_id=12),
        _db_row("r3", status="queued", workflow_run_id=13),
    ]

    entries = controller._collect_active_runs(5, api_runs, db_rows)

    assert [entry["request_id"] for entry in entries] == ["r1", "r2", "r3"]
    assert [entry["status"] for entry in entries] == ["in_progress", "pending", "queued"]
    # The running one has no place in the queue, the two waiting ones do.
    assert [entry["queue_position"] for entry in entries] == [0, 1, 2]


def test_other_chats_runs_are_hidden_and_not_cancelable():
    api_runs = [_api_run(21, "mine", "in_progress"), _api_run(22, "theirs", "in_progress")]
    db_rows = [
        _db_row("mine", chat_id=5, workflow_run_id=21),
        _db_row("theirs", chat_id=6, workflow_run_id=22),
    ]

    entries = controller._collect_active_runs(5, api_runs, db_rows)

    assert [entry["request_id"] for entry in entries] == ["mine"]
    assert entries[0]["cancelable"] is True

    admin_entries = controller._collect_active_runs(5, api_runs, db_rows, is_admin=True)
    assert {entry["request_id"] for entry in admin_entries} == {"mine", "theirs"}


def test_run_without_a_live_counterpart_is_still_listed():
    """The API lags behind the dispatch; the row must not disappear."""
    db_rows = [_db_row("r9", status="queued", workflow_run_id=None)]

    entries = controller._collect_active_runs(5, [], db_rows)

    assert [entry["request_id"] for entry in entries] == ["r9"]
    assert entries[0]["stale"] is True
    assert entries[0]["cancelable"] is False


def test_a_run_of_another_chat_cannot_be_cancelled_from_the_local_rows():
    db_rows = [_db_row("theirs", chat_id=6, status="queued", workflow_run_id=22)]

    assert controller._collect_active_runs(5, [], db_rows) == []

    admin_entries = controller._collect_active_runs(5, [], db_rows, is_admin=True)
    assert admin_entries[0]["request_id"] == "theirs"
    assert admin_entries[0]["cancelable"] is True


def test_active_runs_screen_lists_every_active_run():
    api_runs = [
        _api_run(11, "r1", "in_progress", "2026-11-04T10:00:00Z"),
        _api_run(12, "r2", "pending", "2026-11-04T10:01:00Z"),
        _api_run(13, "r3", "queued", "2026-11-04T10:02:00Z"),
    ]
    db_rows = [
        _db_row("r1", status="in_progress", workflow_run_id=11),
        _db_row("r2", status="pending", workflow_run_id=12),
        _db_row("r3", status="queued", workflow_run_id=13),
    ]
    captured = {}

    def fake_show(chat_id, text, rows, target_mid=None):
        captured["text"] = text
        captured["rows"] = rows
        return 1

    with _patched(controller, "CACHE", {"runs": api_runs, "ts": 0.0}), _patched(
        controller, "q", lambda sql, args=(): db_rows
    ), _patched(controller, "admin", lambda chat_id: False), _patched(controller, "show", fake_show):
        controller.active_runs_screen(5)

    text = captured["text"]
    assert "<b>3</b> پردازش فعال" in text
    for request_id in ("r1", "r2", "r3"):
        assert request_id in text
    assert "نوبت 1" in text
    assert "نوبت 2" in text

    cancel_buttons = [
        data for row in captured["rows"] for _, data in row if str(data).startswith("cancelw:")
    ]
    assert len(cancel_buttons) == 3


def test_run_card_is_a_message_of_its_own():
    """The card must not overwrite the shared UI message of the chat."""
    sent = {}

    def fake_send_message(chat_id, text, rows=None):
        sent["chat_id"] = chat_id
        sent["text"] = text
        sent["rows"] = rows
        return 42

    def fail_show(*args, **kwargs):
        raise AssertionError("the run card must not be written into the shared UI message")

    with _patched(controller, "send_message", fake_send_message), _patched(
        controller, "show", fail_show
    ):
        controller._send_run_card(5, "analysis", "abcdef1234567890", {"ticker": "AAPL"})

    assert sent["chat_id"] == 5
    assert "AAPL" in sent["text"]
    assert "abcdef1234" in sent["text"]
    assert any(data == "active_runs" for row in sent["rows"] for _, data in row)


def test_home_screen_counts_only_the_runs_of_this_chat():
    captured = {}
    seen_sql = []

    def fake_q1(sql, args=()):
        seen_sql.append(sql)
        return {"c": 2}

    def fake_show(chat_id, text, rows, target_mid=None):
        captured["text"] = text
        return 1

    with _patched(controller, "active_model_id", lambda: ""), _patched(
        controller, "q1", fake_q1
    ), _patched(controller, "show", fake_show):
        controller.home_screen(5)

    assert "اجرای فعال شما" in captured["text"]
    assert any("chat_id=?" in sql for sql in seen_sql)
