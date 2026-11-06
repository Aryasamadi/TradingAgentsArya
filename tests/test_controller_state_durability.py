"""The controller's state must survive a runner wipe, and a cancel must tell the truth.

Two defects motivated these tests.

* The state database ran in WAL mode, but only the main file was uploaded as
  the artifact. The write-ahead log stayed on the runner, so the writes that
  had not been folded into the main file were lost and the restored state
  looked frozen. The controller now checkpoints its writes into the file it
  uploads.
* The cancel button reported "لغو شد" even when GitHub refused the request.
  The cancel now reports what GitHub really did, and the local row is only
  marked cancelled when the run really stopped.

The tests below pin that behaviour. They only touch the controller's own
helpers and never reach the network.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import contextmanager, suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_STATE_DIR = Path(tempfile.mkdtemp(prefix="bot-durability-"))
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


@contextmanager
def _no_sleep():
    """Run without waiting; the cancel checks the run back a few times."""
    with _patched(controller, "time", type("T", (), {"sleep": staticmethod(lambda _: None)})):
        yield


def _load_restore_module():
    """Load scripts/restore_state.py as a module."""
    path = ROOT / "scripts" / "restore_state.py"
    spec = importlib.util.spec_from_file_location("restore_state_under_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_state_db(path: Path, providers=1, models=1, runs=1, tables=("providers", "models", "runs")):
    """Write a small state database that looks like the controller's."""
    conn = sqlite3.connect(path)
    try:
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        if "providers" in tables:
            conn.executemany("INSERT INTO providers VALUES (?)", [(f"p{i}",) for i in range(providers)])
        if "models" in tables:
            conn.executemany("INSERT INTO models VALUES (?)", [(f"m{i}",) for i in range(models)])
        if "runs" in tables:
            conn.executemany("INSERT INTO runs VALUES (?)", [(f"r{i}",) for i in range(runs)])
        conn.commit()
    finally:
        conn.close()


def test_state_writes_are_folded_into_the_uploaded_file():
    """The uploaded file must contain the writes on its own.

    The artifact holds one file. If the newest write only lives in the
    write-ahead log next to it, the runner wipe takes it away and the state
    of the next run is missing it.
    """
    db_path = _STATE_DIR / "checkpoint_state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()

    with _patched(controller, "DB", conn), _patched(controller, "DB_LOCK", controller.DB_LOCK):
        controller.db("INSERT INTO t VALUES ('written')")
        controller.checkpoint_state()

    # Read the main file only, with the sidecar out of reach.
    conn.close()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT v FROM t").fetchall()
    finally:
        conn.close()
    assert rows == [("written",)]


def test_checkpoint_leaves_no_unfinished_statement_behind():
    """A checkpoint must not keep the transaction open for the next commit."""
    db_path = _STATE_DIR / "checkpoint_next_write.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    with _patched(controller, "DB", conn), _patched(controller, "DB_LOCK", controller.DB_LOCK):
        controller.checkpoint_state()
        controller.db("INSERT INTO t VALUES ('a')")
        controller.db("INSERT INTO t VALUES ('b')")
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("a",), ("b",)]


def test_cancel_reports_the_error_instead_of_pretending_it_worked():
    """A refused cancel must raise, so the button can say it failed."""
    calls = []

    def fake_gh(method, path, data=None, timeout=30):
        calls.append((method, path))
        raise RuntimeError("GitHub API error 403: forbidden")

    with (
        _patched(controller, "gh", fake_gh),
        suppress(RuntimeError),
    ):
        controller.cancel_run(41836674098)
        raise AssertionError("a refused cancel must not report success")

    assert calls == [("POST", "/actions/runs/41836674098/cancel")]


def test_cancel_of_a_forbidden_run_names_the_reason():
    def fake_gh(method, path, data=None, timeout=30):
        raise RuntimeError("GitHub API error 403: forbidden")

    with _patched(controller, "gh", fake_gh):
        try:
            controller.cancel_run(41836674098)
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("a refused cancel must not report success")

    assert "اجازهٔ لغو" in message


def test_cancel_of_a_finished_run_says_so():
    def fake_gh(method, path, data=None, timeout=30):
        raise RuntimeError("GitHub API error 409: Conflict")

    with _patched(controller, "gh", fake_gh):
        try:
            controller.cancel_run(123)
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("a finished run must not report success")

    assert "قبلاً تمام شده" in message


def test_cancel_reads_the_run_back_before_it_claims_success():
    """202 only means the request was accepted."""
    seen = []

    def fake_gh(method, path, data=None, timeout=30):
        seen.append(path)
        if method == "POST":
            return {}
        return {"status": "completed", "conclusion": "cancelled"}

    with _patched(controller, "gh", fake_gh), _no_sleep():
        outcome = controller.cancel_run(555)

    assert outcome == "cancelled"
    assert seen[0] == "/actions/runs/555/cancel"
    assert seen[1] == "/actions/runs/555"


def test_cancel_that_is_still_running_is_not_called_cancelled():
    def fake_gh(method, path, data=None, timeout=30):
        if method == "POST":
            return {}
        return {"status": "in_progress", "conclusion": ""}

    with _patched(controller, "gh", fake_gh), _no_sleep():
        outcome = controller.cancel_run(556)

    assert outcome == "cancelling"


def test_a_stopped_run_is_marked_cancelled_locally():
    """The list must stop showing a run that really stopped."""
    db_path = _STATE_DIR / "mark_run.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, "
        "chat_id INTEGER, status TEXT, conclusion TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO runs VALUES ('req1', 777, 5, 'in_progress', '', '2026-11-05T00:00:00')")
    conn.commit()
    with _patched(controller, "DB", conn), _patched(controller, "DB_LOCK", controller.DB_LOCK):
        controller.mark_run_cancelled(777, "cancelled")
    row = conn.execute("SELECT status, conclusion FROM runs WHERE request_id='req1'").fetchone()
    conn.close()
    assert row == ("cancelled", "cancelled_by_user")


def test_a_run_that_is_still_stopping_keeps_a_non_terminal_status():
    """The next sync must still be able to correct the row."""
    db_path = _STATE_DIR / "mark_cancelling.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, "
        "chat_id INTEGER, status TEXT, conclusion TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO runs VALUES ('req2', 778, 5, 'in_progress', '', '2026-11-05T00:00:00')")
    conn.commit()
    with _patched(controller, "DB", conn), _patched(controller, "DB_LOCK", controller.DB_LOCK):
        controller.mark_run_cancelled(778, "cancelling")
    row = conn.execute("SELECT status FROM runs WHERE request_id='req2'").fetchone()
    conn.close()
    assert row[0] == "cancelling"
    assert "cancelling" not in controller.TERMINAL
    assert controller.RUN_STATUS_LABEL["cancelling"] == "در حال توقف"


def test_a_failed_cancel_leaves_the_local_row_alone():
    """A failed cancel must not be written to the state as a success."""
    db_path = _STATE_DIR / "mark_failed.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, "
        "chat_id INTEGER, status TEXT, conclusion TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO runs VALUES ('req3', 779, 5, 'in_progress', '', '2026-11-05T00:00:00')")
    conn.commit()

    def fake_gh(method, path, data=None, timeout=30):
        raise RuntimeError("GitHub API error 403: forbidden")

    with (
        _patched(controller, "DB", conn),
        _patched(controller, "DB_LOCK", controller.DB_LOCK),
        _patched(controller, "gh", fake_gh),
        suppress(RuntimeError),
    ):
        controller.cancel_run(779)
    row = conn.execute("SELECT status FROM runs WHERE request_id='req3'").fetchone()
    conn.close()
    assert row[0] == "in_progress"


def test_restore_skips_an_artifact_without_the_state_tables():
    """A file that is not a state database must not replace a good state."""
    restore = _load_restore_module()
    broken = _STATE_DIR / "broken.db"
    _make_state_db(broken, tables=("something_else",))

    summary = restore.inspect_state(broken)
    assert summary["ok"] is False


def test_restore_accepts_a_complete_state_database():
    restore = _load_restore_module()
    good = _STATE_DIR / "good.db"
    _make_state_db(good, providers=2, models=3, runs=4)

    summary = restore.inspect_state(good)
    assert summary["ok"] is True
    assert summary["providers"] == 2
    assert summary["models"] == 3
    assert summary["runs"] == 4


def test_restore_takes_the_newest_usable_artifact_and_skips_the_broken_ones():
    """The newest artifact is not always the newest usable one."""
    restore = _load_restore_module()
    repo = "Aryasamadi/TradingAgentsArya"
    work = _STATE_DIR / "restore-work"
    work.mkdir(parents=True, exist_ok=True)

    good = _STATE_DIR / "restore-good.db"
    _make_state_db(good, providers=1, models=2, runs=3)

    broken_zip = work / "100.zip"
    with zipfile.ZipFile(broken_zip, "w") as archive:
        archive.writestr("bot_state.db", "this is not a database")

    good_zip = work / "99.zip"
    with zipfile.ZipFile(good_zip, "w") as archive:
        archive.write(good, "bot_state.db")

    artifacts = [
        {"id": 100, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-05T00:00:00Z"},
        {"id": 99, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-04T00:00:00Z"},
    ]

    def fake_list(repo_name):
        return artifacts

    def fake_download(repo_name, artifact_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((work / f"{artifact_id}.zip").read_bytes())
        return dest

    destination = _STATE_DIR / "restored" / "bot_state.db"
    with _patched(restore, "list_artifacts", fake_list), _patched(
        restore, "download_artifact", fake_download
    ):
        assert restore.restore(repo, destination) == 0

    assert destination.exists()
    summary = restore.inspect_state(destination)
    assert summary["ok"] is True
    assert summary["models"] == 2


def test_restore_removes_a_stale_file_at_the_destination():
    """The controller must not keep using the file it had before."""
    restore = _load_restore_module()
    repo = "Aryasamadi/TradingAgentsArya"
    work = _STATE_DIR / "restore-stale-work"
    work.mkdir(parents=True, exist_ok=True)

    good = _STATE_DIR / "restore-good2.db"
    _make_state_db(good, providers=1, models=1, runs=1)

    good_zip = work / "1.zip"
    with zipfile.ZipFile(good_zip, "w") as archive:
        archive.write(good, "bot_state.db")

    destination = _STATE_DIR / "restore-stale" / "bot_state.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("stale content")

    def fake_list(repo_name):
        return [{"id": 1, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-05T00:00:00Z"}]

    def fake_download(repo_name, artifact_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(good_zip.read_bytes())
        return dest

    with _patched(restore, "list_artifacts", fake_list), _patched(
        restore, "download_artifact", fake_download
    ):
        assert restore.restore(repo, destination) == 0

    assert restore.inspect_state(destination)["ok"] is True


def test_an_unusable_state_is_not_published():
    """The upload step must refuse to replace the durable state."""
    restore = _load_restore_module()
    broken = _STATE_DIR / "publish-broken.db"
    broken.write_text("not a database")

    assert restore.check(broken) == 1

    good = _STATE_DIR / "publish-good.db"
    _make_state_db(good)
    assert restore.check(good) == 0


def test_an_unrestorable_state_is_not_published_over_the_durable_one():
    """A failed restore must not wipe the state that waits in the artifact."""
    restore = _load_restore_module()
    repo = "Aryasamadi/TradingAgentsArya"
    work = _STATE_DIR / "restore-fail-work"
    work.mkdir(parents=True, exist_ok=True)

    broken_zip = work / "100.zip"
    with zipfile.ZipFile(broken_zip, "w") as archive:
        archive.writestr("bot_state.db", "this is not a database")

    def fake_list(repo_name):
        return [{"id": 100, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-05T00:00:00Z"}]

    def fake_download(repo_name, artifact_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(broken_zip.read_bytes())
        return dest

    destination = _STATE_DIR / "restore-fail" / "bot_state.db"
    with _patched(restore, "list_artifacts", fake_list), _patched(
        restore, "download_artifact", fake_download
    ):
        assert restore.restore(repo, destination) == 0

    marker = destination.with_name(restore.MARKER_NAME)
    assert marker.exists()

    # The controller now runs with an empty state; publishing it would
    # replace the durable state that is still in the artifact.
    empty = _STATE_DIR / "restore-fail" / "empty.db"
    _make_state_db(empty, providers=0, models=0, runs=0)
    assert restore.check(empty) == 1


def test_a_broken_download_does_not_stop_the_search():
    """The next artifact may still hold a usable state."""
    restore = _load_restore_module()
    repo = "Aryasamadi/TradingAgentsArya"
    work = _STATE_DIR / "restore-dl-work"
    work.mkdir(parents=True, exist_ok=True)

    good = _STATE_DIR / "restore-dl-good.db"
    _make_state_db(good, providers=1, models=1, runs=1)
    good_zip = work / "99.zip"
    with zipfile.ZipFile(good_zip, "w") as archive:
        archive.write(good, "bot_state.db")

    artifacts = [
        {"id": 100, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-05T00:00:00Z"},
        {"id": 99, "name": restore.ARTIFACT_NAME, "created_at": "2026-11-04T00:00:00Z"},
    ]

    def fake_list(repo_name):
        return artifacts

    def fake_download(repo_name, artifact_id, dest):
        if artifact_id == 100:
            raise RuntimeError("network is down")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(good_zip.read_bytes())
        return dest

    destination = _STATE_DIR / "restore-dl" / "bot_state.db"
    with _patched(restore, "list_artifacts", fake_list), _patched(
        restore, "download_artifact", fake_download
    ):
        assert restore.restore(repo, destination) == 0

    assert restore.inspect_state(destination)["ok"] is True
    assert not destination.with_name(restore.MARKER_NAME).exists()


def test_the_controller_checkpoints_before_the_state_is_uploaded():
    """The uploaded file must be complete on its own."""
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "wal_checkpoint" in source
    assert "journal_mode=WAL" in source
